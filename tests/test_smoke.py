import json
import os
import subprocess
import sys
import time
import unittest
import urllib.parse
from contextlib import contextmanager
from io import BytesIO
from unittest import mock


from caty_gateway import caty_gateway as cg


class NonClosingBytesIO(BytesIO):
    def close(self):
        pass


class MemorySocket:
    def __init__(self, request_bytes):
        self.input = BytesIO(request_bytes)
        self.output = NonClosingBytesIO()

    def makefile(self, mode, *args, **kwargs):
        if "r" in mode:
            return self.input
        return self.output

    def sendall(self, data):
        self.output.write(data)

    def settimeout(self, timeout):
        pass

    def shutdown(self, how):
        pass

    def close(self):
        pass


class MemoryServer:
    server_name = "127.0.0.1"
    server_port = 0


class FakeHTTPResponse:
    def __init__(self, body=b"", status=200, lines=None):
        self.status = status
        self._body = body
        self._offset = 0
        self._lines = lines or []

    def read(self, size=-1):
        if self._offset >= len(self._body):
            return b""
        if size is None or size < 0:
            size = len(self._body) - self._offset
        chunk = self._body[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk

    def __iter__(self):
        return iter(self._lines)


class FakeHTTPConnection:
    requests = []

    def __init__(self, host, port=None, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._response = FakeHTTPResponse()

    def request(self, method, path, body=None, headers=None):
        FakeHTTPConnection.requests.append((method, path, body, headers or {}))
        if path == "/v1/chat/completions":
            lines = [
                b'data: {"choices":[{"delta":{"content":"First."}}]}\n',
                b'data: {"choices":[{"delta":{"content":"Second."}}]}\n',
                b"data: [DONE]\n",
            ]
            self._response = FakeHTTPResponse(lines=lines)
            return
        payload = json.loads(body or "{}")
        text = payload.get("input", "")
        self._response = FakeHTTPResponse(f"audio({text})".encode("utf-8"))

    def getresponse(self):
        return self._response

    def close(self):
        pass


RECORDED_AGENT_CMDS = []


def fake_subprocess_run(cmd, capture_output=True, text=True, timeout=None):
    if "ffmpeg" in os.path.basename(cmd[0]):
        out_path = cmd[-1]
        with open(out_path, "wb") as f:
            f.write(b"wav")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    if cmd[:4] == [cg.OPENCLAW, "capability", "audio", "transcribe"]:
        return subprocess.CompletedProcess(
            cmd,
            0,
            json.dumps({"outputs": [{"text": "audio transcript"}]}),
            "",
        )
    if len(cmd) >= 2 and cmd[0] == cg.OPENCLAW and cmd[1] == "agent":
        RECORDED_AGENT_CMDS.append(list(cmd))
        return subprocess.CompletedProcess(
            cmd,
            0,
            json.dumps({"result": {"payloads": [{"text": "Legacy reply"}]}}),
            "",
        )
    return subprocess.CompletedProcess(cmd, 1, "", "unexpected command")


@contextmanager
def patched_environment(**values):
    original_env = {k: os.environ.get(k) for k in values}
    original_stream_flag = getattr(cg, "STREAM_TTS_ENABLED", None)
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if hasattr(cg, "STREAM_TTS_ENABLED") and "CATY_STREAM_TTS" in values:
            cg.STREAM_TTS_ENABLED = values["CATY_STREAM_TTS"] == "1"
        yield
    finally:
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if hasattr(cg, "STREAM_TTS_ENABLED"):
            cg.STREAM_TTS_ENABLED = original_stream_flag


class GatewaySmokeTest(unittest.TestCase):
    def setUp(self):
        cg.JOBS.clear()
        cg.FILLERS[:] = []
        cg.SILENCE_1S = None
        FakeHTTPConnection.requests = []
        RECORDED_AGENT_CMDS[:] = []

    def tearDown(self):
        cg.JOBS.clear()
        cg.FILLERS[:] = []
        cg.SILENCE_1S = None

    def request(self, method, path, body=b"", headers=None):
        headers = dict(headers or {})
        headers.setdefault("Host", "127.0.0.1")
        headers.setdefault("Connection", "close")
        headers.setdefault("Content-Length", str(len(body)))
        raw = [f"{method} {path} HTTP/1.1"]
        raw.extend(f"{key}: {value}" for key, value in headers.items())
        request_bytes = ("\r\n".join(raw) + "\r\n\r\n").encode("latin-1") + body

        sock = MemorySocket(request_bytes)
        cg.Handler(sock, ("127.0.0.1", 0), MemoryServer())
        data = sock.output.getvalue()
        head, _, rest = data.partition(b"\r\n\r\n")
        lines = head.decode("iso-8859-1").split("\r\n")
        status = int(lines[0].split()[1])
        response_headers = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                response_headers[key.lower()] = value.strip()
        if "content-length" in response_headers:
            rest = rest[:int(response_headers["content-length"])]
        return status, response_headers, rest

    def wait_for_reply(self, job_id):
        deadline = time.time() + 5
        last = None
        while time.time() < deadline:
            status, headers, body = self.request("GET", f"/reply/{job_id}")
            last = (status, headers, body)
            if status != 202:
                return last
            time.sleep(0.05)
        self.fail(f"reply did not finish: {last!r}")

    def test_talk2_text_returns_job_and_reply_audio(self):
        text = urllib.parse.quote("hello from device")
        with patched_environment(CATY_STREAM_TTS=None), \
                mock.patch("subprocess.run", side_effect=fake_subprocess_run), \
                mock.patch("http.client.HTTPConnection", FakeHTTPConnection):
            status, headers, body = self.request(
                "POST",
                "/talk2",
                headers={"X-Caty-Text": text, "X-Session-Id": "s1"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(headers["content-type"], "application/json")
            self.assertEqual(headers["x-transcript"], "hello from device")
            payload = json.loads(body)
            self.assertRegex(payload["id"], r"^[0-9a-f]{12}$")
            self.assertEqual(payload["transcript"], "hello from device")

            status, headers, body = self.wait_for_reply(payload["id"])
            self.assertEqual(status, 200)
            self.assertEqual(headers["content-type"], "audio/mpeg")
            self.assertEqual(headers["x-reply"], "Legacy reply")
            self.assertEqual(body, b"audio(Legacy reply)")

            # 記憶連続性 invariant: session-key の合成形は現値完全一致であること
            agent_cmds = [c for c in RECORDED_AGENT_CMDS if "--session-key" in c]
            self.assertTrue(agent_cmds, "openclaw agent cmd with --session-key not recorded")
            key = agent_cmds[0][agent_cmds[0].index("--session-key") + 1]
            self.assertEqual(key, "agent:main:caty-s1")

    def test_talk2_audio_transcribes_before_returning_job(self):
        with patched_environment(CATY_STREAM_TTS=None), \
                mock.patch("subprocess.run", side_effect=fake_subprocess_run), \
                mock.patch("http.client.HTTPConnection", FakeHTTPConnection):
            status, headers, body = self.request(
                "POST",
                "/talk2",
                body=b"fake audio",
                headers={"Content-Type": "audio/mp4"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(headers["x-transcript"], "audio transcript")
            payload = json.loads(body)
            self.assertEqual(payload["transcript"], "audio transcript")

            status, headers, body = self.wait_for_reply(payload["id"])
            self.assertEqual(status, 200)
            self.assertEqual(headers["x-reply"], "Legacy reply")
            self.assertTrue(body.startswith(b"audio("))

    def test_filler_returns_mp3_when_fillers_are_loaded(self):
        cg.FILLERS[:] = [(b"filler mp3", 0.3)]

        status, headers, body = self.request("GET", "/filler")

        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "audio/mpeg")
        self.assertEqual(body, b"filler mp3")

    def test_stream_tts_uses_sentence_stream_when_enabled(self):
        text = urllib.parse.quote("stream please")
        with patched_environment(CATY_STREAM_TTS="1", CATY_GATEWAY_TOKEN="token"), \
                mock.patch("subprocess.run", side_effect=fake_subprocess_run), \
                mock.patch("http.client.HTTPConnection", FakeHTTPConnection):
            status, headers, body = self.request(
                "POST",
                "/talk2",
                headers={"X-Caty-Text": text, "X-Session-Id": "s2"},
            )
            self.assertEqual(status, 200)
            payload = json.loads(body)

            status, headers, body = self.wait_for_reply(payload["id"])
            self.assertEqual(status, 200)
            self.assertEqual(headers["content-type"], "audio/mpeg")
            self.assertEqual(headers["x-reply"], "First.Second.")
            self.assertEqual(body, b"audio(First.)audio(Second.)")

            # 記憶連続性 invariant: SSE 経路の session-key ヘッダも現値完全一致であること
            sse = [r for r in FakeHTTPConnection.requests if r[1] == "/v1/chat/completions"]
            self.assertTrue(sse, "SSE request to /v1/chat/completions not recorded")
            self.assertEqual(
                sse[0][3].get("x-openclaw-session-key"), "agent:main:caty-s2")

    def test_stream_reply_exposes_partial_text_without_changing_audio_stream(self):
        job = cg.Job("stream request", session_id="partial-session")
        job.enable_partial_reply(True)
        job.stage_reply("最初の文。")
        job.push(b"partial mp3")
        with cg.JOBS_LOCK:
            cg.JOBS["partial-job"] = job

        with mock.patch.object(cg, "STREAM_TTS_ENABLED", True):
            status, headers, body = self.request("GET", "/reply/partial-job")
            self.assertEqual(status, 202)
            self.assertEqual(headers["content-type"], "application/json")
            self.assertEqual(json.loads(body)["partial_reply"], "最初の文。")

            job.finish()
            status, headers, body = self.request("GET", "/stream/partial-job")
            self.assertEqual(status, 200)
            self.assertEqual(headers["content-type"], "audio/mpeg")
            self.assertEqual(body, b"partial mp3")

    def test_stream_reply_hides_text_until_audio_is_available(self):
        job = cg.Job("stream request", session_id="partial-session")
        job.enable_partial_reply(True)
        job.stage_reply("音声化に失敗した文。")
        with cg.JOBS_LOCK:
            cg.JOBS["no-audio-job"] = job

        with mock.patch.object(cg, "STREAM_TTS_ENABLED", True):
            status, _, body = self.request("GET", "/reply/no-audio-job")

        self.assertEqual(status, 202)
        self.assertNotIn("partial_reply", json.loads(body))

    def test_stream_flag_does_not_expose_legacy_pipeline_reply_as_partial(self):
        job = cg.Job("legacy request", session_id="legacy-session")
        job.update_reply("legacy final")
        job.push(b"legacy audio")
        with cg.JOBS_LOCK:
            cg.JOBS["legacy-stream-job"] = job

        with mock.patch.object(cg, "STREAM_TTS_ENABLED", True):
            status, _, body = self.request("GET", "/reply/legacy-stream-job")

        self.assertEqual(status, 202)
        self.assertNotIn("partial_reply", json.loads(body))

    def test_stream_flag_off_keeps_legacy_202_body_byte_identical(self):
        job = cg.Job("legacy request", session_id="legacy-session")
        job.reply = "途中でも公開しない"
        with cg.JOBS_LOCK:
            cg.JOBS["legacy-job"] = job

        with mock.patch.object(cg, "STREAM_TTS_ENABLED", False), \
                mock.patch.object(cg.presence_state, "PRESENCE_MODE2_ENABLED", False):
            status, _, body = self.request("GET", "/reply/legacy-job")

        self.assertEqual(status, 202)
        self.assertEqual(body, b'{"ok":true,"status":"thinking"}')


class BackendStubTest(unittest.TestCase):
    """Optional backends remain importable without pulling OpenClaw into factory tests."""

    def test_hermes_backend_is_importable_and_non_streaming(self):
        try:
            from caty_gateway.backends.hermes import HermesBackend
        except ImportError as e:
            self.skipTest(f"backends package not present yet (pre-refactor): {e}")
        backend = HermesBackend(
            url="http://127.0.0.1:8642",
            api_key="x",
            voice_hint="voice-hint\n",
            log=lambda *args: None,
        )
        self.assertFalse(backend.supports_stream())
        with self.assertRaises(NotImplementedError):
            list(backend.stream("hi", "sid", 5))

    def test_claude_backend_factory_constructs_without_openclaw(self):
        original = (
            cg.BACKEND_NAME,
            cg.CATY_CLAUDE_BIN,
            cg.CATY_CLAUDE_MODEL,
            cg.CATY_CLAUDE_CWD,
        )
        try:
            cg.BACKEND_NAME = "claude"
            cg.CATY_CLAUDE_BIN = "/tmp/claude"
            cg.CATY_CLAUDE_MODEL = ""
            cg.CATY_CLAUDE_CWD = "/tmp/member-cwd"
            backend = cg._build_backend()
        finally:
            (
                cg.BACKEND_NAME,
                cg.CATY_CLAUDE_BIN,
                cg.CATY_CLAUDE_MODEL,
                cg.CATY_CLAUDE_CWD,
            ) = original
        self.assertEqual(backend.__class__.__name__, "ClaudeCodeBackend")
        self.assertFalse(backend.supports_stream())

    def test_identity_available_uses_backend_health_with_ttl_cache(self):
        class BackendWithHealth:
            def __init__(self):
                self.available = False
                self.calls = 0

            def health(self):
                self.calls += 1
                return self.available

        backend = BackendWithHealth()
        original_backend = cg.BACKEND
        original_cache = dict(cg._IDENTITY_HEALTH_CACHE)
        try:
            cg.BACKEND = backend
            cg._IDENTITY_HEALTH_CACHE["checked_at"] = -cg._IDENTITY_HEALTH_TTL
            cg._IDENTITY_HEALTH_CACHE["available"] = True

            self.assertFalse(cg.identity_payload()["available"])
            self.assertEqual(backend.calls, 1)

            backend.available = True
            self.assertFalse(cg.identity_payload()["available"])
            self.assertEqual(backend.calls, 1)

            cg._IDENTITY_HEALTH_CACHE["checked_at"] -= cg._IDENTITY_HEALTH_TTL + 1
            self.assertTrue(cg.identity_payload()["available"])
            self.assertEqual(backend.calls, 2)
        finally:
            cg.BACKEND = original_backend
            cg._IDENTITY_HEALTH_CACHE.update(original_cache)


if __name__ == "__main__":
    unittest.main()
