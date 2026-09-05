import io
import json
import os
import socket
import sys
import unittest
import urllib.error
from unittest import mock


from caty_gateway import caty_gateway as cg
from caty_gateway.backends.hermes import HermesBackend
from caty_gateway.backends.openclaw import PTT_HINT


class FakeResponse:
    def __init__(self, body, status=200):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def getcode(self):
        return self.status

    def read(self):
        return self._body


class HermesBackendTest(unittest.TestCase):
    def backend(self, url="http://hermes.local:8642", api_key="secret", voice_hint="voice-hint\n"):
        return HermesBackend(
            url=url,
            api_key=api_key,
            voice_hint=voice_hint,
            log=lambda *args: None,
        )

    def response(self, text_parts):
        return json.dumps({
            "output": [
                {"content": [{"text": part} for part in text_parts[:1]]},
                {"content": [{"text": part} for part in text_parts[1:]]},
            ]
        }).encode("utf-8")

    def test_happy_path_posts_responses_body_headers_and_concatenates_reply(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse(self.response(["hello ", "from ", "hermes"]))

        with mock.patch("caty_gateway.backends.hermes.urllib.request.urlopen", side_effect=fake_urlopen):
            reply = self.backend().generate("こんにちは", "phone-a", 12)

        request = captured["request"]
        self.assertEqual(reply, "hello from hermes")
        self.assertEqual(request.full_url, "http://hermes.local:8642/v1/responses")
        self.assertEqual(captured["timeout"], 12)
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["input"], "こんにちは")
        self.assertEqual(body["instructions"], "voice-hint\n")
        self.assertEqual(body["conversation"], "caty-phone-a")

    def test_session_id_none_omits_conversation(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse(self.response(["ok"]))

        with mock.patch("caty_gateway.backends.hermes.urllib.request.urlopen", side_effect=fake_urlopen):
            self.backend().generate("hi", None, 5)

        self.assertNotIn("conversation", captured["body"])

    def test_ptt_route_appends_ptt_hint_but_plain_route_does_not(self):
        bodies = []

        def fake_urlopen(request, timeout):
            bodies.append(json.loads(request.data.decode("utf-8")))
            return FakeResponse(self.response(["ok"]))

        with mock.patch("caty_gateway.backends.hermes.urllib.request.urlopen", side_effect=fake_urlopen):
            self.backend().generate("push", "s1", 5, route="ptt")
            self.backend().generate("live", "s1", 5, route="live")

        self.assertIn(PTT_HINT, bodies[0]["instructions"])
        self.assertNotIn(PTT_HINT, bodies[1]["instructions"])

    def test_http_401_error_body_raises_runtime_error(self):
        body = b'{"error":{"code":"invalid_api_key"}}'
        error = urllib.error.HTTPError(
            "http://hermes.local:8642/v1/responses",
            401,
            "Unauthorized",
            {},
            io.BytesIO(body),
        )

        with mock.patch("caty_gateway.backends.hermes.urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "401.*invalid_api_key"):
                self.backend().generate("hi", "sid", 5)

    def test_timeout_and_urlerror_raise_runtime_error(self):
        with mock.patch("caty_gateway.backends.hermes.urllib.request.urlopen", side_effect=socket.timeout()):
            with self.assertRaisesRegex(RuntimeError, "タイムアウト"):
                self.backend().generate("hi", "sid", 5)

        with mock.patch("caty_gateway.backends.hermes.urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            with self.assertRaisesRegex(RuntimeError, "接続失敗"):
                self.backend().generate("hi", "sid", 5)

    def test_empty_output_raises_runtime_error(self):
        with mock.patch("caty_gateway.backends.hermes.urllib.request.urlopen", return_value=FakeResponse(b'{"output":[]}')):
            with self.assertRaisesRegex(RuntimeError, "空"):
                self.backend().generate("hi", "sid", 5)

    def test_health_false_after_failure_and_when_api_key_empty(self):
        backend = self.backend()
        self.assertTrue(backend.health())

        with mock.patch("caty_gateway.backends.hermes.urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            with self.assertRaises(RuntimeError):
                backend.generate("hi", "sid", 5)
        self.assertFalse(backend.health())

        self.assertFalse(self.backend(api_key="").health())

    def test_factory_constructs_with_key_and_empty_key_fails_fast(self):
        original = (
            cg.BACKEND_NAME,
            cg.CATY_HERMES_URL,
            cg.CATY_HERMES_API_KEY,
        )
        try:
            cg.BACKEND_NAME = "hermes"
            cg.CATY_HERMES_URL = "http://127.0.0.1:8642"
            cg.CATY_HERMES_API_KEY = "x"
            backend = cg._build_backend()
            self.assertIsInstance(backend, HermesBackend)

            cg.CATY_HERMES_API_KEY = ""
            with self.assertRaisesRegex(RuntimeError, "CATY_HERMES_API_KEY"):
                cg._build_backend()
        finally:
            (
                cg.BACKEND_NAME,
                cg.CATY_HERMES_URL,
                cg.CATY_HERMES_API_KEY,
            ) = original

    def test_member_backends_default_to_thin_hint_not_caty_persona(self):
        # CATY_VOICE_HINT 未設定時、hermes/claude の instructions/system prompt に
        # Caty のフル行動指示（DEFAULT_VOICE_HINT）が流れないこと（本人モード invariant）
        original = (
            cg.BACKEND_NAME,
            cg.CATY_HERMES_URL,
            cg.CATY_HERMES_API_KEY,
        )
        try:
            cg.BACKEND_NAME = "hermes"
            cg.CATY_HERMES_URL = "http://127.0.0.1:8642"
            cg.CATY_HERMES_API_KEY = "x"
            backend = cg._build_backend()
            self.assertEqual(backend.voice_hint, cg.MEMBER_VOICE_HINT)
            self.assertNotIn("caty-watch", backend.voice_hint)
            self.assertNotIn("Slack", backend.voice_hint)

            cg.BACKEND_NAME = "claude"
            claude_backend = cg._build_backend()
            self.assertEqual(claude_backend.voice_hint, cg.MEMBER_VOICE_HINT)
        finally:
            (
                cg.BACKEND_NAME,
                cg.CATY_HERMES_URL,
                cg.CATY_HERMES_API_KEY,
            ) = original


if __name__ == "__main__":
    unittest.main()
