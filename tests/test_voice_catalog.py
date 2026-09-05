import json
import os
import sys
import threading
import time
import unittest
from unittest import mock


from caty_gateway import tts_fish
from caty_gateway import voice_catalog


NEUTRAL_REFERENCE_ID = "0089dce5fefb4c6ba9b9f2f0debe1ddc"
LIVE_NEUTRAL_FIXTURE = {
    "_id": NEUTRAL_REFERENCE_ID,
    "type": "tts",
    "state": "trained",
    "visibility": "public",
    "title": "Neutral Japanese",
    "author": {"name": "Fish Author"},
    "languages": ["ja-JP"],
    "tags": ["direction:gentle", "impression:calm"],
    "dmca_taken_down": False,
    "samples": [{"url": "https://sample.example/neutral"}],
}


PUBLIC_FIXTURE = [
    {
        "_id": "ja-warm",
        "type": "tts",
        "state": "trained",
        "visibility": "public",
        "title": "Warm Japanese",
        "author": {"name": "Fish Author"},
        "source": "Community upload",
        "languages": ["Japanese"],
        "directions": ["gentle"],
        "impressions": ["warm"],
        "updated_at": "v2",
        "sample_url": "https://private.example/signed?credential=do-not-leak",
    },
    {
        "_id": "ja-clear",
        "type": "tts",
        "state": "trained",
        "visibility": "public",
        "title": "Clear Japanese",
        "author": "Second Author",
        "source": "Fish Audio",
        "languages": ["ja-JP"],
        "tags": ["direction:bright", "impression:clear"],
        "updated_at": "v1",
    },
    {
        "_id": "en-public",
        "type": "tts",
        "state": "trained",
        "visibility": "public",
        "title": "English Voice",
        "author": "English Author",
        "languages": ["en-US"],
        "updated_at": "v1",
    },
    {"_id": "private-bad", "type": "tts", "state": "trained", "visibility": "private"},
    {"_id": "dmca-bad", "type": "tts", "state": "trained", "visibility": "public", "dmca_taken_down": True},
    {"_id": "dmca-alias-bad", "type": "tts", "state": "trained", "visibility": "public", "dmca": True},
    {"_id": "training-bad", "type": "tts", "state": "training", "visibility": "public"},
    {"_id": "not-tts", "type": "svc", "state": "trained", "visibility": "public"},
]

