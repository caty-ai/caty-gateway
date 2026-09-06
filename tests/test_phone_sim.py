"""Pure smoke-client checks; test_e2e_* requires real loopback sockets."""

import contextlib
import http.client
import http.server
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "smoke" / "phone-sim.py"
SPEC = importlib.util.spec_from_file_location("phone_sim", SCRIPT)
phone_sim = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = phone_sim
SPEC.loader.exec_module(phone_sim)


def _check(condition, message):
    # Only booleans enter assertion rewriting: never show credential operands.
    assert condition, message


def _credentials():
    return {"pair": "1a2b3c4d." + "ab" * 16, "token": "client-" + secrets.token_hex(12)}


def _payload():
    return {"v": 1, "url": "http://127.0.0.1:8765", "pair": _credentials()["pair"], "id": "smoke"}


def test_parse_env_export_quotes_comments_crlf():
    value = "env-" + secrets.token_hex(12)
    text = ' # comment\r\nexport CATY_TOKEN="' + value + '" # note\r\nCATY_PUBLIC_URL=\'http://localhost:8765\'\r\nOTHER="has # inside"\r\nPLAIN=word # comment\r\n'
    result = phone_sim.parse_env_text(text)
    _check(result.get("CATY_TOKEN") == value, "quoted token was not parsed")
    assert result["CATY_PUBLIC_URL"] == "http://localhost:8765"
    assert result["OTHER"] == "has # inside"
    assert result["PLAIN"] == "word"


def test_validate_qr_contract():
    value = _payload()
    _check(phone_sim.validate_qr_payload(value) == value, "contract payload rejected")


@pytest.mark.parametrize("field,value", [
    ("v", 2), ("v", True), ("v", "1"),
    ("url", "http://user@localhost:8765"),
    ("url", "http://localhost:8765?query=yes"),
    ("url", "http://localhost:8765#fragment"),
    ("url", "relative/path"), ("url", "ftp://localhost"),
    ("pair", "invalid"), ("id", ""),
])
def test_validate_qr_rejects_without_secret_in_error(field, value):
    payload = _payload()
    secret_half = payload["pair"].split(".")[1]
    payload[field] = value
    try:
        phone_sim.validate_qr_payload(payload)
    except ValueError as error:
        _check(secret_half not in str(error), "validation disclosed a secret")
    else:
        pytest.fail("invalid QR payload accepted")


def test_redact_known_and_generic_secrets():
    credentials = _credentials()
    credentials["pair_secret"] = credentials["pair"].split(".")[1]
    generic = "9f8e7d6c." + "cd" * 16
    result = phone_sim.redact(" ".join(credentials.values()) + " " + generic, credentials)
    _check(all(value not in result for value in credentials.values()), "known secret survived redaction")
    _check(generic not in result and "cd" * 16 not in result, "generic pair survived redaction")
    assert "[REDACTED]" in result


@pytest.mark.parametrize("name", ["pair", "pair_secret", "token"])
def test_find_leaks_returns_names_only(name):
    credentials = _credentials()
    credentials["pair_secret"] = credentials["pair"].split(".")[1]
    result = phone_sim.find_leaks("log prefix " + credentials[name], credentials)
    _check(name in result, "expected leak name absent")
    _check(all(item in credentials or item == "pair_pattern" for item in result), "leak detector returned a value")
    _check(all(value not in str(result) for value in credentials.values()), "leak detector disclosed secret")


def test_find_leaks_allows_public_pair_id():
    credentials = _credentials()
    credentials["pair_secret"] = credentials["pair"].split(".")[1]
    assert phone_sim.find_leaks(credentials["pair"].split(".")[0], credentials) == []
    assert phone_sim.find_leaks("pid=deadbeef", credentials) == []


def test_find_leaks_generic_pair():
    assert phone_sim.find_leaks("deadbeef." + "cd" * 16, _credentials()) == ["pair_pattern"]


