"""Disk-authoritative, one-time pairing credentials.

Unlike :meth:`share_store.ShareStore.consume`, a successful claim leaves a
disk tombstone so later processes can distinguish consumed (409) from unknown
(401), and expiry leaves an expired tombstone (410).  Revocation deliberately
leaves no tombstone.  Record files are the only authority: this module keeps no
record, expiry, or tombstone index in memory, even as a cache.

Per A3 / #1125, ``fail_count`` / ``locked_until`` updates and issuance-time
revocation are serialized by ``fcntl.flock`` on a persistent ``.lockf`` file.
Each acquisition uses a fresh ``os.open()`` so the kernel's open-file-description
lock semantics keep the critical section real across processes and threads.
``.lockf`` is never unlinked, which avoids the split-inode race where an old fd
keeps a lock on the unlinked inode while a new contender locks a replacement
path.  Crash and normal-exit recovery improve because the kernel releases the
lock immediately when the holder dies or closes its fd.  The accepted tradeoff is
that a holder that stays alive but wedged (for example SIGSTOP or an I/O stall)
keeps the lock until an operator intervenes; contenders fail closed with 503
after ``LOCK_WAIT_SECONDS``.  Single-use still hinges on
``os.unlink(record_path)`` winning exactly once, so §11-3 narrows to availability
degradation rather than overlapping lock holders.  Fencing tokens are
deliberately not introduced.
"""

from dataclasses import dataclass
import errno
import fcntl
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sys
import threading
import time


