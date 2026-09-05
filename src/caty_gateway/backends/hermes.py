import json
import socket
import time
import urllib.error
import urllib.request
from typing import Callable, Iterator, Optional
from urllib.parse import urlparse

from .base import Backend
from .openclaw import PTT_HINT


class HermesBackend(Backend):
    def __init__(
        self,
        url: str,
        api_key: str,
        voice_hint: str,
        log: Callable[..., None],
        resolve_session: Callable[[str], Optional[str]] = None,
    ):
        self.url = url.rstrip("/")
        self.api_key = api_key.strip()
        self.voice_hint = voice_hint
        self.log = log
        self.resolve_session = resolve_session or (lambda sid: None)
        self._last_runtime_error_at = 0.0

    def supports_stream(self) -> bool:
        return False

    def health(self) -> bool:
        if not self.api_key:
            return False
        if not self._valid_url():
            return False
        return (time.monotonic() - self._last_runtime_error_at) >= 30

    def _valid_url(self) -> bool:
        parsed = urlparse(self.url)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)

    def _instructions(self, route=None):
        return self.voice_hint + (PTT_HINT if route == "ptt" else "")

    def _runtime_error(self, message: str) -> RuntimeError:
        self._last_runtime_error_at = time.monotonic()
        self.log("⚠️ hermes backend:", message[:300])
        return RuntimeError(message[:300])

    def _extract_reply(self, data) -> str:
        texts = []
        output = data.get("output") if isinstance(data, dict) else None
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        texts.append(part["text"])
        reply = "".join(texts).strip()
        if not reply:
            raise self._runtime_error("hermes応答が空です")
        return reply

    def generate(self, text: str, session_id: Optional[str], timeout: int, route: Optional[str] = None) -> str:
        if not self.api_key:
            raise self._runtime_error("CATY_HERMES_API_KEY is required")
        if not self._valid_url():
            # URL は userinfo（user:pass@）を含み得るので値そのものはエラーに載せない
            raise self._runtime_error("invalid CATY_HERMES_URL (http/https の絶対URLが必要)")

        body = {
            "input": text,
            "instructions": self._instructions(route),
        }
        if session_id is not None:
            body["conversation"] = self.resolve_session(session_id) or f"caty-{session_id}"

        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.url}/v1/responses",
            data=data,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            # 非2xx は urlopen が HTTPError を投げる（下の except が正規経路）
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read()
            try:
                parsed = json.loads(payload.decode("utf-8"))
            except Exception as e:
                raise self._runtime_error(f"hermes JSON解析失敗: {e}") from e
            return self._extract_reply(parsed)
        except urllib.error.HTTPError as e:
            detail = e.read(300).decode("utf-8", "replace").strip()
            raise self._runtime_error(f"hermes HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise self._runtime_error(f"hermes接続失敗: {e.reason}") from e
        except socket.timeout as e:
            raise self._runtime_error("hermesタイムアウト") from e

    def stream(self, text: str, session_id: Optional[str], timeout: int, route: Optional[str] = None) -> Iterator[str]:
        raise NotImplementedError("Hermes backend does not support streaming")
