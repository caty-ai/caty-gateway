import http.client
import json
import math
import os
import random
import threading
import time
import uuid
from urllib.parse import urlencode, urlparse

from caty_gateway import fish_tts_contract


_health_lock = threading.Lock()
_unhealthy_until = 0.0


class FishTransportError(RuntimeError):
    """Sanitized Fish HTTP error safe to inspect outside the transport."""

    def __init__(self, status=None, retry_after=None, configuration_error=False):
        super().__init__("Fish Audio request failed")
        self.status = status
        self.retry_after = retry_after
        self.configuration_error = bool(configuration_error)


def _env_int(name, default):
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value >= 1 else default


def _env_float(name, default):
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) and value >= 0 else default


def _reset_health():
    global _unhealthy_until
    with _health_lock:
        _unhealthy_until = 0.0


def _check_health():
    global _unhealthy_until
    with _health_lock:
        now = time.time()
        if now < _unhealthy_until:
            raise RuntimeError("Fish Audio TTS 429: cooldown active")
        if _unhealthy_until:
            _unhealthy_until = 0.0


def _record_unhealthy():
    global _unhealthy_until
    cooldown = _env_float("FISH_UNHEALTHY_COOLDOWN_S", 20.0)
    with _health_lock:
        _unhealthy_until = time.time() + cooldown


def _fish_api_key():
    return os.environ.get("FISH_API_KEY", "").strip()


def _raw_fish_api_key():
    return os.environ.get("FISH_API_KEY", "")


def _fish_base_url():
    return os.environ.get("FISH_BASE_URL", "https://api.fish.audio").strip() or "https://api.fish.audio"


def _fish_model():
    return fish_tts_contract.resolve_model()


def _fish_latency():
    return os.environ.get("FISH_LATENCY", "balanced").strip() or "balanced"


def _request_path(parsed):
    base_path = parsed.path.rstrip("/")
    if not base_path:
        return "/v1/tts"
    return f"{base_path}/v1/tts"


def _get_path(parsed, path, params):
    if not isinstance(path, str) or not path.startswith("/") or ".." in path:
        raise FishTransportError()
    base_path = parsed.path.rstrip("/")
    query = urlencode(params or {}, doseq=True)
    target = f"{base_path}{path}"
    return f"{target}?{query}" if query else target


def _connection(parsed, timeout=120):
    if parsed.scheme == "https":
        cls = http.client.HTTPSConnection
    elif parsed.scheme == "http":
        cls = http.client.HTTPConnection
    else:
        raise RuntimeError("FISH_BASE_URL must use http or https")
    if not parsed.hostname:
        raise RuntimeError("FISH_BASE_URL must include a hostname")
    return cls(parsed.hostname, parsed.port, timeout=timeout)


def _request_body(text, voice_id):
    return {
        "text": text,
        "reference_id": voice_id,
        "format": "mp3",
        "latency": _fish_latency(),
        "temperature": 0.7,
        "top_p": 0.7,
        "chunk_length": 300,
        "normalize": True,
    }


def _has_invalid_header_value_chars(value):
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def _redact(message, api_key):
    redacted = str(message)
    encoded_key = api_key.encode()
    for secret in (api_key, str(encoded_key), repr(encoded_key)):
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _redact_bytes(data, api_key):
    return data.replace(api_key.encode(), b"[REDACTED]")


def _stream_response(parsed, body, headers, api_key):
    attempts = min(10, _env_int("FISH_RETRY_ATTEMPTS", 4))
    base = _env_float("FISH_RETRY_BASE_S", 0.5)
    cap = _env_float("FISH_RETRY_CAP_S", 8.0)
    all_429 = True
    yielded = False
    for attempt in range(attempts):
        conn = None
        err = None
        close_err = None
        retry = False
        success = False
        try:
            conn = _connection(parsed)
            conn.request("POST", _request_path(parsed), body, headers)
            res = conn.getresponse()
            if res.status != 200:
                all_429 = all_429 and res.status == 429
                retry = res.status == 429 or 500 <= res.status <= 599
                detail = _redact_bytes(res.read(300), api_key)
                raise RuntimeError(f"Fish Audio TTS {res.status}: {detail!r}")
            all_429 = False
            while True:
                chunk = res.read(4096)
                if not chunk:
                    break
                yielded = True
                yield chunk
            success = True
        except Exception as exc:
            err = RuntimeError(_redact(str(exc), api_key))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception as exc:
                    close_err = RuntimeError(_redact(str(exc), api_key))
        if success:
            return
        if err is not None and yielded:
            raise err
        if retry and attempt + 1 < attempts:
            delay = min(cap, base * 2**attempt) + random.uniform(0, base)
            time.sleep(delay)
            continue
        if err is not None:
            if retry and all_429 and attempt + 1 == attempts:
                _record_unhealthy()
            raise err
        if close_err is not None:
            raise close_err