SELF_FIXTURE = [
    {
        "_id": "self-private",
        "type": "tts",
        "state": "trained",
        "visibility": "private",
        "title": "Private voice",
        "author": "Owner",
        "source": "https://private.example/source",
        "languages": ["ja"],
        "updated_at": "private-v1",
        "samples": [{"url": "https://private.example/sample"}],
    },
    {
        "_id": "self-hidden",
        "type": "tts",
        "state": "trained",
        "visibility": "hidden",
        "title": "Hidden voice",
        "languages": ["ja"],
    },
    {
        "_id": "self-pending",
        "type": "tts",
        "state": "training",
        "visibility": "private",
        "title": "Pending voice",
        "languages": ["ja"],
    },
]


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.error = None
        self.model_responses = {}
        self.missing_refs = set()

    def __call__(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if self.error is not None:
            raise self.error
        if path == "/model":
            items = SELF_FIXTURE if (params or {}).get("self") == "true" else PUBLIC_FIXTURE
            return {"items": items, "total": len(items)}
        ref = path.rsplit("/", 1)[-1]
        if ref in self.missing_refs:
            raise tts_fish.FishTransportError(status=404)
        if ref in self.model_responses:
            return dict(self.model_responses[ref])
        if ref == NEUTRAL_REFERENCE_ID:
            return dict(LIVE_NEUTRAL_FIXTURE)
        for item in PUBLIC_FIXTURE + SELF_FIXTURE:
            if item["_id"] == ref:
                return dict(item)
        error = tts_fish.FishTransportError(status=404)
        raise error


class Clock:
    def __init__(self, value=100.0):
        self.value = value

    def __call__(self):
        return self.value


class VoiceCatalogTest(unittest.TestCase):
    def setUp(self):
        self.transport = FakeTransport()
        self.clock = Clock()
        self.service = voice_catalog.VoiceCatalogService(
            self.transport,
            installation_id="member-a",
            editorial_overlay={
                "ja-clear": {"rank": 1, "summary": "Caty editorial summary"},
            },
            clock=self.clock,
            cache_ttl=10,
            fetch_limit=100,
        )

    def test_recommended_is_editorial_overlay_not_allowlist(self):
        payload = self.service.list_voices(scope="recommended", language="all", page_size=10)

        self.assertEqual([item["title"] for item in payload["items"]], [
            "Clear Japanese", "Warm Japanese", "English Voice",
        ])
        self.assertTrue(payload["items"][0]["editorial"]["recommended"])
        self.assertEqual(payload["items"][0]["caty_summary"], "Caty editorial summary")
        self.assertTrue(any(not item["editorial"]["recommended"] for item in payload["items"]))
        self.assertNotIn("reference_id", payload["items"][0])

    def test_all_enforces_public_selection_and_default_japanese_can_be_removed(self):
        default = self.service.list_voices(scope="all", page_size=10)
        unfiltered = self.service.list_voices(scope="all", language="all", page_size=10)

        self.assertEqual({item["title"] for item in default["items"]}, {
            "Warm Japanese", "Clear Japanese",
        })
        self.assertEqual(len(unfiltered["items"]), 3)
        serialized = json.dumps(unfiltered, ensure_ascii=False)
        for excluded in ("private-bad", "dmca-bad", "dmca-alias-bad", "training-bad", "not-tts"):
            self.assertNotIn(excluded, serialized)

    def test_self_is_credential_partitioned_and_sanitized(self):
        old_key = os.environ.get("FISH_API_KEY")
        try:
            os.environ["FISH_API_KEY"] = "credential-one"
            first = self.service.list_voices(scope="self", page_size=10)
            first_call_count = len(self.transport.calls)
            os.environ["FISH_API_KEY"] = "credential-two"
            second = self.service.list_voices(scope="self", page_size=10)
        finally:
            if old_key is None:
                os.environ.pop("FISH_API_KEY", None)
            else:
                os.environ["FISH_API_KEY"] = old_key

        self.assertGreater(len(self.transport.calls), first_call_count)
        self.assertEqual(len(first["items"]), len(second["items"]))
        serialized = json.dumps(first, ensure_ascii=False)
        self.assertNotIn("private.example", serialized)
        self.assertNotIn("credential-one", serialized)
        by_title = {item["title"]: item for item in first["items"]}
        self.assertEqual(by_title["Private voice"]["availability"], "available")
        self.assertEqual(by_title["Hidden voice"]["availability"], "hidden")
        self.assertEqual(by_title["Pending voice"]["availability"], "unavailable")

    def test_opaque_pagination_search_and_direction_impression_filters(self):
        first = self.service.list_voices(scope="all", language="all", page_size=1)
        self.assertEqual(len(first["items"]), 1)
        self.assertIsInstance(first["next_cursor"], str)
        self.assertNotIn("offset", first["next_cursor"])
        second = self.service.list_voices(
            scope="all", language="all", page_size=1, cursor=first["next_cursor"]
        )
        self.assertNotEqual(first["items"][0]["catalog_id"], second["items"][0]["catalog_id"])

        searched = self.service.list_voices(scope="all", query="fish author", page_size=10)
        self.assertEqual([item["title"] for item in searched["items"]], ["Warm Japanese"])
        filtered = self.service.list_voices(
            scope="all", language="all", direction="bright", impression="clear", page_size=10
        )
        self.assertEqual([item["title"] for item in filtered["items"]], ["Clear Japanese"])
        with self.assertRaisesRegex(voice_catalog.CatalogError, "invalid_cursor"):
            self.service.list_voices(scope="all", language="all", cursor="not-a-cursor")

    def test_availability_normalization_covers_hidden_unavailable_unknown_dmca_private(self):
        self.assertEqual(voice_catalog.normalize_availability({"state": "trained", "visibility": "hidden"}), "hidden")
        self.assertEqual(voice_catalog.normalize_availability({"state": "failed", "visibility": "public"}), "unavailable")
        self.assertEqual(voice_catalog.normalize_availability({"state": "mystery", "visibility": "public"}), "unknown")
        self.assertEqual(voice_catalog.normalize_availability({"state": "trained", "visibility": "public", "dmca_taken_down": True}), "unavailable")
        self.assertEqual(voice_catalog.normalize_availability({"state": "trained", "visibility": "public", "dmca": True}), "unavailable")
        self.assertEqual(voice_catalog.normalize_availability({"state": "trained", "visibility": "private"}), "unavailable")
        self.assertEqual(voice_catalog.normalize_availability({"state": "trained", "visibility": "private"}, self_scope=True), "available")

    def test_stale_last_good_is_returned_for_timeout_429_and_5xx(self):
        fresh = self.service.list_voices(scope="all", page_size=10)
        self.assertFalse(fresh["stale"])
        self.clock.value += 11
        for error in (
            TimeoutError("raw timeout detail"),
            tts_fish.FishTransportError(status=429, retry_after="17"),
            tts_fish.FishTransportError(status=503),
        ):
            self.transport.error = error
            stale = self.service.list_voices(scope="all", page_size=10)
            self.assertTrue(stale["stale"])
            self.clock.value += 1

    def test_uncached_upstream_failures_are_actionable_and_redacted(self):
        self.transport.error = tts_fish.FishTransportError(status=429, retry_after="12")
        with self.assertRaises(voice_catalog.CatalogUpstreamError) as caught:
            self.service.list_voices(scope="self")
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(caught.exception.retry_after, 12)
        self.assertNotIn("credential", str(caught.exception))

    def test_missing_fish_credential_is_non_retryable_configuration_error(self):
        service = voice_catalog.VoiceCatalogService(
            tts_fish.get_json,
            installation_id="member-a",
            clock=self.clock,
        )
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FISH_API_KEY", None)
            with self.assertRaises(voice_catalog.CatalogUpstreamError) as caught:
                service.list_voices(scope="all")
        self.assertEqual(caught.exception.code, "catalog_not_configured")
        self.assertEqual(caught.exception.status, 503)
        self.assertFalse(caught.exception.retryable)
        self.assertIsNone(caught.exception.retry_after)

    def test_snapshot_single_flight_coalesces_concurrent_fetches(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def transport(path, params=None):
            calls.append((path, params))
            entered.set()
            release.wait(2)
            return {"items": PUBLIC_FIXTURE, "total": len(PUBLIC_FIXTURE)}

        service = voice_catalog.VoiceCatalogService(
            transport,
            installation_id="member-a",
            clock=self.clock,
            fetch_limit=100,
        )
        results = []
        errors = []

        def load():
            try:
                results.append(service.list_voices(scope="all", language="all"))
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=load) for _ in range(4)]
        for thread in threads:
            thread.start()
        self.assertTrue(entered.wait(1))
        time.sleep(0.05)
        release.set()
        for thread in threads:
            thread.join(2)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 4)
        self.assertEqual(len(calls), 1)

    def test_snapshot_cache_prunes_expired_partitions_and_is_bounded(self):
        service = voice_catalog.VoiceCatalogService(
            self.transport,
            installation_id="member-a",
            clock=self.clock,
            cache_ttl=10,
            fetch_limit=100,
            max_snapshot_entries=2,
        )
        with mock.patch.dict(os.environ, {}, clear=False):
            for index in range(4):
                os.environ["FISH_API_KEY"] = f"credential-{index}"
                service.list_voices(scope="self", page_size=10)
                self.clock.value += 11
            current_key = ("self", voice_catalog.credential_partition("member-a"))

        self.assertLessEqual(len(service._snapshots), 2)
        self.assertIn(current_key, service._snapshots)

    def test_stale_catalog_is_denied_for_credential_and_definitive_4xx(self):
        self.service.list_voices(scope="self", page_size=10)
        self.clock.value += 11
        for status, expected in (
            (401, "catalog_credentials_rejected"),
            (403, "catalog_credentials_rejected"),
            (400, "catalog_request_rejected"),
        ):
            self.transport.error = tts_fish.FishTransportError(status=status)
            with self.assertRaises(voice_catalog.CatalogUpstreamError) as caught:
                self.service.list_voices(scope="self", page_size=10)
            self.assertEqual(caught.exception.code, expected)
            self.assertFalse(caught.exception.allow_stale)
            self.assertFalse(caught.exception.retryable)

    def test_dmca_alias_is_rejected_during_preview_revalidation(self):
        with self.assertRaises(voice_catalog.CatalogVoiceUnavailable) as caught:
            self.service.resolve_preview(reference_id="dmca-alias-bad")
        self.assertEqual(caught.exception.code, "voice_unavailable")

    def test_diagnostic_public_reference_resolves_to_private_partition(self):
        diagnostic = self.service.resolve_preview(reference_id="ja-warm")
        public_id = voice_catalog.make_catalog_id("all", "ja-warm", "v2")
        public = self.service.resolve_preview(catalog_id=public_id)

        self.assertEqual(diagnostic["scope"], "self")
        self.assertNotEqual(diagnostic["cache_partition"], "shared")
        self.assertEqual(public["scope"], "public")
        self.assertEqual(public["cache_partition"], "shared")

    def test_catalog_id_round_trip_is_opaque_and_source_scoped(self):
        catalog_id = voice_catalog.make_catalog_id("self", "private/reference", "revision-7")
        parsed = voice_catalog.parse_catalog_id(catalog_id)
        self.assertEqual(parsed["scope"], "self")
        self.assertEqual(parsed["reference_id"], "private/reference")
        self.assertEqual(parsed["source_version"], "revision-7")

    def test_preset_catalog_id_resolves_to_public_shared_preview(self):
        resolved = self.service.resolve_preview(catalog_id="fish-neutral-ja-v1")

        self.assertEqual(resolved["reference_id"], NEUTRAL_REFERENCE_ID)
        self.assertEqual(resolved["scope"], "public")
        self.assertEqual(resolved["cache_partition"], "shared")
        self.assertEqual(resolved["preset_id"], "fish-neutral-ja-v1")
        self.assertIsNone(resolved["hint_source_version"])

    def test_preset_catalog_id_fails_closed_when_voice_is_no_longer_public(self):
        private_voice = dict(LIVE_NEUTRAL_FIXTURE)
        private_voice["visibility"] = "private"
        self.transport.model_responses[NEUTRAL_REFERENCE_ID] = private_voice

        with self.assertRaises(voice_catalog.CatalogVoiceUnavailable) as caught:
            self.service.resolve_preview(catalog_id="fish-neutral-ja-v1")

        self.assertEqual(caught.exception.code, "voice_unavailable")

    def test_preset_catalog_id_dmca_and_missing_reference_are_rejected(self):
        dmca_voice = dict(LIVE_NEUTRAL_FIXTURE)
        dmca_voice["dmca_taken_down"] = True
        self.transport.model_responses[NEUTRAL_REFERENCE_ID] = dmca_voice
        with self.assertRaises(voice_catalog.CatalogVoiceUnavailable) as caught:
            self.service.resolve_preview(catalog_id="fish-neutral-ja-v1")
        self.assertEqual(caught.exception.code, "voice_unavailable")

        self.transport.model_responses.pop(NEUTRAL_REFERENCE_ID, None)
        self.transport.missing_refs.add(NEUTRAL_REFERENCE_ID)
        with self.assertRaises(voice_catalog.CatalogVoiceUnavailable) as missing:
            self.service.resolve_preview(catalog_id="fish-neutral-ja-v1")
        self.assertEqual(missing.exception.code, "voice_not_found")

    def test_unknown_logical_preset_id_still_raises_invalid_catalog_id(self):
        with self.assertRaises(voice_catalog.CatalogError) as caught:
            voice_catalog.parse_catalog_id("fish-neutral-ja-v999")
        self.assertEqual(caught.exception.code, "invalid_catalog_id")

    def test_upstream_fields_cannot_echo_host_credential(self):
        raw_key = "credential-echo-canary"
        raw = {
            "_id": "echo-test",
            "type": "tts",
            "state": "trained",
            "visibility": "public",
            "title": "echo " + raw_key,
            "author": "Bearer " + raw_key,
            "source": raw_key,
            "languages": ["ja"],
            "version": raw_key,
        }
        with mock.patch.dict(os.environ, {"FISH_API_KEY": raw_key}, clear=False):
            normalized = voice_catalog.normalize_voice(raw)
        serialized = json.dumps(normalized, ensure_ascii=False)
        self.assertNotIn(raw_key, serialized)
        self.assertIn("[REDACTED]", serialized)


