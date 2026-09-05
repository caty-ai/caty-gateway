"""OpenClawBackend の session key 正規化テスト（#956/#957）。

CatyPhone の session id は大文字。openclaw の openai-compat store は session key を
「全体を小文字化して保存 / 生値で lookup」するため、大文字キーは毎ターン miss →
新規セッション化して文脈が断絶する。修正は「openclaw へ送るキーを全体小文字化」する（#957）。
ここでは generate() の --session-key cmd と stream() の x-openclaw-session-key ヘッダに
実際に載る値を mock HTTP / mock subprocess で検証する（実装の再実装ではなく wire 契約の検証）。
"""

import json
import os
import sys
import unittest
from unittest import mock


from caty_gateway.backends.openclaw import OpenClawBackend


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
        FakeConnection.calls.append((self.host, self.port, self.timeout, method, path, body, headers))

    def getresponse(self):
        return FakeConnection.responses.pop(0)

    def close(self):
        FakeConnection.closed += 1


def sse(content):
    return b"data: " + json.dumps({"choices": [{"delta": {"content": content}}]}).encode() + b"\n"


def ok_run(captured):
    """generate() が呼ぶ module 関数 run(cmd, timeout) の fake。cmd を captured に記録し成功を返す。"""
    def _run(cmd, timeout):
        captured["cmd"] = cmd
        captured.setdefault("calls", []).append(list(cmd))
        return 0, '{"result":{"payloads":[{"text":"ok"}]}}', ""
    return _run


