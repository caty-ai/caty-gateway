import json
import os
import sys
import threading
import time
import unittest
from io import BytesIO


from caty_gateway import caty_gateway as cg
from caty_gateway.backends.claude import ClaudeStreamError, ClaudeStreamTimeout


class NonClosingBytesIO(BytesIO):
    def close(self):
        pass


class BrokenPipeOnSSE(NonClosingBytesIO):
    def write(self, data):
        if data.startswith(b"data: ") or data.startswith(b": "):
            raise BrokenPipeError("simulated disconnect")
        return super().write(data)


class MemorySocket:
    def __init__(self, request_bytes, output=None):
        self.input = BytesIO(request_bytes)
        self.output = output or NonClosingBytesIO()

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


class FakeClaudeChatBackend:
    def __init__(self):
        self.calls = []
        self.complete_text = "reply text"
        self.stream_chunks = ["hello", " world"]
        self.complete_error = None
        self.stream_error = None
        self.stream_error_after_chunks = False
        self.started = threading.Event()
        self.release = threading.Event()
        self.stream_closed = threading.Event()
        self.send_heartbeat = False

    def openai_complete(self, text, session_id, timeout, route=None):
        self.calls.append(("complete", text, session_id, timeout, route))
        self.started.set()
        if not self.release.is_set():
            self.release.wait(timeout=2)
        if self.complete_error:
            raise self.complete_error
        return self.complete_text

    def openai_stream(self, text, session_id, timeout, route=None, on_heartbeat=None, heartbeat_interval=5.0):
        self.calls.append(("stream", text, session_id, timeout, route, heartbeat_interval))
        self.started.set()
        try:
            if self.send_heartbeat and on_heartbeat:
                on_heartbeat()
            if not self.release.is_set():
                self.release.wait(timeout=2)
            if self.stream_error and not self.stream_error_after_chunks:
                raise self.stream_error
            for chunk in self.stream_chunks:
                yield chunk
            if self.stream_error and self.stream_error_after_chunks:
                raise self.stream_error
        finally:
            self.stream_closed.set()


