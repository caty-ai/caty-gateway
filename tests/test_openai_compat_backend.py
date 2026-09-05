import json
import os
import sys
import unittest
from unittest import mock


from caty_gateway import caty_gateway as cg
from caty_gateway.backends.openai_compat import OpenAICompatBackend
from caty_gateway.backends.openclaw import PTT_HINT


class FakeResponse:
    def __init__(self, body=b"", status=200, lines=()):
        self.status = status
        self.body = body
        self.lines = list(lines)

    def read(self, amount=None):
        return self.body if amount is None else self.body[:amount]

    def __iter__(self):
        return iter(self.lines)


class FakeConnection:
    responses = []
    calls = []
    closed = 0

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout

    def request(self, method, path, body, headers):
        self.calls.append((self.host, self.port, self.timeout, method, path, body, headers))

    def getresponse(self):
        return self.responses.pop(0)

    def close(self):
        self.closed += 1


def completion(text):
    return json.dumps({"choices": [{"message": {"content": text}}]}).encode()


def sse(content):
    return b"data: " + json.dumps({"choices": [{"delta": {"content": content}}]}).encode() + b"\n"


class OpenAICompatBackendTest(unittest.TestCase):
    def setUp(self):
        FakeConnection.responses = []
        FakeConnection.calls = []
        FakeConnection.closed = 0

    def backend(self, **kwargs):
        defaults = dict(
            base_url="http://brain.local:8080/v1/",
            model="local-model",
            api_key="secret",
            voice_hint="voice-hint\n",
            log=lambda *args: None,
            is_no_reply=lambda text: text == "NO_REPLY",
            sanitize_for_tts=lambda text: "clean:" + text,
            read_history=lambda sid: [],
            max_history_chars=24000,
        )
        defaults.update(kwargs)
        return OpenAICompatBackend(**defaults)

    def request(self):
        call = FakeConnection.calls[-1]
        return call, json.loads(call[5].decode())

    def test_generate_posts_history_and_omits_empty_auth(self):
        history = [{"role": "user", "text": "old user"}, {"role": "assistant", "text": "old assistant"}]
        FakeConnection.responses = [FakeResponse(completion("reply")), FakeResponse(completion("reply"))]
        with mock.patch("caty_gateway.backends.openai_compat.http.client.HTTPConnection", FakeConnection):
            self.assertEqual(self.backend(read_history=lambda sid: history).generate("now", "s1", 12), "reply")
            _, body = self.request()
            self.assertEqual(body, {"model": "local-model", "stream": False, "messages": [
                {"role": "system", "content": "voice-hint\n"},
                {"role": "user", "content": "old user"},
                {"role": "assistant", "content": "old assistant"},
                {"role": "user", "content": "now"},
            ]})
            self.assertEqual(self.request()[0][6]["Authorization"], "Bearer secret")
            self.assertNotIn("Accept", self.request()[0][6])
            self.backend(api_key="").generate("now", None, 3)
            self.assertNotIn("Authorization", self.request()[0][6])

    def test_generate_trims_newest_history_and_history_errors_do_not_break_turn(self):
        history = [{"role": "user", "text": "old"}, {"role": "assistant", "text": "newest"}]
        FakeConnection.responses = [FakeResponse(completion("ok")), FakeResponse(completion("ok"))]
        with mock.patch("caty_gateway.backends.openai_compat.http.client.HTTPConnection", FakeConnection):
            self.backend(read_history=lambda sid: history, max_history_chars=6).generate("now", "s1")
            self.assertEqual(self.request()[1]["messages"], [{"role": "system", "content": "voice-hint\n"}, {"role": "user", "content": "now"}])
            self.assertEqual(self.backend(read_history=lambda sid: (_ for _ in ()).throw(OSError("gone"))).generate("now", "s1"), "ok")
            self.assertEqual(self.request()[1]["messages"], [{"role": "system", "content": "voice-hint\n"}, {"role": "user", "content": "now"}])

    def test_messages_normalize_orphan_turns_and_preserve_plain_shape(self):
        history = [
            {"role": "user", "text": "first"},
            {"role": "user", "text": "orphan"},
            {"role": "assistant", "text": "answer"},
            {"role": "user", "text": "trailing"},
        ]
        messages = self.backend(read_history=lambda sid: history)._messages("now", "s1", None)
        self.assertEqual(messages, [
            {"role": "system", "content": "voice-hint\n"},
            {"role": "user", "content": "first\norphan"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "trailing\nnow"},
        ])
        self.assertEqual([message["role"] for message in messages[1:]], ["user", "assistant", "user"])
        self.assertEqual(
            self.backend()._messages("now", None, None),
            [{"role": "system", "content": "voice-hint\n"}, {"role": "user", "content": "now"}],
        )

    def test_history_keeps_oversized_newest_turn_and_logs_it(self):
        log = mock.Mock()
        backend = self.backend(
            read_history=lambda sid: [{"role": "user", "text": "much too long"}],
            max_history_chars=3,
            log=log,
        )
        self.assertEqual(backend._history_messages("s1"), [{"role": "user", "content": "much too long"}])
        log.assert_called_once()

    def test_generate_error_no_reply_and_empty_fallback(self):
        FakeConnection.responses = [FakeResponse(b"bad", 429), FakeResponse(completion("NO_REPLY")), FakeResponse(completion(""))]
        with mock.patch("caty_gateway.backends.openai_compat.http.client.HTTPConnection", FakeConnection):
            with self.assertRaisesRegex(RuntimeError, "openai-compat 429"):
                self.backend().generate("now")
            self.assertEqual(self.backend().generate("now"), "")
            self.assertEqual(self.backend().generate("now"), "ごめん、うまく聞き取れなかったみたい。")

    def test_stream_sse_sentences_tail_and_ptt_hint(self):
        FakeConnection.responses = [FakeResponse(lines=[sse("これは一文。次"), sse("です！尾"), b"data: [DONE]\n"])]
        with mock.patch("caty_gateway.backends.openai_compat.http.client.HTTPConnection", FakeConnection):
            self.assertEqual(list(self.backend().stream("now", "s1", route="ptt")), ["clean:これは一文。", "clean:次です！", "clean:尾"])
        _, body = self.request()
        self.assertIn(PTT_HINT, body["messages"][0]["content"])
        self.assertEqual(self.request()[0][6]["Accept"], "text/event-stream")

    def test_stream_short_merge_no_reply_and_robust_skips(self):
        FakeConnection.responses = [
            FakeResponse(lines=[sse("はい。これは長い"), sse("です。尾")]),
            FakeResponse(lines=[sse("NO_REPLY")]),
            FakeResponse(lines=[b"data: {bad}\n", b"data: {\"choices\": []}\n", sse("ok。"), b"data: [DONE]\n", sse("ignored。")] ),
        ]
        with mock.patch("caty_gateway.backends.openai_compat.http.client.HTTPConnection", FakeConnection):
            self.assertEqual(list(self.backend(sanitize_for_tts=lambda text: text).stream("now")), ["はい。これは長いです。", "尾"])
            self.assertEqual(list(self.backend().stream("now")), [])
            self.assertEqual(list(self.backend().stream("now")), ["clean:ok。"])

    def test_stream_accepts_crlf_no_space_newlines_and_pending_flush(self):
        crlf = b"data:" + json.dumps({"choices": [{"delta": {"content": "long\n"}}]}).encode() + b"\r\n"
        FakeConnection.responses = [
            FakeResponse(lines=[crlf, b"data: [DONE]\r\n"]),
            FakeResponse(lines=[sse("a。"), sse("b。"), sse("this is long."), b"data: [DONE]\n"]),
            FakeResponse(lines=[sse("tiny。"), b"data: [DONE]\n"]),
        ]
        with mock.patch("caty_gateway.backends.openai_compat.http.client.HTTPConnection", FakeConnection):
            self.assertEqual(list(self.backend(sanitize_for_tts=lambda text: text).stream("now", timeout=23)), ["long"])
            self.assertEqual(self.request()[0][2], 23)
            self.assertEqual(list(self.backend(sanitize_for_tts=lambda text: text).stream("now")), ["a。b。this is long."])
            self.assertEqual(list(self.backend(sanitize_for_tts=lambda text: text).stream("now")), ["tiny。"])

    def test_stream_empty_and_non_200_raise(self):
        FakeConnection.responses = [FakeResponse(lines=[b"data: [DONE]\n"]), FakeResponse(b"no", 500)]
        with mock.patch("caty_gateway.backends.openai_compat.http.client.HTTPConnection", FakeConnection):
            with self.assertRaisesRegex(RuntimeError, "content delta"):
                list(self.backend().stream("now"))
            with self.assertRaisesRegex(RuntimeError, "openai-compat 500"):
                list(self.backend().stream("now"))

    def test_https_and_factory_selector(self):
        FakeConnection.responses = [FakeResponse(completion("ok"))]
        with mock.patch("caty_gateway.backends.openai_compat.http.client.HTTPSConnection", FakeConnection):
            self.assertEqual(self.backend(base_url="https://remote.example/api/v1").generate("now", timeout=7), "ok")
        self.assertEqual(self.request()[0][0:5], ("remote.example", 443, 7, "POST", "/api/v1/chat/completions"))

        original = (cg.BACKEND_NAME, cg.CATY_OPENAI_BASE_URL, cg.CATY_OPENAI_MODEL, cg.CATY_OPENAI_MAX_HISTORY_CHARS)
        try:
            cg.BACKEND_NAME, cg.CATY_OPENAI_BASE_URL, cg.CATY_OPENAI_MODEL = "openai-compat", "http://localhost:11434/v1", "llama"
            self.assertIsInstance(cg._build_backend(), OpenAICompatBackend)
            cg.CATY_OPENAI_BASE_URL = ""
            with self.assertRaisesRegex(RuntimeError, "CATY_OPENAI_BASE_URL"):
                cg._build_backend()
            cg.CATY_OPENAI_BASE_URL, cg.CATY_OPENAI_MODEL = "http://localhost:11434/v1", ""
            with self.assertRaisesRegex(RuntimeError, "CATY_OPENAI_MODEL"):
                cg._build_backend()
        finally:
            cg.BACKEND_NAME, cg.CATY_OPENAI_BASE_URL, cg.CATY_OPENAI_MODEL, cg.CATY_OPENAI_MAX_HISTORY_CHARS = original

    def test_factory_validates_openai_configuration(self):
        original = (cg.BACKEND_NAME, cg.CATY_OPENAI_BASE_URL, cg.CATY_OPENAI_MODEL, cg.CATY_OPENAI_MAX_HISTORY_CHARS)
        try:
            cg.BACKEND_NAME, cg.CATY_OPENAI_MODEL = "openai-compat", "llama"
            cg.CATY_OPENAI_BASE_URL, cg.CATY_OPENAI_MAX_HISTORY_CHARS = "localhost:11434/v1", "not-a-number"
            with self.assertRaisesRegex(RuntimeError, "CATY_OPENAI_BASE_URL.*localhost"):
                cg._build_backend()
            cg.CATY_OPENAI_BASE_URL = "https://remote.example/v1"
            with self.assertRaisesRegex(RuntimeError, "CATY_OPENAI_MAX_HISTORY_CHARS"):
                cg._build_backend()
            cg.CATY_OPENAI_MAX_HISTORY_CHARS = "24000"
            self.assertIsInstance(cg._build_backend(), OpenAICompatBackend)
        finally:
            cg.BACKEND_NAME, cg.CATY_OPENAI_BASE_URL, cg.CATY_OPENAI_MODEL, cg.CATY_OPENAI_MAX_HISTORY_CHARS = original

    def test_generate_falls_back_for_malformed_success_bodies(self):
        FakeConnection.responses = [
            FakeResponse(b"not json"),
            FakeResponse(json.dumps({"choices": "not-a-list"}).encode()),
            FakeResponse(json.dumps({"choices": [{"message": "not-a-dict"}]}).encode()),
            FakeResponse(json.dumps({"choices": [{"message": {"content": ["not-a-string"]}}]}).encode()),
        ]
        with mock.patch("caty_gateway.backends.openai_compat.http.client.HTTPConnection", FakeConnection):
            for _ in range(4):
                self.assertEqual(self.backend().generate("now"), "ごめん、うまく聞き取れなかったみたい。")

    def test_endpoint_without_path_and_explicit_port(self):
        FakeConnection.responses = [FakeResponse(completion("ok")), FakeResponse(completion("ok"))]
        with mock.patch("caty_gateway.backends.openai_compat.http.client.HTTPConnection", FakeConnection):
            self.backend(base_url="http://host").generate("now")
            self.assertEqual(self.request()[0][0:5], ("host", 80, 180, "POST", "/chat/completions"))
            self.backend(base_url="http://host:1234/v1").generate("now")
            self.assertEqual(self.request()[0][0:5], ("host", 1234, 180, "POST", "/v1/chat/completions"))


if __name__ == "__main__":
    unittest.main()
