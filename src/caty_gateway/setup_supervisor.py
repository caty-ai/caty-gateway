"""One-shot backend restart supervisor for ``setup_orchestrator``.

The supervisor is deliberately transient.  It survives a backend service
restart, resumes setup once, records the outcome, and exits.  It never enables
itself or writes a persistent service definition.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import pathlib
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Iterable, Mapping, Optional, Sequence

from caty_gateway.setup_redaction import redact

TERMINAL_STATES = {"succeeded", "rolled-back", "double-failure", "interrupted", "failed"}
SCHEMA_VERSION = 2
RESUME_SCHEMA_VERSION = SCHEMA_VERSION
BACKUP_GENERATIONS_TO_KEEP = 3
BACKUP_GENERATION_RE = re.compile(r"^\d{8}T\d{6}Z-\d+-\d+$")


def _redacted(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {redact(str(key)): _redacted(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redacted(item) for item in value]
    return value


def atomic_write_json(
    path: pathlib.Path, payload: Mapping[str, object], *, redact_values: bool = True
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    temporary = pathlib.Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            content = _redacted(dict(payload)) if redact_values else dict(payload)
            json.dump(content, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def update_status(
    path: pathlib.Path,
    *,
    clear_fields=(),
    expected_fields: Optional[Mapping[str, object]] = None,
    **changes,
) -> Dict[str, object]:
    """Merge one status update without dropping another process's fields.

    The status file itself is atomically replaced, so locking that inode would
    not serialize later writers.  A stable sibling lock extends the existing
    fcntl + atomic-replace discipline: every writer rereads the latest payload
    while holding the lock, then changes only its declared fields.  This is
    what keeps supervisor ownership and orchestrator ownership from losing one
    another during concurrent status updates.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    lock_path = path.with_name(path.name + ".lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(lock_path), flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("status lock is not a regular file")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    payload = {}
            except (OSError, ValueError):
                payload = {}
            if expected_fields is not None and any(
                payload.get(field) != expected for field, expected in expected_fields.items()
            ):
                return payload
            for field in clear_fields:
                payload.pop(field, None)
            payload.update(changes)
            payload["updated_at"] = time.time()
            atomic_write_json(path, payload)
            return payload
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_file(source: pathlib.Path, destination: pathlib.Path, mode: int = 0o600) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with source.open("rb") as input_handle, os.fdopen(descriptor, "wb") as output_handle:
            shutil.copyfileobj(input_handle, output_handle)
            output_handle.flush()
            os.fsync(output_handle.fileno())
        os.chmod(destination, mode)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _backup_generation_created_at(path: pathlib.Path) -> Optional[float]:
    """Return a managed generation timestamp, ignoring links and foreign entries."""
    # Unrecognized or manifest-less entries are retained forever to prioritize avoiding accidental deletion.
    if not BACKUP_GENERATION_RE.fullmatch(path.name) or path.is_symlink() or not path.is_dir():
        return None
    manifest = path / "manifest.json"
    if manifest.is_symlink() or not manifest.is_file():
        return None
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        created_at = float(payload["created_at"])
    except (KeyError, OSError, TypeError, ValueError):
        return None
    return created_at if math.isfinite(created_at) else None


def _prune_backup_generations(
    backup_root: pathlib.Path, latest_generation: pathlib.Path
) -> None:
    """Keep the newly-created rollback point plus the newest older generations."""
    managed = []
    for entry in backup_root.iterdir():
        created_at = _backup_generation_created_at(entry)
        if created_at is not None:
            managed.append((created_at, entry.name, entry))
    if len(managed) <= BACKUP_GENERATIONS_TO_KEEP:
        return
    keep = {latest_generation}
    older = sorted(
        (item for item in managed if item[2] != latest_generation),
        key=lambda item: (item[0], item[1]),
        reverse=True,
    )
    keep.update(item[2] for item in older[: BACKUP_GENERATIONS_TO_KEEP - 1])
    for _created_at, _name, generation in managed:
        if generation not in keep:
            shutil.rmtree(generation)


def _create_backup_generation(
    paths: Iterable[pathlib.Path], state_dir: pathlib.Path, member: str
) -> pathlib.Path:
    """Create a private, byte-exact backup and return its manifest path."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-%d-%d" % (os.getpid(), time.time_ns())
    backup_dir = state_dir / (member + ".brain-backup") / stamp
    backup_root = state_dir / (member + ".brain-backup")
    for directory in (state_dir, backup_root, backup_dir):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
    roots = []
    for index, raw_path in enumerate(paths):
        path = pathlib.Path(os.path.abspath(os.path.expanduser(str(raw_path))))
        lexists = os.path.lexists(path)
        entry: Dict[str, object] = {"path": str(path), "exists": lexists}
        if not lexists:
            roots.append(entry)
            continue
        if path.is_symlink():
            raise OSError("refusing to back up symlinked brain config root: %s" % path)
        root_backup = backup_dir / ("root-%d" % index)
        if path.is_file():
            mode = stat.S_IMODE(path.stat().st_mode)
            _copy_file(path, root_backup, 0o600)
            entry.update(
                {"kind": "file", "mode": mode, "backup": root_backup.name, "sha256": _sha256(path)}
            )
        elif path.is_dir():
            root_backup.mkdir(mode=0o700)
            items = []
            for current, directories, files in os.walk(path, followlinks=False):
                current_path = pathlib.Path(current)
                relative_dir = current_path.relative_to(path)
                for directory in directories:
                    if (current_path / directory).is_symlink():
                        raise OSError("unsupported symlinked brain config entry: %s" % (current_path / directory))
                for directory in directories:
                    source_dir = current_path / directory
                    relative = relative_dir / directory
                    target_dir = root_backup / relative
                    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                    mode = stat.S_IMODE(source_dir.stat().st_mode)
                    os.chmod(target_dir, 0o700)
                    items.append({"path": str(relative), "kind": "dir", "mode": mode})
                for filename in files:
                    source = current_path / filename
                    if source.is_symlink() or not source.is_file():
                        raise OSError("unsupported brain config entry: %s" % source)
                    relative = relative_dir / filename
                    mode = stat.S_IMODE(source.stat().st_mode)
                    _copy_file(source, root_backup / relative, 0o600)
                    items.append(
                        {"path": str(relative), "kind": "file", "mode": mode, "sha256": _sha256(source)}
                    )
            entry.update(
                {
                    "kind": "dir",
                    "mode": stat.S_IMODE(path.stat().st_mode),
                    "backup": root_backup.name,
                    "items": items,
                }
            )
        else:
            raise OSError("unsupported brain config path: %s" % path)
        roots.append(entry)
    manifest = backup_dir / "manifest.json"
    atomic_write_json(
        manifest,
        {"version": 1, "created_at": time.time(), "roots": roots},
        redact_values=False,
    )
    return manifest


def create_backup(
    paths: Iterable[pathlib.Path], state_dir: pathlib.Path, member: str
) -> pathlib.Path:
    """Create one rollback generation and retain at most the latest three."""
    backup_root = state_dir / (member + ".brain-backup")
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if backup_root.is_symlink():
        raise OSError("backup generation root must not be a symlink")
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state_dir, 0o700)
    os.chmod(backup_root, 0o700)
    lock_path = backup_root / ".gc.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(lock_path), flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("backup generation lock is not a regular file")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        manifest = _create_backup_generation(paths, state_dir, member)
        _prune_backup_generations(backup_root, manifest.parent)
        return manifest
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def validate_backup(manifest_path: pathlib.Path) -> Dict[str, object]:
    """Validate manifest structure and every backup hash without mutation."""
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("version") != 1 or not isinstance(payload.get("roots"), list):
        raise ValueError("unsupported backup manifest")
    backup_dir = manifest_path.parent
    backup_root = backup_dir.resolve()
    for entry in payload["roots"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ValueError("invalid backup root entry")
        if not isinstance(entry.get("exists"), bool):
            raise ValueError("invalid backup existence marker")
        if not entry["exists"]:
            continue
        if entry.get("kind") not in {"file", "dir"} or not isinstance(entry.get("mode"), int):
            raise ValueError("invalid backup root metadata")
        relative_backup = pathlib.Path(str(entry.get("backup", "")))
        source_candidate = backup_dir / relative_backup
        if source_candidate.is_symlink():
            raise ValueError("backup source is a symlink")
        source = source_candidate.resolve()
        if relative_backup.is_absolute() or backup_root not in source.parents:
            raise ValueError("backup path escapes backup directory")
        if entry["kind"] == "file":
            if not source.is_file() or _sha256(source) != entry.get("sha256"):
                raise ValueError("backup file hash mismatch")
            continue
        if not source.is_dir() or not isinstance(entry.get("items"), list):
            raise ValueError("invalid backup directory")
        for item in entry["items"]:
            if not isinstance(item, dict) or item.get("kind") not in {"file", "dir"}:
                raise ValueError("invalid backup directory item")
            relative = pathlib.Path(str(item.get("path", "")))
            if relative.is_absolute() or ".." in relative.parts or not isinstance(item.get("mode"), int):
                raise ValueError("invalid backup item path or mode")
            item_source = source / relative
            if item_source.is_symlink():
                raise ValueError("backup item is a symlink")
            if item["kind"] == "dir":
                if not item_source.is_dir():
                    raise ValueError("backup directory item is missing")
            elif not item_source.is_file() or _sha256(item_source) != item.get("sha256"):
                raise ValueError("backup item hash mismatch")
    return payload


def manifest_has_content(payload: Mapping[str, object]) -> bool:
    """Return whether a validated manifest contains any pre-enable config."""
    roots = payload.get("roots", [])
    return isinstance(roots, list) and any(
        isinstance(entry, dict) and entry.get("exists") is True for entry in roots
    )


def manifest_declared(payload: Mapping[str, object]) -> bool:
    """Return whether a validated manifest declared any roots at all."""
    roots = payload.get("roots", [])
    return isinstance(roots, list) and bool(roots)


def restore_backup(manifest_path: pathlib.Path) -> None:
    """Restore every configured root exactly to its pre-enable state."""
    payload = validate_backup(manifest_path)
    backup_dir = manifest_path.parent
    for entry in payload["roots"]:
        target = pathlib.Path(entry["path"])
        if not entry.get("exists"):
            if target.is_symlink() or (os.path.lexists(target) and not target.is_dir()):
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
            continue
        if target.is_symlink():
            raise OSError("refusing to restore over symlink: %s" % target)
        source = backup_dir / entry["backup"]
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        if entry["kind"] == "file":
            _copy_file(source, target, int(entry["mode"]))
            if _sha256(target) != entry["sha256"]:
                raise OSError("restored file hash mismatch: %s" % target)
            continue
        target.mkdir(parents=True, mode=0o700)
        for item in sorted(entry.get("items", []), key=lambda value: (value["kind"] != "dir", value["path"])):
            destination = target / item["path"]
            if item["kind"] == "dir":
                destination.mkdir(parents=True, exist_ok=True, mode=int(item["mode"]))
                os.chmod(destination, int(item["mode"]))
            else:
                _copy_file(source / item["path"], destination, int(item["mode"]))
                if _sha256(destination) != item["sha256"]:
                    raise OSError("restored file hash mismatch: %s" % destination)
        os.chmod(target, int(entry["mode"]))


def process_start_time(pid: int, system: Optional[str] = None) -> Optional[str]:
    system = system or ("Darwin" if sys.platform == "darwin" else "Linux")
    if system == "Linux":
        try:
            content = pathlib.Path("/proc/%d/stat" % pid).read_text(encoding="utf-8")
            return content[content.rfind(")") + 2 :].split()[19]
        except (OSError, IndexError):
            return None
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="], text=True, capture_output=True, timeout=5, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None


class Supervisor:
    def __init__(self, argv: Optional[Sequence[str]] = None, env: Optional[Mapping[str, str]] = None) -> None:
        self.args = self._parser().parse_args(list(argv) if argv is not None else None)
        self.env = dict(os.environ)
        if env is not None:
            self.env.update(env)
        self.status_path = pathlib.Path(self.args.status_file).resolve()
        self.manifest_path = pathlib.Path(self.args.backup_manifest).resolve()
        self.system = self.args.platform
        self.interrupted = False
        self.resume_process: Optional[subprocess.Popen] = None

    @staticmethod
    def _parser() -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(description="One-shot Caty setup backend restart supervisor")
        parser.add_argument("--member", required=True)
        parser.add_argument("--target", required=True)
        parser.add_argument("--platform", choices=("Linux", "Darwin"), required=True)
        parser.add_argument("--probe-url", required=True)
        parser.add_argument("--backup-manifest", required=True)
        parser.add_argument("--status-file", required=True)
        parser.add_argument("--orchestrator", default="caty_gateway.setup_orchestrator")
        parser.add_argument("--python", required=True)
        parser.add_argument("--parent-pid", type=int)
        parser.add_argument("--parent-start-time")
        parser.add_argument("--timeout", type=float, default=120.0)
        parser.add_argument("--resume-timeout", type=float, default=4000.0)
        parser.add_argument("--grace-seconds", type=float, default=1.0)
        return parser

    def _status(self, state: str, **changes) -> None:
        clear_fields = []
        if state == "validating":
            clear_fields.extend(("qr_url", "expires_at", "recovery_pointer", "resume_output", "qr_error_tail"))
        if state in TERMINAL_STATES:
            # Terminal states have no live flight owner.  A SIGKILL cannot run
            # this cleanup, but PID + start-time liveness checks reject the
            # stale pair left behind in that best-effort case.
            clear_fields.extend(
                (
                    "qr_url",
                    "expires_at",
                    "supervisor_pid",
                    "supervisor_start_time",
                    "orchestrator_pid",
                    "orchestrator_start_time",
                    "orchestrator_registration_pending",
                )
            )
            if "recovery_pointer" not in changes:
                clear_fields.append("recovery_pointer")
        changes.update(
            {
                "state": state,
                "terminal": state in TERMINAL_STATES,
                "active": state not in TERMINAL_STATES,
            }
        )
        if state not in TERMINAL_STATES:
            changes.update(
                {
                    "supervisor_pid": os.getpid(),
                    "supervisor_start_time": process_start_time(os.getpid(), self.system),
                }
            )
        if state == "resuming":
            # The child has been authorized but has not yet published its PID
            # pair.  Readers apply the existing bounded handoff grace only
            # while this marker is present; old status files keep old behavior.
            changes["orchestrator_registration_pending"] = True
        update_status(self.status_path, clear_fields=clear_fields, **changes)

    def _own_unit(self) -> Optional[str]:
        if self.system != "Linux":
            return None
        try:
            content = pathlib.Path("/proc/self/cgroup").read_text(encoding="utf-8")
        except OSError:
            return None
        units = re.findall(r"(?:^|/)([^/]+\.service)(?:/|$)", content, re.MULTILINE)
        return units[-1] if units else None

    def _assert_detached(self) -> None:
        if self.system != "Linux":
            return
        own_unit = self._own_unit()
        if not own_unit:
            raise RuntimeError("cannot verify supervisor cgroup detachment")
        if own_unit == self.args.target:
            raise RuntimeError("supervisor is still inside target service cgroup")

    def _validate_probe_url(self) -> None:
        parsed = urllib.parse.urlsplit(self.args.probe_url)
        try:
            parsed.port
        except ValueError as error:
            raise RuntimeError("probe URL has an invalid port") from error
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise RuntimeError("probe URL must be absolute http(s) without userinfo")

    def _inherit_parent_environment(self) -> None:
        """Recover the caller env in memory before its service is restarted.

        A systemd transient service inherits the user manager environment, not
        necessarily the invoking service's CATY variables.  Reading a validated
        same-user parent process avoids both command-line secrets and a temporary
        secret-bearing resume file.
        """
        if self.system != "Linux" or not self.args.parent_pid:
            return
        expected = self.args.parent_start_time or ""
        if process_start_time(self.args.parent_pid, "Linux") != expected:
            raise RuntimeError("orchestrator parent identity changed before environment handoff")
        path = pathlib.Path("/proc/%d/environ" % self.args.parent_pid)
        if path.stat().st_uid != os.getuid():
            raise RuntimeError("orchestrator parent is not owned by the current user")
        recovered = {}
        for item in path.read_bytes().split(b"\0"):
            if b"=" not in item:
                continue
            key, value = item.split(b"=", 1)
            recovered[key.decode("utf-8", errors="surrogateescape")] = value.decode(
                "utf-8", errors="surrogateescape"
            )
        if not recovered:
            raise RuntimeError("orchestrator parent environment could not be recovered")
        self.env.update(recovered)

    def _restart(self) -> subprocess.CompletedProcess:
        if self.system == "Linux":
            command = ["systemctl", "--user", "restart", self.args.target]
        else:
            target = self.args.target
            if not target.startswith("gui/"):
                target = "gui/%d/%s" % (os.getuid(), target)
            command = ["launchctl", "kickstart", "-k", target]
        return subprocess.run(
            command, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, check=False,
        )

    def _probe(self) -> bool:
        try:
            with urllib.request.urlopen(self.args.probe_url, timeout=2):
                return True
        except urllib.error.HTTPError:
            return True
        except (OSError, urllib.error.URLError, ValueError):
            return False

    def _wait_healthy(self) -> bool:
        deadline = time.monotonic() + self.args.timeout
        while time.monotonic() < deadline:
            if self._probe():
                return True
            time.sleep(0.5)
        return False

    def _resume(self) -> subprocess.CompletedProcess:
        child_env = dict(self.env)
        child_env["CATY_SETUP_SUPERVISED"] = "1"
        command = [self.args.python, "-m", self.args.orchestrator, "--member", self.args.member, "--yes"]
        limit = 16384
        tails = {"stdout": bytearray(), "stderr": bytearray()}
        truncated = {"stdout": False, "stderr": False}
        tail_lock = threading.Lock()

        def drain(name: str, stream) -> None:
            try:
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        return
                    with tail_lock:
                        tails[name].extend(chunk)
                        overflow = len(tails[name]) - limit
                        if overflow > 0:
                            truncated[name] = True
                            del tails[name][:overflow]
            finally:
                stream.close()

        previous_signal_mask = None
        if threading.current_thread() is threading.main_thread():
            previous_signal_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
        try:
            process = subprocess.Popen(
                command,
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            self.resume_process = process
        finally:
            if previous_signal_mask is not None:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_signal_mask)
        assert process.stdout is not None and process.stderr is not None
        threads = [
            threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
            threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
        ]
        for thread in threads:
            thread.start()
        try:
            try:
                returncode = process.wait(timeout=self.args.resume_timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (AttributeError, OSError):
                    process.kill()
                process.wait(timeout=10)
                raise
        finally:
            for thread in threads:
                thread.join(timeout=10)
            self.resume_process = None
        with tail_lock:
            captured = {}
            for name in ("stdout", "stderr"):
                raw_tail = bytes(tails[name])[-limit:]
                if truncated[name]:
                    newline = raw_tail.find(b"\n")
                    if newline >= 0:
                        raw_tail = raw_tail[newline + 1 :]
                captured[name] = raw_tail.decode("utf-8", errors="replace")
            stdout_tail = captured["stdout"]
            stderr_tail = captured["stderr"]
        return subprocess.CompletedProcess(command, returncode, stdout_tail, stderr_tail)

    def _resume_state_cleared_restart(self) -> bool:
        """Allow keeping enablement only for an explicit, readable clear state."""
        resume_state_path = self.status_path.with_name(self.args.member + ".json")
        try:
            payload = json.loads(resume_state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return bool(
            isinstance(payload, dict)
            and payload.get("schema_version") == SCHEMA_VERSION
            and payload.get("member") == self.args.member
            and payload.get("backend_enable_done") is False
            and payload.get("backup_manifest_path") == ""
        )

    def _recovery_text(self, reason: str) -> pathlib.Path:
        recovery_path = self.status_path.with_name(self.args.member + ".recovery.txt")
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            commands = []
            for entry in manifest.get("roots", []):
                target = entry.get("path", "<config-path>")
                commands.append("# Recovery commands for %s (run in order):" % target)
                if not entry.get("exists"):
                    commands.append("rm -rf -- %s" % shlex.quote(str(target)))
                elif entry.get("kind") == "file":
                    source = self.manifest_path.parent / entry.get("backup", "")
                    commands.extend(
                        (
                            "mkdir -p -- %s" % shlex.quote(str(pathlib.Path(target).parent)),
                            "install -m %04o -- %s %s"
                            % (
                                int(entry.get("mode", 0o600)) & 0o7777,
                                shlex.quote(str(source)),
                                shlex.quote(str(target)),
                            ),
                        )
                    )
                else:
                    source = self.manifest_path.parent / entry.get("backup", "")
                    commands.append("rm -rf -- %s" % shlex.quote(str(target)))
                    commands.append("install -d -m 0700 -- %s" % shlex.quote(str(target)))
                    items = list(entry.get("items", []))
                    directories = sorted(
                        (item for item in items if item.get("kind") == "dir"),
                        key=lambda value: (len(pathlib.Path(str(value.get("path", ""))).parts), value.get("path", "")),
                    )
                    files = sorted(
                        (item for item in items if item.get("kind") == "file"),
                        key=lambda value: value.get("path", ""),
                    )
                    for item in directories:
                        relative = pathlib.Path(str(item.get("path", "")))
                        destination = pathlib.Path(target) / relative
                        commands.append("install -d -m 0700 -- %s" % shlex.quote(str(destination)))
                    for item in files:
                        relative = pathlib.Path(str(item.get("path", "")))
                        destination = pathlib.Path(target) / relative
                        commands.append(
                            "install -m %04o -- %s %s"
                            % (
                                int(item.get("mode", 0o600)) & 0o7777,
                                shlex.quote(str(source / relative)),
                                shlex.quote(str(destination)),
                            )
                        )
                    for item in sorted(
                        directories,
                        key=lambda value: (-len(pathlib.Path(str(value.get("path", ""))).parts), value.get("path", "")),
                    ):
                        destination = pathlib.Path(target) / pathlib.Path(str(item.get("path", "")))
                        commands.append(
                            "chmod %04o %s"
                            % (int(item.get("mode", 0o700)) & 0o7777, shlex.quote(str(destination)))
                        )
                    commands.append(
                        "chmod %04o %s"
                        % (int(entry.get("mode", 0o700)) & 0o7777, shlex.quote(str(target)))
                    )
        except Exception:
            commands = ["Inspect and restore files listed in %s" % self.manifest_path]
        lines = [
            "Caty setup recovery is required.",
            "Reason: " + reason,
            "Backup directory: " + str(self.manifest_path.parent),
            *commands,
            "Restart target: " + self.args.target,
        ]
        if self.system == "Linux":
            lines.append("Inspect logs: journalctl --user -u %s --no-pager" % self.args.target)
        else:
            lines.append("Inspect launchd state: launchctl print gui/%d/%s" % (os.getuid(), self.args.target))
        content = redact("\n".join(lines) + "\n")
        recovery_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(str(recovery_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(recovery_path, 0o600)
        return recovery_path

    def _rollback(self, reason: str) -> int:
        has_content: Optional[bool] = None
        declared: Optional[bool] = None
        try:
            manifest = validate_backup(self.manifest_path)
            has_content = manifest_has_content(manifest)
            declared = manifest_declared(manifest)
            if has_content:
                self._status("rolling-back", message=reason)
            elif declared:
                self._status(
                    "rolling-back",
                    message=(
                        reason
                        + "; declared brain configuration paths had no pre-enable content, so any "
                        "enable-created files will be removed"
                    ),
                )
            else:
                self._status(
                    "recovering",
                    message=(
                        reason
                        + "; no brain configuration was declared for backup, so nothing will be restored"
                    ),
                )
            restore_backup(self.manifest_path)
            restarted = self._restart()
            if restarted.returncode:
                raise RuntimeError("backend restart after restore failed: " + redact(restarted.stderr))
            if not self._wait_healthy():
                raise RuntimeError("backend did not recover after configuration restore")
        except Exception as error:
            recovery = self._recovery_text(redact(str(error)))
            message = "automatic rollback failed; follow the recovery document"
            if declared is False:
                message = (
                    "backend recovery failed; no brain configuration was declared for backup, "
                    "so nothing was restored; follow the recovery document"
                )
            self._status(
                "double-failure",
                message=message,
                recovery_pointer=str(recovery),
            )
            return 2
        if has_content:
            self._status(
                "rolled-back",
                message=(
                    "setup resume failed; brain configuration was restored, the backend was restarted "
                    "back onto the old configuration and recovered; rerun the same setup command to "
                    "restart the flow from the backend phase"
                ),
                recovery_pointer="Rerun the same setup command after correcting the reported failure.",
            )
        elif declared:
            self._status(
                "rolled-back",
                message=(
                    "setup resume failed; declared brain configuration paths had no pre-enable content, "
                    "so enable-created files were removed; the backend was restarted and recovered; rerun "
                    "the same setup command to restart the flow from the backend phase"
                ),
                recovery_pointer="Rerun the same setup command after correcting the reported failure.",
            )
        else:
            self._status(
                "failed",
                message=(
                    "setup resume failed; no brain configuration was declared for backup, so nothing "
                    "was restored; the backend was restarted and recovered; rerun the same setup command "
                    "to restart the flow from the backend phase"
                ),
            )
        return 1

    def _sigterm(self, _signum, _frame) -> None:
        self.interrupted = True
        child = self.resume_process
        if child is not None and child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except (AttributeError, OSError):
                child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except (AttributeError, OSError):
                    child.kill()
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        self._status("interrupted", message="supervisor received SIGTERM before setup completed")
        raise SystemExit(143)

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self._sigterm)
        self._status("validating", message="restart supervisor started")
        try:
            self._validate_probe_url()
            self._assert_detached()
            self._inherit_parent_environment()
        except Exception as error:
            try:
                manifest = validate_backup(self.manifest_path)
                has_content = manifest_has_content(manifest)
                declared = manifest_declared(manifest)
                restore_backup(self.manifest_path)
            except Exception as restore_error:
                recovery = self._recovery_text(redact(str(restore_error)))
                self._status(
                    "double-failure",
                    message="detachment verification and backup recovery both failed",
                    recovery_pointer=str(recovery),
                )
                return 2
            if has_content:
                message = "restart aborted before restart; enable changes were rolled back: " + redact(str(error))
                state = "rolled-back"
                recovery_pointer = "Rerun the same setup command after correcting the reported failure."
            elif declared:
                message = (
                    "restart aborted before restart; declared brain configuration paths had no pre-enable content, "
                    "so enable-created files were removed: " + redact(str(error))
                )
                state = "rolled-back"
                recovery_pointer = "Rerun the same setup command after correcting the reported failure."
            else:
                message = (
                    "restart aborted before restart; no brain configuration was declared for backup, "
                    "so nothing was restored: "
                    + redact(str(error))
                )
                state = "failed"
            if state == "rolled-back":
                self._status(
                    state,
                    message=message,
                    recovery_pointer=recovery_pointer,
                )
            else:
                self._status(
                    state,
                    message=message,
                )
            return 1
        try:
            validate_backup(self.manifest_path)
        except Exception as error:
            recovery = self._recovery_text("backup validation failed: " + redact(str(error)))
            self._status(
                "double-failure",
                message="backup validation failed before restart; follow the recovery document",
                recovery_pointer=str(recovery),
            )
            return 2
        self._status("ready", message="supervisor detached and environment handoff completed")
        if self.args.grace_seconds > 0:
            time.sleep(min(self.args.grace_seconds, 10.0))
        try:
            restarted = self._restart()
        except Exception as error:
            return self._rollback("backend restart command failed: " + redact(str(error)))
        if restarted.returncode:
            return self._rollback("backend restart command failed: " + redact(restarted.stderr))
        self._status("waiting-backend", message="backend restart requested")
        if not self._wait_healthy():
            return self._rollback("backend did not recover before the bounded timeout")
        self._status("resuming", message="backend recovered; resuming setup")
        try:
            resumed = self._resume()
        except Exception as error:
            return self._rollback("setup resume could not run: " + redact(str(error)))
        captured = redact((resumed.stdout or "") + (resumed.stderr or ""))
        if resumed.returncode:
            update_status(self.status_path, resume_output=captured)
            if self._resume_state_cleared_restart():
                self._status(
                    "failed",
                    message=(
                        "setup resume failed after the backend was verified healthy; the backend is healthy, "
                        "the enable change is kept, and rerunning the same setup command resumes from the failed phase"
                    ),
                    resume_output=captured,
                )
                return 1
            return self._rollback("setup resume exited non-zero")
        self._status("succeeded", message="setup completed", resume_output=captured)
        return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    supervisor: Optional[Supervisor] = None
    try:
        supervisor = Supervisor(argv)
        return supervisor.run()
    except SystemExit:
        raise
    except Exception as error:
        if supervisor is not None:
            try:
                supervisor._status(
                    "failed",
                    message="unexpected supervisor failure: " + redact(str(error)),
                )
            except OSError:
                pass
        sys.stderr.write(redact("setup supervisor failed unexpectedly: %s\n" % error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
