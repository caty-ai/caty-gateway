"""Canonical managed filler wording and per-member overrides.

This is not the legacy MP3-name text store exposed by ``/fillers:text`` in
``caty_gateway.py``. Managed filler packs consume only the effective kind-based
wording returned here.
"""

from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
from importlib import resources
import tempfile
from typing import Dict, Mapping

try:
    import fcntl
except ImportError:  # pragma: no cover - gateway targets macOS/Linux
    fcntl = None


LOGGER = logging.getLogger(__name__)

SCHEMA = "caty-filler-texts/1"
LANGUAGE = "ja"
REQUIRED_KINDS = ("thinking", "wait", "large", "alive", "fail")
OPTIONAL_KINDS = ("announce",)
KINDS = REQUIRED_KINDS + OPTIONAL_KINDS
MIN_LEN = 1
MAX_LEN = 40
LEGACY_TEXT_VERSION = "voice-picker-ja-v1"


# Public default only; runtime consumers must call max_per_kind().
MAX_PER_KIND = 8


def max_per_kind():
    """Return the current configured per-kind cap, bounded by the public limit."""
    try:
        configured = int(
            os.getenv("CATY_VOICE_FILLER_MAX_TEXTS_PER_KIND") or str(MAX_PER_KIND)
        )
    except (TypeError, ValueError):
        return MAX_PER_KIND
    if configured <= 0:
        return MAX_PER_KIND
    return min(MAX_PER_KIND, configured)

# Keep equal to filler_pack._SECRET_KEY_PARTS without importing that non-leaf
# module. A regression test locks the two copies together.
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


class ValidationError(ValueError):
    """Strict API/store validation failure with per-kind details."""

    def __init__(self, errors):
        super().__init__("invalid filler texts")
        self.errors = errors


class ConflictError(RuntimeError):
    """Optimistic-concurrency failure carrying the current representation."""

    def __init__(self, current):
        super().__init__("filler text version conflict")
        self.current = current


@dataclass(frozen=True)
class EffectiveTexts:
    texts: Dict[str, list]
    version: str
    sources: Dict[str, str]
    override_status: str


def _copy_kinds(kinds):
    return {kind: list(values) for kind, values in kinds.items()}


def normalize(kinds):
    """Return stripped, non-empty, de-duplicated texts with sorted kinds."""
    if not isinstance(kinds, Mapping):
        raise ValidationError({"kinds": ["must be an object"]})
    normalized = {}
    for kind in sorted(kinds):
        values = kinds[kind]
        if not isinstance(values, list):
            raise ValidationError({str(kind): ["must be an array"]})
        cleaned = []
        seen = set()
        for value in values:
            if not isinstance(value, str):
                raise ValidationError({str(kind): ["texts must be strings"]})
            text = value.strip()
            if text and text not in seen:
                seen.add(text)
                cleaned.append(text)
        normalized[str(kind)] = cleaned
    return normalized


def validate(kinds):
    """Return per-kind errors for a partial override-shaped kinds mapping."""
    if not isinstance(kinds, Mapping):
        return {"kinds": ["must be an object"]}
    errors = {}
    limit = max_per_kind()
    for raw_kind, raw_values in kinds.items():
        kind = str(raw_kind)
        kind_errors = []
        if not isinstance(raw_kind, str) or raw_kind not in KINDS:
            kind_errors.append("unknown kind")
        if not isinstance(raw_values, list):
            kind_errors.append("must be an array")
            errors[kind] = kind_errors
            continue
        normalized_values = []
        seen = set()
        for value in raw_values:
            if not isinstance(value, str):
                kind_errors.append("texts must be strings")
                continue
            text = value.strip()
            if not text:
                kind_errors.append("text must not be empty")
                continue
            if len(text) > MAX_LEN:
                kind_errors.append(f"text must be at most {MAX_LEN} characters")
            lowered = text.lower()
            if any(part in lowered for part in _SECRET_KEY_PARTS):
                kind_errors.append("text contains a secret marker")
            if text not in seen:
                seen.add(text)
                normalized_values.append(text)
        if not normalized_values:
            kind_errors.append("kind must contain at least one text")
        if len(normalized_values) > limit:
            kind_errors.append(f"kind must contain at most {limit} texts")
        if kind_errors:
            errors[kind] = list(dict.fromkeys(kind_errors))
    return errors


def _validated_normalized(kinds, *, require_defaults=False):
    errors = validate(kinds)
    present = set(kinds) if isinstance(kinds, Mapping) else set()
    if require_defaults:
        missing = [kind for kind in REQUIRED_KINDS if kind not in present]
        if missing:
            errors["kinds"] = ["missing required kinds: " + ", ".join(missing)]
    if errors:
        raise ValidationError(errors)
    return normalize(kinds)


