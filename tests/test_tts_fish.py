import json
import os
import sys
import unittest
from contextlib import contextmanager
from unittest import mock


from caty_gateway import caty_gateway as cg
from caty_gateway import fish_tts_contract
from caty_gateway import tts_fish


@contextmanager
def patched_environment(**values):
    original_env = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class FakeConfig:
    def __init__(self, voice_id):
        self.voice_id = voice_id

    def get(self):
        return {"voice_id": self.voice_id}


@contextmanager
def patched_voice(voice_id):
    original = cg.CONFIG
    cg.CONFIG = FakeConfig(voice_id)
    try:
        yield
    finally:
        cg.CONFIG = original


class FakeJob:
    def __init__(self):
        self.chunks = []

    def push(self, chunk):
        self.chunks.append(chunk)


class FakeFishResponse:
    def __init__(self, chunks=None, status=200, body=b""):
        self.status = status
        self._chunks = list(chunks or [])
        self._body = body
        self._offset = 0

    def read(self, size=-1):
        if self.status == 200:
            if not self._chunks:
                return b""
            return self._chunks.pop(0)
        if self._offset >= len(self._body):
            return b""
        if size is None or size < 0:
            size = len(self._body) - self._offset
        chunk = self._body[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk


class FakeFishHTTPSConnection:
    requests = []
    response = FakeFishResponse([b"mp3-a", b"mp3-b"])

    def __init__(self, host, port=None, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.closed = False

    def request(self, method, path, body=None, headers=None):
        FakeFishHTTPSConnection.requests.append({
            "host": self.host,
            "port": self.port,
            "timeout": self.timeout,
            "method": method,
            "path": path,
            "body": body,
            "headers": dict(headers or {}),
        })

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class FakeSequenceFishHTTPSConnection(FakeFishHTTPSConnection):
    responses = []
    constructor_count = 0
    close_error = None

    def __init__(self, host, port=None, timeout=None):
        super().__init__(host, port, timeout)
        type(self).constructor_count += 1

    def getresponse(self):
        return type(self).responses.pop(0)

    def close(self):
        if type(self).close_error is not None:
            raise RuntimeError(type(self).close_error)
        super().close()


class FakePartialFailureResponse:
    status = 200

    def __init__(self, error="stream interrupted"):
        self.read_count = 0
        self.error = error

    def read(self, _size=-1):
        self.read_count += 1
        if self.read_count == 1:
            return b"partial"
        raise RuntimeError(self.error)


class FakeFishRequestFailingConnection(FakeFishHTTPSConnection):
    def request(self, method, path, body=None, headers=None):
        key = (headers or {}).get("Authorization", "").removeprefix("Bearer ")
        raise ValueError(f"Invalid header value b'Bearer {key}'")


class FakeFishCloseFailingConnection(FakeFishHTTPSConnection):
    def request(self, method, path, body=None, headers=None):
        super().request(method, path, body, headers)
        self.key = (headers or {}).get("Authorization", "").removeprefix("Bearer ")

    def close(self):
        raise ValueError(f"close failed with Bearer {self.key}")


class FakeProxyResponse:
    status = 200

    def __init__(self, body=b"proxy-mp3"):
        self._body = body
        self._offset = 0

    def read(self, size=-1):
        if self._offset >= len(self._body):
            return b""
        if size is None or size < 0:
            size = len(self._body) - self._offset
        chunk = self._body[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk


class FakeProxyHTTPConnection:
    requests = []

    def __init__(self, host, port=None, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._response = FakeProxyResponse()

    def request(self, method, path, body=None, headers=None):
        FakeProxyHTTPConnection.requests.append((method, path, body, headers or {}))

    def getresponse(self):
        return self._response

    def close(self):
        pass


def write_temp_mp3(data):
    fd, path = cg.tempfile.mkstemp(suffix=".mp3")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


def assert_secret_not_reachable(testcase, exc, raw_key):
    message = str(exc)
    testcase.assertIn("[REDACTED]", message)
    testcase.assertNotIn(raw_key, message)
    testcase.assertIsNone(exc.__context__)
    testcase.assertIsNone(exc.__cause__)
    testcase.assertNotIn(raw_key, repr(exc))
    for arg in exc.args:
        testcase.assertNotIn(raw_key, str(arg))


class FishTTSTest(unittest.TestCase):
    def setUp(self):
        tts_fish._reset_health()
        self.addCleanup(tts_fish._reset_health)
        FakeFishHTTPSConnection.requests = []
        FakeFishHTTPSConnection.response = FakeFishResponse([b"mp3-a", b"mp3-b"])
        FakeSequenceFishHTTPSConnection.responses = []
        FakeSequenceFishHTTPSConnection.constructor_count = 0
        FakeSequenceFishHTTPSConnection.close_error = None
        FakeProxyHTTPConnection.requests = []

    def test_fish_model_contract_defaults_to_s2_1_pro_for_unset_and_blank_env(self):
        with patched_environment(FISH_MODEL=None):
            self.assertEqual(fish_tts_contract.resolve_model(), "s2.1-pro")
            self.assertEqual(
                fish_tts_contract.inference_contract_version(),
                "fish-tts-v1-s2.1-pro",
            )
            self.assertEqual(cg.fish_inference_contract_version(), "fish-tts-v1-s2.1-pro")

        with patched_environment(FISH_MODEL="   "):
            self.assertEqual(fish_tts_contract.resolve_model(), "s2.1-pro")
            self.assertEqual(
                fish_tts_contract.inference_contract_version(),
                "fish-tts-v1-s2.1-pro",
            )

    def test_fish_model_contract_accepts_all_documented_models(self):
        for model in fish_tts_contract.SUPPORTED_MODELS:
            with self.subTest(model=model):
                with patched_environment(FISH_MODEL=model):
                    self.assertEqual(fish_tts_contract.resolve_model(), model)
                    self.assertEqual(
                        fish_tts_contract.inference_contract_version(),
                        f"fish-tts-v1-{model}",
                    )

    def test_fish_model_contract_rejects_legacy_alias_with_clear_error(self):
        with patched_environment(FISH_MODEL="speech-01-turbo"):
            with self.assertRaisesRegex(RuntimeError, "no longer supported") as cm:
                fish_tts_contract.resolve_model()

        self.assertIn("Use s2.1-pro for production", str(cm.exception))
        self.assertIn("s2.1-pro-free", str(cm.exception))

    def test_fish_model_contract_rejects_invalid_values_with_supported_list(self):
        with patched_environment(FISH_MODEL="totally-wrong"):
            with self.assertRaisesRegex(RuntimeError, "Invalid FISH_MODEL='totally-wrong'") as cm:
                fish_tts_contract.resolve_model()

        self.assertIn("s1, s2-pro, s2.1-pro, s2.1-pro-free", str(cm.exception))

    def test_fish_retries_429_with_exponential_backoff_then_succeeds(self):
        FakeSequenceFishHTTPSConnection.responses = [
            FakeFishResponse(status=429, body=b"limited"),
            FakeFishResponse(status=429, body=b"limited"),
            FakeFishResponse([b"recovered"]),
        ]
        with patched_environment(FISH_API_KEY="fish-key"), \
                mock.patch("caty_gateway.tts_fish.http.client.HTTPSConnection", FakeSequenceFishHTTPSConnection), \
                mock.patch("caty_gateway.tts_fish.random.uniform", return_value=0), \
                mock.patch("caty_gateway.tts_fish.time.sleep") as sleep:
            self.assertEqual(tts_fish.synthesize("hello", "voice-1"), b"recovered")

        self.assertEqual(FakeSequenceFishHTTPSConnection.constructor_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.5, 1.0])

    def test_fish_request_includes_idempotency_key(self):
        with patched_environment(FISH_API_KEY="fish-key"), \
                mock.patch("caty_gateway.tts_fish.http.client.HTTPSConnection", FakeFishHTTPSConnection):
            self.assertEqual(tts_fish.synthesize("hello", "voice-1"), b"mp3-amp3-b")

        key = FakeFishHTTPSConnection.requests[0]["headers"]["Idempotency-Key"]
        self.assertRegex(key, r"^[0-9a-f]{32}$")

    def test_fish_retries_reuse_idempotency_key(self):
        FakeSequenceFishHTTPSConnection.responses = [
            FakeFishResponse(status=429, body=b"limited"),
            FakeFishResponse([b"recovered"]),
        ]
        with patched_environment(FISH_API_KEY="fish-key", FISH_RETRY_ATTEMPTS="2"), \
                mock.patch("caty_gateway.tts_fish.http.client.HTTPSConnection", FakeSequenceFishHTTPSConnection), \
                mock.patch("caty_gateway.tts_fish.random.uniform", return_value=0), \
                mock.patch("caty_gateway.tts_fish.time.sleep"):
            self.assertEqual(tts_fish.synthesize("hello", "voice-1"), b"recovered")

        requests = FakeSequenceFishHTTPSConnection.requests
        keys = [request["headers"]["Idempotency-Key"] for request in requests]
        self.assertEqual(len(keys), 2)
        self.assertEqual(keys[0], keys[1])
        self.assertEqual(requests[0]["body"], requests[1]["body"])

    def test_fish_consecutive_requests_use_distinct_idempotency_keys(self):
        FakeSequenceFishHTTPSConnection.responses = [
            FakeFishResponse([b"first"]),
            FakeFishResponse([b"second"]),
        ]
        with patched_environment(FISH_API_KEY="fish-key"), \
                mock.patch("caty_gateway.tts_fish.http.client.HTTPSConnection", FakeSequenceFishHTTPSConnection):
            self.assertEqual(tts_fish.synthesize("hello", "voice-1"), b"first")
            self.assertEqual(tts_fish.synthesize("hello", "voice-1"), b"second")

        keys = [request["headers"]["Idempotency-Key"] for request in FakeSequenceFishHTTPSConnection.requests]
        self.assertEqual(len(keys), 2)
        self.assertNotEqual(keys[0], keys[1])

    def test_fish_exhausted_5xx_attempts_do_not_record_cooldown(self):
        FakeSequenceFishHTTPSConnection.responses = [
            FakeFishResponse(status=503, body=b"unavailable") for _ in range(2)
        ]
        with patched_environment(FISH_API_KEY="fish-key", FISH_RETRY_ATTEMPTS="2"), \
                mock.patch("caty_gateway.tts_fish.http.client.HTTPSConnection", FakeSequenceFishHTTPSConnection), \
                mock.patch("caty_gateway.tts_fish.random.uniform", return_value=0), \
                mock.patch("caty_gateway.tts_fish.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "503"):
                tts_fish.synthesize("hello", "voice-1")
            FakeSequenceFishHTTPSConnection.responses = [FakeFishResponse([b"recovered"])]
            self.assertEqual(tts_fish.synthesize("hello", "voice-1"), b"recovered")

    def test_fish_mixed_exhaustion_does_not_record_cooldown(self):
        FakeSequenceFishHTTPSConnection.responses = [
            FakeFishResponse(status=429, body=b"limited"),
            FakeFishResponse(status=503, body=b"unavailable"),
        ]
        with patched_environment(FISH_API_KEY="fish-key", FISH_RETRY_ATTEMPTS="2"), \
                mock.patch("caty_gateway.tts_fish.http.client.HTTPSConnection", FakeSequenceFishHTTPSConnection), \
                mock.patch("caty_gateway.tts_fish.random.uniform", return_value=0), \
                mock.patch("caty_gateway.tts_fish.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "503"):
                tts_fish.synthesize("hello", "voice-1")
            FakeSequenceFishHTTPSConnection.responses = [FakeFishResponse([b"recovered"])]
            self.assertEqual(tts_fish.synthesize("hello", "voice-1"), b"recovered")

    def test_fish_zero_retry_attempts_uses_default_attempt_count(self):
        FakeSequenceFishHTTPSConnection.responses = [
            FakeFishResponse(status=429, body=b"limited") for _ in range(4)
        ]
        with patched_environment(FISH_API_KEY="fish-key", FISH_RETRY_ATTEMPTS="0"), \
                mock.patch("caty_gateway.tts_fish.http.client.HTTPSConnection", FakeSequenceFishHTTPSConnection), \
                mock.patch("caty_gateway.tts_fish.random.uniform", return_value=0), \
                mock.patch("caty_gateway.tts_fish.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "429"):
                tts_fish.synthesize("hello", "voice-1")

        self.assertEqual(FakeSequenceFishHTTPSConnection.constructor_count, 4)

    def test_fish_synthesize_cooldown_blocks_connection(self):
        FakeSequenceFishHTTPSConnection.responses = [
            FakeFishResponse(status=429, body=b"limited")
        ]
        with patched_environment(FISH_API_KEY="fish-key", FISH_RETRY_ATTEMPTS="1"), \
                mock.patch("caty_gateway.tts_fish.http.client.HTTPSConnection", FakeSequenceFishHTTPSConnection):
            with self.assertRaisesRegex(RuntimeError, "429"):
                tts_fish.synthesize("hello", "voice-1")
            attempts = FakeSequenceFishHTTPSConnection.constructor_count
            with self.assertRaisesRegex(RuntimeError, "429.*cooldown"):
                tts_fish.synthesize("hello", "voice-1")

        self.assertEqual(FakeSequenceFishHTTPSConnection.constructor_count, attempts)

    def test_fish_429_close_error_still_retries_and_succeeds(self):
        FakeSequenceFishHTTPSConnection.responses = [
            FakeFishResponse(status=429, body=b"limited"),
            FakeFishResponse([b"recovered"]),
        ]
        FakeSequenceFishHTTPSConnection.close_error = "close failed"
        with patched_environment(FISH_API_KEY="fish-key", FISH_RETRY_ATTEMPTS="2"), \
                mock.patch("caty_gateway.tts_fish.http.client.HTTPSConnection", FakeSequenceFishHTTPSConnection), \
                mock.patch("caty_gateway.tts_fish.random.uniform", return_value=0), \
                mock.patch("caty_gateway.tts_fish.time.sleep") as sleep:
            self.assertEqual(tts_fish.synthesize("hello", "voice-1"), b"recovered")

        self.assertEqual(FakeSequenceFishHTTPSConnection.constructor_count, 2)
        sleep.assert_called_once_with(0.5)

    def test_fish_invalid_retry_env_uses_defaults_without_healthy_path_sleep(self):
        with patched_environment(
            FISH_API_KEY="fish-key",
            FISH_RETRY_ATTEMPTS="invalid",
            FISH_RETRY_BASE_S="-1",
            FISH_RETRY_CAP_S="nan",
            FISH_UNHEALTHY_COOLDOWN_S="-1",
        ), \
                mock.patch("caty_gateway.tts_fish.http.client.HTTPSConnection", FakeFishHTTPSConnection), \
                mock.patch("caty_gateway.tts_fish.time.sleep") as sleep:
            self.assertEqual(tts_fish.synthesize("hello", "voice-1"), b"mp3-amp3-b")

        sleep.assert_not_called()

    def test_fish_exhausted_429_attempts_records_cooldown(self):
        FakeSequenceFishHTTPSConnection.responses = [
            FakeFishResponse(status=429, body=b"limited") for _ in range(3)
        ]
        with patched_environment(
            FISH_API_KEY="fish-key",
            FISH_RETRY_ATTEMPTS="3",
        ), \
                mock.patch("caty_gateway.tts_fish.http.client.HTTPSConnection", FakeSequenceFishHTTPSConnection), \
                mock.patch("caty_gateway.tts_fish.random.uniform", return_value=0), \
                mock.patch("caty_gateway.tts_fish.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "429"):
                tts_fish.synthesize("hello", "voice-1")
            with self.assertRaisesRegex(RuntimeError, "429.*cooldown"):
                tts_fish.synthesize_stream("hello", "voice-1")

        self.assertEqual(FakeSequenceFishHTTPSConnection.constructor_count, 3)

    def test_preview_429s_do_not_cool_down_conversation_tts(self):
        FakeSequenceFishHTTPSConnection.responses = [
            FakeFishResponse(status=429, body=b"limited"),
            FakeFishResponse(status=429, body=b"limited"),
            FakeFishResponse([b"conversation-recovered"]),
        ]
        with patched_environment(
            FISH_API_KEY="fish-key",
            CATY_VOICE_PREVIEW_TTS_ATTEMPTS="2",
        ), \
                mock.patch("caty_gateway.tts_fish.http.client.HTTPSConnection", FakeSequenceFishHTTPSConnection), \
                mock.patch("caty_gateway.tts_fish.random.uniform", return_value=0), \
                mock.patch("caty_gateway.tts_fish.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "429"):
                list(tts_fish.synthesize_preview("hello", "voice-1"))
            self.assertEqual(
                list(tts_fish.synthesize_stream("hello", "voice-1")),
                [b"conversation-recovered"],
            )

        self.assertEqual(FakeSequenceFishHTTPSConnection.constructor_count, 3)

    def test_conversation_cooldown_does_not_block_preview_tts(self):
        FakeSequenceFishHTTPSConnection.responses = [
            FakeFishResponse(status=429, body=b"limited"),
            FakeFishResponse([b"preview-still-works"]),
        ]
        with patched_environment(
            FISH_API_KEY="fish-key",
            FISH_RETRY_ATTEMPTS="1",
            CATY_VOICE_PREVIEW_TTS_ATTEMPTS="1",
            CATY_VOICE_PREVIEW_TTS_TIMEOUT_SECONDS="7",
        ), \
                mock.patch("caty_gateway.tts_fish.http.client.HTTPSConnection", FakeSequenceFishHTTPSConnection):
            with self.assertRaisesRegex(RuntimeError, "429"):
                list(tts_fish.synthesize_stream("hello", "voice-1"))
            self.assertEqual(
                list(tts_fish.synthesize_preview("hello", "voice-1")),
                [b"preview-still-works"],
            )

        self.assertEqual(FakeSequenceFishHTTPSConnection.constructor_count, 2)
        self.assertEqual(FakeFishHTTPSConnection.requests[-1]["timeout"], 7.0)

    def test_fish_cooldown_blocks_connections_then_recovers(self):
        FakeSequenceFishHTTPSConnection.responses = [
            FakeFishResponse(status=429, body=b"limited"),
            FakeFishResponse(status=429, body=b"limited"),
        ]
        with patched_environment(
            FISH_API_KEY="fish-key",
            FISH_RETRY_ATTEMPTS="2",
            FISH_UNHEALTHY_COOLDOWN_S="20",
        ), \
                mock.patch("caty_gateway.tts_fish.http.client.HTTPSConnection", FakeSequenceFishHTTPSConnection), \
                mock.patch("caty_gateway.tts_fish.random.uniform", return_value=0), \
                mock.patch("caty_gateway.tts_fish.time.sleep"), \
                mock.patch("caty_gateway.tts_fish.time.time", return_value=100):
            with self.assertRaisesRegex(RuntimeError, "429"):
                tts_fish.synthesize("hello", "voice-1")

        attempts = FakeSequenceFishHTTPSConnection.constructor_count
        with patched_environment(FISH_API_KEY="fish-key"), \
                mock.patch("caty_gateway.tts_fish.http.client.HTTPSConnection", FakeSequenceFishHTTPSConnection), \
                mock.patch("caty_gateway.tts_fish.time.time", return_value=119):
            with self.assertRaisesRegex(RuntimeError, "429.*cooldown"):
                tts_fish.synthesize_stream("hello", "voice-1")
        self.assertEqual(FakeSequenceFishHTTPSConnection.constructor_count, attempts)

        FakeSequenceFishHTTPSConnection.responses = [FakeFishResponse([b"recovered"])]
        with patched_environment(FISH_API_KEY="fish-key"), \
                mock.patch("caty_gateway.tts_fish.http.client.HTTPSConnection", FakeSequenceFishHTTPSConnection), \
                mock.patch("caty_gateway.tts_fish.time.time", return_value=121):
            self.assertEqual(tts_fish.synthesize("hello", "voice-1"), b"recovered")
        self.assertEqual(FakeSequenceFishHTTPSConnection.constructor_count, attempts + 1)

    def test_fish_retries_503_but_not_401(self):
        with patched_environment(FISH_API_KEY="fish-key"), \
                mock.patch("caty_gateway.tts_fish.http.client.HTTPSConnection", FakeSequenceFishHTTPSConnection), \
                mock.patch("caty_gateway.tts_fish.random.uniform", return_value=0), \
                mock.patch("caty_gateway.tts_fish.time.sleep") as sleep:
            FakeSequenceFishHTTPSConnection.responses = [
                FakeFishResponse(status=503, body=b"unavailable"),
                FakeFishResponse([b"recovered"]),
            ]
            self.assertEqual(tts_fish.synthesize("hello", "voice-1"), b"recovered")
            self.assertEqual(FakeSequenceFishHTTPSConnection.constructor_count, 2)
            sleep.assert_called_once_with(0.5)

            FakeSequenceFishHTTPSConnection.responses = [FakeFishResponse(status=401, body=b"no")]
            with self.assertRaisesRegex(RuntimeError, "Fish Audio TTS 401"):
                tts_fish.synthesize("hello", "voice-1")
        self.assertEqual(FakeSequenceFishHTTPSConnection.constructor_count, 3)

    def test_fish_does_not_retry_after_first_chunk(self):
        raw_key = "fish-secret-key"
        FakeSequenceFishHTTPSConnection.responses = [
            FakePartialFailureResponse(f"stream interrupted: {raw_key}")
        ]
        with patched_environment(FISH_API_KEY=raw_key), \
                mock.patch("caty_gateway.tts_fish.http.client.HTTPSConnection", FakeSequenceFishHTTPSConnection), \
                mock.patch("caty_gateway.tts_fish.time.sleep") as sleep:
            stream = tts_fish.synthesize_stream("hello", "voice-1")
            self.assertEqual(next(stream), b"partial")
            with self.assertRaisesRegex(RuntimeError, "stream interrupted") as cm:
                next(stream)

        assert_secret_not_reachable(self, cm.exception, raw_key)
        self.assertEqual(FakeSequenceFishHTTPSConnection.constructor_count, 1)
        sleep.assert_not_called()

    def test_engine_fish_streaming_uses_native_api_and_pushes_chunks(self):
        job = FakeJob()
        with patched_environment(
            CATY_TTS_ENGINE="fish",
            FISH_API_KEY="fish-key",
            FISH_BASE_URL=None,
            FISH_MODEL=None,
            FISH_LATENCY=None,
        ), \
                patched_voice("voice-1"), \
                mock.patch("caty_gateway.tts_fish.http.client.HTTPSConnection", FakeFishHTTPSConnection):
            total = cg.tts_stream_to_job("hello [chuckling]", job)

        self.assertEqual(total, len(b"mp3-a") + len(b"mp3-b"))
        self.assertEqual(job.chunks, [b"mp3-a", b"mp3-b"])
        self.assertEqual(len(FakeFishHTTPSConnection.requests), 1)
        req = FakeFishHTTPSConnection.requests[0]
        self.assertEqual(req["host"], "api.fish.audio")
        self.assertIsNone(req["port"])
        self.assertEqual(req["timeout"], 120)
        self.assertEqual(req["method"], "POST")
        self.assertEqual(req["path"], "/v1/tts")
        self.assertEqual(req["headers"]["Authorization"], "Bearer fish-key")
        self.assertEqual(req["headers"]["Content-Type"], "application/json")
        self.assertEqual(req["headers"]["model"], "s2.1-pro")
        body = json.loads(req["body"])
        self.assertEqual(body["text"], "hello [chuckling]")
        self.assertEqual(body["reference_id"], "voice-1")
        self.assertEqual(body["format"], "mp3")
        self.assertEqual(body["latency"], "balanced")
        self.assertEqual(body["temperature"], 0.7)
        self.assertEqual(body["top_p"], 0.7)
        self.assertEqual(body["chunk_length"], 300)
        self.assertIs(body["normalize"], True)
        self.assertNotIn("model", body)

    def test_engine_fish_batch_tts_writes_mp3_file(self):
        FakeFishHTTPSConnection.response = FakeFishResponse([b"batch-", b"mp3"])
        with patched_environment(
            CATY_TTS_ENGINE="fish",
            FISH_API_KEY="fish-key",
            FISH_BASE_URL=None,
            FISH_MODEL=None,
            FISH_LATENCY=None,
        ), \
                patched_voice("voice-1"), \
                mock.patch("caty_gateway.tts_fish.http.client.HTTPSConnection", FakeFishHTTPSConnection):
            path = cg.tts("batch text")
        try:
            with open(path, "rb") as f:
                self.assertEqual(f.read(), b"batch-mp3")
        finally:
            os.remove(path)

    def test_default_proxy_path_uses_shared_production_fish_model(self):
        job = FakeJob()
        expected_body = json.dumps({
            "input": "default path",
            "voice": "voice-1",
            "model": "s2.1-pro",
            "response_format": "mp3",
        })
        with patched_environment(CATY_TTS_ENGINE=None), \
                patched_voice("voice-1"), \
                mock.patch("http.client.HTTPConnection", FakeProxyHTTPConnection):
            total = cg.tts_stream_to_job("default path", job)

        self.assertEqual(total, len(b"proxy-mp3"))
        self.assertEqual(job.chunks, [b"proxy-mp3"])
        self.assertEqual(FakeProxyHTTPConnection.requests, [
            ("POST", "/v1/audio/speech", expected_body, {"Content-Type": "application/json"})
        ])

    def test_proxy_path_rejects_legacy_fish_model_alias(self):
        with patched_environment(CATY_TTS_ENGINE=None, FISH_MODEL="speech-01-turbo"), \
                patched_voice("voice-1"), \
                mock.patch("http.client.HTTPConnection", FakeProxyHTTPConnection):
            with self.assertRaisesRegex(RuntimeError, "no longer supported"):
                cg.tts_stream_to_job("legacy alias", FakeJob())

        self.assertEqual(FakeProxyHTTPConnection.requests, [])

    def test_engine_fish_rejects_legacy_alias_before_network_request(self):
        with patched_environment(
            CATY_TTS_ENGINE="fish",
            FISH_API_KEY="fish-key",
            FISH_MODEL="speech-01-turbo",
        ), \
                patched_voice("voice-1"), \
                mock.patch("caty_gateway.tts_fish.http.client.HTTPSConnection", FakeFishHTTPSConnection):
            with self.assertRaisesRegex(RuntimeError, "no longer supported"):
                cg.tts_stream_to_job("legacy alias", FakeJob())

        self.assertEqual(FakeFishHTTPSConnection.requests, [])

    def test_engine_fish_requires_api_key_and_voice_id(self):
        with patched_environment(
            CATY_TTS_ENGINE="fish",
            FISH_API_KEY=None,
            FISH_BASE_URL=None,
            FISH_MODEL=None,
            FISH_LATENCY=None,
        ), \
                patched_voice("voice-1"):
            with self.assertRaisesRegex(RuntimeError, "FISH_API_KEY is required"):
                cg.tts_stream_to_job("hello", FakeJob())

        with patched_environment(
            CATY_TTS_ENGINE="fish",
            FISH_API_KEY="fish-key",
            FISH_BASE_URL=None,
            FISH_MODEL=None,
            FISH_LATENCY=None,
        ), \
                patched_voice(""):
            with self.assertRaisesRegex(RuntimeError, "voice_id is required"):
                cg.tts_stream_to_job("hello", FakeJob())

    def test_engine_fish_preserves_emotion_tags_in_request_text(self):
        with patched_environment(
            CATY_TTS_ENGINE="fish",
            FISH_API_KEY="fish-key",
            FISH_BASE_URL=None,
            FISH_MODEL=None,
            FISH_LATENCY=None,
        ), \
                patched_voice("voice-1"), \
                mock.patch("caty_gateway.tts_fish.http.client.HTTPSConnection", FakeFishHTTPSConnection):
            cg.tts_stream_to_job("A [chuckling] B", FakeJob())

        body = json.loads(FakeFishHTTPSConnection.requests[0]["body"])
        self.assertEqual(body["text"], "A [chuckling] B")

    def test_engine_fish_reads_voice_id_from_runtime_config_each_call(self):
        config = FakeConfig("voice-a")
        original = cg.CONFIG
        cg.CONFIG = config
        try:
            with patched_environment(
                CATY_TTS_ENGINE="fish",
                FISH_API_KEY="fish-key",
                FISH_BASE_URL=None,
                FISH_MODEL=None,
                FISH_LATENCY=None,
            ), \
                    mock.patch("caty_gateway.tts_fish.http.client.HTTPSConnection", FakeFishHTTPSConnection):
                cg.tts_stream_to_job("first", FakeJob())
                FakeFishHTTPSConnection.response = FakeFishResponse([b"next"])
                config.voice_id = "voice-b"
                cg.tts_stream_to_job("second", FakeJob())
        finally:
            cg.CONFIG = original

        first = json.loads(FakeFishHTTPSConnection.requests[0]["body"])
        second = json.loads(FakeFishHTTPSConnection.requests[1]["body"])
        self.assertEqual(first["reference_id"], "voice-a")
        self.assertEqual(second["reference_id"], "voice-b")

    def test_stream_pipeline_engine_fish_finishes_partial_audio_without_batch_fallback(self):
        def fail_after_partial(_text, job):
            job.push(b"partial-")
            raise RuntimeError("fish stream failed")

        job = cg.Job("user text", session_id="fish-partial")
        with patched_environment(CATY_TTS_ENGINE="fish"), \
                mock.patch.object(cg, "STREAM_TTS_ENABLED", False), \
                mock.patch.object(cg, "brain", return_value="reply"), \
                mock.patch.object(cg, "tts_stream_to_job", side_effect=fail_after_partial), \
                mock.patch.object(cg, "tts", side_effect=AssertionError("batch fallback must not run")), \
                mock.patch.object(cg.history_store, "append_turn"):
            cg.stream_pipeline(job, "user text", 0)

        self.assertEqual(job.chunks, [b"partial-"])
        self.assertTrue(job.done)
        self.assertIsNone(job.error)

    def test_stream_pipeline_does_not_publish_sentence_that_failed_before_audio(self):
        backend = mock.Mock()
        backend.supports_stream.return_value = True

        def synthesize(sentence, job):
            if sentence == "一文目。":
                job.push(b"first-sentence")
                return len(b"first-sentence")
            raise RuntimeError("second sentence failed before audio")

        job = cg.Job("user text", session_id="second-sentence-failure")
        with mock.patch.object(cg, "BACKEND", backend), \
                mock.patch.object(cg, "STREAM_TTS_ENABLED", True), \
                mock.patch.object(cg, "brain_stream", return_value=iter(["一文目。", "二文目。"])), \
                mock.patch.object(cg, "tts_stream_to_job", side_effect=synthesize), \
                mock.patch.object(cg.history_store, "append_turn"):
            cg.stream_pipeline(job, "user text", 0)

        self.assertEqual(job.chunks, [b"first-sentence"])
        self.assertEqual(job.reply, "一文目。")
        self.assertTrue(job.done)
        self.assertIsNone(job.error)

    def test_stream_pipeline_default_engine_finishes_partial_audio_without_batch_fallback_after_proxy_failure(self):
        def fail_after_partial(_text, job):
            job.push(b"partial-")
            raise RuntimeError("proxy stream failed")

        job = cg.Job("user text", session_id="proxy-partial")
        with patched_environment(CATY_TTS_ENGINE=None), \
                mock.patch.object(cg, "STREAM_TTS_ENABLED", False), \
                mock.patch.object(cg, "brain", return_value="reply"), \
                mock.patch.object(cg, "tts_stream_to_job", side_effect=fail_after_partial), \
                mock.patch.object(cg, "tts", side_effect=AssertionError("batch fallback must not run")), \
                mock.patch.object(cg, "_log_turn_summary") as turn_summary, \
                mock.patch.object(cg.history_store, "append_turn"):
            cg.stream_pipeline(job, "user text", 0)

        self.assertEqual(job.chunks, [b"partial-"])
        self.assertTrue(job.done)
        self.assertIsNone(job.error)
        self.assertEqual(turn_summary.call_count, 1)
        self.assertEqual(turn_summary.call_args.args[-1], "fallback")

    def test_stream_pipeline_default_engine_uses_batch_fallback_when_stream_fails_before_chunks(self):
        def fail_before_chunks(_text, _job):
            raise RuntimeError("proxy stream failed")

        job = cg.Job("user text", session_id="proxy-empty")
        with patched_environment(CATY_TTS_ENGINE=None), \
                mock.patch.object(cg, "STREAM_TTS_ENABLED", False), \
                mock.patch.object(cg, "brain", return_value="reply"), \
                mock.patch.object(cg, "tts_stream_to_job", side_effect=fail_before_chunks), \
                mock.patch.object(cg, "tts", side_effect=lambda _text: write_temp_mp3(b"full")), \
                mock.patch.object(cg.history_store, "append_turn"):
            cg.stream_pipeline(job, "user text", 0)

        self.assertEqual(job.chunks, [b"full"])
        self.assertTrue(job.done)
        self.assertIsNone(job.error)

    def test_stream_pipeline_brain_failure_remains_an_error(self):
        job = cg.Job("user text", session_id="brain-failure")
        with mock.patch.object(cg, "STREAM_TTS_ENABLED", False), \
                mock.patch.object(cg, "brain", side_effect=RuntimeError("brain failed")), \
                mock.patch.object(cg, "_log_turn_summary"):
            cg.stream_pipeline(job, "user text", 0)

        self.assertTrue(job.done)
        self.assertIsNotNone(job.error)
        self.assertIsNone(job.degraded)

    def test_stream_pipeline_no_reply_tts_failure_degrades_with_audio(self):
        job = cg.Job("user text", session_id="no-reply-degraded")
        with mock.patch.object(cg, "STREAM_TTS_ENABLED", False), \
                mock.patch.object(cg, "brain", return_value=""), \
                mock.patch.object(cg, "tts_stream_to_job", side_effect=RuntimeError("tts failed")), \
                mock.patch.object(cg, "SILENCE_1S", (b"silence", 1.0)), \
                mock.patch.object(cg, "_log_turn_summary") as turn_summary:
            cg.stream_pipeline(job, "user text", 0)

        self.assertTrue(job.done)
        self.assertIsNone(job.error)
        self.assertEqual(job.chunks, [b"silence"])
        self.assertEqual(job.degraded, "tts")
        self.assertEqual(turn_summary.call_args.args[-1], "fallback")

    def test_stream_pipeline_degradation_uses_fallback_mp3_without_silence_asset(self):
        job = cg.Job("user text", session_id="tts-degraded-fallback")
        with mock.patch.object(cg, "STREAM_TTS_ENABLED", False), \
                mock.patch.object(cg, "brain", return_value="reply text"), \
                mock.patch.object(cg, "tts_stream_to_job", side_effect=RuntimeError("stream failed")), \
                mock.patch.object(cg, "tts", side_effect=RuntimeError("batch failed")), \
                mock.patch.object(cg, "SILENCE_1S", None), \
                mock.patch.object(cg, "_log_turn_summary") as turn_summary, \
                mock.patch.object(cg.history_store, "append_turn"):
            cg.stream_pipeline(job, "user text", 0)

        self.assertTrue(job.done)
        self.assertIsNone(job.error)
        self.assertEqual(job.reply, "reply text")
        self.assertEqual(job.degraded, "tts")
        self.assertTrue(job.chunks)
        self.assertEqual(b"".join(job.chunks), cg._DEGRADED_FALLBACK_MP3)
        self.assertEqual(turn_summary.call_args.args[-1], "text_only")

    def test_stream_pipeline_degradation_prefers_loaded_silence_asset(self):
        job = cg.Job("user text", session_id="tts-degraded")
        with mock.patch.object(cg, "STREAM_TTS_ENABLED", False), \
                mock.patch.object(cg, "brain", return_value="reply text"), \
                mock.patch.object(cg, "tts_stream_to_job", side_effect=RuntimeError("stream failed")), \
                mock.patch.object(cg, "tts", side_effect=RuntimeError("batch failed")), \
                mock.patch.object(cg, "SILENCE_1S", (b"silence", 1.0)), \
                mock.patch.object(cg, "_log_turn_summary") as turn_summary, \
                mock.patch.object(cg.history_store, "append_turn"):
            cg.stream_pipeline(job, "user text", 0)

        self.assertTrue(job.done)
        self.assertIsNone(job.error)
        self.assertEqual(job.reply, "reply text")
        self.assertEqual(job.degraded, "tts")
        self.assertEqual(job.chunks, [b"silence"])
        self.assertEqual(turn_summary.call_args.args[-1], "text_only")

    def test_reply_header_reports_degraded_tts(self):
        job = cg.Job("user text")
        job.reply = "reply text"
        job.degraded = "tts"
        job.chunks = [b"audio"]
        job.done = True
        handler = object.__new__(cg.Handler)
        handler._send = mock.Mock()
        with cg.JOBS_LOCK:
            cg.JOBS["degraded-job"] = job
        self.addCleanup(cg.JOBS.pop, "degraded-job", None)

        handler._do_reply("degraded-job")

        self.assertEqual(handler._send.call_args.kwargs["extra"]["X-Degraded"], "tts")
        self.assertTrue(handler._send.call_args.args[1])

    def test_reply_header_omits_degraded_for_normal_reply(self):
        job = cg.Job("user text")
        job.reply = "reply text"
        job.chunks = [b"audio"]
        job.done = True
        handler = object.__new__(cg.Handler)
        handler._send = mock.Mock()
        with cg.JOBS_LOCK:
            cg.JOBS["normal-job"] = job
        self.addCleanup(cg.JOBS.pop, "normal-job", None)

        handler._do_reply("normal-job")

        self.assertNotIn("X-Degraded", handler._send.call_args.kwargs["extra"])

    def test_engine_fish_non_200_error_redacts_api_key_from_detail(self):
        raw_key = "fish-secret-key"
        FakeSequenceFishHTTPSConnection.responses = [
            FakeFishResponse(
                status=500,
                body=f"upstream echoed Authorization: Bearer {raw_key}".encode(),
            ) for _ in range(2)
        ]
        with patched_environment(
            CATY_TTS_ENGINE="fish",
            FISH_API_KEY=raw_key,
            FISH_RETRY_ATTEMPTS="2",
            FISH_BASE_URL=None,
            FISH_MODEL=None,
            FISH_LATENCY=None,
        ), \
                patched_voice("voice-1"), \
                mock.patch("caty_gateway.tts_fish.http.client.HTTPSConnection", FakeSequenceFishHTTPSConnection), \
                mock.patch("caty_gateway.tts_fish.random.uniform", return_value=0), \
                mock.patch("caty_gateway.tts_fish.time.sleep"):
            with self.assertRaises(RuntimeError) as cm:
                tts_fish.synthesize("hello", "voice-1")

        message = str(cm.exception)
        self.assertIn("Fish Audio TTS 500", message)
        self.assertIn("[REDACTED]", message)
        self.assertNotIn(raw_key, message)

    def test_engine_fish_rejects_api_key_with_control_characters_without_echoing_key(self):
        raw_key = "fish-secret\nkey"
        with patched_environment(
            CATY_TTS_ENGINE="fish",
            FISH_API_KEY=raw_key,
            FISH_BASE_URL=None,
            FISH_MODEL=None,
            FISH_LATENCY=None,
        ):
            with self.assertRaises(RuntimeError) as stream_cm:
                tts_fish.synthesize_stream("hello", "voice-1")
            with self.assertRaises(RuntimeError) as synthesize_cm:
                tts_fish.synthesize("hello", "voice-1")

        for exc in (stream_cm.exception, synthesize_cm.exception):
            message = str(exc)
            self.assertEqual(message, "FISH_API_KEY contains invalid characters")
            self.assertNotIn(raw_key, message)
            self.assertNotIn(raw_key, repr(exc))

    def test_engine_fish_redacts_api_key_when_request_raises_header_error(self):
        raw_key = "fish-secret-key"
        with patched_environment(
            CATY_TTS_ENGINE="fish",
            FISH_API_KEY=raw_key,
            FISH_BASE_URL=None,
            FISH_MODEL=None,
            FISH_LATENCY=None,
        ), \
                mock.patch("caty_gateway.tts_fish.http.client.HTTPSConnection", FakeFishRequestFailingConnection):
            with self.assertRaises(RuntimeError) as cm:
                tts_fish.synthesize("hello", "voice-1")

        assert_secret_not_reachable(self, cm.exception, raw_key)

    def test_engine_fish_success_ignores_close_error(self):
        raw_key = "fish-secret-key"
        with patched_environment(
            CATY_TTS_ENGINE="fish",
            FISH_API_KEY=raw_key,
            FISH_BASE_URL=None,
            FISH_MODEL=None,
            FISH_LATENCY=None,
        ), \
                mock.patch("caty_gateway.tts_fish.http.client.HTTPSConnection", FakeFishCloseFailingConnection):
            self.assertEqual(
                list(tts_fish.synthesize_stream("hello", "voice-1")),
                [b"mp3-a", b"mp3-b"],
            )
