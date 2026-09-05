import json
import os
import sys
import tempfile
import unittest
import uuid
from unittest import mock


from caty_gateway import session_links
from caty_gateway.backends.claude import CLAUDE_SESSION_NAMESPACE, ClaudeCodeBackend
from caty_gateway.backends.hermes import HermesBackend
from caty_gateway.backends.openclaw import OpenClawBackend


class FakeRun:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, cmd, timeout):
        self.calls.append((list(cmd), timeout))
        rc, out, err = self.responses.pop(0)
        return rc, out, err


class FakeHTTPResponse:
    status = 200

    def read(self, size=-1):
        return b""

    def __iter__(self):
        return iter([
            b'data: {"choices":[{"delta":{"content":"Linked."}}]}\n',
            b"data: [DONE]\n",
        ])


class FakeHTTPConnection:
    requests = []

    def __init__(self, host, port=None, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout

    def request(self, method, path, body=None, headers=None):
        FakeHTTPConnection.requests.append((method, path, body, headers or {}))

    def getresponse(self):
        return FakeHTTPResponse()

    def close(self):
        pass


class FakeHermesResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return b'{"output":[{"content":[{"text":"ok"}]}]}'


class SessionLinksTest(unittest.TestCase):
    def test_put_get_find_by_native_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"CATY_HISTORY_DIR": tmp}, clear=False):
                session_links.put("phone-a", "claude", "native-a")

                self.assertEqual(
                    session_links.get("phone-a"),
                    {"backend": "claude", "native": "native-a"},
                )
                self.assertEqual(session_links.find_by_native("native-a"), "phone-a")
                self.assertIsNone(session_links.get("missing"))
                self.assertIsNone(session_links.find_by_native("missing"))

    def test_history_dir_unset_is_noop(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(session_links.get("phone-a"))
            session_links.put("phone-a", "claude", "native-a")
            self.assertIsNone(session_links.find_by_native("native-a"))

    def test_put_creates_valid_json_file_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"CATY_HISTORY_DIR": tmp}, clear=False):
                session_links.put("phone-a", "openclaw", "agent:main:native-a")

                path = os.path.join(tmp, "links.json")
                self.assertTrue(os.path.exists(path))
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.assertEqual(data["links"]["phone-a"]["backend"], "openclaw")
                self.assertEqual(data["links"]["phone-a"]["native"], "agent:main:native-a")

    def test_put_overwrites_existing_sid(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"CATY_HISTORY_DIR": tmp}, clear=False):
                session_links.put("phone-a", "openclaw", "native-a")
                session_links.put("phone-a", "hermes", "native-b")

                self.assertEqual(
                    session_links.get("phone-a"),
                    {"backend": "hermes", "native": "native-b"},
                )
                self.assertIsNone(session_links.find_by_native("native-a"))
                self.assertEqual(session_links.find_by_native("native-b"), "phone-a")


class BackendSessionResolverTest(unittest.TestCase):
    def openclaw_backend(self, **kwargs):
        params = {
            "openclaw_bin": "openclaw",
            "agent": "main",
            "voice_hint": "voice-hint\n",
            "session_key_prefix": "caty-",
            "log": lambda *args: None,
            "is_no_reply": lambda text: False,
            "sanitize_for_tts": lambda text: text,
        }
        params.update(kwargs)
        return OpenClawBackend(**params)

    def claude_backend(self, **kwargs):
        params = {
            "claude_bin": "/tmp/claude",
            "model": "",
            "cwd": "/tmp",
            "voice_hint": "voice-hint\n",
            "log": lambda *args: None,
        }
        params.update(kwargs)
        return ClaudeCodeBackend(**params)

    def hermes_backend(self, **kwargs):
        params = {
            "url": "http://hermes.local:8642",
            "api_key": "secret",
            "voice_hint": "voice-hint\n",
            "log": lambda *args: None,
        }
        params.update(kwargs)
        return HermesBackend(**params)

    def test_openclaw_default_and_none_resolver_are_byte_identical(self):
        self.assertEqual(
            self.openclaw_backend()._session_key("sid-1"),
            "agent:main:caty-sid-1",
        )
        self.assertEqual(
            self.openclaw_backend(resolve_session=lambda sid: None)._session_key("sid-1"),
            "agent:main:caty-sid-1",
        )

    def test_openclaw_resolver_returns_native_key_verbatim_for_generate_and_stream(self):
        backend = self.openclaw_backend(resolve_session=lambda sid: "native-openclaw-key")
        self.assertEqual(backend._session_key("sid-1"), "native-openclaw-key")

        fake = FakeRun([(0, json.dumps({"result": {"payloads": [{"text": "ok"}]}}), "")])
        FakeHTTPConnection.requests = []
        with (
            mock.patch("caty_gateway.backends.openclaw.run", side_effect=fake),
            mock.patch("caty_gateway.backends.openclaw._resolve_gateway_token", return_value="token"),
            mock.patch("caty_gateway.backends.openclaw.http.client.HTTPConnection", FakeHTTPConnection),
        ):
            self.assertEqual(backend.generate("hi", "sid-1", 5), "ok")
            self.assertEqual(list(backend.stream("hi", "sid-1", 5)), ["Linked."])

        cmd = fake.calls[0][0]
        self.assertEqual(cmd[cmd.index("--session-key") + 1], "native-openclaw-key")
        self.assertEqual(FakeHTTPConnection.requests[0][3]["x-openclaw-session-key"], "native-openclaw-key")

    def test_claude_resolved_uuid_is_verbatim_and_known(self):
        backend = self.claude_backend(resolve_session=lambda sid: "native-claude-uuid")

        self.assertEqual(backend._session_uuid("sid-1"), "native-claude-uuid")
        self.assertIn("native-claude-uuid", backend._known_sessions)

    def test_claude_unresolved_uuid_derivation_is_unchanged(self):
        backend = self.claude_backend(resolve_session=lambda sid: None)
        expected = str(uuid.uuid5(CLAUDE_SESSION_NAMESPACE, "caty-sid-1"))

        self.assertEqual(backend._session_uuid("sid-1"), expected)
        self.assertNotIn(expected, backend._known_sessions)

    def test_hermes_resolved_conversation_and_default_conversation(self):
        bodies = []

        def fake_urlopen(request, timeout):
            bodies.append(json.loads(request.data.decode("utf-8")))
            return FakeHermesResponse()

        with mock.patch("caty_gateway.backends.hermes.urllib.request.urlopen", side_effect=fake_urlopen):
            self.hermes_backend(resolve_session=lambda sid: "native-hermes-conversation").generate("hi", "sid-1", 5)
            self.hermes_backend(resolve_session=lambda sid: None).generate("hi", "sid-1", 5)

        self.assertEqual(bodies[0]["conversation"], "native-hermes-conversation")
        self.assertEqual(bodies[1]["conversation"], "caty-sid-1")


if __name__ == "__main__":
    unittest.main()