PAIR_RE = re.compile(r"^[0-9a-f]{8}\.[0-9a-f]{32}$")
_PID_RE = re.compile(r"^[0-9a-f]{8}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_MEMBER_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_RECORD_NAME_RE = re.compile(r"^([0-9a-f]{8})\.json$")
# The only temporary name this store ever creates (_replace_record).  Sweeping by
# a bare ".part" suffix would delete unrelated files if CATY_PAIRING_DIR is ever
# pointed at a directory the gateway does not own.
_PART_NAME_RE = re.compile(r"^[0-9a-f]{8}\.json\.part$")
_TOMBSTONE_NAME_RE = re.compile(r"^([0-9a-f]{8})\.tombstone$")
_STALE_LOCK_NAME_RE = re.compile(r"^\.lock\.stale-[0-9]+-[0-9a-f]{8}$")

TOMBSTONE_GRACE_SECONDS = 24 * 60 * 60
# A2-era parked ``.lock.stale-*`` remnants are still swept during rollout.
LOCK_STALE_SECONDS = 30
# How long a caller waits for a wedged live holder before giving up with 503.
LOCK_WAIT_SECONDS = 1.0
LOCK_TIMEOUT_WARN_WINDOW_SECONDS = 60
SWEEP_INTERVAL_SECONDS = 60


class PairingStoreError(Exception):
    """Base class for pairing-store outcomes."""


class PairingInvalid(PairingStoreError):
    pass


class PairingConsumed(PairingStoreError):
    pass


class PairingExpired(PairingStoreError):
    pass


class PairingRateLimited(PairingStoreError):
    pass


class PairingUnavailable(PairingStoreError):
    pass


@dataclass(frozen=True)
class PairingConfig:
    ttl_seconds: int = 600
    max_failures: int = 5
    lockout_seconds: int = 60
    max_fail_total: int = 50
    rate_per_minute: int = 10
    allow_nontailnet: bool = False


def _warn_default(message):
    print(f"WARN pairing {message}", file=sys.stderr, flush=True)


def _positive_env(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"{name} must be a positive integer; got {raw!r}"
        ) from error
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero; got {raw!r}")
    return value


def load_config(warn=None):
    """Parse pairing env knobs, aborting on invalid numeric values."""
    warn = warn or _warn_default
    ttl = _positive_env("CATY_PAIRING_TTL_SECONDS", 600)
    clamped_ttl = min(3600, max(60, ttl))
    if clamped_ttl != ttl:
        warn(
            "CATY_PAIRING_TTL_SECONDS "
            f"clamped from {ttl} to {clamped_ttl} (allowed 60..3600)"
        )
    return PairingConfig(
        ttl_seconds=clamped_ttl,
        max_failures=_positive_env("CATY_PAIRING_MAX_FAILURES", 5),
        lockout_seconds=_positive_env("CATY_PAIRING_LOCKOUT_SECONDS", 60),
        max_fail_total=_positive_env("CATY_PAIRING_MAX_FAIL_TOTAL", 50),
        rate_per_minute=_positive_env("CATY_PAIRING_RATE_PER_MIN", 10),
        allow_nontailnet=(
            os.environ.get("CATY_PAIRING_ALLOW_NONTAILNET", "").strip() == "1"
        ),
    )


def default_pairing_member():
    """Member namespace per §7-3: CATY_ID when usable, otherwise ``default``.

    The store directory and the record's ``member_id`` are both derived from this
    one rule.  Deriving them separately (e.g. member_id from ``IDENTITY_ID``,
    whose fallback is ``caty``) makes two processes that share the ``default``
    directory read each other's records as foreign and delete them.
    """
    member_id = os.environ.get("CATY_ID", "").strip()
    if (
        member_id
        and member_id not in (".", "..")
        and _MEMBER_RE.fullmatch(member_id)
    ):
        return member_id
    return "default"


def _default_pairing_member_component():
    return default_pairing_member()


def default_pairing_root():
    configured = os.environ.get("CATY_PAIRING_DIR", "").strip()
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    state_home = os.environ.get("XDG_STATE_HOME", "").strip()
    if not state_home:
        state_home = os.path.expanduser("~/.local/state")
    return os.path.join(
        state_home,
        "caty-gateway",
        "pairing",
        _default_pairing_member_component(),
    )


class PairingStore:
    """Private disk store whose record files are the sole source of truth."""

    def __init__(self, root_dir, member_id, config=None, warn=None, clock=None):
        self.root_dir = os.path.abspath(os.path.expanduser(root_dir))
        self.member_id = member_id
        self.config = config or PairingConfig()
        self._warn_callback = warn or _warn_default
        self._clock = clock or time.time
        self._sweeper_stop = threading.Event()
        self._sweeper_thread = None
        self._lock_timeout_warning_window = None
        self._lock_timeout_warning_guard = threading.Lock()
        try:
            os.makedirs(self.root_dir, mode=0o700, exist_ok=True)
            os.chmod(self.root_dir, 0o700)
        except OSError as error:
            raise PairingUnavailable("cannot initialize pairing store root") from error
        with self._file_lock():
            self._load_startup_records()

    def issue(self):
        """Create a new credential, then revoke older live records."""
        with self._file_lock():
            now = float(self._clock())
            for _ in range(100):
                pid = secrets.token_hex(4)
                secret = secrets.token_hex(16)
                record_path = self._record_path(pid)
                if os.path.lexists(self._tombstone_path(pid)) or os.path.lexists(
                    record_path + ".part"
                ):
                    continue
                record = {
                    "pid": pid,
                    "secret_sha256": hashlib.sha256(
                        secret.encode("utf-8")
                    ).hexdigest(),
                    "member_id": self.member_id,
                    "created_at": now,
                    "ttl_seconds": self.config.ttl_seconds,
                    "fail_count": 0,
                    "fail_total": 0,
                    "locked_until": 0,
                }
                try:
                    self._write_json_exclusive(record_path, record)
                except FileExistsError:
                    continue
                except OSError as error:
                    try:
                        self._unlink(record_path)
                    except OSError:
                        pass
                    raise PairingUnavailable("cannot persist pairing record") from error

                # New-first ordering preserves availability across a crash.  A
                # later startup keeps this newest created_at if both survive.
                try:
                    self._revoke_records_except(pid)
                except OSError as error:
                    self._delete_record(pid)
                    raise PairingUnavailable("cannot revoke old pairing record") from error
                return {
                    "pair": f"{pid}.{secret}",
                    "pid": pid,
                    "expires_at": now + self.config.ttl_seconds,
                }
        raise PairingUnavailable("cannot allocate pairing pid")

    def claim(self, pair, credential_getter, enforce_cumulative_revoke=True):
        """Consume *pair* at most once and return the current client credential."""
        # This fullmatch is deliberately before every path build and file read.
        if not isinstance(pair, str) or not PAIR_RE.fullmatch(pair):
            raise PairingInvalid()
        pid, _, secret = pair.partition(".")
        now = float(self._clock())

        self._raise_for_tombstone(pid)
        record = self._read_record(pid)
        if record is None:
            raise PairingInvalid()
        try:
            record = self._validate_record(record, expected_pid=pid)
        except (TypeError, ValueError):
            self._delete_record(pid)
            self._warn(f"pid={pid} corrupted_record deleted")
            raise PairingInvalid()
        if record["member_id"] != self.member_id:
            self._delete_record(pid)
            self._warn(f"pid={pid} foreign_member_record deleted")
            raise PairingInvalid()

        expires_at = record["created_at"] + record["ttl_seconds"]
        if now >= expires_at:
            with self._file_lock():
                current = self._read_record(pid)
                if current is None:
                    self._raise_for_tombstone(pid)
                    raise PairingInvalid()
                try:
                    current = self._validate_record(current, expected_pid=pid)
                except (TypeError, ValueError):
                    self._delete_record(pid)
                    self._warn(f"pid={pid} corrupted_record deleted")
                    raise PairingInvalid()
                if current["member_id"] != self.member_id:
                    self._delete_record(pid)
                    self._warn(f"pid={pid} foreign_member_record deleted")
                    raise PairingInvalid()
                current_expires_at = current["created_at"] + current["ttl_seconds"]
                if now >= current_expires_at:
                    self._expire_record(pid, now, current_expires_at)
                else:
                    raise PairingInvalid()
            raise PairingExpired()
        if record["locked_until"] > now:
            raise PairingRateLimited()

        supplied_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(supplied_hash, record["secret_sha256"]):
            self._record_failure(pid, now, enforce_cumulative_revoke)
            raise PairingInvalid()

        # Serialize the final re-read and unlink with counter replacement and
        # issuance/revoke.  Without this, a concurrent .part -> replace could
        # recreate a record immediately after a successful unlink.
        with self._file_lock():
            self._raise_for_tombstone(pid)
            current = self._read_record(pid)
            if current is None:
                self._raise_for_tombstone(pid)
                raise PairingInvalid()
            try:
                current = self._validate_record(current, expected_pid=pid)
            except (TypeError, ValueError):
                self._delete_record(pid)
                self._warn(f"pid={pid} corrupted_record deleted")
                raise PairingInvalid()
            if current["member_id"] != self.member_id:
                self._delete_record(pid)
                self._warn(f"pid={pid} foreign_member_record deleted")
                raise PairingInvalid()
            current_expires_at = current["created_at"] + current["ttl_seconds"]
            if now >= current_expires_at:
                self._expire_record(pid, now, current_expires_at)
                raise PairingExpired()
            if current["locked_until"] > now:
                raise PairingRateLimited()
            if not hmac.compare_digest(supplied_hash, current["secret_sha256"]):
                raise PairingInvalid()

            # A pre-unlink guard makes the normal token-loss race non-destructive;
            # the post-unlink guard below remains the frozen final post-condition.
            credential = credential_getter()
            if not credential:
                raise PairingUnavailable("client credential unavailable")
            try:
                os.unlink(self._record_path(pid))
            except FileNotFoundError:
                self._raise_for_tombstone(pid)
                raise PairingInvalid()
            except OSError as error:
                raise PairingUnavailable("cannot consume pairing record") from error

            try:
                self._write_tombstone(pid, "consumed", now, current_expires_at)
            except Exception as error:
                self._warn(f"pid={pid} consumed_tombstone_write_failed")
                raise PairingUnavailable("cannot persist consumed tombstone") from error

            credential = credential_getter()
            if not credential:
                raise PairingUnavailable("client credential unavailable")
            return credential

    def revoke(self):
        """Idempotently delete all live records without tombstones."""
        with self._file_lock():
            try:
                self._revoke_records_except(None)
            except OSError as error:
                raise PairingUnavailable("cannot revoke pairing record") from error

    def live_count(self):
        count = 0
        for name in self._list_names():
            if _RECORD_NAME_RE.fullmatch(name):
                count += 1
        return count

    def sweep(self):
        """Expire records by content timestamps and remove aged tombstones."""
        with self._file_lock():
            now = float(self._clock())
            for name in self._list_names():
                if _STALE_LOCK_NAME_RE.fullmatch(name):
                    # §7-9 retains cleanup for A2-era parked stale-lock remnants.
                    path = os.path.join(self.root_dir, name)
                    try:
                        age = now - os.stat(path, follow_symlinks=False).st_mtime
                    except FileNotFoundError:
                        continue
                    except OSError as error:
                        raise PairingUnavailable(
                            "cannot inspect A2 stale-lock remnant"
                        ) from error
                    if age > LOCK_STALE_SECONDS:
                        try:
                            self._unlink(path)
                        except OSError as error:
                            raise PairingUnavailable(
                                "cannot delete A2 stale-lock remnant"
                            ) from error
                    continue
                if _PART_NAME_RE.fullmatch(name):
                    # Writers only create .part while holding this lock, so any
                    # that survives to a sweep is a crash orphan.
                    self._unlink(os.path.join(self.root_dir, name))
                    continue
                record_match = _RECORD_NAME_RE.fullmatch(name)
                if record_match:
                    pid = record_match.group(1)
                    record = self._read_record(pid)
                    try:
                        record = self._validate_record(record, expected_pid=pid)
                    except (TypeError, ValueError):
                        self._warn(f"pid={pid} sweep_unparseable_record retained")
                        continue
                    if record["member_id"] != self.member_id:
                        self._delete_record(pid)
                        self._warn(f"pid={pid} foreign_member_record deleted")
                        continue
                    expires_at = record["created_at"] + record["ttl_seconds"]
                    if now >= expires_at:
                        self._expire_record(pid, now, expires_at)
                    continue
                tombstone_match = _TOMBSTONE_NAME_RE.fullmatch(name)
                if tombstone_match:
                    pid = tombstone_match.group(1)
                    tombstone = self._read_tombstone(pid)
                    try:
                        tombstone = self._validate_tombstone(tombstone, pid)
                    except (TypeError, ValueError):
                        self._warn(f"pid={pid} sweep_unparseable_tombstone retained")
                        continue
                    if now >= tombstone["expires_at"] + TOMBSTONE_GRACE_SECONDS:
                        self._unlink(self._tombstone_path(pid))

    def start_sweeper(self):
        """Sweep once and start the resident-server-only 60 second loop."""
        if self._sweeper_thread is not None:
            return
        self.sweep()
        thread = threading.Thread(target=self._sweeper_loop, daemon=True)
        self._sweeper_thread = thread
        thread.start()

    def close(self):
        self._sweeper_stop.set()
        thread = self._sweeper_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.2)

    def _sweeper_loop(self):
        while not self._sweeper_stop.wait(SWEEP_INTERVAL_SECONDS):
            try:
                self.sweep()
            except Exception as error:
                self._warn(f"sweep_failed error_type={type(error).__name__}")

    def _record_failure(self, pid, now, enforce_cumulative_revoke):
        try:
            with self._file_lock():
                record = self._read_record(pid)
                if record is None:
                    raise PairingInvalid()
                record = self._validate_record(record, expected_pid=pid)
                if record["member_id"] != self.member_id:
                    self._delete_record(pid)
                    self._warn(f"pid={pid} foreign_member_record deleted")
                    raise PairingInvalid()
                if record["locked_until"] > now:
                    raise PairingRateLimited()
                if record["locked_until"] and record["locked_until"] <= now:
                    record["fail_count"] = 0
                    record["locked_until"] = 0
                record["fail_count"] += 1
                record["fail_total"] += 1
                if (
                    enforce_cumulative_revoke
                    and record["fail_total"] >= self.config.max_fail_total
                ):
                    self._delete_record(pid)
                    self._warn(f"pid={pid} cumulative_failure_revoke")
                    return
                if record["fail_count"] >= self.config.max_failures:
                    record["locked_until"] = now + self.config.lockout_seconds
                try:
                    self._replace_record(pid, record)
                except PairingUnavailable as error:
                    self._warn(f"pid={pid} failure_counter_persist_failed")
                    raise PairingInvalid() from error
        except (PairingInvalid, PairingRateLimited, PairingUnavailable):
            raise
        except Exception as error:
            self._warn(f"pid={pid} failure_counter_persist_failed")
            raise PairingInvalid() from error

    def _expire_record(self, pid, now, expires_at):
        try:
            os.unlink(self._record_path(pid))
        except FileNotFoundError:
            self._raise_for_tombstone(pid)
            raise PairingInvalid()
        except OSError as error:
            raise PairingUnavailable("cannot expire pairing record") from error
        self._write_tombstone(pid, "expired", now, expires_at)

    def _write_tombstone(self, pid, state, now, expires_at):
        tombstone = {
            "pid": pid,
            "state": state,
            "at": now,
            "expires_at": expires_at,
        }
        path = self._tombstone_path(pid)
        try:
            self._write_json_exclusive(path, tombstone)
        except FileExistsError:
            existing = self._read_tombstone(pid)
            if not existing or existing.get("state") != state:
                raise PairingUnavailable("conflicting pairing tombstone")

    def _raise_for_tombstone(self, pid):
        tombstone = self._read_tombstone(pid)
        if tombstone is None:
            return
        try:
            tombstone = self._validate_tombstone(tombstone, pid)
        except (TypeError, ValueError):
            self._warn(f"pid={pid} corrupted_tombstone ignored")
            return
        if tombstone["state"] == "consumed":
            raise PairingConsumed()
        raise PairingExpired()

    def _load_startup_records(self):
        valid = []
        for name in self._list_names():
            match = _RECORD_NAME_RE.fullmatch(name)
            if not match:
                continue
            pid = match.group(1)
            record = self._read_record(pid)
            try:
                record = self._validate_record(record, expected_pid=pid)
            except (TypeError, ValueError):
                self._warn(f"pid={pid} startup_unparseable_record retained")
                continue
            if record["member_id"] != self.member_id:
                self._delete_record(pid)
                self._warn(f"pid={pid} foreign_member_record deleted")
                continue
            valid.append(record)
        if len(valid) <= 1:
            return
        newest = max(valid, key=lambda item: item["created_at"])
        for record in valid:
            if record["pid"] != newest["pid"]:
                self._delete_record(record["pid"])
        self._warn(
            f"pid={newest['pid']} duplicate_live_records kept_newest "
            f"deleted={len(valid) - 1}"
        )

    def _revoke_records_except(self, keep_pid):
        for name in self._list_names():
            match = _RECORD_NAME_RE.fullmatch(name)
            if match and match.group(1) != keep_pid:
                self._unlink(os.path.join(self.root_dir, name))

    def _replace_record(self, pid, record):
        path = self._record_path(pid)
        part_path = path + ".part"
        encoded = self._encode_json(record)
        try:
            self._write_private_file(part_path, encoded)
            os.replace(part_path, path)
            os.chmod(path, 0o600)
        except Exception as error:
            self._unlink(part_path)
            raise PairingUnavailable("cannot update pairing record") from error

    def _write_json_exclusive(self, path, payload):
        self._write_private_file(path, self._encode_json(payload))

    @staticmethod
    def _encode_json(payload):
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @staticmethod
    def _write_private_file(path, data):
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

    def _read_record(self, pid):
        try:
            with open(self._record_path(pid), "r", encoding="utf-8") as stream:
                return json.load(stream)
        except FileNotFoundError:
            return None
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def _read_tombstone(self, pid):
        try:
            with open(self._tombstone_path(pid), "r", encoding="utf-8") as stream:
                return json.load(stream)
        except FileNotFoundError:
            return None
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def _validate_record(self, record, expected_pid):
        if not isinstance(record, dict) or set(record) != {
            "pid", "secret_sha256", "member_id", "created_at", "ttl_seconds",
            "fail_count", "fail_total", "locked_until",
        }:
            raise ValueError("invalid pairing record")
        if record["pid"] != expected_pid or not _PID_RE.fullmatch(record["pid"]):
            raise ValueError("invalid pairing pid")
        if not isinstance(record["secret_sha256"], str) or not _HASH_RE.fullmatch(
            record["secret_sha256"]
        ):
            raise ValueError("invalid pairing secret hash")
        if not isinstance(record["member_id"], str):
            raise ValueError("invalid pairing member")
        for name in ("created_at", "locked_until"):
            value = record[name]
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"invalid {name}")
        if not isinstance(record["ttl_seconds"], (int, float)) or (
            not math.isfinite(record["ttl_seconds"])
            or record["ttl_seconds"] <= 0
        ):
            raise ValueError("invalid ttl_seconds")
        for name in ("fail_count", "fail_total"):
            if not isinstance(record[name], int) or record[name] < 0:
                raise ValueError(f"invalid {name}")
        return dict(record)

    @staticmethod
    def _validate_tombstone(tombstone, expected_pid):
        if not isinstance(tombstone, dict) or set(tombstone) != {
            "pid", "state", "at", "expires_at",
        }:
            raise ValueError("invalid tombstone")
        if tombstone["pid"] != expected_pid:
            raise ValueError("invalid tombstone pid")
        if tombstone["state"] not in ("consumed", "expired"):
            raise ValueError("invalid tombstone state")
        for name in ("at", "expires_at"):
            value = tombstone[name]
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"invalid tombstone {name}")
        return dict(tombstone)

    class _FileLock:
        """Non-reentrant `.lockf` flock guard for the store critical section.

        Every acquisition opens a fresh fd for `.lockf`; reusing an fd would
        collapse the kernel's open-file-description lock semantics and let one
        `LOCK_UN` release every nested caller at once.  Nested `_file_lock()`
        calls are therefore forbidden: within a single process they self-conflict
        and time out with `PairingUnavailable("pairing store lock busy")`.
        The current seven call sites (`__init__`, `issue`, the two `claim`
        critical sections, `revoke`, `sweep`, and `_record_failure`) are all
        non-nested.  Because `PairingStore.__init__` acquires this lock, a broken
        or hostile `.lockf` path fails loud during startup instead of silently
        weakening serialization.
        """

        def __init__(self, store):
            self.store = store
            self.path = os.path.join(store.root_dir, ".lockf")
            self.acquired = False
            self.fd = None

        def __enter__(self):
            try:
                fd = self._open_fd()
            except OSError as error:
                raise PairingUnavailable(
                    "cannot open pairing store lock"
                ) from error

            try:
                wait_deadline = time.monotonic() + LOCK_WAIT_SECONDS
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError as error:
                        if error.errno not in (errno.EACCES, errno.EAGAIN):
                            raise PairingUnavailable(
                                "cannot acquire pairing store lock"
                            ) from error
                        if time.monotonic() >= wait_deadline:
                            self.store._warn_lock_wait_timeout()
                            raise PairingUnavailable("pairing store lock busy")
                        time.sleep(0.01)
                    else:
                        self.fd = fd
                        self.acquired = True
                        return self
            except BaseException:
                try:
                    os.close(fd)
                except OSError:
                    # Never mask the acquisition failure being propagated; a
                    # close failure here only degrades diagnostics.
                    self.store._warn("lock_fd_close_failed")
                raise

        def _open_fd(self):
            flags = (
                os.O_RDWR
                | os.O_CREAT
                | os.O_CLOEXEC
                | os.O_NOFOLLOW
            )
            return os.open(
                self.path,
                flags,
                0o600,
            )

        def __exit__(self, exc_type, exc, traceback):
            if self.acquired and self.fd is not None:
                unlock_error = None
                try:
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
                except OSError as error:
                    unlock_error = error
                finally:
                    try:
                        os.close(self.fd)
                    except OSError as close_error:
                        if unlock_error is None:
                            unlock_error = close_error
                    finally:
                        self.fd = None
                        self.acquired = False
                if unlock_error is not None:
                    raise PairingUnavailable(
                        "cannot release pairing store lock"
                    ) from unlock_error

    def _file_lock(self):
        return self._FileLock(self)

    def _record_path(self, pid):
        if not _PID_RE.fullmatch(pid):
            raise PairingInvalid()
        return os.path.join(self.root_dir, pid + ".json")

    def _tombstone_path(self, pid):
        if not _PID_RE.fullmatch(pid):
            raise PairingInvalid()
        return os.path.join(self.root_dir, pid + ".tombstone")

    def _delete_record(self, pid):
        try:
            self._unlink(self._record_path(pid))
        except OSError as error:
            raise PairingUnavailable("cannot delete pairing record") from error

    def _list_names(self):
        try:
            return os.listdir(self.root_dir)
        except OSError as error:
            raise PairingUnavailable("cannot list pairing store") from error

    def _warn(self, message):
        self._warn_callback(message)

    def _warn_lock_wait_timeout(self):
        window = int(self._clock() // LOCK_TIMEOUT_WARN_WINDOW_SECONDS)
        with self._lock_timeout_warning_guard:
            if self._lock_timeout_warning_window == window:
                return
            self._lock_timeout_warning_window = window
        self._warn("lock_wait_timeout")

    @staticmethod
    def _unlink(path):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
