"""Immutable, versioned filler packs generated in the member's environment.

This module deliberately does not own the active voice or playback pointer.
The activation transaction (#1039) selects a ready pack and may then pin the
pack it replaced as the last-known-good rollback candidate.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import tempfile
import threading
import time
from urllib.parse import urlparse
import uuid

from caty_gateway.filler_texts import (
    OPTIONAL_KINDS,
    REQUIRED_KINDS,
    ValidationError,
    max_per_kind,
    normalize,
    validate,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - gateway targets macOS/Linux
    fcntl = None


PUBLISHED_STATUSES = frozenset(("ready", "legacy-unknown"))
ALL_STATUSES = frozenset(("staged", "ready", "stale", "unavailable", "legacy-unknown"))
MAX_AUDIO_BYTES = 10 * 1024 * 1024
MIN_AUDIO_BYTES = 1024
DEFAULT_MAX_TEXTS_PER_KIND = 64
MIN_ENV_SECRET_SUBSTRING_LENGTH = 12
DEFAULT_LOCKLESS_STAGE_GRACE_SECONDS = 180
DEFAULT_RECOVERY_QUARANTINE_RETENTION_SECONDS = 86400
_PACK_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SECRET_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "credential",
    "password",
    "private_key",
    "secret",
    "token",
)
_SECRET_VALUE_RE = re.compile(
    r"(?i)(?:api[_-]?key|credential|password|secret|signature|token)\s*="
)
_STAGE_INTERNAL_FILENAMES = frozenset((".stage.lock",))
_STAGE_LOSER_MARKER = ".publish.loser"


class FillerPackError(RuntimeError):
    """A sanitized pack storage or validation failure."""

    def __init__(self, message, *, code=None, status=None, retry_after=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.retry_after = retry_after


def _synthesis_error(message, error):
    """Preserve only sanitized classification fields from synthesis failures."""
    return FillerPackError(
        message,
        code=getattr(error, "code", None),
        status=getattr(error, "status", None),
        retry_after=getattr(error, "retry_after", None),
    )


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _valid_rfc3339(value):
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _valid_pack_id(pack_id):
    return isinstance(pack_id, str) and pack_id not in (".", "..") and bool(_PACK_ID_RE.fullmatch(pack_id))


def _legacy_sidecar_texts(texts, kinds):
    """Return legacy sidecar texts unchanged after compatibility shape checks."""
    if not isinstance(texts, dict) or set(texts) != set(kinds):
        raise FillerPackError("pack text kinds mismatch")
    checked = {}
    for kind in kinds:
        values = texts[kind]
        if not isinstance(values, list) or any(
            not isinstance(item, str) or len(item) > 2000 for item in values
        ):
            raise FillerPackError("invalid filler text")
        checked[kind] = list(values)
    return checked


def _is_mp3(data):
    if not isinstance(data, bytes) or len(data) < MIN_AUDIO_BYTES:
        return False
    offset = 0
    if data.startswith(b"ID3"):
        if len(data) < 10 or any(byte & 0x80 for byte in data[6:10]):
            return False
        tag_size = sum(byte << shift for byte, shift in zip(data[6:10], (21, 14, 7, 0)))
        offset = 10 + tag_size
        if offset >= len(data) - 1:
            return False
    for index in range(offset, len(data) - 3):
        first, second, third, _fourth = data[index : index + 4]
        if first != 0xFF or second & 0xE0 != 0xE0:
            continue
        version = (second >> 3) & 0x03
        layer = (second >> 1) & 0x03
        bitrate = (third >> 4) & 0x0F
        sample_rate = (third >> 2) & 0x03
        if version != 0x01 and layer != 0 and bitrate not in (0, 0x0F) and sample_rate != 0x03:
            return True
    return False


def _private_url(value):
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    if not parsed.scheme:
        return False
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.scheme in ("file", "ssh")
    ):
        return True
    host = (parsed.hostname or "").lower()
    if host in ("localhost", "localhost.localdomain") or host.endswith(".local") or host.endswith(".internal"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not address.is_global


def _environment_secrets():
    secrets = set()
    for key, value in os.environ.items():
        lowered = key.lower()
        if (
            value
            and len(value) >= MIN_ENV_SECRET_SUBSTRING_LENGTH
            and any(part in lowered for part in _SECRET_KEY_PARTS)
        ):
            secrets.add(value)
    return secrets


def _contains_environment_secret(value, environment_secrets):
    return any(secret in value for secret in environment_secrets)


def _safe_metadata(value, *, path="metadata", environment_secrets=None):
    """Return a JSON-safe copy, rejecting credential-shaped/private values."""
    environment_secrets = environment_secrets if environment_secrets is not None else _environment_secrets()
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise FillerPackError(f"unsafe {path}")
        return value
    if isinstance(value, str):
        if (
            len(value) > 4096
            or "\x00" in value
            or _contains_environment_secret(value, environment_secrets)
            or _SECRET_VALUE_RE.search(value)
        ):
            raise FillerPackError(f"unsafe {path}")
        if value.lower().startswith("bearer ") or _private_url(value):
            raise FillerPackError(f"unsafe {path}")
        return value
    if isinstance(value, list):
        if len(value) > 100:
            raise FillerPackError(f"unsafe {path}")
        return [
            _safe_metadata(item, path=f"{path}[]", environment_secrets=environment_secrets)
            for item in value
        ]
    if isinstance(value, dict):
        if len(value) > 100:
            raise FillerPackError(f"unsafe {path}")
        result = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 128:
                raise FillerPackError(f"unsafe {path}")
            lowered = key.lower()
            if any(part in lowered for part in _SECRET_KEY_PARTS):
                raise FillerPackError(f"unsafe {path}")
            result[key] = _safe_metadata(
                item,
                path=f"{path}.{key}",
                environment_secrets=environment_secrets,
            )
        return result
    raise FillerPackError(f"unsafe {path}")


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_private_directory(path):
    path = Path(path)
    if path.is_symlink():
        raise FillerPackError("registry directory unavailable")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        metadata = path.stat()
    except OSError:
        raise FillerPackError("registry directory unavailable") from None
    if not stat.S_ISDIR(metadata.st_mode):
        raise FillerPackError("registry directory unavailable")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise FillerPackError("registry directory ownership mismatch")
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise FillerPackError("registry directory must not be group/world writable")


def _write_bytes(path, data):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _read_json_file(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise FillerPackError("pack metadata unavailable")
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise FillerPackError("pack metadata unavailable") from None
    if not isinstance(payload, dict):
        raise FillerPackError("pack metadata unavailable")
    return payload


def _env_seconds(name, default, *, minimum=0):
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)
    return max(int(minimum), value)


class FillerPackRegistry:
    """Crash-safe registry for immutable local filler packs."""

    def __init__(
        self,
        root,
        *,
        fault_injector=None,
        max_audio_bytes=MAX_AUDIO_BYTES,
        max_texts_per_kind=None,
    ):
        requested_root = Path(root).expanduser()
        if requested_root.is_symlink():
            raise FillerPackError("registry directory unavailable")
        self.root = requested_root.resolve()
        self.packs_dir = self.root / "packs"
        self.staging_dir = self.root / "staging"
        self.trash_dir = self.root / "trash"
        self.state_dir = self.root / "state"
        self.lkg_path = self.state_dir / "lkg.json"
        self.lock_path = self.state_dir / "registry.lock"
        self._fault_injector = fault_injector
        self._max_audio_bytes = int(max_audio_bytes)
        if max_texts_per_kind is None:
            try:
                max_texts_per_kind = int(
                    os.environ.get(
                        "CATY_VOICE_FILLER_MAX_TEXTS_PER_KIND",
                        DEFAULT_MAX_TEXTS_PER_KIND,
                    )
                )
            except (TypeError, ValueError):
                max_texts_per_kind = DEFAULT_MAX_TEXTS_PER_KIND
        try:
            max_texts_per_kind = int(max_texts_per_kind)
        except (TypeError, ValueError):
            raise FillerPackError("invalid filler text limit") from None
        if not 1 <= max_texts_per_kind <= 100:
            raise FillerPackError("invalid filler text limit")
        self._max_texts_per_kind = max_texts_per_kind
        self._lockless_stage_grace_seconds = DEFAULT_LOCKLESS_STAGE_GRACE_SECONDS
        self._recovery_quarantine_retention_seconds = _env_seconds(
            "CATY_VOICE_FILLER_RECOVERY_QUARANTINE_RETENTION_SECONDS",
            DEFAULT_RECOVERY_QUARANTINE_RETENTION_SECONDS,
        )
        self._thread_lock = threading.RLock()
        self._lock_state = threading.local()
        for directory in (self.root, self.packs_dir, self.staging_dir, self.trash_dir, self.state_dir):
            _ensure_private_directory(directory)

    @classmethod
    def for_member(cls, member_id, *, data_root=None, **kwargs):
        if not _valid_pack_id(member_id):
            raise FillerPackError("invalid member id")
        base = Path(data_root).expanduser() if data_root is not None else Path("~/.local/share/caty-gateway").expanduser()
        return cls(base / member_id / "filler-packs", **kwargs)

    @contextmanager
    def _locked(self, *, shared=False):
        with self._thread_lock:
            depth = getattr(self._lock_state, "depth", 0)
            if depth:
                if not shared and getattr(self._lock_state, "shared", False):
                    raise FillerPackError("registry lock upgrade is unavailable")
                self._lock_state.depth = depth + 1
                try:
                    yield
                finally:
                    self._lock_state.depth -= 1
                return
            handle = self.lock_path.open("a+b")
            self._lock_state.depth = 1
            self._lock_state.shared = bool(shared)
            try:
                if fcntl is not None:
                    operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
                    fcntl.flock(handle.fileno(), operation)
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
                self._lock_state.depth = 0
                self._lock_state.shared = False

    def _fault(self, point, **context):
        if self._fault_injector is not None:
            self._fault_injector(point, context)

    def _pack_path(self, pack_id):
        if not _valid_pack_id(pack_id):
            raise FillerPackError("invalid pack id")
        return self.packs_dir / pack_id

    def _create_stage(self, pack_id):
        stage = None
        stage_handle = None
        try:
            stage = Path(tempfile.mkdtemp(prefix=f"{pack_id}.", dir=self.staging_dir))
            stage_handle = (stage / ".stage.lock").open("a+b")
            if fcntl is not None:
                fcntl.flock(stage_handle.fileno(), fcntl.LOCK_EX)
            return stage, stage_handle
        except OSError:
            if stage_handle is not None:
                stage_handle.close()
            if stage is not None:
                shutil.rmtree(stage, ignore_errors=True)
            raise FillerPackError("pack staging failed") from None

    def _remove_stage_internal_files(self, stage):
        removed = False
        for name in _STAGE_INTERNAL_FILENAMES:
            path = Path(stage) / name
            if path.exists():
                path.unlink()
                removed = True
        if removed:
            _fsync_directory(stage)

    def _discard_stage(self, stage):
        try:
            shutil.rmtree(stage)
        except OSError:
            pass

    def _mark_stage_loser(self, stage):
        marker = Path(stage) / _STAGE_LOSER_MARKER
        if not marker.exists():
            _write_bytes(marker, b"")
            _fsync_directory(stage)

    def _lockless_stage_is_young(self, stage):
        if self._lockless_stage_grace_seconds <= 0:
            return False
        try:
            age_seconds = time.time() - stage.stat().st_mtime
        except OSError:
            return False
        return age_seconds < self._lockless_stage_grace_seconds

    def _quarantine_entry_is_retained(self, entry):
        if not entry.name.startswith("recovery-"):
            return False
        if self._recovery_quarantine_retention_seconds <= 0:
            return False
        try:
            age_seconds = time.time() - entry.stat().st_mtime
        except OSError:
            return False
        return age_seconds < self._recovery_quarantine_retention_seconds

    def _manifest_base(
        self,
        *,
        pack_id,
        generated_for_provider,
        generated_for_reference_id,
        preset_id,
        preset_version,
        filler_text_version,
        inference_contract_version,
        provenance,
        license_metadata,
        generated_at,
        kinds,
    ):
        provider = str(generated_for_provider or "").strip()
        reference = str(generated_for_reference_id or "").strip()
        text_version = str(filler_text_version or "").strip()
        contract_version = str(inference_contract_version or "").strip()
        if not provider or not reference or not text_version or not contract_version:
            raise FillerPackError("required pack metadata is missing")
        if preset_id is not None and (not isinstance(preset_id, str) or not preset_id.strip()):
            raise FillerPackError("invalid preset id")
        if preset_version != 1:
            raise FillerPackError("unsupported preset version")
        timestamp = generated_at or _utc_now()
        if not _valid_rfc3339(timestamp):
            raise FillerPackError("invalid generated_at")
        manifest = {
            "pack_id": pack_id,
            "generated_for_provider": provider,
            "generated_for_reference_id": reference,
            "preset_id": preset_id,
            "preset_version": 1,
            "filler_text_version": text_version,
            "kinds": list(kinds),
            "inference_contract_version": contract_version,
            "generated_at": timestamp,
            "provenance": _safe_metadata(provenance or {}, path="provenance"),
            "license_metadata": _safe_metadata(license_metadata or {}, path="license_metadata"),
        }
        return _safe_metadata(manifest, path="manifest")

    def stage_pack(
        self,
        *,
        generated_for_provider,
        generated_for_reference_id,
        filler_text_version,
        texts,
        synthesizer,
        inference_contract_version,
        preset_id=None,
        preset_version=1,
        provenance=None,
        license_metadata=None,
        pack_id=None,
        generated_at=None,
    ):
        """Generate, validate, and atomically publish a ready pack."""
        pack_id = pack_id or uuid.uuid4().hex
        if not _valid_pack_id(pack_id):
            raise FillerPackError("invalid pack id")
        _safe_metadata(texts, path="texts")
        if not isinstance(texts, dict):
            raise FillerPackError("filler text kinds are incomplete")
        present = set(texts)
        allowed = set(REQUIRED_KINDS) | set(OPTIONAL_KINDS)
        if not set(REQUIRED_KINDS).issubset(present) or not present.issubset(allowed):
            raise FillerPackError("filler text kinds are incomplete")
        text_limit = min(max_per_kind(), self._max_texts_per_kind)
        if any(
            isinstance(values, list) and len(values) > text_limit
            for values in texts.values()
        ):
            raise FillerPackError("too many filler texts")
        errors = validate(texts)
        if errors:
            raise FillerPackError("invalid filler text")
        try:
            normalized_texts = normalize(texts)
        except ValidationError:
            raise FillerPackError("invalid filler text") from None
        normalized_texts = _safe_metadata(normalized_texts, path="texts")
        manifest = self._manifest_base(
            pack_id=pack_id,
            generated_for_provider=generated_for_provider,
            generated_for_reference_id=generated_for_reference_id,
            preset_id=preset_id,
            preset_version=preset_version,
            filler_text_version=filler_text_version,
            inference_contract_version=inference_contract_version,
            provenance=provenance,
            license_metadata=license_metadata,
            generated_at=generated_at,
            kinds=normalized_texts,
        )
        target = self._pack_path(pack_id)
        with self._locked(shared=True):
            if target.exists():
                raise FillerPackError("pack id already exists")
        stage, stage_handle = self._create_stage(pack_id)
        try:
            try:
                files_dir = stage / "files"
                files_dir.mkdir(mode=0o700)
                files = {}
                hashes = {}
                for kind in normalized_texts:
                    files[kind] = []
                    for index, text in enumerate(normalized_texts[kind]):
                        try:
                            synthesized = synthesizer(
                                text, manifest["generated_for_reference_id"]
                            )
                        except Exception as exc:
                            if isinstance(exc, OSError):
                                raise
                            raise _synthesis_error("filler synthesis failed", exc) from None
                        if isinstance(synthesized, bytes):
                            audio = synthesized
                        else:
                            chunks = []
                            total = 0
                            try:
                                for chunk in synthesized:
                                    if not isinstance(chunk, bytes):
                                        raise TypeError
                                    total += len(chunk)
                                    if total > self._max_audio_bytes:
                                        raise ValueError
                                    chunks.append(chunk)
                            except Exception as exc:
                                if isinstance(exc, (TypeError, ValueError)):
                                    raise FillerPackError("invalid synthesized MP3") from None
                                if isinstance(exc, OSError):
                                    raise
                                raise _synthesis_error("filler synthesis failed", exc) from None
                            audio = b"".join(chunks)
                        if len(audio) > self._max_audio_bytes or not _is_mp3(audio):
                            raise FillerPackError("invalid synthesized MP3") from None
                        relative = f"files/{kind}-{index}.mp3"
                        _write_bytes(stage / relative, audio)
                        files[kind].append(relative)
                        hashes[relative] = hashlib.sha256(audio).hexdigest()
                    self._fault("stage_after_kind", stage_dir=str(stage), pack_id=pack_id, kind=kind)
                _atomic_json(
                    stage / "texts.json",
                    {"filler_text_version": manifest["filler_text_version"], "texts": normalized_texts},
                )
                manifest.update({"files": files, "files_sha256": hashes, "status": "staged"})
                _atomic_json(stage / "manifest.json", manifest)
                self._fault("before_validate", stage_dir=str(stage), pack_id=pack_id)
                self._validate_pack_dir(stage, expected_status="staged")
                ready = dict(manifest)
                ready["status"] = "ready"
                _atomic_json(stage / "manifest.json", ready)
                self._validate_pack_dir(stage, expected_status="ready")
                _fsync_directory(files_dir)
                _fsync_directory(stage)
                with self._locked():
                    if target.exists():
                        self._mark_stage_loser(stage)
                        self._discard_stage(stage)
                        raise FillerPackError("pack id already exists")
                    self._fault("before_publish", stage_dir=str(stage), pack_id=pack_id)
                    self._remove_stage_internal_files(stage)
                    os.replace(stage, target)
                    _fsync_directory(self.packs_dir)
                    _fsync_directory(self.staging_dir)
                    return dict(ready)
            except OSError:
                raise FillerPackError("pack staging failed") from None
        finally:
            if fcntl is not None:
                fcntl.flock(stage_handle.fileno(), fcntl.LOCK_UN)
            stage_handle.close()

    def _validate_pack_dir(self, directory, *, expected_status=None):
        directory = Path(directory)
        if directory.is_symlink() or not directory.is_dir():
            raise FillerPackError("pack unavailable")
        manifest = _read_json_file(directory / "manifest.json")
        status = manifest.get("status")
        if status not in ALL_STATUSES or (expected_status is not None and status != expected_status):
            raise FillerPackError("invalid pack status")
        if status not in PUBLISHED_STATUSES and status != "staged":
            raise FillerPackError("invalid stored pack status")
        pack_id = manifest.get("pack_id")
        if not _valid_pack_id(pack_id):
            raise FillerPackError("invalid pack manifest")
        if directory.parent == self.packs_dir and directory.name != pack_id:
            raise FillerPackError("pack id mismatch")
        if not isinstance(manifest.get("generated_for_provider"), str) or not manifest["generated_for_provider"].strip():
            raise FillerPackError("invalid pack manifest")
        if not isinstance(manifest.get("generated_for_reference_id"), str) or not manifest["generated_for_reference_id"].strip():
            raise FillerPackError("invalid pack manifest")
        if manifest.get("preset_id") is not None and not isinstance(manifest.get("preset_id"), str):
            raise FillerPackError("invalid pack manifest")
        if manifest.get("preset_version") != 1:
            raise FillerPackError("invalid pack manifest")
        for key in ("filler_text_version", "inference_contract_version"):
            if not isinstance(manifest.get(key), str) or not manifest[key].strip():
                raise FillerPackError("invalid pack manifest")
        if not _valid_rfc3339(manifest.get("generated_at")):
            raise FillerPackError("invalid pack manifest")
        _safe_metadata(manifest, path="manifest")
        kinds = manifest.get("kinds")
        allowed_kinds = set(REQUIRED_KINDS) | set(OPTIONAL_KINDS)
        if (
            not isinstance(kinds, list)
            or len(kinds) != len(set(kinds))
            or not set(REQUIRED_KINDS).issubset(kinds)
            or not set(kinds).issubset(allowed_kinds)
        ):
            raise FillerPackError("pack kinds are incomplete")
        files = manifest.get("files")
        hashes = manifest.get("files_sha256")
        if not isinstance(files, dict) or set(files) != set(kinds) or not isinstance(hashes, dict):
            raise FillerPackError("invalid pack files")
        expected_files = set()
        for kind in kinds:
            names = files[kind]
            if not isinstance(names, list) or (not names and status != "legacy-unknown"):
                raise FillerPackError("pack kinds are incomplete")
            if len(names) != len(set(names)):
                raise FillerPackError("duplicate pack file")
            for relative in names:
                if not isinstance(relative, str) or not relative.startswith("files/"):
                    raise FillerPackError("invalid pack file")
                relative_path = Path(relative)
                if relative_path.is_absolute() or ".." in relative_path.parts or relative_path.suffix.lower() != ".mp3":
                    raise FillerPackError("invalid pack file")
                expected_files.add(relative)
        if set(hashes) != expected_files:
            raise FillerPackError("pack hash set mismatch")
        files_root = directory / "files"
        if files_root.is_symlink() or not files_root.is_dir():
            raise FillerPackError("pack files unavailable")
        actual_files = set()
        for child in files_root.iterdir():
            if child.is_symlink() or not child.is_file():
                raise FillerPackError("pack file unavailable")
            actual_files.add(f"files/{child.name}")
        if actual_files != expected_files:
            raise FillerPackError("pack file set mismatch")
        for relative in sorted(expected_files):
            path = directory / relative
            try:
                data = path.read_bytes()
            except OSError:
                raise FillerPackError("pack file unavailable") from None
            if len(data) > self._max_audio_bytes or not _is_mp3(data):
                raise FillerPackError("invalid pack MP3")
            digest = hashes.get(relative)
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise FillerPackError("invalid pack hash")
            if hashlib.sha256(data).hexdigest() != digest:
                raise FillerPackError("pack hash mismatch")
        sidecar = _read_json_file(directory / "texts.json")
        _safe_metadata(sidecar, path="texts_sidecar")
        if sidecar.get("filler_text_version") != manifest["filler_text_version"]:
            raise FillerPackError("pack text version mismatch")
        if status == "legacy-unknown":
            checked_texts = _legacy_sidecar_texts(sidecar.get("texts"), kinds)
        else:
            try:
                checked_texts = normalize(sidecar.get("texts"))
            except ValidationError:
                raise FillerPackError("invalid filler text") from None
        if set(checked_texts) != set(kinds):
            raise FillerPackError("pack text kinds mismatch")
        if status in PUBLISHED_STATUSES and any(
            len(checked_texts[kind]) != len(files[kind]) for kind in kinds
        ):
            raise FillerPackError("pack text/audio count mismatch")
        if status == "legacy-unknown" and not expected_files:
            raise FillerPackError("legacy pack is empty")
        return manifest

    def inspect(self, pack_id):
        """Return the immutable manifest, or an effective unavailable status."""
        with self._locked(shared=True):
            try:
                manifest = self._validate_pack_dir(self._pack_path(pack_id))
            except FillerPackError:
                return {"pack_id": pack_id, "status": "unavailable", "files": {}}
            return dict(manifest)

    def read_texts(self, pack_id):
        """Return a validated pack's versioned text sidecar under the shared lock."""
        with self._locked(shared=True):
            directory = self._pack_path(pack_id)
            manifest = self._validate_pack_dir(directory)
            sidecar = _read_json_file(directory / "texts.json")
            return {
                "filler_text_version": sidecar["filler_text_version"],
                "texts": (
                    _legacy_sidecar_texts(sidecar["texts"], manifest["kinds"])
                    if manifest["status"] == "legacy-unknown"
                    else normalize(sidecar["texts"])
                ),
            }

    def resolve(
        self,
        pack_id,
        *,
        active_provider,
        active_reference_id,
        expected_text_version=None,
    ):
        """Resolve only an intact pack generated for the exact active voice."""
        with self._locked(shared=True):
            path = self._pack_path(pack_id)
            try:
                manifest = self._validate_pack_dir(path)
            except FillerPackError:
                return {"pack_id": pack_id, "status": "unavailable", "files": {}}
            if manifest["status"] not in PUBLISHED_STATUSES:
                return {"pack_id": pack_id, "status": "unavailable", "files": {}}
            if (
                manifest["generated_for_provider"] != active_provider
                or manifest["generated_for_reference_id"] != active_reference_id
            ):
                stale = dict(manifest)
                stale["status"] = "stale"
                stale["reason"] = "voice"
                stale["files"] = {}
                return stale
            if (
                expected_text_version is not None
                and manifest["filler_text_version"] != expected_text_version
            ):
                stale = dict(manifest)
                stale["status"] = "stale"
                stale["reason"] = "text"
                stale["files"] = {}
                return stale
            resolved = dict(manifest)
            resolved["files"] = {
                kind: tuple(str(path / relative) for relative in manifest["files"][kind])
                for kind in manifest["kinds"]
            }
            return resolved

    def read_audio(self, pack_id, *, active_provider, active_reference_id):
        """Validate voice binding and hashes, then read one clip under the lock."""
        with self._locked(shared=True):
            directory = self._pack_path(pack_id)
            try:
                manifest = self._validate_pack_dir(directory)
            except FillerPackError:
                return {"status": "unavailable", "audio": None}
            if manifest["status"] not in PUBLISHED_STATUSES:
                return {"status": "unavailable", "audio": None}
            if (
                manifest["generated_for_provider"] != active_provider
                or manifest["generated_for_reference_id"] != active_reference_id
            ):
                return {"status": "stale", "audio": None}
            relatives = [
                relative
                for kind in REQUIRED_KINDS
                for relative in manifest["files"][kind]
            ]
            if not relatives:
                return {"status": "unavailable", "audio": None}
            relative = secrets.choice(relatives)
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = -1
            try:
                fd = os.open(directory / relative, flags)
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError
                with os.fdopen(fd, "rb") as handle:
                    fd = -1
                    audio = handle.read(self._max_audio_bytes + 1)
            except (OSError, ValueError):
                return {"status": "unavailable", "audio": None}
            finally:
                if fd >= 0:
                    os.close(fd)
            if (
                len(audio) > self._max_audio_bytes
                or not _is_mp3(audio)
                or hashlib.sha256(audio).hexdigest()
                != manifest["files_sha256"].get(relative)
            ):
                return {"status": "unavailable", "audio": None}
            return {"status": manifest["status"], "audio": audio}

    def pin_lkg(self, pack_id):
        """Pin a caller-selected predecessor; this does not activate a pack."""
        with self._locked():
            manifest = self._validate_pack_dir(self._pack_path(pack_id))
            if manifest["status"] not in PUBLISHED_STATUSES:
                raise FillerPackError("LKG pack is not published")
            _atomic_json(self.lkg_path, {"pack_id": pack_id, "recorded_at": _utc_now()})
            return pack_id

    def lkg_pack_id(self):
        with self._locked(shared=True):
            if not self.lkg_path.exists():
                return None
            try:
                payload = _read_json_file(self.lkg_path)
            except FillerPackError:
                return None
            pack_id = payload.get("pack_id")
            return pack_id if _valid_pack_id(pack_id) else None

    def list_packs(self):
        """List packs without exposing registry layout to activation callers."""
        with self._locked(shared=True):
            packs = []
            for path in sorted(self.packs_dir.iterdir()):
                if path.is_symlink() or not path.is_dir() or not _valid_pack_id(path.name):
                    continue
                try:
                    manifest = self._validate_pack_dir(path)
                except FillerPackError:
                    packs.append(
                        {"pack_id": path.name, "status": "unavailable", "generated_for": None}
                    )
                    continue
                packs.append(
                    {
                        "pack_id": manifest["pack_id"],
                        "status": manifest["status"],
                        "generated_for": {
                            "provider": manifest["generated_for_provider"],
                            "reference_id": manifest["generated_for_reference_id"],
                        },
                    }
                )
            return packs

    def resolve_lkg(self, *, active_provider, active_reference_id):
        pack_id = self.lkg_pack_id()
        if pack_id is None:
            return None
        return self.resolve(
            pack_id,
            active_provider=active_provider,
            active_reference_id=active_reference_id,
        )

    def recover_staging(self):
        """Idempotently publish validated ready stages and collect leftovers."""
        report = {
            "published": [],
            "removed_stages": [],
            "removed_trash": [],
            "removed_journals": [],
            "removed_state_temps": [],
        }
        with self._locked():
            for stage in sorted(self.staging_dir.iterdir()):
                if stage.is_symlink() or not stage.is_dir():
                    continue
                if (stage / _STAGE_LOSER_MARKER).exists():
                    self._discard_stage(stage)
                    report["removed_stages"].append(stage.name)
                    continue
                stage_handle = None
                stage_lock = stage / ".stage.lock"
                if stage_lock.is_file() and fcntl is not None:
                    try:
                        stage_handle = stage_lock.open("a+b")
                        fcntl.flock(
                            stage_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                        )
                    except (BlockingIOError, OSError):
                        if stage_handle is not None:
                            stage_handle.close()
                        continue
                lockless_stage = not stage_lock.exists()
                try:
                    manifest = self._validate_pack_dir(stage, expected_status="ready")
                except (FillerPackError, OSError):
                    if lockless_stage and self._lockless_stage_is_young(stage):
                        if stage_handle is not None:
                            stage_handle.close()
                        continue
                    try:
                        shutil.rmtree(stage)
                        report["removed_stages"].append(stage.name)
                    except OSError:
                        pass
                    finally:
                        if stage_handle is not None:
                            stage_handle.close()
                    continue
                target = self._pack_path(manifest["pack_id"])
                if target.exists():
                    try:
                        published = self._validate_pack_dir(target)
                        if published["status"] not in PUBLISHED_STATUSES:
                            raise FillerPackError("target is not published")
                    except (FillerPackError, OSError):
                        quarantine = self.trash_dir / (
                            f"recovery-{uuid.uuid4().hex}.{manifest['pack_id']}"
                        )
                        try:
                            os.replace(target, quarantine)
                            os.utime(quarantine, None)
                            _fsync_directory(self.packs_dir)
                            _fsync_directory(self.trash_dir)
                            self._remove_stage_internal_files(stage)
                            os.replace(stage, target)
                            _fsync_directory(self.packs_dir)
                            _fsync_directory(self.staging_dir)
                            report["published"].append(manifest["pack_id"])
                        except OSError:
                            pass
                    else:
                        try:
                            shutil.rmtree(stage)
                            report["removed_stages"].append(stage.name)
                        except OSError:
                            pass
                else:
                    try:
                        self._remove_stage_internal_files(stage)
                        os.replace(stage, target)
                        _fsync_directory(self.packs_dir)
                        _fsync_directory(self.staging_dir)
                        report["published"].append(manifest["pack_id"])
                    except OSError:
                        pass
                if stage_handle is not None:
                    stage_handle.close()

            for journal in sorted(self.state_dir.glob("gc-*.json")):
                try:
                    payload = _read_json_file(journal)
                    run_id = payload.get("run_id")
                    pack_ids = payload.get("pack_ids")
                    if not _valid_pack_id(run_id) or not isinstance(pack_ids, list):
                        raise FillerPackError("GC metadata unavailable")
                    if any(not _valid_pack_id(pack_id) for pack_id in pack_ids):
                        raise FillerPackError("GC metadata unavailable")
                    for pack_id in pack_ids:
                        entry = self.trash_dir / f"{run_id}.{pack_id}"
                        if entry.is_dir() and not entry.is_symlink():
                            shutil.rmtree(entry)
                            report["removed_trash"].append(entry.name)
                    journal.unlink()
                    _fsync_directory(self.state_dir)
                    report["removed_journals"].append(journal.name)
                except (FillerPackError, OSError):
                    continue

            for entry in sorted(self.trash_dir.iterdir()):
                if self._quarantine_entry_is_retained(entry):
                    continue
                if entry.is_symlink() or entry.is_file():
                    try:
                        entry.unlink()
                        report["removed_trash"].append(entry.name)
                    except OSError:
                        pass
                elif entry.is_dir():
                    try:
                        shutil.rmtree(entry)
                        report["removed_trash"].append(entry.name)
                    except OSError:
                        pass
            for temporary in sorted(self.state_dir.iterdir()):
                if not temporary.name.startswith(".") or temporary.is_dir():
                    continue
                try:
                    temporary.unlink()
                    report["removed_state_temps"].append(temporary.name)
                except OSError:
                    pass
            return report

    def garbage_collect(self, candidate_pack_ids, *, protected_pack_ids):
        """Collect only explicit candidates, always protecting caller state and LKG."""
        candidates = list(dict.fromkeys(candidate_pack_ids))
        protected = set(protected_pack_ids)
        for pack_id in candidates + list(protected):
            if not _valid_pack_id(pack_id):
                raise FillerPackError("invalid pack id")
        with self._locked():
            lkg = None
            if self.lkg_path.exists():
                # Corrupt retention state must stop GC, not silently unprotect
                # the very pack the pointer was meant to preserve.
                lkg = _read_json_file(self.lkg_path).get("pack_id")
                if not _valid_pack_id(lkg):
                    raise FillerPackError("LKG metadata unavailable")
            if _valid_pack_id(lkg):
                protected.add(lkg)
            doomed = [pack_id for pack_id in candidates if pack_id not in protected and self._pack_path(pack_id).is_dir()]
            run_id = uuid.uuid4().hex
            journal = self.state_dir / f"gc-{run_id}.json"
            _atomic_json(journal, {"run_id": run_id, "pack_ids": doomed, "created_at": _utc_now()})
            removed = []
            for pack_id in doomed:
                source = self._pack_path(pack_id)
                if not source.exists():
                    continue
                destination = self.trash_dir / f"{run_id}.{pack_id}"
                os.replace(source, destination)
                _fsync_directory(self.packs_dir)
                _fsync_directory(self.trash_dir)
                self._fault("gc_after_move", pack_id=pack_id, trash_path=str(destination))
                shutil.rmtree(destination)
                removed.append(pack_id)
            try:
                journal.unlink()
                _fsync_directory(self.state_dir)
            except OSError:
                pass
            return {"removed": removed, "protected": sorted(protected)}

    def import_legacy(
        self,
        legacy_dir,
        *,
        generated_for_provider,
        generated_for_reference_id,
        filler_text_version="legacy-unknown",
        inference_contract_version="legacy-unknown",
        preset_id=None,
        preset_version=1,
        provenance=None,
        license_metadata=None,
        texts_path=None,
        pack_id=None,
        generated_at=None,
    ):
        """Explicitly copy a flat pool into a legacy-unknown immutable pack."""
        legacy_dir = Path(legacy_dir)
        if legacy_dir.is_symlink() or not legacy_dir.is_dir():
            raise FillerPackError("legacy filler directory unavailable")
        source_files = [path for path in sorted(legacy_dir.iterdir()) if path.suffix.lower() == ".mp3"]
        if not source_files or any(path.is_symlink() or not path.is_file() for path in source_files):
            raise FillerPackError("legacy filler files unavailable")
        legacy_texts = {}
        if texts_path is not None:
            if not Path(texts_path).exists():
                raise FillerPackError("legacy filler texts unavailable")
            legacy_texts = _read_json_file(texts_path)
        pack_id = pack_id or uuid.uuid4().hex
        if not _valid_pack_id(pack_id):
            raise FillerPackError("invalid pack id")
        manifest = self._manifest_base(
            pack_id=pack_id,
            generated_for_provider=generated_for_provider,
            generated_for_reference_id=generated_for_reference_id,
            preset_id=preset_id,
            preset_version=preset_version,
            filler_text_version=filler_text_version,
            inference_contract_version=inference_contract_version,
            provenance=provenance or {"source": "legacy-flat-pool"},
            license_metadata=license_metadata,
            generated_at=generated_at,
            kinds=REQUIRED_KINDS,
        )
        with self._locked():
            target = self._pack_path(pack_id)
            if target.exists():
                raise FillerPackError("pack id already exists")
            stage = Path(tempfile.mkdtemp(prefix=f"{pack_id}.", dir=self.staging_dir))
            (stage / "files").mkdir(mode=0o700)
            files = {kind: [] for kind in REQUIRED_KINDS}
            hashes = {}
            texts = {kind: [] for kind in REQUIRED_KINDS}
            for index, source in enumerate(source_files):
                try:
                    data = source.read_bytes()
                except OSError:
                    raise FillerPackError("legacy filler unavailable") from None
                if len(data) > self._max_audio_bytes or not _is_mp3(data):
                    raise FillerPackError("invalid legacy MP3")
                relative = f"files/legacy-{index}.mp3"
                _write_bytes(stage / relative, data)
                hashes[relative] = hashlib.sha256(data).hexdigest()
                text = legacy_texts.get(source.name)
                if text is not None and not isinstance(text, str):
                    raise FillerPackError("invalid legacy filler text")
                for kind in REQUIRED_KINDS:
                    files[kind].append(relative)
                    texts[kind].append(text or "")
            texts = _safe_metadata(texts, path="texts")
            _atomic_json(stage / "texts.json", {"filler_text_version": filler_text_version, "texts": texts})
            manifest.update({"files": files, "files_sha256": hashes, "status": "legacy-unknown"})
            _atomic_json(stage / "manifest.json", manifest)
            self._validate_pack_dir(stage, expected_status="legacy-unknown")
            _fsync_directory(stage / "files")
            _fsync_directory(stage)
            self._fault("before_publish", stage_dir=str(stage), pack_id=pack_id)
            os.replace(stage, target)
            _fsync_directory(self.packs_dir)
            _fsync_directory(self.staging_dir)
            return dict(manifest)
