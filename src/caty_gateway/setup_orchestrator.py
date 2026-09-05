"""One-command, resumable setup for a Caty gateway member.

This module intentionally does not import ``caty_gateway``: that module builds
the selected backend at import time and may contain deployment secrets.
"""

from __future__ import annotations

import argparse
import datetime
import getpass
import hashlib
import json
import math
import os
import pathlib
import platform
import plistlib
import queue
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from importlib import resources
from xml.sax.saxutils import escape
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from caty_gateway.doctor import Doctor, normalize_backend
from caty_gateway.setup_redaction import redact
from caty_gateway.setup_supervisor import (
    SCHEMA_VERSION,
    TERMINAL_STATES,
    create_backup,
    manifest_declared,
    manifest_has_content,
    process_start_time,
    restore_backup,
    update_status,
    validate_backup,
)
DEFAULT_RESUME_TTL = 86400
MAX_RESUME_TTL = 86400
VOICE_STATE_TTL_SECONDS = 15 * 60
VOICE_STATE_MAX_STALENESS_SECONDS = VOICE_STATE_TTL_SECONDS * 4
VOICE_STATE_MAX_FUTURE_SKEW_SECONDS = 60
PHASES = (
    "preflight",
    "plan",
    "backend",
    "install",
    "start",
    "linger",
    "health",
    "identity",
    "voice",
    "qr",
)
MEMBER_RE = re.compile(r"^[A-Za-z0-9._-]+$")
OWNER_FIELDS = (
    "supervisor_pid",
    "supervisor_start_time",
    "orchestrator_pid",
    "orchestrator_start_time",
)


class SetupError(RuntimeError):
    """An actionable setup failure safe to show after redaction."""


class UnsupportedBackendError(SetupError):
    """An unsupported release option is a CLI usage error."""


@dataclass
class ResumeState:
    schema_version: int
    member: str
    created_at: float
    updated_at: float
    completed_phases: List[str] = field(default_factory=list)
    resolved_config: Dict[str, object] = field(default_factory=dict)
    config_fingerprint: str = ""
    env_file_created_by_us: bool = False
    env_file_sha256: str = ""
    # Salted hash of the CATY_TOKEN passed to the installer. Lets a rerun prove
    # an artifact left by a kill between installer-write and state-write is ours
    # without ever persisting the secret itself.
    token_digest: str = ""
    restart_target: str = ""
    backup_manifest_path: str = ""
    backend_enable_done: bool = False