def _preview_stream_response(
    parsed,
    body,
    headers,
    api_key,
    sanitize_errors=False,
    attempts_env="CATY_VOICE_PREVIEW_TTS_ATTEMPTS",
    timeout_env="CATY_VOICE_PREVIEW_TTS_TIMEOUT_SECONDS",
):
    """Stream a preview without consulting or mutating conversation health."""
    attempts = min(4, _env_int(attempts_env, 2))
    timeout = min(60.0, max(1.0, _env_float(timeout_env, 30.0)))
    base = _env_float("FISH_RETRY_BASE_S", 0.5)
    cap = _env_float("FISH_RETRY_CAP_S", 8.0)
    yielded = False
    for attempt in range(attempts):
        conn = None
        err = None
        close_err = None
        retry = False
        success = False
        try:
            conn = _connection(parsed, timeout=timeout)
            conn.request("POST", _request_path(parsed), body, headers)
            res = conn.getresponse()
            if res.status != 200:
                retry = res.status == 429 or 500 <= res.status <= 599
                detail = _redact_bytes(res.read(300), api_key)
                if sanitize_errors:
                    raise RuntimeError("Fish Audio filler synthesis failed")
                raise RuntimeError(f"Fish Audio TTS {res.status}: {detail!r}")
            while True:
                chunk = res.read(4096)
                if not chunk:
                    break
                yielded = True
                yield chunk
            success = True
        except Exception as exc:
            err = RuntimeError(
                "Fish Audio filler synthesis failed"
                if sanitize_errors
                else _redact(str(exc), api_key)
            )
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception as exc:
                    close_err = RuntimeError(
                        "Fish Audio filler synthesis failed"
                        if sanitize_errors
                        else _redact(str(exc), api_key)
                    )
        if success:
            return
        if err is not None and yielded:
            raise err
        if retry and attempt + 1 < attempts:
            delay = min(cap, base * 2**attempt) + random.uniform(0, base)
            time.sleep(delay)
            continue
        if err is not None:
            raise err
        if close_err is not None:
            raise close_err


def synthesize_stream(text, voice_id):
    _check_health()
    api_key = _fish_api_key()
    if not api_key:
        raise RuntimeError("FISH_API_KEY is required when CATY_TTS_ENGINE=fish")
    if _has_invalid_header_value_chars(_raw_fish_api_key()) or _has_invalid_header_value_chars(api_key):
        raise RuntimeError("FISH_API_KEY contains invalid characters")
    voice_id = (voice_id or "").strip()
    if not voice_id:
        raise RuntimeError("CATY_TTS_VOICE or runtime voice_id is required when CATY_TTS_ENGINE=fish")

    parsed = urlparse(_fish_base_url())
    body = json.dumps(_request_body(text, voice_id))
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Idempotency-Key": uuid.uuid4().hex,
        "model": _fish_model(),
    }
    return _stream_response(parsed, body, headers, api_key)


def synthesize(text, voice_id):
    return b"".join(synthesize_stream(text, voice_id))


def synthesize_preview(text, voice_id):
    """Synthesize a bounded preview isolated from conversation cooldown state."""
    return _synthesize_isolated(text, voice_id, "voice preview")


def synthesize_filler(text, reference_id):
    """Stream an explicit target voice without conversation cooldown state."""
    return _synthesize_isolated(text, reference_id, "filler pack")


def _synthesize_isolated(text, voice_id, purpose):
    api_key = _fish_api_key()
    if not api_key:
        raise RuntimeError(f"FISH_API_KEY is required for {purpose}")
    if _has_invalid_header_value_chars(_raw_fish_api_key()) or _has_invalid_header_value_chars(api_key):
        raise RuntimeError("FISH_API_KEY contains invalid characters")
    voice_id = (voice_id or "").strip()
    if not voice_id:
        raise RuntimeError(f"voice_id is required for {purpose}")

    parsed = urlparse(_fish_base_url())
    body = json.dumps(_request_body(text, voice_id))
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Idempotency-Key": uuid.uuid4().hex,
        "model": _fish_model(),
    }
    filler = purpose == "filler pack"
    return _preview_stream_response(
        parsed,
        body,
        headers,
        api_key,
        sanitize_errors=filler,
        attempts_env=(
            "CATY_VOICE_FILLER_TTS_ATTEMPTS"
            if filler
            else "CATY_VOICE_PREVIEW_TTS_ATTEMPTS"
        ),
        timeout_env=(
            "CATY_VOICE_FILLER_TTS_TIMEOUT_SECONDS"
            if filler
            else "CATY_VOICE_PREVIEW_TTS_TIMEOUT_SECONDS"
        ),
    )


def get_json(path, params=None):
    """Issue one authenticated Fish GET and return a bounded JSON document.

    Response bodies and transport exception text intentionally never cross this
    boundary; callers receive only status and a sanitized retry hint.
    """
    api_key = _fish_api_key()
    if not api_key:
        raise FishTransportError(configuration_error=True)
    if _has_invalid_header_value_chars(_raw_fish_api_key()) or _has_invalid_header_value_chars(api_key):
        raise FishTransportError()
    parsed = urlparse(_fish_base_url())
    conn = None
    try:
        timeout = min(60.0, max(1.0, _env_float("CATY_VOICE_CATALOG_TIMEOUT_SECONDS", 15.0)))
        conn = _connection(parsed, timeout=timeout)
        conn.request(
            "GET",
            _get_path(parsed, path, params),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
        )
        response = conn.getresponse()
        getheader = getattr(response, "getheader", None)
        retry_after = getheader("Retry-After") if callable(getheader) else None
        if response.status != 200:
            # Drain a small bounded amount for connection hygiene, but never
            # include upstream content in the exception.
            response.read(1024)
            raise FishTransportError(response.status, retry_after)
        raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise FishTransportError()
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, (dict, list)):
            raise FishTransportError()
        return payload
    except FishTransportError:
        raise
    except Exception:
        raise FishTransportError() from None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