class OpenClawBackendSessionKeyTest(unittest.TestCase):
    def setUp(self):
        FakeConnection.responses = []
        FakeConnection.calls = []
        FakeConnection.closed = 0

    def backend(self, **kwargs):
        defaults = dict(
            openclaw_bin="/usr/bin/openclaw",
            agent="main",
            voice_hint="voice-hint\n",
            session_key_prefix="caty-",
            log=lambda *args: None,
            is_no_reply=lambda text: text == "NO_REPLY",
            sanitize_for_tts=lambda text: text,
            resolve_session=lambda sid: None,
        )
        defaults.update(kwargs)
        return OpenClawBackend(**defaults)

    def last_headers(self):
        return FakeConnection.calls[-1][6]

    def cmd_session_key(self, cmd):
        """cmd リストから --session-key の値を取り出す。存在しなければ assertion 失敗にする。"""
        self.assertIn("--session-key", cmd)
        return cmd[cmd.index("--session-key") + 1]

    # --- 純関数レベル（HTTP 不要） -------------------------------------------

    def test_session_key_lowercases_uppercase_fallback(self):
        """resolve_session が None の fallback 経路で送出キーが全体小文字化される。
        sid charset（[A-Za-z0-9-]）のハイフンは小文字化で不変のまま通ることも確認。"""
        self.assertEqual(
            self.backend()._session_key("ABC123DEF456"),
            "agent:main:caty-abc123def456",
        )
        self.assertEqual(
            self.backend()._session_key("ABC-123-DEF"),
            "agent:main:caty-abc-123-def",
        )

    def test_session_key_is_idempotent_for_lowercase_and_mixed_case(self):
        """既に小文字 / mixed-case の sid でも同じ小文字キーになる（冪等・回帰防止）。"""
        backend = self.backend()
        self.assertEqual(backend._session_key("abc123def456"), "agent:main:caty-abc123def456")
        self.assertEqual(backend._session_key("AbC123DeF456"), "agent:main:caty-abc123def456")

    def test_session_key_lowercases_uppercase_agent_and_prefix(self):
        """agent / prefix が env で大文字設定されても命中するよう、キー全体が小文字化される。
        （sid のみ小文字化だと残る miss をレビュー3系統が指摘 → 全体小文字化で解消）"""
        backend = self.backend(agent="Main", session_key_prefix="caty-")
        self.assertEqual(backend._session_key("ABC123"), "agent:main:caty-abc123")

    def test_session_key_uses_resolved_native_key_verbatim(self):
        """links.json 由来の native キーは無改変（小文字化しない）— 不変条件1。
        native 値は openclaw の store 由来で既に正規形。ここでは大文字を含む合成値を使い、
        『小文字化が適用されていない（＝verbatim 通過）』ことを証明する。"""
        backend = self.backend(resolve_session=lambda sid: "agent:main:native-UPPER-123")
        self.assertEqual(backend._session_key("ABC123"), "agent:main:native-UPPER-123")

    def test_resolve_session_receives_original_uppercase_sid(self):
        """resolve_session には元の（小文字化前の）sid が渡る — links.json は元 sid でキーされるため。
        ここで sid を先に小文字化してしまうと link lookup が miss する回帰を防ぐ（不変条件1）。"""
        seen = {}

        def resolver(sid):
            seen["sid"] = sid
            return None

        self.backend(resolve_session=resolver)._session_key("ABC123DEF456")
        self.assertEqual(seen["sid"], "ABC123DEF456")

    def test_session_key_stringifies_and_lowercases_non_string(self):
        """非文字列 sid は f-string で文字列化 → 全体小文字化される。cased な __str__ を使い、
        小文字化が実際に効いていること（.lower() を外すと落ちること）を観測可能にする。"""

        class Sid:
            def __str__(self):
                return "AbC123"

        self.assertEqual(self.backend()._session_key(Sid()), "agent:main:caty-abc123")
        # cased 文字を含まない素の非文字列でも例外を起こさない。
        self.assertEqual(self.backend()._session_key(12345), "agent:main:caty-12345")

    def test_none_sid_sends_no_session_key(self):
        """caller の `if session_id:` ガードにより、None/空 sid では session key を一切送らない
        契約を lock する（generate の --session-key も stream のヘッダも付かない）。"""
        captured = {}
        with mock.patch("caty_gateway.backends.openclaw.run", ok_run(captured)):
            self.assertEqual(self.backend().generate("hi", None), "ok")
        self.assertNotIn("--session-key", captured["cmd"])
        self._stream_once(self.backend(), None)
        self.assertNotIn("x-openclaw-session-key", self.last_headers())

    # --- stream(): x-openclaw-session-key ヘッダ ------------------------------

    def _stream_once(self, backend, sid):
        FakeConnection.responses = [FakeResponse(lines=[sse("これはテスト応答です。"), b"data: [DONE]\n"])]
        with mock.patch("caty_gateway.backends.openclaw.http.client.HTTPConnection", FakeConnection), \
                mock.patch("caty_gateway.backends.openclaw._resolve_gateway_token", lambda: "tok"):
            return list(backend.stream("hi", sid))

    def test_stream_lowercases_uppercase_sid_in_header(self):
        out = self._stream_once(self.backend(), "ABC123DEF456")
        self.assertEqual(out, ["これはテスト応答です。"])
        self.assertEqual(self.last_headers()["x-openclaw-session-key"], "agent:main:caty-abc123def456")

    def test_stream_sends_same_key_across_consecutive_turns(self):
        """同一 sid での2連続ターンが同一の小文字キーを送る（#956 の『毎ターン新規セッション』回帰防止）。"""
        backend = self.backend()
        self._stream_once(backend, "ABC123DEF456")
        key_turn1 = self.last_headers()["x-openclaw-session-key"]
        self._stream_once(backend, "ABC123DEF456")
        key_turn2 = self.last_headers()["x-openclaw-session-key"]
        self.assertEqual(key_turn1, "agent:main:caty-abc123def456")
        self.assertEqual(key_turn1, key_turn2)

    def test_stream_keeps_resolved_native_key_in_header(self):
        backend = self.backend(resolve_session=lambda sid: "agent:main:native-UPPER-123")
        self._stream_once(backend, "ABC123DEF456")
        self.assertEqual(self.last_headers()["x-openclaw-session-key"], "agent:main:native-UPPER-123")

    # --- generate(): --session-key cmd 引数 -----------------------------------

    def test_generate_lowercases_uppercase_sid_in_cmd(self):
        captured = {}
        with mock.patch("caty_gateway.backends.openclaw.run", ok_run(captured)):
            reply = self.backend().generate("hi", "ABC123DEF456")
        self.assertEqual(reply, "ok")
        self.assertEqual(self.cmd_session_key(captured["cmd"]), "agent:main:caty-abc123def456")

    def test_generate_sends_same_key_across_consecutive_turns(self):
        """generate 経路でも2連続ターンが同一小文字キーを送る（両経路一貫・不変条件3）。"""
        captured = {}
        backend = self.backend()
        with mock.patch("caty_gateway.backends.openclaw.run", ok_run(captured)):
            backend.generate("hi", "ABC123DEF456")
            backend.generate("hey", "ABC123DEF456")
        keys = [self.cmd_session_key(c) for c in captured["calls"]]
        self.assertEqual(keys, ["agent:main:caty-abc123def456", "agent:main:caty-abc123def456"])

    def test_generate_keeps_resolved_native_key_in_cmd(self):
        captured = {}
        backend = self.backend(resolve_session=lambda sid: "agent:main:native-UPPER-123")
        with mock.patch("caty_gateway.backends.openclaw.run", ok_run(captured)):
            backend.generate("hi", "ABC123DEF456")
        self.assertEqual(self.cmd_session_key(captured["cmd"]), "agent:main:native-UPPER-123")

    # --- 両経路一致（不変条件3 を直接 lock） ---------------------------------

    def test_generate_and_stream_send_identical_key_for_same_sid(self):
        """generate(legacy) の --session-key と stream() の x-openclaw-session-key が、
        同一 sid に対して完全に同じ値になる（両経路が単一の _session_key を共有する保証）。"""
        captured = {}
        with mock.patch("caty_gateway.backends.openclaw.run", ok_run(captured)):
            self.backend().generate("hi", "ABC123DEF456")
        cmd_key = self.cmd_session_key(captured["cmd"])
        self._stream_once(self.backend(), "ABC123DEF456")
        header_key = self.last_headers()["x-openclaw-session-key"]
        self.assertEqual(cmd_key, header_key)
        self.assertEqual(cmd_key, "agent:main:caty-abc123def456")


if __name__ == "__main__":
    unittest.main()