class _FakeOpenAI(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, content_type, body):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path != "/v1/models":
            self.send_error(404)
            return
        self._send("application/json", json.dumps({"data": [{"id": "phone-sim-fake"}]}).encode())

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        users = [message["content"] for message in request["messages"] if message["role"] == "user"]
        reply = "OK (%d user messages so far)" % len(users)
        if users and "What was the codeword" in users[-1]:
            match = re.search(r"\bblue-[0-9a-f]{6}\b", "\n".join(users[:-1]))
            reply = match.group(0) if match else "unknown"
        if request.get("stream"):
            chunks = [reply[:4], reply[4:]]
            body = "".join("data: " + json.dumps({"choices": [{"delta": {"content": chunk}}]}) + "\n\n" for chunk in chunks)
            self._send("text/event-stream", (body + "data: [DONE]\n\n").encode())
        else:
            self._send("application/json", json.dumps({"choices": [{"message": {"content": reply}}]}).encode())


_SUPERVISOR = r'''
import pathlib, signal, subprocess, sys, time
root = pathlib.Path(sys.argv[1])
child = None
def stop(signum, frame):
    (root / "stop").touch()
    if child is not None and child.poll() is None:
        child.terminate()
signal.signal(signal.SIGTERM, stop)
with (root / "gateway.log").open("ab", buffering=0) as log:
    try:
        while not (root / "stop").exists():
            child = subprocess.Popen([sys.executable, "-B", "-m", "caty_gateway.caty_gateway"], stdout=log, stderr=log)
            (root / "gateway.pid").write_text(str(child.pid))
            child.wait()
            (root / "gateway.pid").unlink(missing_ok=True)
            time.sleep(1.5)
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
'''


def _health(port, token):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
    try:
        connection.request("GET", "/health", headers={"Authorization": "Bearer " + token})
        response = connection.getresponse()
        response.read()
        return response.status == 200
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