def text_version(kinds):
    normalized = normalize(kinds)
    canonical = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "ft1-" + hashlib.sha256(canonical).hexdigest()[:16]


def load_default():
    """Load and strictly validate the bundled Japanese default."""
    path = resources.files("caty_gateway").joinpath("data", "filler-texts-ja.json")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("bundled filler texts are unavailable") from error
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != SCHEMA
        or payload.get("language") != LANGUAGE
    ):
        raise RuntimeError("bundled filler texts have invalid metadata")
    try:
        return _validated_normalized(payload.get("kinds"), require_defaults=True)
    except ValidationError as error:
        raise RuntimeError("bundled filler texts are invalid") from error


def _validate_member_id(member_id):
    if (
        not isinstance(member_id, str)
        or not member_id
        or member_id in (".", "..")
        or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for char in member_id)
    ):
        raise ValueError("invalid member id")
    return member_id


def _base_root(data_root=None):
    return (
        Path(data_root).expanduser()
        if data_root is not None
        else Path("~/.local/share/caty-gateway").expanduser()
    ).resolve()


def override_path(member_id, data_root=None):
    return _base_root(data_root) / _validate_member_id(member_id) / "filler-texts.json"


def _read_override(path):
    if not path.exists():
        return None, "none"
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("override is not a regular file")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("override must be an object")
        if payload.get("schema") != SCHEMA:
            raise ValueError("unsupported schema")
        if payload.get("language") != LANGUAGE:
            raise ValueError("unsupported language")
        kinds = _validated_normalized(payload.get("kinds"))
        return kinds, "ok"
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError, ValueError) as error:
        if isinstance(error, ValidationError):
            reason = "; ".join(
                f"{kind}: {', '.join(messages)}"
                for kind, messages in sorted(error.errors.items())
            )
        else:
            reason = str(error) or type(error).__name__
        return None, "invalid: " + reason


def _effective_from_path(member_id, path):
    defaults = load_default()
    override, status = _read_override(path)
    if status.startswith("invalid:"):
        LOGGER.warning("invalid filler text override member=%s reason=%s", member_id, status[9:])
    texts = _copy_kinds(defaults)
    sources = {kind: "default" for kind in texts}
    if override is not None:
        for kind, values in override.items():
            texts[kind] = list(values)
            sources[kind] = "override"
    normalized = normalize(texts)
    return EffectiveTexts(normalized, text_version(normalized), sources, status)


def effective(member_id, data_root=None):
    """Return one immutable snapshot of defaults plus the member override."""
    path = override_path(member_id, data_root)
    lock_path = path.with_name("filler-texts.lock")
    if not lock_path.parent.exists():
        return _effective_from_path(member_id, path)
    with lock_path.open("a+b") as lock:
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
        try:
            return _effective_from_path(member_id, path)
        finally:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _payload(kinds):
    return {
        "schema": SCHEMA,
        "language": LANGUAGE,
        "tone": "member",
        "source": "member override",
        "license": "private",
        "kinds": kinds,
    }


def _fsync_directory(path):
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def save_override(member_id, kinds, *, if_match=None, data_root=None):
    """Atomically replace a whole override after optimistic version checking."""
    normalized = _validated_normalized(kinds)
    path = override_path(member_id, data_root)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = path.with_name("filler-texts.lock")
    with lock_path.open("a+b") as lock:
        os.chmod(lock_path, 0o600)
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            current = _effective_from_path(member_id, path)
            if (
                (path.exists() and if_match is None)
                or (if_match is not None and if_match != current.version)
            ):
                raise ConflictError(current)
            temporary = None
            try:
                fd, temporary = tempfile.mkstemp(
                    prefix=".filler-texts-", suffix=".json", dir=path.parent
                )
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(_payload(normalized), handle, ensure_ascii=False, sort_keys=True)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, path)
                temporary = None
                _fsync_directory(path.parent)
            finally:
                if temporary is not None:
                    try:
                        os.unlink(temporary)
                    except OSError:
                        pass
            return _effective_from_path(member_id, path)
        finally:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def delete_override(member_id, *, if_match=None, data_root=None):
    """Delete an override only when its effective version matches If-Match."""
    path = override_path(member_id, data_root)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = path.with_name("filler-texts.lock")
    with lock_path.open("a+b") as lock:
        os.chmod(lock_path, 0o600)
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            current = _effective_from_path(member_id, path)
            if if_match is None or if_match != current.version:
                raise ConflictError(current)
            try:
                path.unlink()
                _fsync_directory(path.parent)
            except FileNotFoundError:
                pass
            return _effective_from_path(member_id, path)
        finally:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