class SetupOrchestrator:
    def __init__(
        self,
        argv: Optional[Sequence[str]] = None,
        *,
        env: Optional[Mapping[str, str]] = None,
        workdir: Optional[pathlib.Path] = None,
    ) -> None:
        self.args = self._parser().parse_args(list(argv) if argv is not None else None)
        self.env = dict(os.environ)
        if env is not None:
            self.env.update(env)
        self.home = pathlib.Path(self.env.get("HOME", str(pathlib.Path.home()))).expanduser()
        self.system = platform.system()
        self.member = self.args.member
        try:
            self.backend = normalize_backend(self.args.backend or self.env.get("CATY_BACKEND", "openclaw"))
        except ValueError as exc:
            raise UnsupportedBackendError(str(exc)) from exc
        self.port = self._resolve_port()
        self.name = self.args.name or self.env.get("CATY_NAME") or self.member
        self.accent = self.args.accent or self.env.get("CATY_ACCENT_COLOR") or "#7FB1FF"
        self.public_url = self.args.public_url or self.env.get("CATY_PUBLIC_URL", "")
        self.qr_delivery = self.args.qr_delivery or self.env.get("CATY_QR_DELIVERY", "auto").strip().lower()
        self.resume_ttl = self._parse_resume_ttl(self.env.get("CATY_SETUP_RESUME_TTL_SECONDS"))
        self.qr_timeout = self._qr_timeout()
        self.state_path = self._state_path()
        self.status_path = self.state_path.with_name(self.member + ".status.json")
        self.orchestrator_pid = os.getpid()
        self.orchestrator_start_time = process_start_time(self.orchestrator_pid, self.system)
        self._claim_status: Dict[str, object] = {}
        self.probe_python = self.env.get("PYTHON") or sys.executable
        self.service_python = self.probe_python
        self.state: Optional[ResumeState] = None
        self.state_expired = False
        self.artifact_path, self.service_name = self._platform_paths()
        self.config: Dict[str, object] = {}
        if not MEMBER_RE.fullmatch(self.member) or self.member in {".", ".."}:
            raise SetupError("--member must contain only letters, numbers, dot, underscore, or hyphen")
        if not 1 <= self.port <= 65535:
            raise SetupError("--port must be between 1 and 65535")
        if self.args.health_timeout <= 0:
            raise SetupError("--health-timeout must be greater than zero")
        if self.qr_delivery not in {"auto", "tty", "url"}:
            raise SetupError("CATY_QR_DELIVERY/--qr-delivery must be one of: auto, tty, url")
        if self.args.wait and not self.args.status:
            raise SetupError("--wait requires --status")

    @staticmethod
    def _parser() -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(description="Install, start, verify, and pair one Caty gateway member")
        parser.add_argument("--member", required=True)
        parser.add_argument("--backend")
        parser.add_argument("--port", type=int)
        parser.add_argument("--name")
        parser.add_argument("--accent")
        parser.add_argument("--public-url")
        parser.add_argument("--qr-delivery", choices=("auto", "tty", "url"))
        parser.add_argument("--yes", action="store_true")
        parser.add_argument("--plan-only", action="store_true")
        parser.add_argument("--health-timeout", type=int, default=30)
        parser.add_argument("--reset", action="store_true", help="Discard resume metadata only")
        parser.add_argument("--status", action="store_true", help="Show setup/supervisor progress")
        parser.add_argument("--no-history", action="store_true")
        parser.add_argument("--wait", action="store_true", help="Wait for a QR URL or terminal setup state")
        return parser

    def _resolve_port(self) -> int:
        raw = self.args.port if self.args.port is not None else self.env.get("CATY_GATEWAY_PORT", "8788")
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise SetupError("CATY_GATEWAY_PORT/--port must be an integer") from exc

    def _parse_resume_ttl(self, raw: Optional[str]) -> int:
        if raw is None or raw == "":
            return DEFAULT_RESUME_TTL
        try:
            value = int(raw)
        except ValueError as exc:
            raise SetupError("CATY_SETUP_RESUME_TTL_SECONDS must be a positive integer") from exc
        if value <= 0:
            raise SetupError("CATY_SETUP_RESUME_TTL_SECONDS must be greater than zero")
        if value > MAX_RESUME_TTL:
            print("WARN: CATY_SETUP_RESUME_TTL_SECONDS exceeds 86400 and was clamped to 86400")
            return MAX_RESUME_TTL
        return value

    def _state_path(self) -> pathlib.Path:
        root = pathlib.Path(self.env.get("XDG_STATE_HOME", str(self.home / ".local" / "state"))).expanduser()
        return root / "caty-gateway" / "setup" / (self.member + ".json")

    def _platform_paths(self) -> Tuple[pathlib.Path, str]:
        if self.system == "Linux":
            return (
                member_artifact_path(self.member, self.home, self.system),
                "caty-gateway-" + self.member + ".service",
            )
        if self.system == "Darwin":
            label = "ai.caty.gateway." + self.member
            return member_artifact_path(self.member, self.home, self.system), label
        return pathlib.Path("/__unsupported__"), ""

    def _run(
        self,
        command: Sequence[str],
        *,
        env: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                list(command),
                env=dict(env or self.env),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise SetupError("command not found: " + command[0]) from exc
        except subprocess.TimeoutExpired as exc:
            raise SetupError("command timed out: " + command[0]) from exc

    def _command_path(self, value: str) -> Optional[str]:
        if os.sep in value:
            path = pathlib.Path(value).expanduser()
            return str(path.resolve()) if path.is_file() and os.access(str(path), os.X_OK) else None
        return shutil.which(value, path=self.env.get("PATH"))

    def _python_version(self, executable: str) -> Optional[Tuple[int, int]]:
        result = self._run([executable, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"])
        if result.returncode:
            return None
        try:
            major, minor = result.stdout.strip().split(".", 1)
            return int(major), int(minor)
        except ValueError:
            return None

    def _has_qrcode(self, executable: str) -> bool:
        try:
            probe_env = dict(self.env)
            probe_env["PYTHONDONTWRITEBYTECODE"] = "1"
            return self._run([executable, "-c", "import qrcode"], env=probe_env).returncode == 0
        except SetupError:
            return False

    def _resolve_public_url(self, tailscale_ip: Optional[str]) -> None:
        if not self.public_url and tailscale_ip:
            self.public_url = "http://%s:%d" % (tailscale_ip, self.port)

    def _resolved_config(self) -> Dict[str, object]:
        return {
            "platform": self.system,
            "member": self.member,
            "backend": self.backend,
            "port": self.port,
            "name": self.name,
            "accent": self.accent,
            "public_url": self.public_url,
            "qr_delivery": self.qr_delivery,
            "probe_python": self.probe_python,
            "service_python": self.service_python,
            "no_history": self.args.no_history,
            "artifact_path": str(self.artifact_path),
            "service_name": self.service_name,
        }

    @staticmethod
    def _fingerprint(config: Mapping[str, object]) -> str:
        data = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(data.encode("utf-8")).hexdigest()

    def _config_fingerprint(self) -> str:
        config = self.config or self._resolved_config()
        return self._fingerprint(config)

    def _read_state(self, validate_fingerprint: bool = True) -> Optional[ResumeState]:
        if not self.state_path.exists():
            return None
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if int(payload["schema_version"]) != SCHEMA_VERSION:
                raise SetupError(
                    "state is from a different setup version — rerun with `--reset` after confirming no setup is mid-flight"
                )
            state = ResumeState(
                schema_version=int(payload["schema_version"]),
                member=str(payload["member"]),
                created_at=float(payload["created_at"]),
                updated_at=float(payload["updated_at"]),
                completed_phases=list(payload.get("completed_phases", [])),
                resolved_config=dict(payload.get("resolved_config", payload.get("config", {}))),
                config_fingerprint=str(payload["config_fingerprint"]),
                env_file_created_by_us=bool(payload.get("env_file_created_by_us", False)),
                env_file_sha256=str(payload.get("env_file_sha256", "")),
                token_digest=str(payload.get("token_digest", "")),
                restart_target=str(payload.get("restart_target", "")),
                backup_manifest_path=str(payload.get("backup_manifest_path", "")),
                backend_enable_done=bool(payload.get("backend_enable_done", False)),
            )
        except SetupError:
            raise
        except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
            raise SetupError("resume state is unreadable; use --reset after inspecting it") from exc
        if state.member != self.member:
            raise SetupError("resume state member mismatch; use --reset")
        if time.time() - state.updated_at > self.resume_ttl:
            self.state_expired = True
            return None
        if validate_fingerprint and state.config_fingerprint != self._config_fingerprint():
            raise SetupError(self._fingerprint_mismatch_message())
        return state

    def _fingerprint_mismatch_message(self) -> str:
        return (
            "non-secret setup flags contradict resume state; finish with the original flags, "
            "or use --reset AND move aside %s (it holds a live CATY_TOKEN)" % self.artifact_path
        )

    def _write_state(self) -> None:
        assert self.state is not None
        payload = {
            "schema_version": self.state.schema_version,
            "member": self.state.member,
            "created_at": self.state.created_at,
            "updated_at": self.state.updated_at,
            "completed_phases": self.state.completed_phases,
            "resolved_config": self.state.resolved_config,
            "config_fingerprint": self.state.config_fingerprint,
            "env_file_created_by_us": self.state.env_file_created_by_us,
            "env_file_sha256": self.state.env_file_sha256,
            "token_digest": self.state.token_digest,
            "restart_target": self.state.restart_target,
            "backup_manifest_path": self.state.backup_manifest_path,
            "backend_enable_done": self.state.backend_enable_done,
        }
        state_member_root = self.state_path.parent.parent
        setup_directory = self.state_path.parent
        for directory in (state_member_root, setup_directory):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(str(directory), 0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=self.state_path.name + ".", suffix=".tmp", dir=str(setup_directory)
        )
        temporary = pathlib.Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(str(temporary), 0o600)
            os.replace(str(temporary), str(self.state_path))
        finally:
            if temporary.exists():
                temporary.unlink()

    def _start_state(self) -> None:
        now = time.time()
        self.state = ResumeState(
            SCHEMA_VERSION,
            self.member,
            now,
            now,
            [],
            dict(self.config),
            self._config_fingerprint(),
        )
        self._write_state()

    def _mark(self, phase: str) -> None:
        assert self.state is not None
        if phase not in self.state.completed_phases:
            self.state.completed_phases.append(phase)
        self.state.updated_at = time.time()
        self._write_state()
        self._update_status(
            state="running",
            active=True,
            terminal=False,
            phase=phase,
            completed_phases=list(self.state.completed_phases),
            timeline_entry="phase %s completed" % phase,
        )

    def _update_status(self, **changes) -> Dict[str, object]:
        timeline_entry = changes.pop("timeline_entry", None)
        clear_fields = changes.pop("clear_fields", ())
        expected_fields = changes.pop("expected_fields", None)
        try:
            existing = json.loads(self.status_path.read_text(encoding="utf-8"))
            timeline = existing.get("timeline", []) if isinstance(existing, dict) else []
        except (OSError, ValueError):
            timeline = []
        if timeline_entry:
            timeline = list(timeline)[-99:]
            timeline.append({"at": time.time(), "event": redact(str(timeline_entry))})
            changes["timeline"] = timeline
        changes.setdefault("member", self.member)
        return update_status(
            self.status_path,
            clear_fields=clear_fields,
            expected_fields=expected_fields,
            **changes,
        )

    @staticmethod
    def _hash_file(path: pathlib.Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _token_digest(token: str) -> str:
        return hashlib.sha256(("caty-setup-token:" + token).encode("utf-8")).hexdigest()

    def _owned_artifact(self) -> bool:
        return bool(
            self.state
            and self.state.env_file_created_by_us
            and self.state.env_file_sha256
            and self.artifact_path.is_file()
            and self._hash_file(self.artifact_path) == self.state.env_file_sha256
        )

    def _artifact_is_ours(self) -> bool:
        if self._owned_artifact():
            return True
        if not (self.state and self.state.token_digest and self.artifact_path.is_file()):
            return False
        artifact_credential = self._installed_env().get("CATY_TOKEN", "")
        return bool(
            artifact_credential
            and self._token_digest(artifact_credential) == self.state.token_digest
        )

    def _adopt_window_artifact(self) -> bool:
        """Adopt an artifact left by a kill between installer-write and state-write.

        Ownership proof: the artifact's CATY_TOKEN hashes to the salted digest we
        persisted before invoking the installer. A foreign artifact cannot match a
        192-bit-entropy token digest, so this never weakens the collision guard.
        """
        if not (self.state and not self.state.env_file_created_by_us and self._artifact_is_ours()):
            return False
        self.state.env_file_created_by_us = True
        self.state.env_file_sha256 = self._hash_file(self.artifact_path)
        self.state.token_digest = ""
        self.state.updated_at = time.time()
        self._write_state()
        return True

    @staticmethod
    def _read_env_file(path: pathlib.Path, *, strict: bool = False) -> Dict[str, str]:
        values: Dict[str, str] = {}
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return values
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                if strict:
                    raise ValueError("invalid environment assignment")
                continue
            key, value = stripped.split("=", 1)
            value = value.strip()
            quoted = value[:1] in ("'", '"')
            closing_quote_escaped = (
                value.startswith('"')
                and len(value) >= 2
                and (len(value[:-1]) - len(value[:-1].rstrip("\\"))) % 2 == 1
            )
            if strict and (
                not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key.strip())
                or (quoted and (len(value) < 2 or value[-1] != value[0] or closing_quote_escaped))
            ):
                raise ValueError("invalid environment assignment")
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                quote = value[0]
                value = value[1:-1]
                if quote == '"':
                    value = re.sub(r'\\([\\"`$])', r'\1', value)
            values[key.strip()] = value
        return values

    @staticmethod
    def _plist_env(path: pathlib.Path, *, strict: bool = False) -> Dict[str, str]:
        try:
            payload = plistlib.loads(path.read_bytes())
            raw = payload.get("EnvironmentVariables", {})
            if strict and isinstance(raw, dict) and any(
                not isinstance(key, str) or not isinstance(value, str) for key, value in raw.items()
            ):
                raise ValueError("invalid plist environment value")
            return {str(key): str(value) for key, value in raw.items()} if isinstance(raw, dict) else {}
        except (OSError, plistlib.InvalidFileException, AttributeError):
            return {}

    def _port_is_listening(self) -> bool:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(0.2)
            return sock.connect_ex(("127.0.0.1", self.port)) == 0
        finally:
            sock.close()

    def _collision_with_other_members(self) -> List[str]:
        failures: List[str] = []
        if self.system == "Linux":
            for path in sorted((self.home / ".config" / "caty-gateway").glob("*.env")):
                if path == self.artifact_path:
                    continue
                if self._read_env_file(path).get("CATY_GATEWAY_PORT") == str(self.port):
                    failures.append("port %d is assigned in %s; choose --port with an unused value" % (self.port, path))
        elif self.system == "Darwin":
            for path in sorted((self.home / "Library" / "LaunchAgents").glob("ai.caty.gateway.*.plist")):
                if path == self.artifact_path:
                    continue
                if self._plist_env(path).get("CATY_GATEWAY_PORT") == str(self.port):
                    failures.append("port %d is assigned in %s; choose --port with an unused value" % (self.port, path))
        if self._port_is_listening() and not self._artifact_is_ours():
            failures.append("port %d is already listening; stop that service or choose another --port" % self.port)
        return failures

    def _expected_systemd_unit(self) -> bytes:
        template = resources.files("caty_gateway").joinpath("templates", "systemd.service").read_text(encoding="utf-8")
        template = template.replace("%i", self.member)
        # Quote each path for systemd's syntax; escape literal specifiers as well.
        def quoted(value):
            return '"' + str(value).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'
        rendered = template.replace("WorkingDirectory=__WORKDIR__", "WorkingDirectory=" + quoted(self.home))
        rendered = rendered.replace("__PYTHON__", quoted(self.service_python))
        rendered = rendered.replace("Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin",
                                    "Environment=" + quoted("PATH=" + str(self.home) + "/.local/bin:/usr/local/bin:/usr/bin:/bin"))
        rendered = rendered.replace("EnvironmentFile=%h/.config/caty-gateway/" + self.member + ".env", "EnvironmentFile=" + quoted(self.artifact_path))
        return (rendered.rstrip("\n") + "\n").encode("utf-8")

    def _systemd_unit_matches_expected(self, unit: pathlib.Path) -> bool:
        try:
            return unit.read_bytes() == self._expected_systemd_unit()
        except OSError:
            return False

    def _member_collision(self) -> Optional[str]:
        if self.artifact_path.exists():
            if self._artifact_is_ours():
                return None
            if self.state and self.state.env_file_created_by_us:
                return "target configuration changed outside this setup job; restore it or use --reset with a new member"
            if self.state is None:
                if self.state_expired:
                    return (
                        "an earlier incomplete setup for this member expired (resume TTL); inspect %s — "
                        "if the service works, run `caty-gateway qr` from the installed env; "
                        "otherwise move the file aside and re-run setup" % self.artifact_path
                    )
                return (
                    "member appears already set up at %s; to show the pairing QR again run "
                    "`caty-gateway qr` from the installed env (see docs), or move the file aside / "
                    "choose another --member to re-install" % self.artifact_path
                )
            return "target configuration already exists at %s; choose another --member or move it aside" % self.artifact_path
        if self.system == "Linux":
            unit = self.home / ".config" / "systemd" / "user" / self.service_name
            if unit.exists():
                in_install_window = bool(self.state and self.state.token_digest)
                if not (in_install_window and self._systemd_unit_matches_expected(unit)):
                    return "member unit already exists at %s; choose another --member or move it aside" % unit
        return None

    def _preflight(self) -> None:
        failures: List[str] = []
        if self.backend == "hermes" and not self.env.get("CATY_HERMES_API_KEY"):
            if self._artifact_is_ours():
                self.env["CATY_HERMES_API_KEY"] = self._installed_env().get("CATY_HERMES_API_KEY", "")
            if not self.env.get("CATY_HERMES_API_KEY") and not self.args.plan_only and sys.stdin.isatty():
                self.env["CATY_HERMES_API_KEY"] = getpass.getpass("CATY_HERMES_API_KEY: ")
        options = {}
        if self._artifact_is_ours():
            options["port_available"] = lambda port: True
        doctor = Doctor(env=self.env, runner=self._run, member=self.member, port=self.port,
                        public_url=self.public_url, backend=self.backend, python=self.probe_python,
                        home=self.home, system=self.system, command_path=self._command_path, **options)
        if not doctor.run():
            failures.extend(check.name + ": " + check.hint for check in doctor.checks if check.status == "FAIL")
        self.public_url = doctor.public_url
        if self.system == "Linux":
            systemctl = self._command_path("systemctl")
            loginctl = self._command_path("loginctl")
            if not systemctl:
                failures.append("systemctl is missing; install systemd user-service support")
            if not loginctl:
                failures.append("loginctl is missing; install systemd login support for enable-linger")
            if not self.env.get("XDG_RUNTIME_DIR"):
                failures.append("XDG_RUNTIME_DIR is unset; log in through a systemd user session and retry")
            elif systemctl:
                manager = self._run([systemctl, "--user", "is-system-running"])
                manager_state = manager.stdout.strip().lower()
                if manager.returncode and manager_state not in {"running", "degraded", "starting"}:
                    failures.append("systemd user manager is unreachable; verify `systemctl --user is-system-running`")

        if not self._has_qrcode(self.service_python):
            failures.append("qrcode[pil] is missing; reinstall caty-gateway with the selected PYTHON interpreter")
        self.config = self._resolved_config()
        failures.extend(self._collision_with_other_members())
        collision = self._member_collision()
        if collision:
            failures.append(collision)
        if failures:
            print("Preflight failed (all detected issues):", file=sys.stderr)
            for failure in failures:
                print("FAIL: " + failure, file=sys.stderr)
            raise SetupError("preflight failed; apply every fix above and rerun")
        print("PASS: setup service checks; continue with the resolved plan")
        if self.system == "Linux":
            print("Elevation summary: at most one sudo command may be required: `sudo loginctl enable-linger <user>`")
        else:
            print("Elevation summary: none required.")

    def _print_plan(self) -> None:
        masked = dict(self.config)
        print("Resolved setup plan (secrets masked)")
        for key in ("member", "backend", "port", "name", "accent", "public_url", "platform", "service_python"):
            print("  %s: %s" % (key, masked[key]))
        print("  health_timeout: %s" % self.args.health_timeout)
        print("  qr_delivery: %s" % self.qr_delivery)
        print("Exact actions")
        if self.args.reset and self.args.plan_only:
            print("  - would delete resume metadata at %s (--reset)" % self.state_path)
        print("  - write/update resume metadata at %s (0600; deleted on success)" % self.state_path)
        if self.system == "Linux":
            unit = self.home / ".config" / "systemd" / "user" / self.service_name
            print("  - write %s and %s from package templates" % (self.artifact_path, unit))
            print("  - systemctl --user daemon-reload; enable --now %s" % self.service_name)
            print("  - attempt loginctl enable-linger as the current user")
        else:
            print("  - write and bootstrap %s from package templates" % self.artifact_path)
            print("  - verify launchctl label %s" % self.service_name)
        print("  - poll http://127.0.0.1:%d/health for %ds" % (self.port, self.args.health_timeout))
        print("  - authenticate /identity and require id=%s" % self.member)
        print("  - authenticate /tts/voice-state and require neutral voice readiness when engine=fish")
        print(
            "  - run %s -m caty_gateway qr --qr-delivery %s with inherited terminal streams"
            % (self.service_python, self.qr_delivery)
        )
        print("  CATY_TOKEN: [REDACTED] (generated only during install)")

    def _backend_probe_url(self) -> Optional[str]:
        if self.backend == "openclaw":
            name = "CATY_GATEWAY_URL"
            value = self.env.get(name, "http://127.0.0.1:18789").rstrip("/")
        elif self.backend == "openai-compat":
            name = "CATY_OPENAI_BASE_URL"
            value = self.env.get(name, "http://127.0.0.1:1234/v1").rstrip("/") + "/models"
        elif self.backend == "hermes":
            name = "CATY_HERMES_URL"
            value = self.env.get(name, "http://127.0.0.1:8642").rstrip("/") + "/v1/models"
        else:
            return None
        parsed = urllib.parse.urlsplit(value)
        try:
            parsed.port
        except ValueError as exc:
            raise SetupError("%s must be an absolute http(s) URL with a valid port" % name) from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise SetupError("%s must be an absolute http(s) URL without userinfo" % name)
        return value

    def _probe_backend(self) -> bool:
        if self.backend == "codex":
            executable = self._command_path("codex")
            return bool(executable and self._run([executable, "--version"], timeout=10).returncode == 0
                        and self._run([executable, "login", "status"], timeout=10).returncode == 0)
        if self.backend == "claude":
            executable = self._command_path(self.env.get("CATY_CLAUDE_BIN", "claude"))
            if not executable:
                return False
            try:
                return self._run([executable, "--version"], timeout=10).returncode == 0
            except SetupError:
                return False
        url = self._backend_probe_url()
        assert url is not None
        token = self.env.get("CATY_HERMES_API_KEY", "") if self.backend == "hermes" else self.env.get("CATY_OPENAI_API_KEY", "")
        request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token} if token else {}, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status == 200
        except urllib.error.HTTPError:
            return self.backend == "openclaw"  # The gateway root can require endpoint-specific authorization.
        except (OSError, ValueError, urllib.error.URLError):
            return False

    def _brain_config_paths(self) -> List[pathlib.Path]:
        defaults: List[pathlib.Path] = []
        if self.backend == "openclaw":
            configured_home = pathlib.Path(self.env.get("OPENCLAW_HOME", str(self.home / ".openclaw"))).expanduser()
            defaults.append(configured_home / "openclaw.json")
        elif self.backend == "hermes":
            configured_home = pathlib.Path(self.env.get("HERMES_HOME", str(self.home / ".hermes"))).expanduser()
            defaults.extend((configured_home / "config.yaml", configured_home / "profile.yaml"))
        candidates = defaults
        candidates.extend(
            pathlib.Path(value).expanduser()
            for value in self.env.get("CATY_BACKEND_CONFIG_PATHS", "").split(os.pathsep)
            if value.strip()
        )
        normalized_candidates: List[pathlib.Path] = []
        for path in candidates:
            normalized = pathlib.Path(os.path.abspath(os.path.expanduser(str(path))))
            if normalized not in normalized_candidates:
                normalized_candidates.append(normalized)
        # Never back up overlapping roots twice: restoring an absent child after
        # its existing parent would delete the restored file.
        return [
            path
            for path in normalized_candidates
            if not any(path != other and path.is_relative_to(other) for other in normalized_candidates)
        ]

    def _create_backend_backup(self) -> pathlib.Path:
        try:
            return create_backup(self._brain_config_paths(), self.state_path.parent, self.member)
        except (OSError, ValueError) as exc:
            raise SetupError("could not create the required brain-config rollback point: " + redact(str(exc))) from exc

    def _run_enable_command(self, command: str) -> None:
        shell = self._command_path("sh")
        if not shell:
            raise SetupError("CATY_BACKEND_ENABLE_CMD requires sh, but sh is unavailable")
        result = self._run([shell, "-c", command], timeout=60)
        output = redact((result.stdout or "") + (result.stderr or ""))
        if output.strip():
            print(output.rstrip())
        if result.returncode:
            raise SetupError("CATY_BACKEND_ENABLE_CMD failed with exit %d" % result.returncode)

    @staticmethod
    def _cgroup_unit(path: pathlib.Path) -> Optional[str]:
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return None
        units = re.findall(r"(?:^|/)([^/]+\.service)(?:/|$)", content, re.MULTILINE)
        return units[-1] if units else None

    def _listener_pid_from_ss(self, port: int) -> Optional[int]:
        executable = self._command_path("ss")
        if not executable:
            return None
        try:
            result = self._run([executable, "-tlnp"], timeout=10)
        except SetupError:
            return None
        if result.returncode:
            return None
        for line in result.stdout.splitlines():
            fields = line.split()
            if not any(
                field.rsplit(":", 1)[-1] == str(port)
                for field in fields
                if ":" in field
            ):
                continue
            match = re.search(r"\bpid=(\d+)\b", line)
            if match:
                return int(match.group(1))
        return None

    @staticmethod
    def _listening_socket_inodes(port: int) -> set[str]:
        inodes: set[str] = set()
        for path in (pathlib.Path("/proc/net/tcp"), pathlib.Path("/proc/net/tcp6")):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()[1:]
            except OSError:
                continue
            for line in lines:
                fields = line.split()
                if len(fields) > 9 and fields[3] == "0A":
                    try:
                        candidate_port = int(fields[1].rsplit(":", 1)[1], 16)
                    except (IndexError, ValueError):
                        continue
                    if candidate_port == port:
                        inodes.add(fields[9])
        return inodes

    def _listener_pid_from_proc(self, port: int) -> Optional[int]:
        inodes = self._listening_socket_inodes(port)
        if not inodes:
            return None
        own_uid = os.getuid()
        for process_dir in pathlib.Path("/proc").glob("[0-9]*"):
            try:
                if process_dir.stat().st_uid != own_uid:
                    continue
                descriptors = process_dir / "fd"
                for descriptor in descriptors.iterdir():
                    try:
                        target = os.readlink(descriptor)
                    except OSError:
                        continue
                    match = re.fullmatch(r"socket:\[(\d+)\]", target)
                    if match and match.group(1) in inodes:
                        return int(process_dir.name)
            except (OSError, ValueError):
                continue
        return None

    def _probe_port(self) -> int:
        url = self._backend_probe_url()
        if not url:
            raise SetupError("backend has no restartable HTTP probe")
        parsed = urllib.parse.urlsplit(url)
        try:
            return parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as exc:
            raise SetupError("backend probe URL has an invalid port") from exc

    def _linux_restart_target(self) -> Tuple[str, bool]:
        port = self._probe_port()
        pid = self._listener_pid_from_ss(port) or self._listener_pid_from_proc(port)
        if not pid:
            raise SetupError(
                "backend restart target cannot be verified: no same-user listener PID was found on port %d; "
                "restart the backend manually, then rerun setup" % port
            )
        target = self._cgroup_unit(pathlib.Path("/proc/%d/cgroup" % pid))
        own = self._cgroup_unit(pathlib.Path("/proc/self/cgroup"))
        if not target:
            raise SetupError(
                "backend restart target cannot be verified from the listener cgroup; restart it manually, then rerun setup"
            )
        # Interactive shells normally live in a session .scope, so a missing
        # self service unit means the orchestrator is outside the target unit.
        return target, bool(own and target == own)

    def _macos_restart_target(self) -> str:
        lsof = self._command_path("lsof")
        if not lsof:
            raise SetupError("backend restart target cannot be verified because lsof is unavailable")
        result = self._run([lsof, "-nP", "-iTCP:%d" % self._probe_port(), "-sTCP:LISTEN", "-t"], timeout=10)
        try:
            pid = int(next(line for line in result.stdout.splitlines() if line.strip()).strip())
        except (StopIteration, ValueError):
            raise SetupError("backend restart target cannot be verified: no listener PID was found")
        listed = self._run(["launchctl", "list"], timeout=10)
        if listed.returncode:
            raise SetupError("launchctl could not identify the backend listener's service label")
        for line in listed.stdout.splitlines():
            columns = line.split(None, 2)
            if len(columns) != 3 or columns[0] == "-":
                continue
            try:
                listed_pid = int(columns[0])
            except ValueError:
                continue
            if listed_pid == pid and columns[2].strip():
                return columns[2].strip()
        raise SetupError(
            "backend launchd label could not be found for the listener PID; restart it manually, then rerun setup"
        )

    def _wait_backend(self, timeout: float = 120.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._probe_backend():
                return True
            time.sleep(0.5)
        return False

    @staticmethod
    def _restored_message(*, has_content: bool, declared: bool) -> str:
        if has_content:
            return "brain configuration was restored"
        if declared:
            return "declared brain configuration paths had no pre-enable content, so enable-created files were removed"
        return "no brain configuration was declared for backup, so nothing was restored"

    def _inline_restart(self, target: str, manifest: pathlib.Path) -> None:
        result = self._run(["systemctl", "--user", "restart", target], timeout=30)
        if not result.returncode and self._wait_backend():
            return
        try:
            restored = self._restore_enable_change(manifest)
            rollback_restart = self._run(["systemctl", "--user", "restart", target], timeout=30)
            recovered = not rollback_restart.returncode and self._wait_backend()
        except Exception as exc:
            raise SetupError(
                "backend restart failed and automatic rollback failed; restore %s manually: %s"
                % (manifest, redact(str(exc)))
            ) from exc
        restored_message = self._restored_message(**restored)
        if not recovered:
            raise SetupError("backend restart failed; %s; backend recovery was not verified" % restored_message)
        raise SetupError("backend restart failed; %s; backend recovered; inspect its logs" % restored_message)

    def _supervisor_command(self, target: str, manifest: pathlib.Path) -> List[str]:
        probe_url = self._backend_probe_url()
        assert probe_url is not None
        supervisor_python = self._command_path(self.probe_python) or self.probe_python
        return [
            supervisor_python,
            "-m",
            "caty_gateway.setup_supervisor",
            "--member",
            self.member,
            "--target",
            target,
            "--platform",
            self.system,
            "--probe-url",
            probe_url,
            "--backup-manifest",
            str(manifest),
            "--status-file",
            str(self.status_path),
            "--orchestrator",
            "caty_gateway.setup_orchestrator",
            "--python",
            supervisor_python,
            "--parent-pid",
            str(os.getpid()),
            "--parent-start-time",
            process_start_time(os.getpid(), "Linux") or "unknown",
            "--timeout",
            self.env.get("CATY_SETUP_BACKEND_RECOVERY_TIMEOUT_SECONDS", "120"),
            "--resume-timeout",
            str(self.qr_timeout + 300),
        ]

    def _wait_supervisor_ready(self, timeout: float = 15.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            payload = self._read_status()
            state = str(payload.get("state", ""))
            if state in {"ready", "waiting-backend", "resuming", "waiting-qr", "succeeded"}:
                return self._supervisor_is_live(payload) or state == "succeeded"
            if state in TERMINAL_STATES:
                return False
            time.sleep(0.1)
        return False

    def _supervised_handoff(self, target: str, manifest: pathlib.Path) -> bool:
        assert self.state is not None
        self.state.restart_target = target
        self.state.backup_manifest_path = str(manifest)
        self.state.backend_enable_done = True
        self.state.updated_at = time.time()
        self._write_state()
        self._update_status(
            state="handoff",
            active=True,
            terminal=False,
            phase="backend",
            restart_target=target,
            backup_manifest_path=str(manifest),
            timeline_entry="supervised backend restart handoff",
        )
        supervisor = self._supervisor_command(target, manifest)
        transient_unit = ""
        spawned_process = None
        try:
            if self.system == "Linux":
                systemd_run = self._command_path("systemd-run")
                if not systemd_run:
                    raise SetupError("systemd-run is unavailable; safe restart detachment is impossible")
                unit = "caty-setup-supervisor-" + self.member
                transient_unit = unit
                result = self._run(
                    [systemd_run, "--user", "--collect", "--unit=" + unit, *supervisor], timeout=30
                )
                if result.returncode:
                    raise SetupError("systemd-run failed: " + redact(result.stderr))
            else:
                stdout_path = self.state_path.parent / (self.member + ".supervisor.stdout.log")
                stderr_path = self.state_path.parent / (self.member + ".supervisor.stderr.log")
                for path in (stdout_path, stderr_path):
                    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    os.close(descriptor)
                with stdout_path.open("ab", buffering=0) as stdout_handle, stderr_path.open("ab", buffering=0) as stderr_handle:
                    spawned_process = subprocess.Popen(
                        supervisor,
                                env=self.env,
                        stdin=subprocess.DEVNULL,
                        stdout=stdout_handle,
                        stderr=stderr_handle,
                        start_new_session=True,
                    )
            if not self._wait_supervisor_ready():
                if self.system == "Linux" and transient_unit:
                    self._run(["systemctl", "--user", "stop", transient_unit], timeout=30)
                elif spawned_process is not None:
                    try:
                        os.killpg(spawned_process.pid, signal.SIGTERM)
                    except OSError:
                        spawned_process.terminate()
                raise SetupError("restart supervisor did not confirm a safe environment handoff")
        except Exception as exc:
            try:
                restored = self._restore_enable_change(manifest)
            except Exception as restore_exc:
                raise SetupError(
                    "supervisor spawn failed and rollback also failed; restore %s manually: %s"
                    % (manifest, redact(str(restore_exc)))
                ) from restore_exc
            raise SetupError(
                "supervisor spawn failed; %s: %s"
                % (self._restored_message(**restored), redact(str(exc)))
            ) from exc
        print(
            redact(
                "Backend %s will restart now. Setup will resume automatically under the one-shot supervisor. "
                "After reconnecting, run `caty-gateway setup --status --member %s` for progress and the QR URL."
                % (self.backend, self.member)
            )
        )
        return False

    def _clear_restart_state(self) -> None:
        if not self.state:
            return
        self.state.restart_target = ""
        self.state.backup_manifest_path = ""
        self.state.backend_enable_done = False
        self.state.updated_at = time.time()
        self._write_state()

    def _restore_enable_change(self, manifest: pathlib.Path) -> Dict[str, bool]:
        payload = validate_backup(manifest)
        result = {
            "has_content": manifest_has_content(payload),
            "declared": manifest_declared(payload),
        }
        restore_backup(manifest)
        self._clear_restart_state()
        return result

    def _backend(self) -> bool:
        assert self.state is not None
        if self.state.backup_manifest_path and not self.state.backend_enable_done:
            interrupted_manifest = pathlib.Path(self.state.backup_manifest_path)
            if not interrupted_manifest.is_file():
                raise SetupError(
                    "an interrupted enable attempt references a missing rollback manifest; recover manually before rerun"
                )
            try:
                restored = self._restore_enable_change(interrupted_manifest)
            except Exception as exc:
                raise SetupError(
                    "interrupted enable recovery failed; restore %s manually: %s"
                    % (interrupted_manifest, redact(str(exc)))
                ) from exc
            raise SetupError(
                "an interrupted backend enable attempt was recovered safely; %s; rerun setup to retry "
                "from the backend phase" % self._restored_message(**restored)
            )
        if self._probe_backend():
            self._clear_restart_state()
            return True
        if self.env.get("CATY_SETUP_SUPERVISED") == "1":
            raise SetupError("backend remained unreachable after supervised restart")
        enable_command = self.env.get("CATY_BACKEND_ENABLE_CMD", "").strip()
        manifest: Optional[pathlib.Path] = None
        if self.state.backend_enable_done and self.state.backup_manifest_path:
            manifest = pathlib.Path(self.state.backup_manifest_path)
            if not manifest.is_file():
                raise SetupError("restart resume state points to a missing backup manifest; stop and recover manually")
        elif not enable_command:
            if self.backend == "openclaw":
                action = "enable the OpenClaw gateway HTTP service at CATY_GATEWAY_URL"
            elif self.backend == "hermes":
                action = "enable the Hermes /v1/responses HTTP service at CATY_HERMES_URL"
            else:
                action = "install/login to Claude Code and ensure CATY_CLAUDE_BIN --version succeeds"
            raise SetupError(
                "backend is unreachable; %s, then rerun setup (completed phases resume idempotently)" % action
            )
        if manifest is None:
            manifest = self._create_backend_backup()
            self.state.backup_manifest_path = str(manifest)
            self.state.updated_at = time.time()
            self._write_state()
            try:
                self._run_enable_command(enable_command)
            except Exception:
                try:
                    self._restore_enable_change(manifest)
                except Exception as exc:
                    raise SetupError("enable failed and rollback failed; restore %s manually: %s" % (manifest, redact(str(exc))))
                raise
            self.state.backend_enable_done = True
            self.state.updated_at = time.time()
            self._write_state()
            if self._probe_backend():
                self._clear_restart_state()
                return True
        if self.backend in {"claude", "codex", "openai-compat"}:
            restored = self._restore_enable_change(manifest)
            raise SetupError(
                "Claude backend remained unavailable after enable command; %s"
                % self._restored_message(**restored)
            )
        try:
            if self.system == "Linux":
                target, same_unit = self._linux_restart_target()
                if same_unit:
                    return self._supervised_handoff(target, manifest)
                self.state.restart_target = target
                self._write_state()
                self._inline_restart(target, manifest)
                self._clear_restart_state()
                return True
            target = self._macos_restart_target()
            return self._supervised_handoff(target, manifest)
        except Exception:
            # _supervised_handoff and _inline_restart already restore on their
            # own failure paths.  Target detection has not, so restore only if
            # the restart sub-state still says enable changes are live.
            if self.state.backend_enable_done:
                try:
                    self._restore_enable_change(manifest)
                except Exception as restore_exc:
                    raise SetupError(
                        "restart target detection failed and rollback failed; restore %s manually: %s"
                        % (manifest, redact(str(restore_exc)))
                    ) from restore_exc
            raise

    def _service_env(self, token: str) -> Dict[str, str]:
        # Preserve configured runtime settings without capturing shell credentials.
        values = {key: value for key, value in self.env.items()
                  if key.startswith(("CATY_", "OPENCLAW_", "OPENAI_", "FISH_", "POYO_", "RENOISE_"))
                  and not key.startswith("CATY_SETUP_")}
        for key in ("FFMPEG_BIN", "FFPROBE_BIN", "XDG_STATE_HOME", "ANTHROPIC_API_KEY"):
            if key in self.env:
                values[key] = self.env[key]
        values.update({
            "PATH": str(self.home / ".local" / "bin") + os.pathsep + self.env.get("PATH", os.defpath),
            "CATY_ID": self.member, "CATY_GATEWAY_PORT": str(self.port),
            "CATY_BACKEND": self.backend, "CATY_AGENT": self.env.get("CATY_AGENT", "main"),
            "CATY_LANG": self.env.get("CATY_LANG", "ja"), "CATY_NAME": self.name,
            "CATY_ACCENT_COLOR": self.accent, "CATY_TOKEN": token,
            "CATY_PUBLIC_URL": self.public_url, "CATY_REQUIRE_AUTH": "1",
        })
        member_data = self.home / ".local" / "share" / "caty-gateway" / self.member
        values.setdefault("CATY_ASSET_DIR", str(member_data / "assets"))
        values.setdefault("CATY_FILLER_DIR", str(member_data / "fillers"))
        values.setdefault("CATY_CONFIG_DIR", str(self.home / ".config" / "caty-gateway" / self.member))
        if self.args.no_history:
            values.pop("CATY_HISTORY_DIR", None)
        else:
            root = pathlib.Path(self.env.get("XDG_STATE_HOME", str(self.home / ".local" / "state"))).expanduser()
            values["CATY_HISTORY_DIR"] = str(root / "caty-gateway" / "history" / self.member)
        return values

    @staticmethod
    def _write_private(path: pathlib.Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix="." + path.name, dir=str(path.parent))
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _render_service(self, token: str) -> None:
        values = self._service_env(token)
        if any(any(char in value for char in "\r\n\0") for value in values.values()):
            raise SetupError("service environment values must not contain newlines or NUL; remove them and retry")
        data_root = self.home / ".local" / "share"
        (data_root / "caty-gateway" / self.member).mkdir(parents=True, exist_ok=True)
        for key in ("CATY_ASSET_DIR", "CATY_FILLER_DIR", "CATY_CONFIG_DIR", "CATY_HISTORY_DIR"):
            if values.get(key):
                pathlib.Path(values[key]).expanduser().mkdir(parents=True, exist_ok=True)
        if values.get("CATY_ASSET_DIR"):
            asset_dir = pathlib.Path(values["CATY_ASSET_DIR"]).expanduser()
            for asset in resources.files("caty_gateway").joinpath("assets").iterdir():
                target = asset_dir / asset.name
                if asset.name.endswith(".png") and not target.exists():
                    self._write_private(target, asset.read_bytes())
        if self.system == "Linux":
            unit = self.home / ".config" / "systemd" / "user" / self.service_name
            if unit.exists() and not self._systemd_unit_matches_expected(unit):
                raise SetupError("refusing to overwrite a foreign service unit: %s" % unit)
            # systemd EnvironmentFile double quotes preserve whitespace, $, and #.
            def quote(value):
                return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$") + '"'
            content = "".join(key + "=" + quote(value) + "\n" for key, value in sorted(values.items()))
            self._write_private(unit, self._expected_systemd_unit())
            self._write_private(self.artifact_path, content.encode("utf-8"))
        else:
            template = resources.files("caty_gateway").joinpath("templates", "launchd.plist").read_text(encoding="utf-8")
            replacements = {"MEMBER_ID": self.member, "PYTHON": self.service_python, "WORKDIR": str(self.home),
                            "LOG_PATH": str(self.home / "Library" / "Logs" / ("caty-gateway-" + self.member + ".log"))}
            template = re.sub(r"__([A-Z0-9_]+)__", lambda match: escape(replacements.get(match.group(1), "")), template)
            payload = plistlib.loads(template.encode("utf-8"))
            payload["EnvironmentVariables"].update(values)
            # Empty optional placeholders must not override backend defaults.
            payload["EnvironmentVariables"] = {key: value for key, value in payload["EnvironmentVariables"].items() if value != "" or key in values}
            (self.home / "Library" / "Logs").mkdir(parents=True, exist_ok=True)
            self._write_private(self.artifact_path, plistlib.dumps(payload))

    def _install(self) -> None:
        if self.artifact_path.exists():
            if not self._owned_artifact() and not self._adopt_window_artifact():
                raise SetupError("refusing to alter existing target configuration: %s" % self.artifact_path)
            print("Install already completed by this interrupted job; verified artifact ownership.")
            return
        credential = secrets.token_hex(24)
        assert self.state is not None
        self.state.token_digest = self._token_digest(credential)
        self.state.updated_at = time.time()
        self._write_state()  # persist ownership proof before the installer can write the artifact
        self._render_service(credential)
        self.state.env_file_created_by_us = True
        self.state.env_file_sha256 = self._hash_file(self.artifact_path)
        self.state.token_digest = ""
        self.state.updated_at = time.time()
        self._write_state()  # close the installer-write / phase-checkpoint kill window

    def _start(self) -> None:
        if self.system == "Linux":
            for command in (
                ["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "enable", "--now", self.service_name],
            ):
                result = self._run(command)
                if result.returncode:
                    raise SetupError("%s failed: %s" % (" ".join(command), redact(result.stderr)))
            return
        target = "gui/%d/%s" % (os.getuid(), self.service_name)
        check = self._run(["launchctl", "print", target])
        if check.returncode:
            # Not loaded (e.g. resume after adopting an interrupted install where
            # the installer never reached bootstrap): bootstrap the plist first.
            bootstrap = self._run(["launchctl", "bootstrap", "gui/%d" % os.getuid(), str(self.artifact_path)])
            if bootstrap.returncode:
                kick = self._run(["launchctl", "kickstart", "-k", target])
                if kick.returncode:
                    raise SetupError(
                        "launchd label could not be loaded; rerun installer or inspect launchctl: "
                        + redact(bootstrap.stderr + "\n" + kick.stderr)
                    )
            verify = self._run(["launchctl", "print", target])
            if verify.returncode:
                raise SetupError("launchd label is not loaded after bootstrap; inspect launchctl print " + target)

    def _linger(self) -> None:
        result = self._run(["loginctl", "enable-linger"])
        if result.returncode:
            print("Elevation required: sudo loginctl enable-linger %s" % getpass.getuser())
            raise SetupError("self enable-linger was denied: " + redact(result.stderr))

    def _diagnostics(self) -> str:
        if self.system == "Linux":
            status = self._run(["systemctl", "--user", "status", self.service_name])
            journal = self._run(["journalctl", "--user", "-u", self.service_name, "-n", "20", "--no-pager"])
            return redact(status.stdout + status.stderr + "\n" + journal.stdout + journal.stderr)
        path = self.home / "Library" / "Logs" / ("caty-gateway-%s.log" % self.member)
        try:
            return redact("\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]))
        except OSError:
            return "No macOS gateway log file was available."

    def _health(self) -> None:
        deadline = time.monotonic() + self.args.health_timeout
        url = "http://127.0.0.1:%d/health" % self.port
        token = self._identity_token()
        if not token:
            raise SetupError("installed service configuration has no CATY_TOKEN")
        request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(request, timeout=2) as response:
                    if response.status == 200:
                        return
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(0.5)
        print("Health diagnostics (redacted):\n" + self._diagnostics())
        raise SetupError("health timed out; inspect the service status and last 20 log lines above")

    def _installed_env(self) -> Dict[str, str]:
        return self._read_env_file(self.artifact_path) if self.system == "Linux" else self._plist_env(self.artifact_path)

    def _identity_token(self) -> str:
        return self._installed_env().get("CATY_TOKEN", "")

    def _identity(self) -> None:
        credential = self._identity_token()
        if not credential:
            raise SetupError("installed service configuration has no CATY_TOKEN")
        request = urllib.request.Request(
            "http://127.0.0.1:%d/identity" % self.port,
            headers={"Authorization": "Bearer " + credential},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                body = json.loads(response.read().decode("utf-8"))
                status = response.status
        except urllib.error.HTTPError as exc:
            raise SetupError("authenticated /identity returned HTTP %d" % exc.code) from exc
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise SetupError("authenticated /identity could not be validated") from exc
        if not isinstance(body, dict):
            raise SetupError("authenticated /identity returned a non-object JSON body; inspect the running service")
        if status != 200 or body.get("id") != self.member:
            raise SetupError(
                "/identity did not return expected member id %s; if another service occupies "
                "port %d, stop it or choose a different --port" % (self.member, self.port)
            )

    @staticmethod
    def _parse_checked_at(value):
        if not isinstance(value, str) or not value:
            return None
        normalized = value.replace("Z", "+00:00")
        try:
            parsed = datetime.datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.timestamp()

    def _voice_diagnostic_hint(self) -> str:
        if self.system == "Linux":
            return "journalctl --user -u %s -n 20 --no-pager" % self.service_name
        return str(
            self.home / "Library" / "Logs" / ("caty-gateway-%s.log" % self.member)
        )

    def _voice(self) -> None:
        credential = self._identity_token()
        if not credential:
            raise SetupError("installed service configuration has no CATY_TOKEN")
        deadline = time.monotonic() + max(50.0, min(60.0, float(self.args.health_timeout)))
        url = "http://127.0.0.1:%d/tts/voice-state" % self.port
        last_error = None
        while time.monotonic() < deadline:
            request = urllib.request.Request(
                url,
                headers={"Authorization": "Bearer " + credential},
            )
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    body = json.loads(response.read().decode("utf-8"))
                    status = response.status
            except urllib.error.HTTPError as exc:
                last_error = "authenticated /tts/voice-state returned HTTP %d" % exc.code
            except (OSError, ValueError, urllib.error.URLError):
                last_error = "authenticated /tts/voice-state could not be validated"
            else:
                if not isinstance(body, dict):
                    raise SetupError(
                        "authenticated /tts/voice-state returned a non-object JSON body; inspect the running service"
                    )
                if status != 200:
                    last_error = "authenticated /tts/voice-state returned HTTP %d" % status
                elif body.get("engine") != "fish":
                    return
                else:
                    neutral = body.get("neutral")
                    availability = neutral.get("availability") if isinstance(neutral, dict) else None
                    checked_at = neutral.get("checked_at") if isinstance(neutral, dict) else None
                    checked_at_epoch = self._parse_checked_at(checked_at)
                    age = (
                        None
                        if checked_at_epoch is None
                        else time.time() - checked_at_epoch
                    )
                    fresh = bool(
                        age is not None
                        and -VOICE_STATE_MAX_FUTURE_SKEW_SECONDS <= age <= VOICE_STATE_MAX_STALENESS_SECONDS
                    )
                    if availability == "available" and fresh and not neutral.get("stale"):
                        return
                    last_error = (
                        "neutral voice readiness is %r (checked_at=%r)"
                        % (availability, checked_at)
                    )
            time.sleep(0.5)
        print("Voice diagnostics (redacted):\n" + self._diagnostics())
        raise SetupError(
            "%s; inspect %s"
            % (
                last_error or "neutral voice readiness timed out",
                self._voice_diagnostic_hint(),
            )
        )

    def _qr_timeout(self) -> float:
        try:
            timeout = float(self.env.get("CATY_SETUP_QR_TIMEOUT_SECONDS", "3700"))
        except ValueError as exc:
            raise SetupError("CATY_SETUP_QR_TIMEOUT_SECONDS must be numeric") from exc
        if not math.isfinite(timeout) or timeout <= 0:
            raise SetupError("CATY_SETUP_QR_TIMEOUT_SECONDS must be finite and greater than zero")
        return timeout

    def _run_supervised_qr(self, command: Sequence[str], child: Mapping[str, str]) -> int:
        timeout = self.qr_timeout
        try:
            process = subprocess.Popen(
                list(command),
                env=dict(child),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise SetupError("service Python is unavailable for QR display") from exc
        assert process.stdout is not None and process.stderr is not None
        lines: queue.Queue = queue.Queue(maxsize=128)
        stderr_tail = bytearray()
        stderr_limit = 8192
        stderr_truncated = False

        def terminate_child(sig: int) -> None:
            try:
                os.killpg(process.pid, sig)
            except (AttributeError, OSError):
                if sig == signal.SIGKILL:
                    process.kill()
                else:
                    process.terminate()

        previous_sigterm = None
        sigterm_handler = None
        sigterm_handler_installed = False
        if threading.current_thread() is threading.main_thread():
            previous_sigterm = signal.getsignal(signal.SIGTERM)

            def handle_sigterm(_signum, _frame) -> None:
                try:
                    if process.poll() is None:
                        terminate_child(signal.SIGTERM)
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            terminate_child(signal.SIGKILL)
                            try:
                                process.wait(timeout=2)
                            except subprocess.TimeoutExpired:
                                pass
                finally:
                    if signal.getsignal(signal.SIGTERM) is handle_sigterm:
                        signal.signal(signal.SIGTERM, previous_sigterm)
                raise SystemExit(143)

            sigterm_handler = handle_sigterm

        def drain_stdout(stream) -> None:
            try:
                for line in stream:
                    lines.put(("stdout", line))
            finally:
                lines.put(("stdout", None))

        def drain_stderr(stream) -> None:
            try:
                while True:
                    chunk = stream.read(2048)
                    if not chunk:
                        break
                    lines.put(("stderr", chunk))
            finally:
                lines.put(("stderr", None))

        threads = [
            threading.Thread(target=drain_stdout, args=(process.stdout,), daemon=True),
            threading.Thread(target=drain_stderr, args=(process.stderr,), daemon=True),
        ]
        started_threads = []
        deadline = time.monotonic() + min(timeout, 7200.0)
        finished_streams = set()
        qr_url = ""
        expires_at = ""
        try:
            if sigterm_handler is not None:
                signal.signal(signal.SIGTERM, sigterm_handler)
                sigterm_handler_installed = True
            for thread in threads:
                thread.start()
                started_threads.append(thread)
            while process.poll() is None or len(finished_streams) < 2:
                if time.monotonic() >= deadline:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (AttributeError, OSError):
                        process.kill()
                    process.wait(timeout=10)
                    raise SetupError("QR command exceeded its bounded supervisor timeout")
                try:
                    source, line = lines.get(timeout=0.2)
                except queue.Empty:
                    continue
                if line is None:
                    finished_streams.add(source)
                    continue
                if source == "stderr":
                    stderr_tail.extend(line.encode("utf-8", errors="replace"))
                    overflow = len(stderr_tail) - stderr_limit
                    if overflow > 0:
                        stderr_truncated = True
                        del stderr_tail[:overflow]
                    continue
                if line.startswith("QR URL: "):
                    qr_url = line.removeprefix("QR URL: ").strip()
                elif line.startswith("Expires: "):
                    expires_at = line.removeprefix("Expires: ").strip()
                if qr_url or expires_at:
                    changes: Dict[str, object] = {
                        "state": "waiting-qr",
                        "active": True,
                        "terminal": False,
                        "phase": "qr",
                    }
                    if qr_url:
                        changes["qr_url"] = qr_url
                    if expires_at:
                        changes["expires_at"] = expires_at
                    self._update_status(**changes)
            returncode = process.wait(timeout=10)
            if returncode:
                raw_stderr_tail = bytes(stderr_tail)
                if stderr_truncated:
                    newline = raw_stderr_tail.find(b"\n")
                    if newline >= 0:
                        raw_stderr_tail = raw_stderr_tail[newline + 1 :]
                self._update_status(
                    qr_error_tail=redact(raw_stderr_tail.decode("utf-8", errors="replace"))
                )
            return returncode
        finally:
            process.stdout.close()
            process.stderr.close()
            for thread in started_threads:
                thread.join(timeout=1)
            if (
                sigterm_handler_installed
                and signal.getsignal(signal.SIGTERM) is sigterm_handler
            ):
                signal.signal(signal.SIGTERM, previous_sigterm)

    def _qr(self) -> None:
        installed = self._installed_env()
        child = dict(self.env)
        child.update(installed)
        child["PYTHON"] = self.service_python
        supervised = self.env.get("CATY_SETUP_SUPERVISED") == "1"
        command = [
            self.service_python,
            "-m",
            "caty_gateway",
            "qr",
            "--qr-delivery",
            "url" if supervised else self.qr_delivery,
        ]
        if supervised:
            returncode = self._run_supervised_qr(command, child)
            self._update_status(timeline_entry="supervised QR command completed")
        else:
            try:
                result = subprocess.run(
                    command,
                        env=child,
                    stdin=None,
                    stdout=None,
                    stderr=None,
                    check=False,
                )
            except FileNotFoundError as exc:
                raise SetupError("service Python is unavailable for QR display") from exc
            returncode = result.returncode
        if returncode:
            raise SetupError("QR command exited non-zero; rerun it from the installed service environment")

    def _confirm(self) -> None:
        try:
            answer = input("Run this plan? [y/N] ").strip().lower()
        except EOFError as exc:
            raise SetupError("stdin is not interactive; rerun with --yes to accept the plan") from exc
        if answer not in {"y", "yes"}:
            raise SetupError("setup cancelled before side effects")

    def _read_status(self) -> Dict[str, object]:
        try:
            payload = json.loads(self.status_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, ValueError):
            return {}

    def _owner_is_live(self, payload: Mapping[str, object], owner: str) -> bool:
        try:
            pid = int(payload.get(owner + "_pid", 0))
            expected = str(payload.get(owner + "_start_time", ""))
        except (TypeError, ValueError):
            return False
        if pid <= 0 or not expected:
            return False
        # A bare PID is never ownership: the recorded start time must still
        # match so PID reuse cannot impersonate either flight owner.
        return process_start_time(pid, self.system) == expected

    def _supervisor_is_live(self, payload: Mapping[str, object]) -> bool:
        return self._owner_is_live(payload, "supervisor")

    def _orchestrator_is_live(self, payload: Mapping[str, object]) -> bool:
        return self._owner_is_live(payload, "orchestrator")

    def _flight_is_live(self, payload: Mapping[str, object]) -> bool:
        return self._supervisor_is_live(payload) or self._orchestrator_is_live(payload)

    @staticmethod
    def _identifies_flight_owner(payload: Mapping[str, object]) -> bool:
        return any(
            payload.get(owner + "_pid") and payload.get(owner + "_start_time")
            for owner in ("supervisor", "orchestrator")
        )

    def _handoff_grace(self) -> float:
        try:
            configured = float(self.env.get("CATY_SETUP_HANDOFF_GRACE_SECONDS", "5"))
            return min(5.0, max(0.0, configured))
        except ValueError:
            return 5.0

    def _owner_registration_pending(self, payload: Mapping[str, object]) -> bool:
        # ``resuming`` covers supervisor spawn -> child registration.  A dead
        # supervisor in this bounded window must not open a parallel-run gap;
        # after the existing handoff grace expires, stale status unlocks.
        state = str(payload.get("state", ""))
        registration_window = state == "handoff" or (
            state == "resuming" and payload.get("orchestrator_registration_pending") is True
        )
        return bool(
            payload.get("active")
            and registration_window
            and not self._flight_is_live(payload)
        )

    def _claim_orchestrator_owner(self, **initial_status) -> bool:
        if not self.orchestrator_start_time:
            # Owner registration is an additive safety net.  Some supported
            # hosts cannot resolve process start time, so preserve the legacy
            # supervisor-only single-flight behavior instead of aborting setup.
            if initial_status:
                self._update_status(**initial_status)
            return True
        identity = {
            "orchestrator_pid": self.orchestrator_pid,
            "orchestrator_start_time": self.orchestrator_start_time,
        }
        if self.env.get("CATY_SETUP_SUPERVISED") == "1":
            # The resumed child is expected to overlap its supervisor, so it
            # keeps that owner pair while conditionally claiming its own pair.
            # A supervised marker alone is not authority to create status: an
            # actual supervisor must already have published the active flight.
            if not self._claim_status.get("active") or not self.status_path.is_file():
                return True
            snapshot = self._read_status()
            compared_fields = (
                "active",
                "orchestrator_pid",
                "orchestrator_start_time",
                "orchestrator_registration_pending",
            )
            for _attempt in range(3):
                owns_snapshot = all(
                    snapshot.get(field) == value for field, value in identity.items()
                )
                if not snapshot.get("active"):
                    self._claim_status = dict(snapshot)
                    return False
                if self._orchestrator_is_live(snapshot) and not owns_snapshot:
                    self._claim_status = dict(snapshot)
                    return False
                expected = {field: snapshot.get(field) for field in compared_fields}
                payload = self._update_status(
                    expected_fields=expected,
                    clear_fields=("orchestrator_registration_pending",),
                    **identity,
                )
                if all(payload.get(field) == value for field, value in identity.items()):
                    return True
                snapshot = dict(payload)
            raise SetupError(
                "setup status changed repeatedly while registering supervised ownership"
            )

        # Manual orchestrators register through the status update that the old
        # flow already performed after preflight, planning, and confirmation.
        # With no explicit initial update, retain the narrow direct-call helper
        # behavior used by ownership tests.
        status_changes = dict(initial_status) or {
            "state": "starting",
            "active": True,
            "terminal": False,
            "phase": "preflight",
        }
        clear_fields = tuple(status_changes.pop("clear_fields", ())) + (
            "orchestrator_registration_pending",
        )
        snapshot = dict(self._claim_status)
        compared_fields = ("updated_at", "active", "state", *OWNER_FIELDS)
        for _attempt in range(3):
            expected = {field: snapshot.get(field) for field in compared_fields}
            payload = self._update_status(
                expected_fields=expected,
                clear_fields=clear_fields,
                **status_changes,
                **identity,
            )
            if all(payload.get(field) == value for field, value in identity.items()):
                return True
            if payload.get("active") and self._flight_is_live(payload):
                self._claim_status = dict(payload)
                return False
            snapshot = dict(payload)
        raise SetupError("setup status changed repeatedly while claiming single-flight ownership")

    def _clear_orchestrator_owner(self) -> None:
        if not self.orchestrator_start_time or not self.status_path.is_file():
            return
        identity = {
            "orchestrator_pid": self.orchestrator_pid,
            "orchestrator_start_time": self.orchestrator_start_time,
        }
        # Compare-and-clear prevents an exiting process from deleting a newer
        # orchestrator's owner pair after handoff or recovery.
        self._update_status(
            expected_fields=identity,
            clear_fields=("orchestrator_pid", "orchestrator_start_time"),
        )

    def _print_status(self, payload: Mapping[str, object]) -> None:
        if not payload:
            state = self._read_state(validate_fingerprint=False)
            if state:
                print(redact("Setup is incomplete. Completed phases: " + ", ".join(state.completed_phases)))
            else:
                print("No active or recent setup status was found for member " + redact(self.member) + ".")
            return
        state_name = str(payload.get("state", "unknown"))
        phase = str(payload.get("phase", ""))
        message = str(payload.get("message", ""))
        print(redact("Setup status for %s: %s" % (self.member, state_name)))
        if phase:
            print(redact("Current phase: " + phase))
        if message:
            print(redact("Detail: " + message))
        if payload.get("qr_url"):
            print(redact("QR URL: " + str(payload["qr_url"])))
        if payload.get("expires_at"):
            print(redact("Expires: " + str(payload["expires_at"])))
        if payload.get("recovery_pointer"):
            print(redact("Recovery: " + str(payload["recovery_pointer"])))
        if payload.get("qr_error_tail"):
            print(redact("QR Error Tail: " + str(payload["qr_error_tail"])))
        if payload.get("resume_output"):
            print(redact("Resume Output: " + str(payload["resume_output"])))

    def _status(self, wait: bool) -> int:
        try:
            timeout = float(self.env.get("CATY_SETUP_STATUS_WAIT_SECONDS", "600"))
        except ValueError as exc:
            raise SetupError("CATY_SETUP_STATUS_WAIT_SECONDS must be numeric") from exc
        if timeout <= 0:
            raise SetupError("CATY_SETUP_STATUS_WAIT_SECONDS must be greater than zero")
        deadline = time.monotonic() + min(timeout, 3600.0)
        payload = self._read_status()
        if not payload:
            resume_state = self._read_state(validate_fingerprint=False)
            if resume_state is None or not wait:
                self._print_status(payload)
                return 0
        dead_owner_polls = 0
        owner_grace_deadline: Optional[float] = None
        while (
            wait
            and not payload.get("qr_url")
            and str(payload.get("state", "")) not in TERMINAL_STATES
        ):
            flight_is_live = self._flight_is_live(payload)
            if self._owner_registration_pending(payload):
                if owner_grace_deadline is None:
                    owner_grace_deadline = time.monotonic() + self._handoff_grace()
            else:
                owner_grace_deadline = None
            inside_owner_grace = (
                owner_grace_deadline is not None
                and time.monotonic() < owner_grace_deadline
            )
            if (
                payload.get("active")
                and self._identifies_flight_owner(payload)
                and not flight_is_live
                and not inside_owner_grace
            ):
                dead_owner_polls += 1
                if dead_owner_polls >= 3:
                    self._print_status(payload)
                    print(
                        "The restart supervisor is no longer running; setup did not complete — "
                        "rerun the setup command to resume."
                    )
                    return 1
            else:
                dead_owner_polls = 0
            if time.monotonic() >= deadline:
                raise SetupError("status wait timed out; rerun --status to continue following progress")
            time.sleep(0.5)
            payload = self._read_status()
        self._print_status(payload)
        return 0

    def _single_flight_active(self) -> bool:
        if self.env.get("CATY_SETUP_SUPERVISED") == "1":
            self._claim_status = self._read_status()
            return False
        status = self._read_status()
        if (
            status.get("orchestrator_pid") == self.orchestrator_pid
            and status.get("orchestrator_start_time") == self.orchestrator_start_time
        ):
            self._claim_status = dict(status)
            return False
        if self._owner_registration_pending(status):
            deadline = time.monotonic() + self._handoff_grace()
            while time.monotonic() < deadline and self._owner_registration_pending(status):
                time.sleep(0.1)
                status = self._read_status()
        self._claim_status = dict(status)
        return bool(status.get("active") and self._flight_is_live(status))

    def _hydrate_from_resume_state(self) -> None:
        """Restore non-secret invocation choices for supervisor-driven resume."""
        if not self.state:
            return
        config = self.state.resolved_config
        try:
            self.backend = normalize_backend(str(config["backend"]))
            self.args.no_history = bool(config.get("no_history", False))
            self.port = int(config["port"])
            self.name = str(config["name"])
            self.accent = str(config["accent"])
            self.public_url = str(config["public_url"])
            self.qr_delivery = str(config.get("qr_delivery", "auto"))
            self.probe_python = str(config.get("probe_python", self.probe_python))
            self.service_python = str(config.get("service_python", self.service_python))
        except (KeyError, TypeError, ValueError) as exc:
            raise SetupError("resume state lacks the frozen setup configuration") from exc

    def run(self) -> int:
        if self.args.status:
            return self._status(self.args.wait)
        if self._single_flight_active():
            owner = (
                "restart supervisor"
                if self._supervisor_is_live(self._claim_status)
                else "setup orchestrator"
            )
            print(
                redact(
                    "A %s is already running for %s; following its status instead of starting a second setup."
                    % (owner, self.member)
                )
            )
            return self._status(True)
        supervised = self.env.get("CATY_SETUP_SUPERVISED") == "1"
        if supervised and not self.args.plan_only and not self._claim_orchestrator_owner():
            print(
                redact(
                    "A setup orchestrator is already running for %s; following its status instead of starting a second setup."
                    % self.member
                )
            )
            return self._status(True)
        if self.args.reset and not self.args.plan_only and self.state_path.exists():
            self.state_path.unlink()
        self.state = self._read_state(validate_fingerprint=False)
        if supervised:
            self._hydrate_from_resume_state()
        # The gap from the read-only _single_flight_active() check to the
        # post-preflight registration below is intentional.  As in the old
        # implementation, two manual invocations can race through preflight;
        # #1129 preserves that accepted legacy window.
        self._preflight()
        if self.state and self.state.config_fingerprint != self._config_fingerprint():
            raise SetupError(self._fingerprint_mismatch_message())
        self._print_plan()
        if self.args.plan_only:
            print("Plan only: no files, directories, services, or resume state were changed.")
            return 0
        if not self.args.yes:
            self._confirm()
        if self.state is None:
            self._start_state()
        initial_status = {
            "state": "running",
            "active": True,
            "terminal": False,
            "phase": "preflight",
            "completed_phases": list(self.state.completed_phases),
            "timeline_entry": "setup run started",
            "clear_fields": (
                "qr_url",
                "expires_at",
                "recovery_pointer",
                "resume_output",
                "qr_error_tail",
            ),
        }
        if not supervised and not self._claim_orchestrator_owner(**initial_status):
            print(
                redact(
                    "A setup orchestrator is already running for %s; following its status instead of starting a second setup."
                    % self.member
                )
            )
            return self._status(True)
        if supervised:
            self._update_status(**initial_status)
        for phase in PHASES:
            assert self.state is not None
            if phase in self.state.completed_phases:
                if phase == "start" and any(
                    pending not in self.state.completed_phases for pending in ("health", "identity", "voice", "qr")
                ):
                    self._start()
                continue
            self._update_status(
                state="running",
                active=True,
                terminal=False,
                phase=phase,
                timeline_entry="phase %s started" % phase,
            )
            if phase in {"preflight", "plan"}:
                pass
            elif phase == "install":
                self._install()
            elif phase == "backend":
                if not self._backend():
                    return 0
            elif phase == "start":
                self._start()
            elif phase == "linger":
                if self.system == "Linux":
                    self._linger()
            elif phase == "health":
                self._health()
            elif phase == "identity":
                self._identity()
            elif phase == "voice":
                self._voice()
            elif phase == "qr":
                self._qr()
            else:
                raise SetupError("unhandled setup phase: %s" % phase)
            self._mark(phase)
        if self.state_path.exists():
            self.state_path.unlink()
        self._update_status(
            state="succeeded",
            active=False,
            terminal=True,
            phase="complete",
            completed_phases=list(PHASES),
            timeline_entry="setup completed",
            clear_fields=("qr_url", "expires_at", "recovery_pointer", *OWNER_FIELDS),
        )
        effective_qr_delivery = self.qr_delivery
        if effective_qr_delivery == "auto":
            effective_qr_delivery = "tty" if sys.stdout.isatty() else "url"
        if effective_qr_delivery == "tty":
            print("Setup complete; the qr step displayed the one-time pairing QR in the terminal.")
        else:
            print(
                "Setup complete; the qr step printed URL/PNG relay instructions and confirmed "
                "that the URL was fetched or pairing was claimed. The local PNG was cleaned "
                "automatically; delete any uploaded copy after pairing."
            )
        return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    orchestrator: Optional[SetupOrchestrator] = None
    previous_sigterm = None
    sigterm_handler = None
    if threading.current_thread() is threading.main_thread():
        previous_sigterm = signal.getsignal(signal.SIGTERM)

        def handle_sigterm(_signum, _frame) -> None:
            raise SystemExit(143)

        sigterm_handler = handle_sigterm
        signal.signal(signal.SIGTERM, sigterm_handler)
    if os.environ.get("CATY_SETUP_SUPERVISED") == "1":
        # The supervisor blocks SIGTERM across Popen.  Install our terminal
        # cleanup handler before unblocking so there is no default-action gap.
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})

    def record_terminal(**changes) -> None:
        if orchestrator is None or orchestrator.args.status:
            return
        try:
            identity = {
                "orchestrator_pid": orchestrator.orchestrator_pid,
                "orchestrator_start_time": orchestrator.orchestrator_start_time,
            }
            snapshot = orchestrator._read_status()
            if any(snapshot.get(field) is not None for field in identity):
                # A different registered pair means this process has lost the
                # flight.  Its terminal path must not stamp over the new owner.
                changes.setdefault("expected_fields", identity)
            else:
                # Preserve terminal reporting before ownership registration,
                # but make the absent-pair observation atomic with the write.
                changes.setdefault(
                    "expected_fields",
                    {field: None for field in identity},
                )
            changes.setdefault(
                "clear_fields",
                ("qr_url", "expires_at", "recovery_pointer", *OWNER_FIELDS),
            )
            orchestrator._update_status(**changes)
        except OSError:
            # Preserve the primary failure even if the state filesystem itself
            # is unavailable; stderr remains the fail-loud surface.
            pass

    try:
        orchestrator = SetupOrchestrator(argv)
        return orchestrator.run()
    except SetupError as exc:
        record_terminal(
            state="failed", active=False, terminal=True,
            message=redact(str(exc)), timeline_entry="setup failed",
        )
        prefix = "" if isinstance(exc, UnsupportedBackendError) else "ERROR: "
        print(prefix + redact(str(exc)), file=sys.stderr)
        return 2 if isinstance(exc, UnsupportedBackendError) else 1
    except KeyboardInterrupt:
        record_terminal(
            state="interrupted", active=False, terminal=True,
            message="setup was interrupted; rerun the same command to resume",
            timeline_entry="setup interrupted",
        )
        print("ERROR: interrupted; rerun the same command to resume", file=sys.stderr)
        return 130
    except SystemExit as exc:
        if exc.code == 143:
            record_terminal(
                state="interrupted",
                active=False,
                terminal=True,
                message="setup received SIGTERM; rerun the same command to resume",
                timeline_entry="setup interrupted by SIGTERM",
            )
        raise
    except Exception:  # defensive: unexpected exceptions may carry secret-bearing values
        record_terminal(
            state="failed", active=False, terminal=True,
            message="unexpected setup failure", timeline_entry="setup failed unexpectedly",
        )
        if os.environ.get("CATY_SETUP_DEBUG") == "1":
            print(redact(traceback.format_exc()), file=sys.stderr)
        print("ERROR: unexpected setup failure; rerun with the same flags or inspect local service logs", file=sys.stderr)
        return 1
    finally:
        if (
            orchestrator is not None
            and not orchestrator.args.status
            and not orchestrator.args.plan_only
        ):
            try:
                orchestrator._clear_orchestrator_owner()
            except OSError:
                pass
        if sigterm_handler is not None and signal.getsignal(signal.SIGTERM) is sigterm_handler:
            signal.signal(signal.SIGTERM, previous_sigterm)


def member_artifact_path(member: str, home: pathlib.Path, system: str) -> pathlib.Path:
    """Locate an installed member without constructing a setup orchestrator."""
    if system == "Linux":
        return home / ".config" / "caty-gateway" / (member + ".env")
    if system == "Darwin":
        return home / "Library" / "LaunchAgents" / ("ai.caty.gateway." + member + ".plist")
    raise ValueError("unsupported platform")


def read_member_env(path: pathlib.Path, system: str) -> Dict[str, str]:
    """Read service values using the installer parsers, without running setup."""
    if system == "Linux":
        values = SetupOrchestrator._read_env_file(path, strict=True)
    elif system == "Darwin":
        values = SetupOrchestrator._plist_env(path, strict=True)
    else:
        raise ValueError("unsupported platform")
    if any(not key or "=" in key or "\x00" in key or "\x00" in value for key, value in values.items()):
        raise ValueError("invalid environment value")
    return values


if __name__ == "__main__":
    raise SystemExit(main())
