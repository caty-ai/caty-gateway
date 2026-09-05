"""Session-bound, single-use staging for app-to-agent file shares."""

import hashlib
import json
import math
import os
import re
import secrets
import stat
import threading
import time
import uuid
from dataclasses import dataclass


SHARE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SHARE_MEMBER_RE = re.compile(r"^[A-Za-z0-9._-]+$")
SHARE_MAX_LIVE_PER_SESSION = 4
ATTACHMENT_MIME_PNG = "image/png"
ATTACHMENT_MIME_JPEG = "image/jpeg"
ATTACHMENT_MIME_PDF = "application/pdf"


@dataclass(frozen=True)
class ClaimedFile:
    path: str
    sniffed_mime: str
    size: int
    filename: str
    declared_kind: str


Claimed = ClaimedFile


@dataclass(frozen=True)
class TextBytes:
    data: bytes
    filename: str
    size: int
    mime: str = "application/octet-stream"


@dataclass(frozen=True)
class Rejected:
    reason: str
    filename: str
    size: int
    declared_kind: str


def sniff_attachment_mime(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ATTACHMENT_MIME_PNG
    if data.startswith(b"\xff\xd8\xff"):
        return ATTACHMENT_MIME_JPEG
    if data.startswith(b"%PDF"):
        return ATTACHMENT_MIME_PDF
    return None


def cleanup_claimed_orphans(root_dir):
    """Remove regular claimed files without creating or following paths."""
    claimed_dir = os.path.join(
        os.path.abspath(os.path.expanduser(root_dir)), "claimed"
    )
    try:
        entries = list(os.scandir(claimed_dir))
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_file(follow_symlinks=False):
                try:
                    os.unlink(entry.path)
                except FileNotFoundError:
                    pass
        except OSError:
            continue


class ShareStoreError(Exception):
    pass


class InvalidShareId(ShareStoreError):
    pass


class ShareNotFound(ShareStoreError):
    pass


class SessionMismatch(ShareStoreError):
    pass


class ShareExpired(ShareStoreError):
    pass


class IdempotencyConflict(ShareStoreError):
    pass


class ShareQuotaExceeded(ShareStoreError):
    pass


class ShareStagingError(ShareStoreError):
    pass


def _default_share_member_component():
    member_id = os.environ.get("CATY_ID", "").strip()
    if (
        member_id
        and member_id not in (".", "..")
        and SHARE_MEMBER_RE.fullmatch(member_id)
    ):
        return member_id
    return "default"


def default_share_root():
    configured = os.environ.get("CATY_SHARE_DIR", "").strip()
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    state_home = os.environ.get("XDG_STATE_HOME", "").strip()
    if not state_home:
        state_home = os.path.expanduser("~/.local/state")
    return os.path.join(
        state_home,
        "caty-gateway",
        "share-spool",
        _default_share_member_component(),
    )


class ShareStore:
    """Short-lived share spool with atomic publication and single-use reads."""

    def __init__(
        self,
        root_dir,
        ttl_seconds=900,
        sweep_grace_seconds=86400,
        sweep_interval_seconds=900,
    ):
        self.root_dir = os.path.abspath(os.path.expanduser(root_dir))
        self.ttl_seconds = float(ttl_seconds)
        self.sweep_grace_seconds = float(sweep_grace_seconds)
        if sweep_interval_seconds is None or float(sweep_interval_seconds) <= 0:
            self._sweep_interval_seconds = None
        else:
            self._sweep_interval_seconds = float(sweep_interval_seconds)
        self._lock = threading.Lock()
        self._metadata = {}
        self._idempotency = {}
        self._expired = {}
        self._sweeper_started = False
        self._sweeper_stop = threading.Event()
        self._sweeper_thread = None
        os.makedirs(self.root_dir, mode=0o700, exist_ok=True)
        os.chmod(self.root_dir, 0o700)
        self.claimed_dir = os.path.join(self.root_dir, "claimed")
        os.makedirs(self.claimed_dir, mode=0o700, exist_ok=True)
        os.chmod(self.claimed_dir, 0o700)
        with self._lock:
            self._load_sidecars_locked()
            self._sweep_locked(time.time())

    def put(
        self,
        session_id,
        kind,
        filename,
        mime,
        data,
        idempotency_key=None,
    ):
        session_id = self._validate_session_id(session_id)
        kind = self._validate_kind(kind)
        filename = self._sanitize_filename(filename)
        mime = self._validate_mime(mime)
        idempotency_key = self._validate_idempotency_key(idempotency_key)
        if not isinstance(data, bytes):
            raise ValueError("data must be bytes")
        digest = hashlib.sha256(data).hexdigest()

        with self._lock:
            now = time.time()
            self._ensure_sweeper_locked()
            self._sweep_locked(now)
            idem_key = (
                (session_id, idempotency_key)
                if idempotency_key is not None
                else None
            )
            if idem_key is not None:
                existing_id = self._idempotency.get(idem_key)
                existing = self._metadata.get(existing_id)
                if existing is not None and self._data_exists(existing_id):
                    if existing["sha256"] != digest:
                        raise IdempotencyConflict()
                    return {
                        "share_id": existing_id,
                        "expires_at": existing["created_at"] + self.ttl_seconds,
                    }
                if existing_id is not None:
                    self._remove_locked(existing_id)
            self._check_live_share_quota_locked(session_id)

            share_id = self._new_share_id_locked()
            created_at = now
            metadata = {
                "kind": kind,
                "filename": filename,
                "mime": mime,
                "size": len(data),
                "sha256": digest,
                "session_id": session_id,
                "created_at": created_at,
                "idempotency_key": idempotency_key,
            }
            self._publish_locked(share_id, data, metadata)
            self._metadata[share_id] = metadata
            if idem_key is not None:
                self._idempotency[idem_key] = share_id
            return {
                "share_id": share_id,
                "expires_at": created_at + self.ttl_seconds,
            }

    def consume(self, share_id, session_id):
        self._validate_share_id(share_id)
        session_id = self._validate_session_id(session_id)
        with self._lock:
            metadata, data, now = self._read_valid_share_locked(
                share_id, session_id
            )
            return self._consume_locked(share_id, metadata, data, now)

    def take(self, share_id, session_id):
        """Atomically classify and consume one staged share."""
        self._validate_share_id(share_id)
        session_id = self._validate_session_id(session_id)
        with self._lock:
            metadata, data, now = self._read_valid_share_locked(
                share_id, session_id
            )
            sniffed_mime = sniff_attachment_mime(data)
            if sniffed_mime is None and metadata["kind"] == "file":
                consumed = self._consume_locked(
                    share_id, metadata, data, now
                )
                return TextBytes(
                    data=consumed["data"],
                    filename=metadata["filename"],
                    size=metadata["size"],
                    mime=metadata["mime"],
                )
            if sniffed_mime is None:
                self._remove_locked(share_id, strict=True)
                self._sweep_locked(now)
                return Rejected(
                    reason="mime-rejected",
                    filename=metadata["filename"],
                    size=metadata["size"],
                    declared_kind=metadata["kind"],
                )

            claimed_path = os.path.join(self.claimed_dir, uuid.uuid4().hex)
            os.replace(self._data_path(share_id), claimed_path)
            try:
                os.chmod(claimed_path, 0o600)
                self._remove_locked(share_id, strict=True)
            except Exception:
                self._unlink(claimed_path)
                raise
            self._sweep_locked(now)
            return ClaimedFile(
                path=claimed_path,
                sniffed_mime=sniffed_mime,
                size=metadata["size"],
                filename=metadata["filename"],
                declared_kind=metadata["kind"],
            )

    def stage_claimed_bytes(
        self, data: bytes, filename: str, declared_kind: str
    ) -> ClaimedFile | Rejected:
        """Atomically stage trusted request bytes for attachment delivery."""
        if not isinstance(data, bytes):
            raise ValueError("data must be bytes")
        filename = self._sanitize_filename(filename)
        declared_kind = self._validate_kind(declared_kind)
        with self._lock:
            self._sweep_locked(time.time())
            sniffed_mime = sniff_attachment_mime(data)
            if sniffed_mime is None:
                return Rejected(
                    reason="mime-rejected",
                    filename=filename,
                    size=len(data),
                    declared_kind=declared_kind,
                )

            claimed_path = os.path.join(self.claimed_dir, uuid.uuid4().hex)
            part_path = claimed_path + ".part"
            try:
                self._write_private_file(part_path, data)
                os.replace(part_path, claimed_path)
                os.chmod(claimed_path, 0o600)
            except Exception:
                self._unlink(part_path)
                self._unlink(claimed_path)
                raise
            return ClaimedFile(
                path=claimed_path,
                sniffed_mime=sniffed_mime,
                size=len(data),
                filename=filename,
                declared_kind=declared_kind,
            )

    def cleanup_claimed_orphans(self):
        """Remove regular claimed files left by a previous process."""
        cleanup_claimed_orphans(self.root_dir)

    def _read_valid_share_locked(self, share_id, session_id):
        expired = self._expired.get(share_id)
        if expired is not None:
            self._sweep_locked(time.time())
            raise ShareExpired()
        metadata = self._metadata.get(share_id)
        if metadata is None:
            metadata = self._load_sidecar_locked(share_id)
        if metadata is None or not self._data_exists(share_id):
            self._remove_locked(share_id)
            self._sweep_locked(time.time())
            raise ShareNotFound()

        now = time.time()
        if now >= metadata["created_at"] + self.ttl_seconds:
            self._remove_locked(share_id)
            if now - metadata["created_at"] < self.sweep_grace_seconds:
                self._expired[share_id] = metadata["created_at"]
            self._sweep_locked(now)
            raise ShareExpired()
        if metadata["session_id"] != session_id:
            self._sweep_locked(now, exclude=share_id)
            raise SessionMismatch()

        try:
            with open(self._data_path(share_id), "rb") as stream:
                data = stream.read()
        except OSError:
            self._remove_locked(share_id)
            raise ShareNotFound()
        if (
            len(data) != metadata["size"]
            or hashlib.sha256(data).hexdigest() != metadata["sha256"]
        ):
            self._remove_locked(share_id)
            raise ShareNotFound()
        return metadata, data, now

    def _consume_locked(self, share_id, metadata, data, now):
        result = dict(metadata)
        result.update({
            "share_id": share_id,
            "expires_at": metadata["created_at"] + self.ttl_seconds,
            "data": data,
        })
        self._remove_locked(share_id, strict=True)
        self._sweep_locked(now)
        return result

    def sweep(self):
        with self._lock:
            self._sweep_locked(time.time())

    def close(self):
        self._sweeper_stop.set()
        thread = self._sweeper_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.2)

    def _publish_locked(self, share_id, data, metadata):
        data_path = self._data_path(share_id)
        part_path = data_path + ".part"
        sidecar_path = self._sidecar_path(share_id)
        sidecar_part = sidecar_path + ".part"
        published_data = False
        published_sidecar = False
        try:
            self._write_private_file(part_path, data)
            self._validate_staged(part_path, metadata)
            encoded = json.dumps(
                metadata,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            self._write_private_file(sidecar_part, encoded)
            os.replace(part_path, data_path)
            published_data = True
            os.chmod(data_path, 0o600)
            os.replace(sidecar_part, sidecar_path)
            published_sidecar = True
            os.chmod(sidecar_path, 0o600)
        except Exception:
            for path in (part_path, sidecar_part):
                self._unlink(path)
            if published_data:
                self._unlink(data_path)
            if published_sidecar:
                self._unlink(sidecar_path)
            raise

    def _validate_staged(self, part_path, metadata):
        hasher = hashlib.sha256()
        size = 0
        with open(part_path, "rb") as stream:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                hasher.update(chunk)
        if size != metadata["size"] or hasher.hexdigest() != metadata["sha256"]:
            raise ShareStagingError("staged share validation failed")

    def _ensure_sweeper_locked(self):
        if self._sweep_interval_seconds is None or self._sweeper_started:
            return
        self._sweeper_started = True
        self._sweeper_thread = threading.Thread(
            target=self._sweeper_loop,
            daemon=True,
        )
        self._sweeper_thread.start()

    def _sweeper_loop(self):
        interval = self._sweep_interval_seconds
        while interval is not None and not self._sweeper_stop.wait(interval):
            try:
                self.sweep()
            except Exception:
                pass

    def _write_private_file(self, path, data):
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(path, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                fd = None
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if fd is not None:
                os.close(fd)
        os.chmod(path, 0o600)

    def _load_sidecars_locked(self):
        try:
            names = os.listdir(self.root_dir)
        except OSError:
            return
        for name in names:
            match = re.fullmatch(r"([0-9a-f]{32})\.json", name)
            if match:
                self._load_sidecar_locked(match.group(1))

    def _load_sidecar_locked(self, share_id):
        if not SHARE_ID_RE.fullmatch(share_id):
            return None
        try:
            with open(self._sidecar_path(share_id), "r", encoding="utf-8") as stream:
                metadata = json.load(stream)
            metadata = self._validate_metadata(metadata)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
        self._metadata[share_id] = metadata
        idempotency_key = metadata.get("idempotency_key")
        if idempotency_key is not None:
            key = (metadata["session_id"], idempotency_key)
            current_id = self._idempotency.get(key)
            current = self._metadata.get(current_id)
            if current is None or current["created_at"] <= metadata["created_at"]:
                self._idempotency[key] = share_id
        return metadata

    def _validate_metadata(self, metadata):
        if not isinstance(metadata, dict):
            raise ValueError("invalid metadata")
        required = {
            "kind", "filename", "mime", "size", "sha256", "session_id",
            "created_at", "idempotency_key",
        }
        if set(metadata) != required:
            raise ValueError("invalid metadata")
        result = {
            "kind": self._validate_kind(metadata["kind"]),
            "filename": self._sanitize_filename(metadata["filename"]),
            "mime": self._validate_mime(metadata["mime"]),
            "session_id": self._validate_session_id(metadata["session_id"]),
            "idempotency_key": self._validate_idempotency_key(
                metadata["idempotency_key"]
            ),
        }
        if not isinstance(metadata["size"], int) or metadata["size"] < 0:
            raise ValueError("invalid metadata")
        if not isinstance(metadata["sha256"], str) or not re.fullmatch(
            r"[0-9a-f]{64}", metadata["sha256"]
        ):
            raise ValueError("invalid metadata")
        if (
            not isinstance(metadata["created_at"], (int, float))
            or not math.isfinite(metadata["created_at"])
        ):
            raise ValueError("invalid metadata")
        result.update({
            "size": metadata["size"],
            "sha256": metadata["sha256"],
            "created_at": float(metadata["created_at"]),
        })
        return result

    def _sweep_locked(self, now, exclude=None):
        for share_id, created_at in list(self._expired.items()):
            if now - created_at >= self.sweep_grace_seconds:
                self._expired.pop(share_id, None)
        for share_id, metadata in list(self._metadata.items()):
            if share_id == exclude:
                continue
            age = now - metadata["created_at"]
            if age >= self.sweep_grace_seconds:
                self._remove_locked(share_id)
                self._expired.pop(share_id, None)
            elif age >= self.ttl_seconds:
                self._remove_locked(share_id)
                self._expired[share_id] = metadata["created_at"]

        try:
            entries = list(os.scandir(self.root_dir))
        except OSError:
            return
        for entry in entries:
            name = entry.name
            if name.endswith(".part"):
                self._unlink(entry.path)
                continue
            try:
                age = now - entry.stat(follow_symlinks=False).st_mtime
            except OSError:
                continue
            if age >= self.sweep_grace_seconds:
                match = re.fullmatch(r"([0-9a-f]{32})(?:\.json)?", name)
                if match:
                    self._remove_locked(match.group(1))
                    self._expired.pop(match.group(1), None)
                else:
                    self._unlink(entry.path)

    def _check_live_share_quota_locked(self, session_id):
        live_share_count = 0
        for share_id, metadata in list(self._metadata.items()):
            if metadata["session_id"] != session_id:
                continue
            if not self._data_exists(share_id):
                self._remove_locked(share_id)
                continue
            live_share_count += 1
        if live_share_count >= SHARE_MAX_LIVE_PER_SESSION:
            raise ShareQuotaExceeded()

    def _remove_locked(self, share_id, strict=False):
        metadata = self._metadata.pop(share_id, None)
        if metadata is not None and metadata.get("idempotency_key") is not None:
            key = (metadata["session_id"], metadata["idempotency_key"])
            if self._idempotency.get(key) == share_id:
                self._idempotency.pop(key, None)
        # Commit logical single-use removal, including the reloadable sidecar,
        # before the data unlink. A physical failure must not resurrect a share.
        first_error = None
        for path in (self._sidecar_path(share_id), self._data_path(share_id)):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError as error:
                if strict and first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error

    def _data_exists(self, share_id):
        try:
            mode = os.stat(self._data_path(share_id), follow_symlinks=False).st_mode
        except OSError:
            return False
        return stat.S_ISREG(mode)

    def _new_share_id_locked(self):
        for _ in range(100):
            share_id = secrets.token_hex(16)
            paths = (
                self._data_path(share_id),
                self._data_path(share_id) + ".part",
                self._sidecar_path(share_id),
                self._sidecar_path(share_id) + ".part",
            )
            if (
                share_id not in self._expired
                and not any(os.path.lexists(path) for path in paths)
            ):
                return share_id
        raise RuntimeError("unable to allocate share id")

    def _data_path(self, share_id):
        self._validate_share_id(share_id)
        return os.path.join(self.root_dir, share_id)

    def _sidecar_path(self, share_id):
        self._validate_share_id(share_id)
        return os.path.join(self.root_dir, share_id + ".json")

    @staticmethod
    def _validate_share_id(share_id):
        if not isinstance(share_id, str) or not SHARE_ID_RE.fullmatch(share_id):
            raise InvalidShareId()
        return share_id

    @staticmethod
    def _validate_session_id(session_id):
        if (
            not isinstance(session_id, str)
            or not session_id
            or "\x00" in session_id
            or "/" in session_id
            or "\\" in session_id
            or ".." in session_id
        ):
            raise ValueError("invalid session id")
        return session_id

    @staticmethod
    def _validate_kind(kind):
        if kind not in ("image", "file"):
            raise ValueError("invalid kind")
        return kind

    @staticmethod
    def _sanitize_filename(filename):
        if not isinstance(filename, str):
            raise ValueError("invalid filename")
        if (
            "\x00" in filename
            or "/" in filename
            or "\\" in filename
            or ".." in filename
            or os.path.basename(filename) != filename
        ):
            raise ValueError("invalid filename")
        return os.path.basename(filename)

    @staticmethod
    def _validate_mime(mime):
        if not isinstance(mime, str) or not mime or "\x00" in mime or ".." in mime:
            raise ValueError("invalid mime")
        if any(ord(char) < 0x20 or ord(char) > 0x7e for char in mime):
            raise ValueError("invalid mime")
        return mime

    @staticmethod
    def _validate_idempotency_key(key):
        if key is None:
            return None
        if not isinstance(key, str) or not key or len(key) > 128:
            raise ValueError("invalid idempotency key")
        if any(ord(char) < 0x20 or ord(char) > 0x7e for char in key):
            raise ValueError("invalid idempotency key")
        return key

    @staticmethod
    def _unlink(path):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError:
            pass