@contextlib.contextmanager
def _running_gateway(tmp_path):
    fake = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenAI)
    thread = threading.Thread(target=fake.serve_forever, daemon=True)
    thread.start()
    supervisor = None
    try:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        token = "smoke-auth-" + secrets.token_hex(16)
        home = tmp_path / "home"
        home.mkdir()
        env = {key: value for key, value in os.environ.items() if not key.startswith("CATY_")}
        env["PYTHONPATH"] = str(ROOT / "src") + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env.update({
            "HOME": str(home), "CATY_AGENT": "phone-sim-fake", "CATY_ID": "smoke-e2e",
            "CATY_BACKEND": "openai-compat", "CATY_GATEWAY_BIND": "127.0.0.1",
            "CATY_GATEWAY_PORT": str(port), "CATY_PAIRING_DIR": str(tmp_path / "pairing"),
            "CATY_PUBLIC_URL": "http://127.0.0.1:%d" % port,
            "CATY_REQUIRE_AUTH": "1", "CATY_TOKEN": token, "PYTHONUNBUFFERED": "1",
            "CATY_OPENAI_BASE_URL": "http://127.0.0.1:%d/v1" % fake.server_port,
            "CATY_OPENAI_MODEL": "phone-sim-fake", "CATY_HISTORY_DIR": str(tmp_path / "history"),
        })
        envfile = tmp_path / "member.env"
        envfile.touch(mode=0o600)
        envfile.write_text("CATY_TOKEN=" + token + "\nCATY_PUBLIC_URL=" + env["CATY_PUBLIC_URL"] + "\n")
        supervisor = subprocess.Popen([sys.executable, "-B", "-c", _SUPERVISOR, str(tmp_path)], env=env, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 60
        while not _health(port, token):
            if supervisor.poll() is not None or time.monotonic() >= deadline:
                log = tmp_path / "gateway.log"
                tail = "\n".join(log.read_text(errors="replace").splitlines()[-20:]) if log.exists() else "(no gateway.log)"
                raise AssertionError("gateway did not become ready:\n" + phone_sim.redact(tail, [token]))
            time.sleep(0.05)
        yield envfile, token
    finally:
        (tmp_path / "stop").touch()
        if supervisor is not None:
            # The supervisor knows its owned child's Popen object and terminates it.
            supervisor.terminate()
            try:
                supervisor.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pidfile = tmp_path / "gateway.pid"
                if pidfile.exists():
                    try:
                        os.kill(int(pidfile.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                supervisor.kill()
                supervisor.wait(timeout=5)
        fake.shutdown()
        fake.server_close()
        thread.join(timeout=5)


def _run_client(envfile, token, *args):
    result = subprocess.run([sys.executable, "-B", str(SCRIPT), "--env-file", str(envfile), "--label", "e2e", *args], cwd=ROOT, capture_output=True, text=True, timeout=120)
    output = result.stdout + result.stderr
    _check(token not in output, "client disclosed the administrator token")
    _check(re.search(r"[0-9a-f]{8}\.[0-9a-f]{32}", output) is None, "client disclosed a pairing credential")
    _check(len(result.stdout.splitlines()) == 1, "client did not print exactly one JSON line")
    try:
        summary = json.loads(result.stdout)
    except ValueError:
        pytest.fail("client stdout was not JSON")
    return result.returncode, summary


def test_e2e_phone_sim_round_trip(tmp_path):
    with _running_gateway(tmp_path) as (envfile, token):
        pidfile = shlex.quote(str(tmp_path / "gateway.pid"))
        # Wait until the old child is reaped; the supervisor pauses before respawn.
        restart = 'pid=$(cat %s); kill -TERM "$pid"; while kill -0 "$pid" 2>/dev/null; do sleep 0.05; done' % pidfile
        returncode, summary = _run_client(envfile, token, "--restart-cmd", restart, "--log-file", str(tmp_path / "gateway.log"), "--require-recall", "--require-restart-observed")
        _check(returncode == 0, "round trip client failed")
        assert summary["ok"] is True
        assert summary["stage"] == "done"
        assert len(summary["turns"]) == 3
        assert all(turn["http_status"] == 200 for turn in summary["turns"])
        assert summary["restart"]["observed"] is True
        assert summary["resume_recall"] is True
        assert summary["log_check"] == "pass"


def test_e2e_phone_sim_detects_log_leak(tmp_path):
    with _running_gateway(tmp_path) as (envfile, token):
        leakfile = tmp_path / "leaky.log"
        leakfile.write_text("deliberate negative control: " + token + "\n")
        returncode, summary = _run_client(envfile, token, "--no-restart", "--log-file", str(leakfile))
        _check(returncode == 1, "log leak did not fail the client")
        assert summary["ok"] is False
        assert summary["stage"] == "logcheck"
        assert summary["log_check"] == "leak"
        assert summary["log_secret_leak"] is True


class _MockGateway:
    """HTTP seam for the full client flow without opening sockets."""

    def __init__(self, claim_status=200, health=(200,)):
        self.credentials = _credentials()
        self.credentials["CATY_TOKEN"] = "admin-" + secrets.token_hex(12)
        self.credentials["pair_secret"] = self.credentials["pair"].split(".")[1]
        self.payload = {**_payload(), "pair": self.credentials["pair"]}
        self.calls = []
        self.claim_status = claim_status
        self.health = iter(health)
        self.turn = 0
        self.polls = {}
        self.codeword = None

    def request(self, url, method, path, *, token=None, headers=None, body=None, timeout=15):
        self.calls.append((method, path))
        if path == "/pair/new":
            _check(token == self.credentials["CATY_TOKEN"], "issuance used wrong token")
            _check(method == "POST" and body == b"", "issuance shape differs from contract")
            return 200, {}, json.dumps({"ok": True, **self.payload}).encode()
        if path == "/pair/claim":
            _check(token is None, "claim must be unauthenticated")
            _check(method == "POST" and body["pair"] == self.payload["pair"], "claim shape differs from contract")
            _check(body["device"].get("name") == "phone-sim", "claim lacks a display label")
            if self.claim_status != 200:
                # Treat even error bodies containing credentials as untrusted.
                return self.claim_status, {}, json.dumps(self.credentials).encode()
            return 200, {}, json.dumps({"ok": True, "v": 1, "url": self.payload["url"], "id": self.payload["id"], "token": self.credentials["token"]}).encode()
        _check(token == self.credentials["token"], "conversation used wrong token")
        if path == "/health":
            status = next(self.health)
            if status is None:
                raise ConnectionRefusedError("simulated downtime")
            return status, {}, b"{}"
        if path == "/talk2":
            _check(method == "POST" and body == b"", "text turn must have empty body")
            _check(headers["Content-Length"] == "0", "text turn length missing")
            _check(headers["X-Session-Id"] == "smoke-mocked", "session changed between turns")
            text = phone_sim.urllib.parse.unquote(headers["X-Caty-Text"])
            _check(headers["X-Caty-Text"] == phone_sim.urllib.parse.quote(text, safe=""), "text header is not percent encoded")
            self.turn += 1
            if self.turn == 1:
                self.codeword = re.search(r"blue-[0-9a-f]{6}", text).group()
            return 200, {}, json.dumps({"id": "job-%d" % self.turn}).encode()
        _check(method == "GET" and path == "/reply/job-%d" % self.turn, "unexpected reply endpoint")
        self.polls[path] = self.polls.get(path, 0) + 1
        if self.polls[path] == 1:
            return 202, {}, b"{}"
        reply = self.codeword if self.turn == 3 else "p" * 75 + " ".join(self.credentials.values())
        return 200, {"x-reply-enc": phone_sim.urllib.parse.quote(reply, safe=""), "x-degraded": "tts"}, b"mp3"


def _mock_main(monkeypatch, capsys, gateway, *args):
    env_text = "CATY_TOKEN=" + gateway.credentials["CATY_TOKEN"] + "\nCATY_PUBLIC_URL=" + gateway.payload["url"]
    monkeypatch.setattr(phone_sim, "request", gateway.request)
    monkeypatch.setattr(phone_sim, "read_text", lambda path: env_text if path == "member.env" else "clean logs")
    monkeypatch.setattr(phone_sim.time, "sleep", lambda seconds: None)
    result = phone_sim.main(["--env-file", "member.env", "--session-id", "smoke-mocked", *args])
    captured = capsys.readouterr()
    _check(all(value not in captured.out + captured.err for value in gateway.credentials.values()), "main disclosed a credential")
    _check(len(captured.out.splitlines()) == 1, "main output is not a single JSON line")
    return result, json.loads(captured.out), captured.err


def test_main_happy_flow_and_redaction_before_preview(monkeypatch, capsys):
    gateway = _MockGateway()
    result, summary, progress = _mock_main(monkeypatch, capsys, gateway, "--no-restart", "--require-recall", "--label", gateway.credentials["CATY_TOKEN"])
    assert result == 0
    assert summary["ok"] is True
    assert summary["stage"] == "done"
    assert summary["label"] == "[REDACTED]"
    assert summary["restart"]["observed"] == "skipped"
    assert summary["resume_recall"] is True
    assert summary["log_check"] == "skipped"
    assert gateway.calls.count(("POST", "/pair/new")) == 1
    assert gateway.calls.count(("POST", "/pair/claim")) == 1
    assert gateway.calls.count(("POST", "/talk2")) == 3
    assert all(count == 2 for count in gateway.polls.values())
    assert len(summary["turns"]) == 3
    assert all(turn["http_status"] == 200 and turn["degraded"] == "tts" for turn in summary["turns"])
    assert summary["turns"][0]["reply_preview"] == ("p" * 75 + "[REDACTED]")[:80]
    assert [line.split()[-1] for line in progress.splitlines()] == ["qr", "claim", "turn1", "turn2", "restart", "turn3", "logcheck", "done"]


def test_main_consumed_claim_is_never_retried(monkeypatch, capsys):
    gateway = _MockGateway(claim_status=409)
    result, summary, _ = _mock_main(monkeypatch, capsys, gateway, "--no-restart")
    assert result == 1
    assert summary["stage"] == "claim"
    assert summary["claim"]["http_status"] == 409
    assert gateway.calls.count(("POST", "/pair/claim")) == 1
    assert summary["turns"] == []


def test_main_request_error_suppresses_details(monkeypatch, capsys):
    gateway = _MockGateway()
    def fail_request(*args, **kwargs):
        raise ConnectionRefusedError(gateway.credentials["CATY_TOKEN"])
    monkeypatch.setattr(gateway, "request", fail_request)
    result, summary, _ = _mock_main(monkeypatch, capsys, gateway, "--no-restart")
    assert summary["error"] == "operation failed (ConnectionRefusedError; details suppressed)"


def test_main_invalid_session_precedes_env_read(monkeypatch, capsys):
    gateway = _MockGateway()
    result, summary, _ = _mock_main(monkeypatch, capsys, gateway, "--no-restart", "--session-id", "bad id", "--env-file", "missing.env")
    assert result == 1
    assert gateway.calls == []
    assert summary["stage"] == "qr"
    assert summary["error"] == "session id must use only letters, digits, dot, underscore or hyphen"


@pytest.mark.parametrize("args,timeout", [((), 60), (("--log-timeout", "37"), 37)])
def test_main_log_command_timeout(monkeypatch, capsys, args, timeout):
    def run(command, **kwargs):
        assert command == "read-logs"
        assert kwargs["timeout"] == timeout
        return subprocess.CompletedProcess(command, 0, stdout=b"clean logs")
    monkeypatch.setattr(phone_sim.subprocess, "run", run)
    result, summary, _ = _mock_main(monkeypatch, capsys, _MockGateway(), "--no-restart", "--log-cmd", "read-logs", *args)
    assert result == 0
    assert summary["log_check"] == "pass"


def test_main_log_command_leak_is_fatal_and_redacted(monkeypatch, capsys):
    gateway = _MockGateway()
    monkeypatch.setattr(phone_sim, "run_command", lambda command, timeout: gateway.credentials["token"])
    result, summary, _ = _mock_main(monkeypatch, capsys, gateway, "--no-restart", "--log-cmd", "read-logs")
    assert result == 1
    assert summary["stage"] == "logcheck"
    assert summary["log_check"] == "leak"
    assert summary["log_secret_leak"] is True


@pytest.mark.parametrize("health,require,expected_result,observed", [
    ((200,), False, 0, False), ((200,), True, 1, False),
    ((None, 503, 200), True, 0, True),
])
def test_main_restart_observation(monkeypatch, capsys, health, require, expected_result, observed):
    gateway = _MockGateway(health=health)
    commands = []
    monkeypatch.setattr(phone_sim, "run_command", lambda command, timeout: commands.append(command) or "")
    args = ["--restart-cmd", "restart-service"]
    if require:
        args.append("--require-restart-observed")
    result, summary, _ = _mock_main(monkeypatch, capsys, gateway, *args)
    assert result == expected_result
    assert summary["restart"]["observed"] is observed
    assert summary["restart"]["downtime_s"] >= 0
    assert commands == ["restart-service"]
    assert summary["stage"] == ("restart" if expected_result else "done")


def test_main_argparse_error_suppresses_unknown_secrets(capsys):
    credentials = _credentials()
    result = phone_sim.main(["--qr-json", "unused", "--unknown", *credentials.values()])
    captured = capsys.readouterr()
    _check(all(value not in captured.out + captured.err for value in credentials.values()), "argparse disclosed an argument")
    assert result == 1
    assert len(captured.out.splitlines()) == 1
    assert json.loads(captured.out)["stage"] == "qr"


def test_main_env_command_failure_suppresses_unlearned_output(monkeypatch, capsys):
    credentials = _credentials()
    def fail_command(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, stdout=credentials["token"].encode(), stderr=credentials["pair"].encode())
    monkeypatch.setattr(phone_sim.subprocess, "run", fail_command)
    result = phone_sim.main(["--env-cmd", "read-member", "--no-restart"])
    captured = capsys.readouterr()
    _check(all(value not in captured.out + captured.err for value in credentials.values()), "failed env command disclosed output")
    assert result == 1
    assert len(captured.out.splitlines()) == 1
    assert json.loads(captured.out)["stage"] == "qr"


def test_main_malformed_source_does_not_echo_unlearned_secret(monkeypatch, capsys):
    token = "member-" + secrets.token_hex(12)
    monkeypatch.setattr(phone_sim, "read_text", lambda path: "CATY_TOKEN=" + token + "\ninvalid assignment")
    result = phone_sim.main(["--env-file", "member.env", "--label", token,
                             "--session-id", token, "--no-restart"])
    captured = capsys.readouterr()
    _check(token not in captured.out + captured.err, "malformed source disclosed an unlearned secret")
    assert result == 1
    assert len(captured.out.splitlines()) == 1
    assert json.loads(captured.out)["stage"] == "qr"
