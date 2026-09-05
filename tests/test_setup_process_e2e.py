import errno
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path


GATEWAY_COMMAND = [sys.executable, "-m", "caty_gateway.caty_gateway"]
TEST_AGENT = "pairing-process-e2e"
TEST_TOKEN = "process-e2e-" + "token-1128"
START_TIMEOUT_SECONDS = 8.0


class GatewayStartError(AssertionError):
    def __init__(self, message, stderr):
        super().__init__(message)
        self.stderr = stderr

    @property
    def address_in_use(self):
        return (
            "Address already in use" in self.stderr
            or f"[Errno {errno.EADDRINUSE}]" in self.stderr
        )


def _free_loopback_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _gateway_env(tmp_path, pairing_dir, port):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("CATY_")
    }
    env.update(
        {
            "HOME": str(home),
            "CATY_AGENT": TEST_AGENT,
            "CATY_BACKEND": "openclaw",
            "CATY_GATEWAY_BIND": "127.0.0.1",
            "CATY_GATEWAY_PORT": str(port),
            "CATY_ID": TEST_AGENT,
            "CATY_PAIRING_DIR": str(pairing_dir),
            "CATY_PUBLIC_URL": f"http://127.0.0.1:{port}",
            "CATY_REQUIRE_AUTH": "1",
            "CATY_TOKEN": TEST_TOKEN,
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


def _request(port, method, path, *, body=None, headers=None, timeout=1.0):
    request_headers = {"Connection": "close", **(headers or {})}
    request_body = body
    if isinstance(body, dict):
        request_body = json.dumps(body).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(method, path, body=request_body, headers=request_headers)
        response = connection.getresponse()
        raw = response.read()
        payload = json.loads(raw.decode("utf-8")) if raw else None
        return response.status, payload
    finally:
        connection.close()


def _terminate_and_capture(process):
    if process.poll() is None:
        process.terminate()
    try:
        return process.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.communicate(timeout=3)


def _spawn_and_wait_ready(env, port):
    process = subprocess.Popen(
        GATEWAY_COMMAND,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    ready = False
    try:
        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        last_observation = "no response"
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                raise GatewayStartError(
                    "gateway exited before readiness: "
                    f"rc={process.returncode} stdout={stdout!r} stderr={stderr!r}",
                    stderr,
                )
            try:
                status, payload = _request(
                    port,
                    "GET",
                    "/health",
                    headers={"Authorization": f"Bearer {TEST_TOKEN}"},
                    timeout=0.25,
                )
                last_observation = f"status={status} payload={payload!r}"
                if (
                    process.poll() is None
                    and status == 200
                    and payload == {"ok": True, "agent": TEST_AGENT}
                ):
                    ready = True
                    return process
            except (OSError, http.client.HTTPException, ValueError) as error:
                last_observation = repr(error)
            time.sleep(0.05)

        stdout, stderr = _terminate_and_capture(process)
        raise GatewayStartError(
            "timed out waiting for gateway readiness: "
            f"last={last_observation}; stdout={stdout!r}; stderr={stderr!r}",
            stderr,
        )
    finally:
        if not ready and process.poll() is None:
            _terminate_and_capture(process)


def _start_gateway(env, port, attempts=3):
    last_error = None
    for attempt in range(attempts):
        try:
            return _spawn_and_wait_ready(env, port)
        except GatewayStartError as error:
            last_error = error
            if not error.address_in_use or attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))
    raise last_error


def _start_gateway_on_free_port(tmp_path, pairing_dir, attempts=3):
    last_error = None
    for _ in range(attempts):
        port = _free_loopback_port()
        env = _gateway_env(tmp_path, pairing_dir, port)
        try:
            return _start_gateway(env, port), port, env
        except GatewayStartError as error:
            last_error = error
            if not error.address_in_use:
                raise
    raise last_error


def _sigkill_and_wait(process):
    process.send_signal(signal.SIGKILL)
    try:
        stdout, stderr = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate(timeout=3)
        raise AssertionError(
            f"gateway did not exit after SIGKILL: stdout={stdout!r} stderr={stderr!r}"
        )
    assert process.returncode == -signal.SIGKILL, (
        f"gateway did not report SIGKILL exit: rc={process.returncode} "
        f"stdout={stdout!r} stderr={stderr!r}"
    )


def test_pair_claim_survives_sigkill_and_consumed_tombstone_survives_process_restart(
    tmp_path,
):
    pairing_dir = tmp_path / "pairing"
    process = None
    try:
        process, port, env = _start_gateway_on_free_port(tmp_path, pairing_dir)
        status, issued = _request(
            port,
            "POST",
            "/pair/new",
            body=b"",
            headers={"Authorization": f"Bearer {TEST_TOKEN}"},
        )
        assert status == 200, issued
        assert issued["ok"] is True
        pair = issued["pair"]

        _sigkill_and_wait(process)
        process = None

        process = _start_gateway(env, port)
        status, claimed = _request(
            port,
            "POST",
            "/pair/claim",
            body={"pair": pair},
        )
        assert status == 200, claimed
        assert claimed["token"] == TEST_TOKEN

        _sigkill_and_wait(process)
        process = None

        process = _start_gateway(env, port)
        status, consumed = _request(
            port,
            "POST",
            "/pair/claim",
            body={"pair": pair},
        )
        assert (status, consumed["error"]) == (409, "pairing_consumed")
    finally:
        if process is not None:
            _terminate_and_capture(process)
