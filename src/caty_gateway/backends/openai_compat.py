"""OpenAI-compatible chat completions backend for local and hosted servers."""

import http.client
import json
import re
from typing import Callable, Iterator, Optional
from urllib.parse import urlparse

from .base import Backend
from .openclaw import PTT_HINT


class OpenAICompatBackend(Backend):
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        voice_hint: str,
        log: Callable[..., None],
        is_no_reply: Callable[[str], bool],
        sanitize_for_tts: Callable[[str], str],
        read_history: Callable[[str], list],
        max_history_chars: int = 24000,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.voice_hint = voice_hint
        self.log = log
        self.is_no_reply = is_no_reply
        self.sanitize_for_tts = sanitize_for_tts
        self.read_history = read_history
        self.max_history_chars = max_history_chars

    def supports_stream(self) -> bool:
        return True

    def _voice_hint(self, route=None):
        return str(self.voice_hint) + (PTT_HINT if route == "ptt" else "")

    def _history_messages(self, session_id):
        if not session_id:
            return []
        try:
            turns = self.read_history(session_id)
        except Exception as e:
            self.log("openai-compat history read failed:", repr(e))
            return []

        messages = []
        for turn in turns or []:
            if not isinstance(turn, dict):
                continue
            role, text = turn.get("role"), turn.get("text")
            if role not in ("user", "assistant") or not isinstance(text, str) or not text.strip():
                continue
            messages.append({"role": role, "content": text})

        kept = []
        total = 0
        for message in reversed(messages):
            size = len(message["content"])
            if total + size > self.max_history_chars:
                break
            kept.append(message)
            total += size
        if not kept and messages:
            kept.append(messages[-1])
            self.log("openai-compat history newest turn exceeds character budget; keeping it:", len(messages[-1]["content"]))
        return list(reversed(kept))

    def _messages(self, user_text, session_id, route):
        history = self._history_messages(session_id) + [{"role": "user", "content": user_text}]
        normalized = []
        # NO_REPLY 後の孤立 user と厳格な交互ロールテンプレートを両立させる。
        for message in history:
            if normalized and normalized[-1]["role"] == message["role"]:
                normalized[-1]["content"] += "\n" + message["content"]
            else:
                normalized.append(dict(message))
        if normalized and normalized[0]["role"] == "assistant":
            normalized.pop(0)
        return [{"role": "system", "content": self._voice_hint(route)}] + normalized

    def _connection(self, timeout):
        parsed = urlparse(self.base_url)
        connection = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        return connection(parsed.hostname or "127.0.0.1", parsed.port or (443 if parsed.scheme == "https" else 80), timeout=timeout)

    def _endpoint(self):
        parsed = urlparse(self.base_url)
        return (parsed.path.rstrip("/") or "") + "/chat/completions"

    def _headers(self, stream=False):
        headers = {"Content-Type": "application/json"}
        if stream:
            headers["Accept"] = "text/event-stream"
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def generate(self, user_text: str, session_id: Optional[str] = None, timeout: int = 180, route: Optional[str] = None) -> str:
        body = json.dumps({
            "model": self.model,
            "stream": False,
            "messages": self._messages(user_text, session_id, route),
        }).encode("utf-8")
        conn = self._connection(timeout)
        try:
            conn.request("POST", self._endpoint(), body, self._headers())
            res = conn.getresponse()
            response_body = res.read()
            if res.status != 200:
                raise RuntimeError(f"openai-compat {res.status}: {response_body[:300]!r}")
            try:
                data = json.loads(response_body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                data = {}
            choices = data.get("choices") or [] if isinstance(data, dict) else []
            message = choices[0].get("message") if choices and isinstance(choices[0], dict) else {}
            reply = message.get("content") if isinstance(message, dict) else ""
            reply = reply if isinstance(reply, str) else ""
            if self.is_no_reply(reply):
                self.log("🤐 NO_REPLY — 無音スキップ")
                return ""
            return reply or "ごめん、うまく聞き取れなかったみたい。"
        finally:
            conn.close()

    def stream(self, text: str, session_id: Optional[str] = None, timeout: int = 180, route: Optional[str] = None) -> Iterator[str]:
        body = json.dumps({
            "model": self.model,
            "stream": True,
            "messages": self._messages(text, session_id, route),
        }).encode("utf-8")
        conn = self._connection(timeout)
        try:
            conn.request("POST", self._endpoint(), body, self._headers(stream=True))
            res = conn.getresponse()
            if res.status != 200:
                detail = res.read(300)
                raise RuntimeError(f"openai-compat {res.status}: {detail!r}")

            buf = ""
            pending = ""
            full = ""
            # SSE は行区切り。行ごとの decode なら UTF-8 のマルチバイト文字を割らない。
            for raw in res:
                line = raw.decode("utf-8", "ignore").rstrip("\r\n")
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].lstrip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except ValueError:
                    continue
                choices = obj.get("choices") or [] if isinstance(obj, dict) else []
                delta = ((choices[0].get("delta") if choices and isinstance(choices[0], dict) else {}) or {}).get("content") or ""
                if not isinstance(delta, str) or not delta:
                    continue
                buf += delta
                full += delta
                while True:
                    match = re.search(r"[。！？\n.!?]", buf)
                    if not match:
                        break
                    sentence = self.sanitize_for_tts(buf[:match.end()]).strip()
                    buf = buf[match.end():]
                    if not sentence or self.is_no_reply(sentence):
                        continue
                    if len(sentence) < 6:
                        pending = pending + sentence if pending else sentence
                        continue
                    if pending:
                        sentence, pending = pending + sentence, ""
                    yield sentence
            if not full:
                raise RuntimeError("brain_stream: content delta 未受信（空応答）")
            if self.is_no_reply(full):
                return
            rest = (pending or "") + buf
            if rest.strip():
                sentence = self.sanitize_for_tts(rest).strip()
                if sentence:
                    yield sentence
        finally:
            conn.close()