class FakeCatalogResponse:
    def __init__(self, status, body, retry_after=None):
        self.status = status
        self.body = body
        self.retry_after = retry_after

    def read(self, size=-1):
        return self.body[:size] if size >= 0 else self.body

    def getheader(self, name):
        return self.retry_after if name.lower() == "retry-after" else None


class FakeCatalogConnection:
    response = None
    request_data = None

    def __init__(self, host, port=None, timeout=None):
        self.host = host

    def request(self, method, path, body=None, headers=None):
        type(self).request_data = (method, path, dict(headers or {}))

    def getresponse(self):
        return type(self).response

    def close(self):
        pass


class FishCatalogTransportTest(unittest.TestCase):
    def test_get_json_uses_host_credential_and_never_exposes_upstream_body(self):
        raw_key = "fish-credential-canary"
        FakeCatalogConnection.response = FakeCatalogResponse(
            503,
            ("raw upstream Authorization: Bearer " + raw_key).encode("utf-8"),
        )
        with mock.patch.dict(os.environ, {"FISH_API_KEY": raw_key}, clear=False), \
                mock.patch.object(tts_fish.http.client, "HTTPSConnection", FakeCatalogConnection):
            with self.assertRaises(tts_fish.FishTransportError) as caught:
                tts_fish.get_json("/model", {"type": "tts"})

        self.assertEqual(caught.exception.status, 503)
        self.assertNotIn(raw_key, str(caught.exception))
        self.assertNotIn("raw upstream", str(caught.exception))
        method, path, headers = FakeCatalogConnection.request_data
        self.assertEqual(method, "GET")
        self.assertIn("type=tts", path)
        self.assertEqual(headers["Authorization"], "Bearer " + raw_key)


if __name__ == "__main__":
    unittest.main()
