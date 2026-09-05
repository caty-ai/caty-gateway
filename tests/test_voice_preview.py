import io
import json
import os
import sys
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock


from caty_gateway import caty_gateway as cg
from caty_gateway import voice_catalog
from caty_gateway import voice_preview


class Clock:
    def __init__(self, value=100.0):
        self.value = value

    def __call__(self):
        return self.value


class FakeCatalog:
    def __init__(self):
        self.calls = []
        self.error = None
        self.version = "source-v1"
        self.private_partition = "installation-credential-a"

    def resolve_preview(self, catalog_id=None, reference_id=None):
        self.calls.append((catalog_id, reference_id))
        if self.error is not None:
            raise self.error
        if catalog_id:
            hint = voice_catalog.parse_catalog_id(catalog_id)
            scope = hint["scope"]
            ref = hint["reference_id"]
        else:
            scope = "self"
            ref = reference_id
        return {
            "provider": "fish",
            "scope": scope,
            "reference_id": ref,
            "source_version": self.version,
            "hint_source_version": self.version,
            "cache_partition": self.private_partition if scope == "self" else "shared",
            "availability": "available",
        }


class Synthesizer:
    def __init__(self, chunks=None, error=None):
        self.calls = []
        self.chunks = list(chunks or [b"ID3preview-audio"])
        self.error = error

    def __call__(self, text, reference_id):
        self.calls.append((text, reference_id))
        if self.error is not None:
            raise self.error
        return iter(self.chunks)


class VoicePreviewServiceTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.catalog = FakeCatalog()
        self.synth = Synthesizer()
        self.contract = ["fish-contract-from-1032"]
        self.service = self.make_service()

    def make_service(self, **kwargs):
        defaults = {
            "catalog": self.catalog,
            "synthesizer": self.synth,
            "inference_contract_version": lambda: self.contract[0],
            "installation_id": "installation-a",
            "duration_probe": lambda _audio: 3.25,
            "clock": self.clock,
            "cache_ttl": 60,
            "rate_per_minute": 100,
            "max_audio_size": 1024,
            "max_duration": 10,
            "single_flight_timeout": 2,
        }
        defaults.update(kwargs)
        return voice_preview.VoicePreviewService(**defaults)

    @staticmethod
    def catalog_id(scope="all", ref="voice-a", version="source-v1"):
        return voice_catalog.make_catalog_id(scope, ref, version)

    def test_fixed_script_only_cache_hit_and_contract_version_in_key(self):
        first = self.service.preview("principal", catalog_id=self.catalog_id())
        second = self.service.preview("principal", catalog_id=self.catalog_id())

        self.assertEqual(first["cache"], "miss")
        self.assertEqual(second["cache"], "hit")
        self.assertEqual(len(self.synth.calls), 1)
        self.assertEqual(self.synth.calls[0], (voice_preview.SCRIPT_TEXT, "voice-a"))
        self.assertEqual(first["script_id"], "voice-picker-ja-v1")
        self.assertEqual(first["inference_contract_version"], "fish-contract-from-1032")

        self.contract[0] = "fish-contract-new-from-1032"
        third = self.service.preview("principal", catalog_id=self.catalog_id())
        self.assertEqual(third["cache"], "miss")
        self.assertEqual(len(self.synth.calls), 2)

    def test_logical_neutral_preset_catalog_id_uses_shared_cache_path(self):
        first = self.service.preview("principal", catalog_id="fish-neutral-ja-v1")
        second = self.service.preview("principal", catalog_id="fish-neutral-ja-v1")

        self.assertEqual(first["cache"], "miss")
        self.assertEqual(second["cache"], "hit")
        self.assertEqual(self.synth.calls, [(voice_preview.SCRIPT_TEXT, "0089dce5fefb4c6ba9b9f2f0debe1ddc")])

    def test_single_flight_coalesces_same_miss_to_one_generation(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def synthesize(text, reference_id):
            calls.append((text, reference_id))

            def chunks():
                entered.set()
                release.wait(2)
                yield b"ID3coalesced"
            return chunks()

        service = self.make_service(synthesizer=synthesize)
        results = []
        errors = []

        def request():
            try:
                results.append(service.preview("principal", catalog_id=self.catalog_id()))
            except Exception as error:
                errors.append(error)

        first = threading.Thread(target=request)
        second = threading.Thread(target=request)
        first.start()
        self.assertTrue(entered.wait(1))
        second.start()
        time.sleep(0.05)
        release.set()
        first.join(2)
        second.join(2)

        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 1)
        self.assertEqual({result["cache"] for result in results}, {"miss", "coalesced"})

    def test_public_and_self_private_cache_never_cross(self):
        public = self.catalog_id("all", ref="same-ref")
        private = self.catalog_id("self", ref="same-ref")

        public_result = self.service.preview("principal", catalog_id=public)
        private_result = self.service.preview("principal", catalog_id=private)
        public_again = self.service.preview("principal", catalog_id=public)

        self.assertEqual(public_result["cache"], "miss")
        self.assertEqual(private_result["cache"], "miss")
        self.assertEqual(public_again["cache"], "hit")
        self.assertEqual(len(self.synth.calls), 2)

    def test_private_cache_partition_change_forces_new_generation(self):
        private = self.catalog_id("self")
        self.service.preview("principal", catalog_id=private)
        self.catalog.private_partition = "installation-credential-b"
        result = self.service.preview("principal", catalog_id=private)

        self.assertEqual(result["cache"], "miss")
        self.assertEqual(len(self.synth.calls), 2)

    def test_ttl_expiry_regenerates(self):
        self.service.preview("principal", catalog_id=self.catalog_id())
        self.clock.value += 61
        result = self.service.preview("principal", catalog_id=self.catalog_id())
        self.assertEqual(result["cache"], "miss")
        self.assertEqual(len(self.synth.calls), 2)

    def test_rate_limit_returns_retry_after_even_for_cache_hit(self):
        service = self.make_service(rate_per_minute=1)
        service.preview("principal", catalog_id=self.catalog_id())
        with self.assertRaises(voice_preview.PreviewRateLimited) as caught:
            service.preview("principal", catalog_id=self.catalog_id())
        self.assertEqual(caught.exception.status, 429)
        self.assertGreaterEqual(caught.exception.retry_after, 1)

    def test_size_and_duration_caps_fail_closed_without_cache(self):
        oversized = self.make_service(
            synthesizer=Synthesizer([b"ID3", b"x" * 20]), max_audio_size=10
        )
        with self.assertRaisesRegex(voice_preview.PreviewError, "preview_audio_too_large"):
            oversized.preview("principal", catalog_id=self.catalog_id())

        too_long = self.make_service(duration_probe=lambda _audio: 10.1, max_duration=10)
        with self.assertRaisesRegex(voice_preview.PreviewError, "preview_audio_too_long"):
            too_long.preview("principal", catalog_id=self.catalog_id())

    def test_ffprobe_unavailable_caches_paid_audio_with_unknown_duration(self):
        with mock.patch.dict(os.environ, {"FFPROBE_BIN": "/nonexistent"}, clear=False):
            service = self.make_service(duration_probe=voice_preview.probe_mp3_duration)
            first = service.preview("principal", catalog_id=self.catalog_id())
            second = service.preview("principal", catalog_id=self.catalog_id())

        self.assertEqual(first["cache"], "miss")
        self.assertEqual(second["cache"], "hit")
        self.assertIsNone(first["duration_seconds"])
        self.assertEqual(len(self.synth.calls), 1)

    def test_duration_cap_negative_cache_prevents_repeat_billing_until_ttl(self):
        service = self.make_service(
            duration_probe=lambda _audio: 10.1,
            max_duration=10,
            negative_cache_ttl=5,
        )
        for _ in range(2):
            with self.assertRaisesRegex(voice_preview.PreviewError, "preview_audio_too_long"):
                service.preview("principal", catalog_id=self.catalog_id())
        self.assertEqual(len(self.synth.calls), 1)

        self.clock.value += 6
        with self.assertRaisesRegex(voice_preview.PreviewError, "preview_audio_too_long"):
            service.preview("principal", catalog_id=self.catalog_id())
        self.assertEqual(len(self.synth.calls), 2)

    def test_invalid_identifier_does_not_consume_rate_limit_budget(self):
        service = self.make_service(rate_per_minute=1)
        for kwargs, expected in (
            ({"catalog_id": "not-a-catalog-id"}, "invalid_catalog_id"),
            ({"reference_id": "x" * 161}, "invalid_reference_id"),
        ):
            with self.assertRaisesRegex(voice_preview.PreviewError, expected):
                service.preview("principal", **kwargs)

        result = service.preview("principal", catalog_id=self.catalog_id())
        self.assertEqual(result["cache"], "miss")

    def test_cache_evicts_by_total_audio_bytes_as_well_as_entry_count(self):
        synth = Synthesizer([b"ID3abc"])
        service = self.make_service(
            synthesizer=synth,
            max_audio_size=10,
            max_cache_entries=10,
            max_cache_bytes=12,
        )
        for ref in ("voice-a", "voice-b", "voice-c"):
            service.preview("principal", catalog_id=self.catalog_id(ref=ref))

        self.assertLessEqual(service._cache_bytes, 12)
        self.assertEqual(len(service._cache), 2)
        service.preview("principal", catalog_id=self.catalog_id(ref="voice-a"))
        self.assertEqual(len(synth.calls), 4)

    def test_offline_serves_cached_only_and_transient_failure_is_stale(self):
        online = self.service.preview("principal", catalog_id=self.catalog_id())
        self.assertEqual(online["cache"], "miss")
        offline = self.service.preview("principal", catalog_id=self.catalog_id(), offline=True)
        self.assertEqual(offline["cache"], "hit")
        self.assertTrue(offline["stale"])
        with self.assertRaisesRegex(voice_preview.PreviewError, "preview_unavailable_offline"):
            self.service.preview(
                "principal", catalog_id=self.catalog_id(ref="uncached"), offline=True
            )

        self.catalog.error = voice_catalog.CatalogUpstreamError(
            "catalog_temporarily_unavailable", 503, retry_after=20, allow_stale=True
        )
        stale = self.service.preview("principal", catalog_id=self.catalog_id())
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["cache"], "hit")

    def test_definitive_404_dmca_or_private_invalidation_blocks_cached_replay(self):
        self.service.preview("principal", catalog_id=self.catalog_id())
        self.catalog.error = voice_catalog.CatalogVoiceUnavailable(
            "voice_unavailable", reference_id="voice-a"
        )
        with self.assertRaisesRegex(voice_preview.PreviewError, "voice_unavailable"):
            self.service.preview("principal", catalog_id=self.catalog_id())

        self.catalog.error = voice_catalog.CatalogUpstreamError(
            "catalog_temporarily_unavailable", 503
        )
        with self.assertRaisesRegex(voice_preview.PreviewError, "catalog_temporarily_unavailable"):
            self.service.preview("principal", catalog_id=self.catalog_id())

    def test_raw_synthesis_error_and_credential_are_redacted(self):
        canary = "fish-secret-and-raw-upstream-body"
        service = self.make_service(synthesizer=Synthesizer(error=RuntimeError(canary)))
        with self.assertRaises(voice_preview.PreviewError) as caught:
            service.preview("principal", catalog_id=self.catalog_id())
        self.assertEqual(caught.exception.code, "preview_generation_failed")
        self.assertNotIn(canary, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_diagnostic_reference_id_is_private_scoped(self):
        result = self.service.preview("principal", reference_id="diagnostic-ref")
        self.assertEqual(result["cache"], "miss")
        self.assertEqual(self.catalog.calls[-1], (None, "diagnostic-ref"))

    def test_diagnostic_public_voice_never_warms_or_reuses_shared_cache(self):
        diagnostic = self.service.preview("principal", reference_id="same-public-ref")
        public_id = self.catalog_id("all", ref="same-public-ref")
        public = self.service.preview("principal", catalog_id=public_id)
        diagnostic_again = self.service.preview("principal", reference_id="same-public-ref")

        self.assertEqual(diagnostic["cache"], "miss")
        self.assertEqual(public["cache"], "miss")
        self.assertEqual(diagnostic_again["cache"], "hit")
        self.assertEqual(len(self.synth.calls), 2)

    def test_private_cached_preview_is_denied_on_credential_failure(self):
        private_id = self.catalog_id("self", ref="private-ref")
        self.service.preview("principal", catalog_id=private_id)
        self.catalog.error = voice_catalog.CatalogUpstreamError(
            "catalog_credentials_rejected",
            503,
            allow_stale=False,
            retryable=False,
        )

        with self.assertRaises(voice_preview.PreviewError) as caught:
            self.service.preview("principal", catalog_id=private_id)
        self.assertEqual(caught.exception.code, "catalog_credentials_rejected")
        self.assertFalse(caught.exception.retryable)

    def test_rate_limiter_globally_prunes_and_hard_bounds_principals(self):
        limiter = voice_preview._RateLimiter(
            limit=2, window=60, clock=self.clock, max_principals=2
        )
        self.assertIsNone(limiter.check("principal-a"))
        self.assertIsNone(limiter.check("principal-b"))
        self.assertIsNone(limiter.check("principal-a"))  # refresh LRU order
        self.assertIsNone(limiter.check("principal-c"))
        self.assertEqual(list(limiter._events), ["principal-a", "principal-c"])

        self.clock.value += 61
        self.assertIsNone(limiter.check("principal-d"))
        self.assertEqual(list(limiter._events), ["principal-d"])


class NonClosingBytesIO(io.BytesIO):
    def close(self):
        pass


class MemorySocket:
    def __init__(self, request_bytes):
        self.input = io.BytesIO(request_bytes)
        self.output = NonClosingBytesIO()

    def makefile(self, mode, *args, **kwargs):
        return self.input if "r" in mode else self.output

    def sendall(self, data):
        self.output.write(data)

    def settimeout(self, _timeout):
        pass

    def shutdown(self, _how):
        pass

    def close(self):
        pass


class MemoryServer:
    server_name = "127.0.0.1"
    server_port = 0


def request(method, path, payload=None, headers=None, raw_body=None):
    body = raw_body if raw_body is not None else (
        b"" if payload is None else json.dumps(payload).encode("utf-8")
    )
    headers = dict(headers or {})
    headers.setdefault("Host", "127.0.0.1")
    headers.setdefault("Connection", "close")
    headers.setdefault("Content-Length", str(len(body)))
    if payload is not None:
        headers.setdefault("Content-Type", "application/json")
    raw = [f"{method} {path} HTTP/1.1"]
    raw.extend(f"{key}: {value}" for key, value in headers.items())
    request_bytes = ("\r\n".join(raw) + "\r\n\r\n").encode("latin-1") + body
    sock = MemorySocket(request_bytes)
    cg.Handler(sock, ("127.0.0.1", 0), MemoryServer())
    response = sock.output.getvalue()
    head, _, response_body = response.partition(b"\r\n\r\n")
    lines = head.decode("iso-8859-1").split("\r\n")
    status = int(lines[0].split()[1])
    response_headers = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            response_headers[key.lower()] = value.strip()
    length = int(response_headers.get("content-length", "0"))
    return status, response_headers, response_body[:length]


class FakeHTTPPreviewService:
    def __init__(self):
        self.calls = []
        self.error = None

    def preview(self, principal, catalog_id=None, reference_id=None):
        self.calls.append((principal, catalog_id, reference_id))
        if self.error is not None:
            raise self.error
        return {
            "audio": b"ID3http-preview",
            "content_type": "audio/mpeg",
            "script_id": voice_preview.SCRIPT_ID,
            "inference_contract_version": "contract-from-1032",
            "cache": "miss",
            "stale": False,
            "duration_seconds": 2.5,
        }


class FakeHTTPCatalogService:
    def list_voices(self, **kwargs):
        return {
            "scope": kwargs["scope"],
            "items": [],
            "next_cursor": None,
            "page_size": int(kwargs["page_size"]),
            "stale": False,
            "filters": {"language": "ja", "query": None, "direction": None, "impression": None},
        }


class VoiceHTTPRouteTest(unittest.TestCase):
    def setUp(self):
        self.old = {
            "CATY_TOKEN": cg.CATY_TOKEN,
            "CATY_ADMIN_TOKEN": cg.CATY_ADMIN_TOKEN,
            "VOICE_SCOPE_AUTHORIZER": cg.VOICE_SCOPE_AUTHORIZER,
            "_voice_catalog_service": cg._voice_catalog_service,
            "_voice_preview_service": cg._voice_preview_service,
        }
        cg.CATY_TOKEN = ""
        cg.CATY_ADMIN_TOKEN = ""
        cg.VOICE_SCOPE_AUTHORIZER = None
        cg._voice_catalog_service = FakeHTTPCatalogService()
        cg._voice_preview_service = FakeHTTPPreviewService()

    def tearDown(self):
        for key, value in self.old.items():
            setattr(cg, key, value)

    def test_voice_routes_fail_closed_while_legacy_health_remains_open(self):
        status, _, _ = request("GET", "/health")
        self.assertEqual(status, 200)
        for method, path, payload in (
            ("GET", "/tts/voices", None),
            ("POST", "/tts/voices/preview", {"catalog_id": "anything"}),
        ):
            status, headers, body = request(method, path, payload)
            self.assertEqual(status, 401)
            self.assertEqual(json.loads(body)["error"], "unauthorized")
            self.assertEqual(headers["www-authenticate"], "Bearer")

    def test_catalog_and_preview_accept_current_token_and_scoped_authorizer_seam(self):
        cg.CATY_TOKEN = "client-token"
        auth = {"Authorization": "Bearer client-token"}
        status, _, body = request("GET", "/tts/voices?scope=all&language=all&page_size=3", headers=auth)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["scope"], "all")

        status, headers, body = request(
            "POST", "/tts/voices/preview", {"reference_id": "diagnostic"}, headers=auth
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"ID3http-preview")
        self.assertEqual(headers["content-type"], "audio/mpeg")
        self.assertEqual(headers["x-voice-preview-script-id"], voice_preview.SCRIPT_ID)
        self.assertEqual(headers["x-inference-contract-version"], "contract-from-1032")

        status, _, body = request(
            "POST",
            "/tts/voices/preview",
            {"catalog_id": "fish-neutral-ja-v1"},
            headers=auth,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"ID3http-preview")
        self.assertEqual(
            cg._voice_preview_service.calls[-1][1:],
            ("fish-neutral-ja-v1", None),
        )

        cg.CATY_TOKEN = ""
        cg.VOICE_SCOPE_AUTHORIZER = lambda tokens, capability: (
            "scoped-installation-principal" if tokens == ("scoped-token",) and capability == "voice_catalog:read" else None
        )
        status, _, _ = request(
            "GET", "/tts/voices", headers={"Authorization": "Bearer scoped-token"}
        )
        self.assertEqual(status, 200)

    def test_voice_routes_accept_admin_token_only_with_distinct_principal(self):
        cg.CATY_ADMIN_TOKEN = "admin-token"
        auth = {"Authorization": "Bearer admin-token"}
        status, _, _ = request("GET", "/tts/voices", headers=auth)
        self.assertEqual(status, 200)
        status, _, _ = request(
            "POST", "/tts/voices/preview", {"reference_id": "diagnostic"}, headers=auth
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            cg._voice_preview_service.calls[-1][0],
            voice_preview.request_principal("admin-token"),
        )

    def test_json_null_returns_explicit_object_required_error(self):
        cg.CATY_TOKEN = "client-token"
        status, _, body = request(
            "POST",
            "/tts/voices/preview",
            headers={"Authorization": "Bearer client-token"},
            raw_body=b"null",
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "json_object_required")
        self.assertEqual(cg._voice_preview_service.calls, [])

    def test_unknown_duration_omits_duration_header(self):
        cg.CATY_TOKEN = "client-token"
        service = cg._voice_preview_service
        original_preview = service.preview

        def preview(*args, **kwargs):
            result = original_preview(*args, **kwargs)
            result["duration_seconds"] = None
            return result

        service.preview = preview
        status, headers, _ = request(
            "POST",
            "/tts/voices/preview",
            {"reference_id": "diagnostic"},
            headers={"Authorization": "Bearer client-token"},
        )
        self.assertEqual(status, 200)
        self.assertNotIn("x-voice-preview-duration", headers)

    def test_arbitrary_text_and_unknown_fields_never_reach_tts(self):
        cg.CATY_TOKEN = "client-token"
        service = cg._voice_preview_service
        auth = {"Authorization": "Bearer client-token"}
        for payload, expected in (
            ({"catalog_id": "id", "text": "charge arbitrary text"}, "arbitrary_text_not_allowed"),
            ({"catalog_id": "id", "language": "ja"}, "invalid_preview_fields"),
            ({"catalog_id": "id", "reference_id": "ref"}, "exactly_one_voice_identifier_required"),
        ):
            status, _, body = request("POST", "/tts/voices/preview", payload, headers=auth)
            self.assertEqual(status, 400)
            self.assertEqual(json.loads(body)["error"], expected)
        self.assertEqual(service.calls, [])

    def test_rate_limit_and_raw_failure_are_actionable_and_redacted(self):
        cg.CATY_TOKEN = "client-token"
        auth = {"Authorization": "Bearer client-token"}
        service = cg._voice_preview_service
        service.error = voice_preview.PreviewError("preview_rate_limited", 429, retry_after=9)
        status, headers, body = request(
            "POST", "/tts/voices/preview", {"reference_id": "voice"}, headers=auth
        )
        self.assertEqual(status, 429)
        self.assertEqual(headers["retry-after"], "9")
        self.assertTrue(json.loads(body)["retryable"])

        canary = "fish-api-key-and-raw-upstream-canary"
        service.error = voice_preview.PreviewError("preview_generation_failed", 502)
        captured = io.StringIO()
        with redirect_stdout(captured), redirect_stderr(captured):
            status, _, body = request(
                "POST", "/tts/voices/preview", {"reference_id": canary}, headers=auth
            )
        self.assertEqual(status, 502)
        self.assertNotIn(canary, body.decode("utf-8"))
        self.assertNotIn(canary, captured.getvalue())


if __name__ == "__main__":
    unittest.main()
