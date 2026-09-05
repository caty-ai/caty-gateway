import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable


ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_VISION_MODEL = "claude-haiku-4-5-20251001"
# Mirrors IDENTITY_DESCRIPTION_LIMIT in caty_gateway.py — keep in sync if that changes.
IDENTITY_DESCRIPTION_LIMIT = 1000
MAX_RESPONSE_BYTES = 1024 * 1024

VISION_DESCRIPTION_PROMPT = (
    "Describe this pixel-art character as a compact, comma-separated English identity "
    "description suitable for reuse inside an image-edit prompt. Cover hair style and "
    "color, accessories or glasses, outfit from top to bottom, expression, and any held "
    "items. Use this style: long black ponytail, blunt bangs, rectangular glasses, sailor "
    "school uniform with white top, navy collar, red neckerchief and navy pleated skirt, "
    "calm composed expression, holding a small dark tablet in one arm. Respond with "
    "exactly one line, no preamble, no quotes."
)


class VisionDescriberDisabled(RuntimeError):
    """Raised when avatar vision description is not configured."""


class VisionDescriberError(RuntimeError):
    """Raised when avatar vision description fails."""


class VisionDescriber:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        urlopen: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        retry_attempts: int = 2,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise VisionDescriberDisabled("ANTHROPIC_API_KEY is required for avatar vision description")
        self.model = model if model is not None else os.environ.get("CATY_VISION_MODEL")
        if not self.model:
            self.model = DEFAULT_VISION_MODEL
        self.urlopen = urlopen or urllib.request.urlopen
        self.sleep = sleep
        self.retry_attempts = min(2, max(1, int(retry_attempts)))

    def describe(self, image_bytes: bytes) -> str:
        payload = {
            "model": self.model,
            "max_tokens": 300,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": base64.b64encode(image_bytes).decode("ascii"),
                            },
                        },
                        {"type": "text", "text": VISION_DESCRIPTION_PROMPT},
                    ],
                }
            ],
        }
        data = self._request_json(payload)
        text = self._extract_text(data)
        cleaned = self._clean_text(text)
        if not cleaned:
            raise VisionDescriberError("avatar vision response missing content text")
        return cleaned

    def _request_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_body = json.dumps(payload).encode("utf-8")
        for attempt in range(self.retry_attempts):
            request = urllib.request.Request(
                ANTHROPIC_MESSAGES_URL,
                data=request_body,
                method="POST",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": ANTHROPIC_VERSION,
                    "content-type": "application/json",
                    "user-agent": "caty-gateway/1.0",
                },
            )
            try:
                with self.urlopen(request, timeout=30) as response:
                    raw_bytes = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw_bytes) > MAX_RESPONSE_BYTES:
                    raise VisionDescriberError("avatar vision response exceeded the size limit")
                raw = raw_bytes.decode("utf-8")
            except VisionDescriberError:
                raise
            except urllib.error.HTTPError as exc:
                if self._should_retry(exc.code, attempt):
                    self.sleep(1.0)
                    continue
                raise VisionDescriberError(f"avatar vision request failed with HTTP {exc.code}") from exc
            except urllib.error.URLError as exc:
                raise VisionDescriberError("avatar vision request failed") from exc
            except Exception as exc:
                # Contract: describe() only ever raises VisionDescriber* — approve-base
                # must always be able to fall back to the generic description.
                # (IncompleteRead/BadStatusLine are not OSError; decode can raise too.)
                raise VisionDescriberError("avatar vision response could not be read") from exc
            try:
                decoded = json.loads(raw)
            except ValueError as exc:
                raise VisionDescriberError("avatar vision response was not valid JSON") from exc
            if not isinstance(decoded, dict):
                raise VisionDescriberError("avatar vision response must be a JSON object")
            return decoded
        raise VisionDescriberError("avatar vision request failed")  # unreachable safety net

    def _should_retry(self, status_code: int, attempt: int) -> bool:
        if attempt + 1 >= self.retry_attempts:
            return False
        return status_code == 429 or 500 <= status_code <= 599

    def _extract_text(self, payload: dict[str, Any]) -> str:
        content = payload.get("content")
        if not isinstance(content, list) or not content:
            raise VisionDescriberError("avatar vision response missing content")
        first = content[0]
        if not isinstance(first, dict):
            raise VisionDescriberError("avatar vision response content must be an object")
        text = first.get("text")
        if not isinstance(text, str) or not text.strip():
            raise VisionDescriberError("avatar vision response missing content text")
        return text

    def _clean_text(self, text: str) -> str:
        cleaned = text.strip().strip("\"'“”‘’")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        # Braces would break the downstream str.format() slot-prompt templating.
        cleaned = cleaned.replace("{", "(").replace("}", ")")
        return cleaned[:IDENTITY_DESCRIPTION_LIMIT]