class OpenAIChatCompletionsTest(unittest.TestCase):
    def setUp(self):
        self.original_backend = cg.BACKEND
        self.original_token = cg.CATY_TOKEN
        self.original_chat_token = cg.CATY_OPENAI_CHAT_TOKEN
        self.original_model = cg.CATY_CLAUDE_MODEL
        self.original_semaphore = cg._OPENAI_CHAT_CONCURRENCY
        self.original_timeout = cg.OPENAI_CHAT_TIMEOUT
        self.original_heartbeat = cg.OPENAI_CHAT_HEARTBEAT_SEC
        cg.CATY_TOKEN = "secret-token"
        cg.CATY_OPENAI_CHAT_TOKEN = "test-token"
        cg.CATY_CLAUDE_MODEL = "configured-model"
        cg.OPENAI_CHAT_TIMEOUT = 7
        cg.OPENAI_CHAT_HEARTBEAT_SEC = 1.0
        cg._OPENAI_CHAT_ACTIVE.clear()
        cg._OPENAI_CHAT_CONCURRENCY = threading.BoundedSemaphore(1)

    def tearDown(self):
        cg.BACKEND = self.original_backend
        cg.CATY_TOKEN = self.original_token
        cg.CATY_OPENAI_CHAT_TOKEN = self.original_chat_token
        cg.CATY_CLAUDE_MODEL = self.original_model
        cg._OPENAI_CHAT_CONCURRENCY = self.original_semaphore
        cg.OPENAI_CHAT_TIMEOUT = self.original_timeout
        cg.OPENAI_CHAT_HEARTBEAT_SEC = self.original_heartbeat
        cg._OPENAI_CHAT_ACTIVE.clear()

    def make_request(self, method, path, payload=None, headers=None, output=None):
        body = b""
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = dict(headers or {})
        headers.setdefault("Host", "127.0.0.1")
        headers.setdefault("Connection", "close")
        headers.setdefault("Content-Type", "application/json")
        headers.setdefault("Content-Length", str(len(body)))
        raw = [f"{method} {path} HTTP/1.1"]
        raw.extend(f"{key}: {value}" for key, value in headers.items())
        request_bytes = ("\r\n".join(raw) + "\r\n\r\n").encode("latin-1") + body

        sock = MemorySocket(request_bytes, output=output)
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
        length = int(response_headers.get("content-length", "0"))
        return status, response_headers, rest[:length], rest

    def payload(self, **overrides):
        body = {
            "model": "ignored-by-gateway",
            "stream": False,
            "user": "meet-123-agent-a",
            "messages": [
                {"role": "system", "content": "ignore me"},
                {"role": "assistant", "content": "old answer"},
                {"role": "user", "content": "latest prompt"},
            ],
        }
        body.update(overrides)
        return body

    def auth_headers(self, **overrides):
        headers = {
            "Authorization": "Bearer test-token",
            "X-Caty-Agent-Trust": "trusted",
        }
        headers.update(overrides)
        return headers

    def assert_released_after_stream_path(
        self,
        *,
        backend,
        output=None,
        expected_status=200,
        expected_fragment=None,
        same_user="meet-123-agent-a",
        other_user="meet-123-agent-b",
    ):
        cg.BACKEND = backend
        status, _, body, rest = self.make_request(
            "POST",
            "/v1/chat/completions",
            payload=self.payload(stream=True, user=same_user),
            headers=self.auth_headers(),
            output=output,
        )
        self.assertEqual(status, expected_status)
        if expected_fragment is not None:
            haystack = rest.decode("utf-8") if rest else body.decode("utf-8")
            self.assertIn(expected_fragment, haystack)

        next_backend = FakeClaudeChatBackend()
        next_backend.release.set()
        cg.BACKEND = next_backend

        same_status, _, same_body, _ = self.make_request(
            "POST",
            "/v1/chat/completions",
            payload=self.payload(user=same_user),
            headers=self.auth_headers(),
        )
        self.assertEqual(same_status, 200)
        self.assertEqual(json.loads(same_body.decode("utf-8"))["choices"][0]["message"]["content"], "reply text")

        other_status, _, other_body, _ = self.make_request(
            "POST",
            "/v1/chat/completions",
            payload=self.payload(user=other_user),
            headers=self.auth_headers(),
        )
        self.assertEqual(other_status, 200)
        self.assertEqual(json.loads(other_body.decode("utf-8"))["choices"][0]["message"]["content"], "reply text")
        return status, body, rest

    def test_nonstream_uses_latest_user_only_and_meetmate_session_prefix(self):
        backend = FakeClaudeChatBackend()
        backend.release.set()
        cg.BACKEND = backend

        status, headers, body, _ = self.make_request(
            "POST",
            "/v1/chat/completions",
            payload=self.payload(),
            headers=self.auth_headers(),
        )

        self.assertEqual(status, 200)
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["choices"][0]["message"]["content"], "reply text")
        self.assertEqual(payload["model"], "ignored-by-gateway")
        self.assertEqual(
            backend.calls,
            [("complete", "latest prompt", "meetmate:meet-123-agent-a", 7, None)],
        )

    def test_stream_returns_sse_heartbeats_finish_and_done(self):
        backend = FakeClaudeChatBackend()
        backend.release.set()
        backend.send_heartbeat = True
        cg.BACKEND = backend

        status, headers, _, rest = self.make_request(
            "POST",
            "/v1/chat/completions",
            payload=self.payload(stream=True),
            headers=self.auth_headers(),
        )

        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers["content-type"])
        text = rest.decode("utf-8")
        self.assertIn(": heartbeat", text)
        self.assertIn('"content": "hello"', text)
        self.assertIn('"finish_reason": "stop"', text)
        self.assertIn("data: [DONE]", text)

    def test_stream_failure_emits_error_event_without_done(self):
        backend = FakeClaudeChatBackend()
        backend.release.set()
        backend.stream_chunks = ["partial"]
        backend.stream_error = ClaudeStreamError("boom /Users/private/path")
        backend.stream_error_after_chunks = True
        status, _, rest = self.assert_released_after_stream_path(
            backend=backend,
            expected_fragment="event: error",
        )
        self.assertEqual(status, 200)
        text = rest.decode("utf-8")
        self.assertIn('"content": "partial"', text)
        self.assertIn("event: error", text)
        self.assertIn('"message": "chat completion failed"', text)
        self.assertNotIn("/Users/private/path", text)
        self.assertNotIn("data: [DONE]", text)

    def test_missing_configured_token_fails_closed(self):
        backend = FakeClaudeChatBackend()
        backend.release.set()
        cg.BACKEND = backend
        cg.CATY_OPENAI_CHAT_TOKEN = ""

        status, _, body, _ = self.make_request(
            "POST",
            "/v1/chat/completions",
            payload=self.payload(),
            headers=self.auth_headers(),
        )

        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body.decode("utf-8"))["error"]["code"], "chat_completions_unavailable")

    def test_bearer_auth_is_required_and_x_caty_token_is_not_enough(self):
        backend = FakeClaudeChatBackend()
        backend.release.set()
        cg.BACKEND = backend

        status, headers, body, _ = self.make_request(
            "POST",
            "/v1/chat/completions",
            payload=self.payload(),
            headers={"X-Caty-Agent-Trust": "trusted"},
        )
        self.assertEqual(status, 401)
        self.assertEqual(headers["www-authenticate"], "Bearer")
        self.assertEqual(json.loads(body.decode("utf-8"))["error"]["type"], "authentication_error")

        status, _, _, _ = self.make_request(
            "POST",
            "/v1/chat/completions",
            payload=self.payload(),
            headers={"X-Caty-Token": "test-token", "X-Caty-Agent-Trust": "trusted"},
        )
        self.assertEqual(status, 401)

    def test_chat_token_is_dedicated_and_trust_header_is_required(self):
        backend = FakeClaudeChatBackend()
        backend.release.set()
        cg.BACKEND = backend

        status, _, body, _ = self.make_request(
            "POST",
            "/v1/chat/completions",
            payload=self.payload(),
            headers={"Authorization": "Bearer secret-token", "X-Caty-Agent-Trust": "trusted"},
        )
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body.decode("utf-8"))["error"]["code"], "unauthorized")

        status, _, body, _ = self.make_request(
            "POST",
            "/v1/chat/completions",
            payload=self.payload(),
            headers={"Authorization": "Bearer test-token"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body.decode("utf-8"))["error"]["code"], "trusted_meeting_required")

    def test_validation_rejects_missing_user_and_empty_latest_message(self):
        backend = FakeClaudeChatBackend()
        backend.release.set()
        cg.BACKEND = backend

        status, _, body, _ = self.make_request(
            "POST",
            "/v1/chat/completions",
            payload=self.payload(user=""),
            headers=self.auth_headers(),
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body.decode("utf-8"))["error"]["code"], "missing_user")

        status, _, body, _ = self.make_request(
            "POST",
            "/v1/chat/completions",
            payload=self.payload(messages=[{"role": "user", "content": "   "}]),
            headers=self.auth_headers(),
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body.decode("utf-8"))["error"]["code"], "empty_user_message")

    def test_same_session_returns_nonblocking_409(self):
        backend = FakeClaudeChatBackend()
        cg.BACKEND = backend
        first_result = {}

        def first_call():
            first_result["response"] = self.make_request(
                "POST",
                "/v1/chat/completions",
                payload=self.payload(),
                headers=self.auth_headers(),
            )

        thread = threading.Thread(target=first_call)
        thread.start()
        self.assertTrue(backend.started.wait(timeout=1))

        status, _, body, _ = self.make_request(
            "POST",
            "/v1/chat/completions",
            payload=self.payload(),
            headers=self.auth_headers(),
        )
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body.decode("utf-8"))["error"]["code"], "session_busy")

        backend.release.set()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(first_result["response"][0], 200)

    def test_global_concurrency_limit_returns_429(self):
        backend = FakeClaudeChatBackend()
        cg.BACKEND = backend
        first_result = {}

        def first_call():
            first_result["response"] = self.make_request(
                "POST",
                "/v1/chat/completions",
                payload=self.payload(user="meet-123-agent-a"),
                headers=self.auth_headers(),
            )

        thread = threading.Thread(target=first_call)
        thread.start()
        self.assertTrue(backend.started.wait(timeout=1))

        status, _, body, _ = self.make_request(
            "POST",
            "/v1/chat/completions",
            payload=self.payload(user="meet-123-agent-b"),
            headers=self.auth_headers(),
        )
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(body.decode("utf-8"))["error"]["code"], "gateway_busy")

        backend.release.set()
        thread.join(timeout=2)
        self.assertEqual(first_result["response"][0], 200)

    def test_nonstream_timeout_and_failure_are_sanitized(self):
        backend = FakeClaudeChatBackend()
        backend.release.set()
        backend.complete_error = ClaudeStreamTimeout("timed out")
        cg.BACKEND = backend

        status, _, body, _ = self.make_request(
            "POST",
            "/v1/chat/completions",
            payload=self.payload(),
            headers=self.auth_headers(),
        )
        self.assertEqual(status, 504)
        self.assertEqual(json.loads(body.decode("utf-8"))["error"]["code"], "timeout")

        backend = FakeClaudeChatBackend()
        backend.release.set()
        backend.complete_error = ClaudeStreamError("boom /tmp/private")
        cg.BACKEND = backend

        status, _, body, _ = self.make_request(
            "POST",
            "/v1/chat/completions",
            payload=self.payload(),
            headers=self.auth_headers(),
        )
        self.assertEqual(status, 502)
        text = body.decode("utf-8")
        self.assertIn("chat completion failed", text)
        self.assertNotIn("/tmp/private", text)

        backend = FakeClaudeChatBackend()
        backend.release.set()
        backend.complete_text = ""
        cg.BACKEND = backend

        status, _, body, _ = self.make_request(
            "POST",
            "/v1/chat/completions",
            payload=self.payload(),
            headers=self.auth_headers(),
        )
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body.decode("utf-8"))["error"]["code"], "empty_response")

    def test_stream_cleanup_releases_session_and_global_slot(self):
        backend = FakeClaudeChatBackend()
        backend.release.set()
        backend.send_heartbeat = True
        self.assert_released_after_stream_path(
            backend=backend,
            output=BrokenPipeOnSSE(),
        )
        self.assertTrue(backend.stream_closed.wait(timeout=1))

        backend = FakeClaudeChatBackend()
        backend.release.set()
        backend.stream_error = ClaudeStreamTimeout("timed out")
        self.assert_released_after_stream_path(
            backend=backend,
            expected_fragment='"code": "timeout"',
        )
        self.assertTrue(backend.stream_closed.wait(timeout=1))


if __name__ == "__main__":
    unittest.main()
