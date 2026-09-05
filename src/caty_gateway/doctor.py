"""Passive installation checks. No check submits an agent prompt."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import ipaddress
from http.client import HTTPException
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import sys
from urllib import error, parse, request

from .setup_redaction import redact

BACKENDS = ("claude", "codex", "openclaw", "hermes", "openai-compat")
TIMEOUT = 10


def normalize_backend(value: str) -> str:
    value = value.strip().lower().replace("openai_compat", "openai-compat")
    if value not in BACKENDS:
        raise ValueError(
            "post-release: backend '%s' is not supported in this release; supported: %s"
            % (value, ", ".join(BACKENDS))
        )
    return value


@dataclass(frozen=True)
class Check:
    status: str
    name: str
    hint: str

    def __str__(self) -> str:
        return "PASS %s" % self.name if self.status == "PASS" else "%s %s: %s" % (self.status, self.name, self.hint)


def _runner(command, *, env, timeout):
    return subprocess.run(command, env=env, timeout=timeout, capture_output=True, text=True, check=False)


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _get_json(url, headers, timeout):
    # Do not forward credentials through a redirect or submit anything but GET.
    opener = request.build_opener(_NoRedirect())
    with opener.open(request.Request(url, headers=headers, method="GET"), timeout=timeout) as response:
        data = response.read(1024 * 1024 + 1)
        try:
            payload = json.loads(data) if len(data) <= 1024 * 1024 else None
        except (ValueError, UnicodeError):
            payload = None
        return response.status, payload


def _resolve(host):
    return {item[4][0] for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}


def _port_available(port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False


def _writable(path):
    # Check the nearest existing parent without leaving directories or files behind.
    while not path.exists() and path != path.parent:
        path = path.parent
    if not path.is_dir() or not os.access(path, os.W_OK | os.X_OK):
        return False
    return True


def _private_address(value):
    address = ipaddress.ip_address(value)
    return (address.is_loopback or
            (address.version == 4 and address in ipaddress.ip_network("100.64.0.0/10")) or
            (address.version == 6 and address in ipaddress.ip_network("fd7a:115c:a1e0::/48")))


class Doctor:
    def __init__(self, *, backend, member="default", port=8788, public_url="", env=None,
                 runner=None, python=None, home=None, system=None, emit=print,
                 command_path=None, get_json=None, resolver=None, port_available=None,
                 writable=None):
        self.backend = normalize_backend(backend)
        self.env = dict(os.environ if env is None else env)
        self.home = Path(home or self.env.get("HOME", str(Path.home())))
        self.member = member
        self.port = port
        self.public_url = public_url or self.env.get("CATY_PUBLIC_URL", "")
        self.python = python or self.env.get("PYTHON") or sys.executable
        self.system = system or platform.system()
        self.runner = runner or _runner
        self.emit = emit
        self.command_path = command_path or self._command_path
        self.get_json = get_json or _get_json
        self.resolver = resolver or _resolve
        self.port_available = port_available or _port_available
        self.writable = writable or _writable
        self.checks = []
        self.tailscale_ip = None

    def _command_path(self, value):
        path = Path(value).expanduser()
        if os.sep in value:
            return str(path) if path.is_file() and os.access(path, os.X_OK) else None
        return shutil.which(value, path=self.env.get("PATH", os.defpath))

    def _check(self, name, ok, hint, *, warning=False):
        hint = redact(hint)
        for key, value in self.env.items():
            if value and any(part in key.upper() for part in ("TOKEN", "API_KEY", "PASSWORD")):
                hint = hint.replace(value, "[REDACTED]")
        hint = " ".join(hint.split())
        result = Check("PASS" if ok else ("WARN" if warning else "FAIL"), name, hint)
        self.checks.append(result)
        self.emit(str(result))
        return ok

    def _run(self, command):
        try:
            return self.runner(command, env=self.env, timeout=TIMEOUT)
        except (OSError, subprocess.SubprocessError, RuntimeError):
            return subprocess.CompletedProcess(command, 1, "", "")

    def _command(self, name, value, args, hint):
        executable = self.command_path(value)
        result = self._run([executable, *args]) if executable else None
        self._check(name, result is not None and result.returncode == 0, hint)
        return result

    def _get(self, name, url, token, hint):
        try:
            parsed = parse.urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("invalid URL")
            parsed.port  # Validate the port before urllib consumes the URL.
            headers = {"Authorization": "Bearer " + token} if token else {}
            status, payload = self.get_json(url, headers, TIMEOUT)
            ok = status == 200
        except (OSError, error.URLError, HTTPException, ValueError, TypeError):
            ok, payload = False, None
        self._check(name, ok, hint)
        return ok, payload

    def _common(self):
        self._check("OS", self.system in {"Darwin", "Linux"}, "use macOS or Linux")
        result = self._run([self.python, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"])
        try:
            version = tuple(int(part) for part in result.stdout.strip().split("."))
            valid = result.returncode == 0 and len(version) == 2 and version >= (3, 10)
        except (ValueError, AttributeError):
            valid = False
        self._check("Python", valid, "install Python 3.10+ and set PYTHON to its executable")
        self._check("ffmpeg", bool(self.command_path(self.env.get("FFMPEG_BIN", "ffmpeg"))),
                    "install ffmpeg and set FFMPEG_BIN or PATH")
        self._check("ffprobe", bool(self.command_path(self.env.get("FFPROBE_BIN", "ffprobe"))), "install ffprobe and add it to PATH")
        tailscale = self.command_path("tailscale")
        self._check("tailscale executable", bool(tailscale), "install Tailscale and add tailscale to PATH")
        if tailscale:
            status = self._run([tailscale, "status"])
            logged_in = status.returncode == 0 and not any(
                word in (status.stdout or "").lower() for word in ("logged out", "needslogin", "not logged in")
            )
            self._check("tailscale login", logged_in, "run tailscale up and sign in")
            result = self._run([tailscale, "ip", "-4"])
            try:
                candidate = next(line.strip() for line in result.stdout.splitlines() if line.strip())
                address = ipaddress.IPv4Address(candidate)
                valid = result.returncode == 0 and address in ipaddress.ip_network("100.64.0.0/10")
            except (ValueError, StopIteration, AttributeError):
                valid = False
            if valid:
                self.tailscale_ip = candidate
            self._check("tailscale IPv4", valid, "connect this host to a tailnet with an IPv4 address")
        valid_port = isinstance(self.port, int) and 1 <= self.port <= 65535
        self._check("port", valid_port and self.port_available(self.port),
                    "choose an unused --port between 1 and 65535 or stop the existing listener")
        if not self.public_url and self.tailscale_ip:
            self.public_url = "http://%s:%s" % (self.tailscale_ip, self.port)
        try:
            parsed = parse.urlsplit(self.public_url)
            valid = (parsed.scheme in {"http", "https"} and bool(parsed.hostname) and
                     parsed.username is None and parsed.password is None and
                     parsed.port != 0)
            addresses = list(self.resolver(parsed.hostname)) if valid else []
            valid = bool(addresses) and all(_private_address(address) for address in addresses)
        except (OSError, ValueError):
            valid = False
        self._check("public URL", valid, "set --public-url to an http(s) URL resolving only to loopback or tailnet addresses")
        paths = (
            ("config directory", self.home / ".config" / "caty-gateway"),
            ("state directory", Path(self.env.get("XDG_STATE_HOME", str(self.home / ".local" / "state"))) / "caty-gateway"),
            ("data directory", self.home / ".local" / "share" / "caty-gateway" / self.member),
        )
        for name, path in paths:
            self._check(name, self.writable(path), "grant this user write access to " + str(path))

    def _backend(self):
        if self.backend == "claude":
            self._command("claude version", self.env.get("CATY_CLAUDE_BIN", "claude"), ["--version"], "install Claude CLI and add claude to PATH")
            cwd = Path(self.env.get("CATY_CLAUDE_CWD", str(self.home))).expanduser()
            self._check("claude working directory", cwd.is_dir(), "set CATY_CLAUDE_CWD to an existing directory")
            self._check("claude credentials", (self.home / ".claude" / ".credentials.json").is_file(),
                        "sign in with Claude CLI; credentials stored in the OS keychain cannot be checked passively", warning=True)
        elif self.backend == "codex":
            self._command("codex version", "codex", ["--version"], "install Codex CLI and add codex to PATH")
            self._command("codex login", "codex", ["login", "status"], "run codex login before setup")
        elif self.backend == "openclaw":
            result = self._command("openclaw agents", self.env.get("OPENCLAW_BIN", "openclaw"),
                                   ["agents", "list"], "set OPENCLAW_BIN to an executable and configure its agents")
            agent = self.env.get("CATY_AGENT", "main")
            output = result.stdout if result is not None and result.returncode == 0 else ""
            present = bool(agent and re.search(r"(?<![\w.-])" + re.escape(agent) + r"(?![\w.-])", output))
            self._check("openclaw agent", present, "set CATY_AGENT to an agent shown by openclaw agents list")
            token = self.env.get("CATY_GATEWAY_TOKEN") or self.env.get("OPENCLAW_GATEWAY_TOKEN") or ""
            if not token.strip():
                try:
                    data = json.loads((self.home / ".openclaw" / "openclaw.json").read_text())
                    token = data["gateway"]["auth"]["token"]
                except (OSError, ValueError, KeyError, TypeError):
                    token = ""
            token = token.strip() if isinstance(token, str) else ""
            self._check("openclaw gateway token", bool(token),
                        "set CATY_GATEWAY_TOKEN/OPENCLAW_GATEWAY_TOKEN or gateway.auth.token in the OpenClaw config")
            self._get("openclaw gateway", self.env.get("CATY_GATEWAY_URL", "http://127.0.0.1:18789"),
                      token, "start the gateway and set CATY_GATEWAY_URL to its reachable HTTP URL")
        elif self.backend == "hermes":
            token = self.env.get("CATY_HERMES_API_KEY", "").strip()
            self._check("hermes API key", bool(token), "set CATY_HERMES_API_KEY")
            base = self.env.get("CATY_HERMES_URL", "http://127.0.0.1:8642").rstrip("/")
            self._get("hermes models", base + "/v1/models", token, "start Hermes and verify CATY_HERMES_URL and CATY_HERMES_API_KEY")
        else:
            base = self.env.get("CATY_OPENAI_BASE_URL", "").strip().rstrip("/")
            ok, data = self._get("openai-compat models", base + "/models",
                                 self.env.get("CATY_OPENAI_API_KEY", "").strip(),
                                 "start the API server and set CATY_OPENAI_BASE_URL (including /v1 when required) and CATY_OPENAI_API_KEY")
            if ok:
                models = [item.get("id") for item in data.get("data", []) if isinstance(item, dict) and isinstance(item.get("id"), str)] if isinstance(data, dict) and isinstance(data.get("data"), list) else []
                model = self.env.get("CATY_OPENAI_MODEL", "").strip()
                self._check("openai-compat model", bool(model) and model in models,
                            "set CATY_OPENAI_MODEL to an available model; available: " + ", ".join(models), warning=True)

    def run(self):
        self.checks = []
        self.tailscale_ip = None
        if not re.fullmatch(r"[A-Za-z0-9._-]+", self.member) or self.member in {".", ".."}:
            self._check("member", False, "use a member ID containing only letters, numbers, dot, underscore or hyphen")
            return False
        self._common()
        self._backend()
        return not any(check.status == "FAIL" for check in self.checks)


def build_parser(add_help=True):
    parser = argparse.ArgumentParser(add_help=add_help)
    parser.add_argument("--backend", required=True)
    parser.add_argument("--member", default="default")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--public-url", default="")
    parser.add_argument("--probe", action="store_true", help="reserved; this release runs passive checks only")
    return parser


def main(argv=None):
    parser = argparse.ArgumentParser(prog="caty-gateway doctor", parents=[build_parser(add_help=False)])
    args = parser.parse_args(argv)
    try:
        backend = normalize_backend(args.backend)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        port = args.port if args.port is not None else int(os.environ.get("CATY_GATEWAY_PORT", "8788"))
    except ValueError:
        print("FAIL port: set CATY_GATEWAY_PORT or --port to an integer between 1 and 65535", file=sys.stderr)
        return 2
    if args.probe:
        print("FAIL probe: --probe is unavailable in this release; omit it to run passive checks only", file=sys.stderr)
        return 2
    return 0 if Doctor(backend=backend, member=args.member, port=port, public_url=args.public_url).run() else 1


if __name__ == "__main__":
    raise SystemExit(main())
