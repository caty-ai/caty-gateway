import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


from caty_gateway import caty_config
from caty_gateway import caty_gateway as cg
from caty_gateway import filler_pack
from caty_gateway import filler_texts
from caty_gateway import setup_orchestrator
from caty_gateway import voice_activation
from caty_gateway import voice_catalog
from caty_gateway import voice_presets


class FakeCatalog:
    def __init__(self):
        self.error = None
        self.calls = []
        self.entered = None
        self.release = None
        self.listed_items = []
        self.list_calls = 0

    def resolve_preview(self, catalog_id=None, reference_id=None):
        self.calls.append((catalog_id, reference_id))
        if self.entered:
            self.entered.set()
        if self.release:
            self.release.wait(2)
        if self.error:
            raise self.error
        ref = reference_id
        preset_id = ""
        if catalog_id:
            hint = voice_catalog.parse_catalog_id(catalog_id)
            ref = hint["reference_id"]
            preset_id = hint.get("preset_id", "")
        return {
            "provider": "fish",
            "scope": "public" if catalog_id else "self",
            "reference_id": ref,
            "source_version": "v1",
            "hint_source_version": "v1" if catalog_id else None,
            "cache_partition": "shared" if catalog_id else "test-private",
            "availability": "available",
            "preset_id": preset_id,
        }

    def list_voices(self, **_kwargs):
        self.list_calls += 1
        return {"items": list(self.listed_items), "next_cursor": None}


class FakeRegistry:
    def __init__(self, root):
        self.root = Path(root)
        self.packs = {}
        self.stage_error = None
        self.stage_calls = 0
        self.gc_calls = []
        self.pinned = []
        self.recovered = 0

    def recover_staging(self):
        self.recovered += 1
        return {}

    def list_packs(self):
        return [
            {
                "pack_id": pack_id,
                "status": pack["status"],
                "generated_for": {
                    "provider": pack["provider"],
                    "reference_id": pack["reference_id"],
                    "preset_id": pack.get("preset_id"),
                },
            }
            for pack_id, pack in self.packs.items()
        ]

    def stage_pack(self, **kwargs):
        self.stage_calls += 1
        if self.stage_error:
            if callable(self.stage_error):
                return self.stage_error()
            raise self.stage_error
        pack_id = f"pack-{self.stage_calls}"
        self.packs[pack_id] = {
            "status": "ready",
            "provider": kwargs["generated_for_provider"],
            "reference_id": kwargs["generated_for_reference_id"],
            "preset_id": kwargs.get("preset_id"),
            "filler_text_version": kwargs["filler_text_version"],
            "texts": kwargs["texts"],
            "audio": b"ID3matching",
        }
        return {"pack_id": pack_id, "status": "ready"}

    def resolve(
        self, pack_id, *, active_provider, active_reference_id,
        expected_text_version=None,
    ):
        pack = self.packs.get(pack_id)
        if not pack:
            return {"pack_id": pack_id, "status": "unavailable", "files": {}}
        if pack["provider"] != active_provider or pack["reference_id"] != active_reference_id:
            return {"pack_id": pack_id, "status": "stale", "files": {}}
        if (
            expected_text_version is not None
            and pack.get("filler_text_version") != expected_text_version
        ):
            return {"pack_id": pack_id, "status": "stale", "reason": "text", "files": {}}
        return {"pack_id": pack_id, "status": pack["status"], "files": {}}

    def inspect(self, pack_id):
        pack = self.packs.get(pack_id)
        if not pack:
            return {"pack_id": pack_id, "status": "unavailable"}
        return {
            "pack_id": pack_id, "status": pack["status"],
            "preset_id": pack.get("preset_id"),
            "filler_text_version": pack.get(
                "filler_text_version", filler_texts.LEGACY_TEXT_VERSION
            ),
        }

    def read_texts(self, pack_id):
        pack = self.packs[pack_id]
        return {
            "filler_text_version": pack.get(
                "filler_text_version", filler_texts.LEGACY_TEXT_VERSION
            ),
            "texts": pack.get("texts", filler_texts.load_default()),
        }

    def read_audio(self, pack_id, *, active_provider, active_reference_id):
        resolved = self.resolve(
            pack_id,
            active_provider=active_provider,
            active_reference_id=active_reference_id,
        )
        if resolved["status"] not in ("ready", "legacy-unknown"):
            return {"status": resolved["status"], "audio": None}
        return {"status": resolved["status"], "audio": self.packs[pack_id]["audio"]}

    def pin_lkg(self, pack_id):
        self.pinned.append(pack_id)

    def garbage_collect(self, candidates, *, protected_pack_ids):
        self.gc_calls.append((list(candidates), set(protected_pack_ids)))
        for candidate in candidates:
            self.packs.pop(candidate, None)
        return {"removed": list(candidates), "protected": sorted(protected_pack_ids)}


class CountingConfig(caty_config.OverlayConfig):
    def __init__(self, defaults, **kwargs):
        super().__init__(defaults, **kwargs)
        self.write_count = 0

    def _write_unlocked(self, full_config):
        self.write_count += 1
        return super()._write_unlocked(full_config)


class InterleavingConfig(CountingConfig):
    """Return one managed snapshot while committing a legacy reset behind it."""

    def __init__(self, defaults, **kwargs):
        super().__init__(defaults, **kwargs)
        self.interleave_on_get = False
        self.interleaved = False
        self.snapshot_gets = 0

    def get(self):
        snapshot = super().get()
        self.snapshot_gets += 1
        if self.interleave_on_get and not self.interleaved:
            self.interleaved = True
            super().update(
                {"voice_id": self._defaults_dict()["voice_id"]},
                str(snapshot["config_version"]),
            )
        return snapshot


class VoiceActivationServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="voice-activation-")
        self.old_config_dir = os.environ.get("CATY_CONFIG_DIR")
        os.environ["CATY_CONFIG_DIR"] = self.tmp
        self.catalog = FakeCatalog()
        self.registry = FakeRegistry(
            Path(self.tmp) / "test-member" / "filler-packs"
        )
        self.config = CountingConfig({
            "voice_id": "old-voice", "stream_tts": "on", "voice_hint": "unchanged"
        })
        self.fault = None
        self.engine = "fish"
        self.service = self.make_service()

    def tearDown(self):
        if self.old_config_dir is None:
            os.environ.pop("CATY_CONFIG_DIR", None)
        else:
            os.environ["CATY_CONFIG_DIR"] = self.old_config_dir
        shutil.rmtree(self.tmp)

    def make_service(self, config=None):
        return voice_activation.VoiceActivationService(
            config or self.config,
            self.catalog,
            self.registry,
            member_id="test-member",
            synthesizer=lambda _text, _reference: b"ID3audio",
            inference_contract_version=lambda: "fish-tts-v1-test",
            engine_truth=lambda: self.engine,
            clock=lambda: "2026-08-09T00:00:00Z",
            fault_injector=lambda point, context: self.fault(point, context)
            if self.fault else None,
        )

    @staticmethod
    def catalog_id(ref="new-voice"):
        return voice_catalog.make_catalog_id("all", ref, "v1")

    def activate(self, **overrides):
        payload = {
            "catalog_id": self.catalog_id(),
            "filler_policy": "require_matching",
        }
        payload.update(overrides)
        return self.service.activate(payload, "1")

    def test_one_request_stages_and_atomically_commits_matching_voice_pack(self):
        result = self.activate()

        config = self.config.get()
        self.assertEqual(self.config.write_count, 1)
        self.assertEqual(config["config_version"], 2)
        self.assertEqual(config["voice_id"], "new-voice")
        self.assertEqual(config["voice_reference_id"], "new-voice")
        self.assertEqual(config["active_pack_id"], "pack-1")
        self.assertEqual(config["lkg_voice_reference_id"], "old-voice")
        self.assertEqual(config["fillers_version"], 2)
        self.assertEqual(result["state"]["filler"]["effective_status"], "ready")
        self.assertEqual(result["state"]["inference_contract_version"], "fish-tts-v1-test")
        self.assertEqual(result["state"]["active"]["display_metadata"], {})
        self.assertEqual(self.registry.gc_calls[-1][1], {"pack-1"})

    def test_neutral_preset_catalog_id_persists_logical_id_raw_reference_and_title(self):
        result = self.service.activate(
            {
                "catalog_id": "fish-neutral-ja-v1",
                "filler_policy": "voice_only",
            },
            "1",
        )

        preset = voice_presets.PRESETS["fish-neutral-ja-v1"]
        config = self.config.get()
        self.assertEqual(config["voice_catalog_id"], "fish-neutral-ja-v1")
        self.assertEqual(config["voice_id"], preset["reference_id"])
        self.assertEqual(config["voice_reference_id"], preset["reference_id"])
        self.assertEqual(config["voice_preset_id"], "")
        self.assertEqual(
            config["voice_display_metadata"]["title"],
            preset["display_name_ja"],
        )
        self.assertEqual(
            result["state"]["active"]["display_metadata"]["title"],
            preset["display_name_ja"],
        )
        rollback = self.service.activate({"action": "rollback"}, "2")
        rolled_back = self.config.get()
        self.assertEqual(rolled_back["voice_id"], "old-voice")
        self.assertEqual(rolled_back["lkg_voice_catalog_id"], "fish-neutral-ja-v1")
        self.assertEqual(rolled_back["lkg_voice_reference_id"], preset["reference_id"])
        self.assertEqual(rollback["state"]["active"]["reference_id"], "old-voice")

    def test_actual_preview_shape_does_not_page_walk_inside_activation_lock(self):
        result = self.service.activate(
            {"catalog_id": self.catalog_id(), "filler_policy": "voice_only"}, "1"
        )
        self.assertEqual(result["state"]["active"]["display_metadata"], {})
        self.assertEqual(self.catalog.list_calls, 0)

    def test_missing_stale_and_malformed_if_match_preserve_existing_schema(self):
        for value, code, status in (
            (None, "version_conflict", 409),
            ("0", "version_conflict", 409),
            ('W/"1"', "invalid_if_match_header", 400),
        ):
            with self.subTest(value=value):
                with self.assertRaises(voice_activation.ActivationError) as raised:
                    self.service.activate(
                        {"catalog_id": self.catalog_id(), "filler_policy": "voice_only"},
                        value,
                    )
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(raised.exception.status, status)
                self.assertEqual(self.config.get()["config_version"], 1)

    def test_atomic_commit_primitive_reports_live_version_for_missing_match(self):
        self.config.update({"name": "newer"}, "1")
        current = self.config.get()
        target = {
            "catalog_id": self.catalog_id(), "preset_id": "",
            "reference_id": "new-voice", "provider": "fish",
            "display_metadata": {}, "availability": "available",
            "checked_at": "2026-08-09T00:00:00Z",
        }
        fields = self.service._active_fields(target, "pack-x", "ready", current)
        with self.assertRaises(caty_config.VersionConflict) as raised:
            self.config.commit_voice_pointers(fields, None)
        self.assertEqual(raised.exception.current_version, 2)
        self.assertEqual(self.config.write_count, 1)

    def test_atomic_commit_primitive_rejects_partial_pointer_sets(self):
        with self.assertRaises(caty_config.InvalidConfig):
            self.config.commit_voice_pointers({"active_pack_id": "pack-x"}, "1")
        self.assertEqual(self.config.get()["config_version"], 1)
        self.assertEqual(self.config.write_count, 0)

    def test_legacy_overlay_reconciles_pointer_to_effective_nondefault_voice(self):
        with open(self.config.path(), "w", encoding="utf-8") as handle:
            json.dump({"voice_id": "USER_SELECTED", "config_version": 1}, handle)
        migrated = CountingConfig({"voice_id": "ENV_DEFAULT"})
        service = self.make_service(migrated)

        state = service.state()
        self.assertEqual(state["active"]["reference_id"], "USER_SELECTED")
        self.assertEqual(state["active"]["provider"], "fish")
        service.activate(
            {"catalog_id": self.catalog_id(), "filler_policy": "voice_only"}, "1"
        )
        committed = migrated.get()
        self.assertEqual(committed["lkg_voice_reference_id"], "USER_SELECTED")
        self.assertNotEqual(committed["lkg_voice_reference_id"], "ENV_DEFAULT")
        service.activate({"action": "rollback"}, "2")
        self.assertEqual(migrated.get()["voice_id"], "USER_SELECTED")
        self.assertNotEqual(migrated.get()["voice_id"], "ENV_DEFAULT")

    def test_voice_only_voice_change_bumps_filler_cache_version_without_pack_change(self):
        result = self.activate(filler_policy="voice_only")
        self.assertEqual(result["state"]["filler"]["pack_id"], None)
        self.assertEqual(result["state"]["fillers_version"], 2)

    def test_non_fish_engine_reports_capability_but_rejects_activation_without_change(self):
        self.engine = "openclaw"
        state = self.service.state()
        self.assertEqual(state["engine"], "openclaw")
        self.assertFalse(state["picker"]["capable"])
        with self.assertRaises(voice_activation.ActivationError) as raised:
            self.activate(filler_policy="voice_only")
        self.assertEqual(raised.exception.code, "voice_activation_unsupported")
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.catalog.calls, [])
        self.assertEqual(self.config.write_count, 0)

    def test_validation_unknown_and_permanent_unavailable_never_change_state(self):
        errors = (
            ("offline", OSError("offline"), "voice_validation_unknown", "unknown"),
            ("429", voice_catalog.CatalogUpstreamError("catalog_rate_limited", 429, retry_after=7), "catalog_rate_limited", "unknown"),
            ("5xx", voice_catalog.CatalogUpstreamError("catalog_temporarily_unavailable", 503), "catalog_temporarily_unavailable", "unknown"),
            ("404", voice_catalog.CatalogVoiceUnavailable("voice_not_found"), "voice_not_found", "unavailable"),
            ("dmca-or-private", voice_catalog.CatalogVoiceUnavailable("voice_unavailable"), "voice_unavailable", "unavailable"),
        )
        for label, error, code, expected in errors:
            with self.subTest(label=label):
                self.catalog.error = error
                with self.assertRaises(voice_activation.ActivationError) as raised:
                    self.activate(filler_policy="voice_only")
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(raised.exception.details["availability"], expected)
                self.assertIn("reselect", raised.exception.details["recovery_candidates"])
                self.assertEqual(self.config.get()["voice_id"], "old-voice")
                self.assertEqual(self.config.write_count, 0)

    def test_stage_and_precommit_crashes_leave_old_successful_state(self):
        self.registry.stage_error = filler_pack.FillerPackError("paid transport failed")
        with self.assertRaises(voice_activation.ActivationError) as raised:
            self.activate()
        self.assertEqual(raised.exception.code, "filler_generation_unknown")
        self.assertTrue(raised.exception.retryable)
        self.assertTrue(raised.exception.details["state_unchanged"])
        self.assertEqual(self.config.get()["voice_id"], "old-voice")
        self.assertFalse(self.service.journal_path.exists())
        self.assertNotEqual(
            self.service.state()["filler"]["effective_status"], "generating"
        )
        self.registry.stage_error = None

        def crash(point, _context):
            if point == "activation_after_stage_before_commit":
                raise RuntimeError("crash")

        self.fault = crash
        with self.assertRaises(voice_activation.ActivationError) as raised:
            self.activate()
        self.assertEqual(raised.exception.code, "voice_activation_unknown")
        self.assertTrue(raised.exception.retryable)
        self.assertTrue(raised.exception.details["state_unchanged"])
        self.assertEqual(self.config.get()["voice_id"], "old-voice")
        self.assertEqual(self.config.write_count, 0)

    def test_unexpected_stage_exception_always_clears_generating_marker(self):
        def unexpected_stage_failure():
            raise RuntimeError("unexpected stage failure")

        self.registry.stage_error = unexpected_stage_failure
        with self.assertRaises(voice_activation.ActivationError) as raised:
            self.activate()
        self.assertEqual(raised.exception.code, "voice_activation_unknown")
        self.assertTrue(raised.exception.retryable)
        self.assertNotEqual(
            self.service.state()["filler"]["effective_status"], "generating"
        )

    def test_config_temp_write_crash_keeps_old_file_and_restart_does_not_adopt_stage(self):
        def crash(point, _context):
            if point == "config_after_tmp_write_before_replace":
                raise RuntimeError("power loss")

        crashing = CountingConfig({"voice_id": "old-voice"}, fault_injector=crash)
        service = self.make_service(crashing)
        with self.assertRaises(voice_activation.ActivationError) as raised:
            service.activate(
                {"catalog_id": self.catalog_id(), "filler_policy": "require_matching"},
                "1",
            )
        self.assertTrue(raised.exception.details["state_unchanged"])
        self.assertEqual(crashing.get()["voice_id"], "old-voice")
        self.assertFalse(service.journal_path.exists())

        recovered = self.make_service(crashing)
        self.assertFalse(recovered.journal_path.exists())
        self.assertEqual(crashing.get()["voice_id"], "old-voice")

    def test_permanent_stage_failure_returns_recovery_without_changing_state(self):
        self.registry.stage_error = filler_pack.FillerPackError(
            "voice disappeared", code="voice_unavailable", status=404
        )
        with self.assertRaises(voice_activation.ActivationError) as raised:
            self.activate()
        self.assertEqual(raised.exception.code, "voice_unavailable")
        self.assertEqual(raised.exception.status, 404)
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(raised.exception.details["availability"], "unavailable")
        self.assertIn("reselect", raised.exception.details["recovery_candidates"])
        self.assertTrue(raised.exception.details["state_unchanged"])
        self.assertEqual(self.config.get()["voice_id"], "old-voice")

    def test_sanitized_stage_failure_is_revalidated_as_permanent(self):
        def voice_disappears():
            self.catalog.error = voice_catalog.CatalogVoiceUnavailable(
                "voice_unavailable"
            )
            raise filler_pack.FillerPackError("filler synthesis failed")

        self.registry.stage_error = voice_disappears
        with self.assertRaises(voice_activation.ActivationError) as raised:
            self.activate()
        self.assertEqual(raised.exception.code, "voice_unavailable")
        self.assertEqual(raised.exception.status, 404)
        self.assertFalse(raised.exception.retryable)
        self.assertTrue(raised.exception.details["state_unchanged"])
        self.assertEqual(self.config.get()["voice_id"], "old-voice")

    def test_after_replace_crash_is_whole_new_pointer_set_not_partial(self):
        def crash(point, _context):
            if point == "activation_after_config_replace_before_gc":
                raise RuntimeError("crash")

        self.fault = crash
        with self.assertRaises(voice_activation.ActivationError) as raised:
            self.activate()
        self.assertFalse(raised.exception.details["state_unchanged"])
        config = self.config.get()
        self.assertEqual(
            (config["voice_reference_id"], config["active_pack_id"]),
            ("new-voice", "pack-1"),
        )

    def test_cas_race_with_raw_put_wins_without_activation_partial_commit(self):
        self.catalog.entered = threading.Event()
        self.catalog.release = threading.Event()
        errors = []

        def request():
            try:
                self.activate(filler_policy="voice_only")
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=request)
        thread.start()
        self.assertTrue(self.catalog.entered.wait(1))
        self.config.update({"name": "Concurrent"}, "1")
        self.catalog.release.set()
        thread.join(2)

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].code, "version_conflict")
        config = self.config.get()
        self.assertEqual(config["name"], "Concurrent")
        self.assertEqual(config["voice_id"], "old-voice")
        self.assertEqual(config["config_version"], 2)

    def test_voice_only_is_explicit_never_stages_and_fails_closed_without_pack(self):
        result = self.activate(filler_policy="voice_only")
        self.assertEqual(self.registry.stage_calls, 0)
        self.assertEqual(result["state"]["filler"]["effective_status"], "stale")
        self.assertIsNone(self.service.filler_audio()["audio"])
        self.assertEqual(self.config.get()["stream_tts"], "on")
        self.assertEqual(self.config.get()["voice_hint"], "unchanged")

        config = self.config.get()
        self.config.update({"voice_id": "raw-diagnostic"}, str(config["config_version"]))
        raw = self.config.get()
        self.assertEqual(raw["voice_management_state"], "raw")
        self.assertEqual(raw["active_pack_id"], "")
        self.assertEqual(raw["filler_effective_status"], "stale")
        self.assertIsNone(self.service.filler_audio()["audio"])

        self.config.update({"voice_id": ""}, str(raw["config_version"]))
        self.assertEqual(self.config.get()["filler_effective_status"], "unavailable")

    def test_second_activation_protects_both_active_and_config_lkg_from_gc(self):
        self.activate()
        result = self.service.activate(
            {"catalog_id": self.catalog_id("newer-voice"), "filler_policy": "require_matching"},
            "2",
        )
        self.assertEqual(result["state"]["filler"]["pack_id"], "pack-2")
        self.assertEqual(self.config.get()["lkg_pack_id"], "pack-1")
        self.assertEqual(self.registry.gc_calls[-1][1], {"pack-1", "pack-2"})
        self.assertEqual(self.registry.pinned[-1], "pack-1")

        rollback = self.service.activate({"action": "rollback"}, "3")
        self.assertEqual(rollback["state"]["filler"]["pack_id"], "pack-1")
        self.assertEqual(self.config.get()["lkg_pack_id"], "pack-2")

    def test_new_profile_voice_only_readiness_does_not_wait_for_filler(self):
        empty = CountingConfig({"voice_id": ""})
        service = self.make_service(empty)
        result = service.activate(
            {"catalog_id": self.catalog_id(), "filler_policy": "voice_only"}, "1"
        )
        self.assertEqual(self.registry.stage_calls, 0)
        self.assertTrue(result["state"]["voice_ready"])
        self.assertEqual(result["state"]["filler"]["effective_status"], "unavailable")

    def test_matching_existing_pack_is_adopted_even_by_explicit_voice_only(self):
        self.registry.packs["existing"] = {
            "status": "ready", "provider": "fish",
            "reference_id": "new-voice", "audio": b"ID3existing",
        }
        result = self.activate(filler_policy="voice_only")
        self.assertEqual(self.registry.stage_calls, 0)
        self.assertEqual(result["state"]["filler"]["pack_id"], "existing")

    def test_matching_pack_honors_explicit_preset(self):
        self.registry.packs["wrong-preset"] = {
            "status": "ready", "provider": "fish",
            "reference_id": "new-voice", "preset_id": "other",
            "audio": b"ID3wrong",
        }
        result = self.activate(preset_id="requested")
        self.assertEqual(self.registry.stage_calls, 1)
        self.assertEqual(result["state"]["filler"]["pack_id"], "pack-1")

    def test_same_voice_different_text_version_without_preset_stages_new_pack(self):
        self.registry.packs["old-text"] = {
            "status": "ready", "provider": "fish",
            "reference_id": "new-voice", "preset_id": None,
            "filler_text_version": "ft1-old",
            "texts": filler_texts.load_default(),
            "audio": b"ID3old-text",
        }

        result = self.activate()

        self.assertEqual(self.registry.stage_calls, 1)
        self.assertEqual(result["state"]["filler"]["pack_id"], "pack-1")

    def test_grandfathered_default_pack_matches_without_staging_or_text_stale(self):
        self.registry.packs["legacy-default"] = {
            "status": "ready", "provider": "fish",
            "reference_id": "new-voice", "preset_id": None,
            "filler_text_version": filler_texts.LEGACY_TEXT_VERSION,
            "texts": filler_texts.load_default(),
            "audio": b"ID3legacy-default",
        }

        result = self.activate()

        self.assertEqual(self.registry.stage_calls, 0)
        self.assertEqual(result["state"]["filler"]["pack_id"], "legacy-default")
        self.assertEqual(
            result["state"]["filler"]["active_text_version"],
            filler_texts.LEGACY_TEXT_VERSION,
        )
        self.assertFalse(result["state"]["filler"]["text_stale"])

    def test_text_stale_never_silences_voice_matching_active_pack(self):
        self.activate()
        before = self.config.get()["active_pack_id"]
        filler_texts.save_override(
            "test-member", {"wait": ["changed"]}, data_root=self.service.data_root
        )

        state = self.service.state()
        audio = self.service.filler_audio()

        self.assertEqual(state["filler"]["pack_id"], before)
        self.assertEqual(state["filler"]["effective_status"], "ready")
        self.assertTrue(state["filler"]["text_stale"])
        self.assertIsNotNone(audio)
        self.assertEqual(audio["status"], "ready")
        self.assertEqual(audio["audio"], b"ID3matching")

    def test_unavailable_active_pack_does_not_report_unknown_text_version_as_stale(self):
        self.activate()
        self.registry.packs.clear()

        filler = self.service.state()["filler"]

        self.assertEqual(filler["effective_status"], "unavailable")
        self.assertIsNone(filler["active_text_version"])
        self.assertFalse(filler["text_stale"])

    def test_registry_root_is_required(self):
        registry = object()

        with self.assertRaisesRegex(ValueError, "registry root required"):
            voice_activation.VoiceActivationService(
                self.config,
                self.catalog,
                registry,
                member_id="test-member",
                synthesizer=lambda _text, _reference: b"ID3audio",
                inference_contract_version=lambda: "fish-tts-v1-test",
                engine_truth=lambda: self.engine,
            )

    def test_generating_state_keeps_active_text_version_and_staleness_visible(self):
        self.activate()
        active_version = self.service.state()["filler"]["active_text_version"]
        filler_texts.save_override(
            "test-member", {"wait": ["changed"]}, data_root=self.service.data_root
        )
        with self.service._runtime_lock:
            self.service._generating = "new-voice"
        try:
            state = self.service.state()["filler"]
        finally:
            with self.service._runtime_lock:
                self.service._generating = None

        self.assertEqual(state["effective_status"], "generating")
        self.assertEqual(state["active_text_version"], active_version)
        self.assertTrue(state["text_stale"])

    def test_stage_matching_pack_reads_effective_texts_once(self):
        target = {
            "catalog_id": self.catalog_id(), "preset_id": "",
            "reference_id": "new-voice", "provider": "fish",
            "display_metadata": {}, "availability": "available",
            "checked_at": "now",
        }
        with mock.patch.object(
            filler_texts, "effective", wraps=filler_texts.effective
        ) as effective:
            self.service._stage_matching_pack(target)
        effective.assert_called_once_with("test-member", self.service.data_root)

    def test_regenerate_twice_synthesizes_once_and_reuses_ready_pack(self):
        first = self.service.regenerate()
        second = self.service.regenerate()

        self.assertEqual(self.registry.stage_calls, 1)
        self.assertEqual(first["state"]["filler"]["pack_id"], "pack-1")
        self.assertEqual(second["state"]["filler"]["pack_id"], "pack-1")

    def test_regenerate_failure_keeps_active_pack_and_surfaces_last_error(self):
        self.service.regenerate()
        active = self.config.get()["active_pack_id"]
        current = filler_texts.effective("test-member", self.service.data_root)
        filler_texts.save_override(
            "test-member", {"wait": ["changed"]},
            if_match=current.version, data_root=self.service.data_root,
        )
        self.registry.stage_error = filler_pack.FillerPackError(
            "quota", code="quota_exhausted", status=429
        )

        with self.assertRaises(voice_activation.ActivationError) as raised:
            self.service.regenerate()

        self.assertEqual(raised.exception.code, "quota_exhausted")
        self.assertEqual(self.config.get()["active_pack_id"], active)
        self.assertEqual(
            self.service.state()["filler"]["last_error"], "quota_exhausted"
        )

    def test_invalid_override_blocks_regenerate_unless_forced_but_activation_falls_back(self):
        path = filler_texts.override_path("test-member", self.service.data_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{", encoding="utf-8")

        with self.assertRaises(voice_activation.ActivationError) as raised:
            self.service.regenerate()
        self.assertEqual(raised.exception.code, "override_invalid")
        self.assertEqual(self.registry.stage_calls, 0)

        forced = self.service.regenerate(force=True)
        self.assertEqual(self.registry.stage_calls, 1)
        self.assertEqual(forced["state"]["filler"]["fallback_reason"], "override_invalid")

        activated = self.service.activate(
            {
                "catalog_id": self.catalog_id(),
                "filler_policy": "require_matching",
            },
            str(self.config.get()["config_version"]),
        )
        self.assertEqual(self.registry.stage_calls, 2)
        self.assertEqual(
            activated["state"]["filler"]["fallback_reason"], "override_invalid"
        )

    def test_lkg_rollback_swaps_complete_pointer_set_and_is_crash_safe(self):
        self.activate()
        self.fault = lambda point, _context: (
            (_ for _ in ()).throw(RuntimeError("crash"))
            if point == "rollback_before_config_replace" else None
        )
        with self.assertRaises(voice_activation.ActivationError) as raised:
            self.service.activate({"action": "rollback"}, "2")
        self.assertTrue(raised.exception.details["state_unchanged"])
        self.assertEqual(self.config.get()["voice_id"], "new-voice")

        self.fault = None
        result = self.service.activate({"action": "rollback"}, "2")
        config = self.config.get()
        self.assertEqual(result["action"], "rolled_back")
        self.assertEqual(config["voice_id"], "old-voice")
        self.assertEqual(config["lkg_voice_reference_id"], "new-voice")

    def test_first_activation_rollback_restores_legacy_filler_pool_state(self):
        self.activate(filler_policy="voice_only")
        activated = self.config.get()
        self.assertEqual(activated["lkg_voice_management_state"], "legacy")
        self.assertEqual(activated["lkg_filler_effective_status"], "legacy-unknown")

        result = self.service.activate({"action": "rollback"}, "2")
        restored = self.config.get()
        self.assertEqual(result["action"], "rolled_back")
        self.assertEqual(restored["voice_id"], "old-voice")
        self.assertEqual(restored["voice_management_state"], "legacy")
        self.assertEqual(restored["filler_effective_status"], "legacy-unknown")
        self.assertIsNone(self.service.filler_audio())

    def test_rollback_trusts_saved_lkg_on_transient_validation_only(self):
        self.activate()
        self.catalog.error = voice_catalog.CatalogUpstreamError(
            "catalog_temporarily_unavailable", 503
        )
        result = self.service.activate({"action": "rollback"}, "2")
        self.assertEqual(result["action"], "rolled_back")
        self.assertEqual(self.config.get()["voice_id"], "old-voice")

        self.catalog.error = voice_catalog.CatalogVoiceUnavailable("voice_unavailable")
        before = self.config.get()
        with self.assertRaises(voice_activation.ActivationError) as raised:
            self.service.activate({"action": "rollback"}, "3")
        self.assertEqual(raised.exception.code, "voice_unavailable")
        self.assertIn("reselect", raised.exception.details["recovery_candidates"])
        self.assertEqual(self.config.get(), before)

    def test_managed_raw_default_round_trip_restores_bundled_legacy_and_saves_lkg(self):
        self.activate()
        managed = self.config.get()
        self.config.update({"voice_id": "raw-diagnostic"}, str(managed["config_version"]))
        raw = self.config.get()
        self.assertEqual(raw["voice_management_state"], "raw")
        self.assertEqual(raw["lkg_voice_reference_id"], "new-voice")
        self.assertEqual(raw["lkg_pack_id"], "pack-1")

        self.config.update({"voice_id": "old-voice"}, str(raw["config_version"]))
        restored = self.config.get()
        self.assertEqual(restored["voice_management_state"], "legacy")
        self.assertEqual(restored["filler_effective_status"], "legacy-unknown")
        self.assertEqual(restored["active_pack_id"], "")
        self.assertEqual(restored["lkg_pack_id"], "pack-1")
        self.assertIsNone(self.service.filler_audio())

    def test_raw_put_of_same_default_voice_resets_managed_state(self):
        target = self.service._validate_target(reference_id="old-voice")
        fields = self.service._active_fields(
            target, "", "unavailable", self.config.get()
        )
        self.config.commit_voice_pointers(fields, "1")
        self.config.update({"voice_id": "old-voice"}, "2")
        self.assertEqual(self.config.get()["voice_management_state"], "legacy")

    def test_mismatched_or_corrupt_active_pack_never_returns_audio(self):
        self.registry.packs["wrong"] = {
            "status": "ready", "provider": "fish",
            "reference_id": "other", "audio": b"ID3wrong",
        }
        target = self.service._validate_target(catalog_id=self.catalog_id())
        fields = self.service._active_fields(target, "wrong", "ready", self.config.get())
        self.config.commit_voice_pointers(fields, "1")
        self.assertEqual(self.service.state()["filler"]["effective_status"], "stale")
        self.assertIsNone(self.service.filler_audio()["audio"])


class MutableClock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value


class NeutralCatalogStub:
    def __init__(self, response=None, error=None, entered=None, release=None):
        self.response = response or {
            "provider": "fish",
            "scope": "public",
            "reference_id": "0089dce5fefb4c6ba9b9f2f0debe1ddc",
            "availability": "available",
        }
        self.error = error
        self.entered = entered
        self.release = release
        self.calls = 0

    def resolve_preview(self, catalog_id=None, reference_id=None):
        self.calls += 1
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            self.release.wait(2)
        if self.error is not None:
            raise self.error
        return dict(self.response)


class NeutralVoiceReadinessTest(unittest.TestCase):
    @staticmethod
    def _iso(clock):
        return cg.datetime.datetime.fromtimestamp(
            clock.value, tz=cg.datetime.timezone.utc
        ).isoformat().replace("+00:00", "Z")

    def test_default_max_staleness_is_four_ttls(self):
        helper = cg._NeutralVoiceReadiness(
            catalog_service_getter=lambda: NeutralCatalogStub(),
            preset_id="fish-neutral-ja-v1",
            reference_id="0089dce5fefb4c6ba9b9f2f0debe1ddc",
            ttl_seconds=10,
        )

        self.assertEqual(helper._max_staleness_seconds, 40)

    def test_gateway_defaults_match_setup_voice_state_contract(self):
        helper = cg._NeutralVoiceReadiness(
            catalog_service_getter=lambda: NeutralCatalogStub(),
            preset_id="fish-neutral-ja-v1",
            reference_id="0089dce5fefb4c6ba9b9f2f0debe1ddc",
        )

        self.assertEqual(
            helper._ttl_seconds,
            setup_orchestrator.VOICE_STATE_TTL_SECONDS,
        )
        self.assertEqual(
            helper._max_staleness_seconds,
            setup_orchestrator.VOICE_STATE_MAX_STALENESS_SECONDS,
        )
        self.assertEqual(
            setup_orchestrator.VOICE_STATE_MAX_STALENESS_SECONDS,
            setup_orchestrator.VOICE_STATE_TTL_SECONDS * 4,
        )

    def test_refresh_is_read_only_model_lookup(self):
        calls = []

        def transport(path, params=None):
            calls.append((path, dict(params or {})))
            self.assertEqual(path, "/model/0089dce5fefb4c6ba9b9f2f0debe1ddc")
            self.assertEqual(params, None)
            return {
                "_id": "0089dce5fefb4c6ba9b9f2f0debe1ddc",
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

        catalog = voice_catalog.VoiceCatalogService(
            transport,
            installation_id="member-a",
        )
        helper = cg._NeutralVoiceReadiness(
            catalog_service_getter=lambda: catalog,
            preset_id="fish-neutral-ja-v1",
            reference_id="0089dce5fefb4c6ba9b9f2f0debe1ddc",
            retry_attempts=1,
        )

        helper._refresh_now()

        self.assertEqual(calls, [("/model/0089dce5fefb4c6ba9b9f2f0debe1ddc", {})])
        self.assertEqual(helper.state()["availability"], "available")

    def test_transient_failure_retains_definite_until_hard_max_staleness(self):
        clock = MutableClock()
        catalog = NeutralCatalogStub()
        helper = cg._NeutralVoiceReadiness(
            catalog_service_getter=lambda: catalog,
            preset_id="fish-neutral-ja-v1",
            reference_id="0089dce5fefb4c6ba9b9f2f0debe1ddc",
            clock=clock,
            now_iso=lambda: self._iso(clock),
            sleep=lambda _seconds: None,
            ttl_seconds=10,
            max_staleness_seconds=40,
            retry_attempts=1,
        )

        helper._refresh_now()
        self.assertEqual(helper.state()["availability"], "available")

        catalog.error = voice_catalog.CatalogUpstreamError(
            "catalog_temporarily_unavailable", 503
        )
        clock.value += 11
        helper._refresh_now()
        stale = helper.state()
        self.assertEqual(stale["availability"], "available")
        self.assertTrue(stale["stale"])

        clock.value += 41
        expired = helper.state()
        self.assertEqual(expired["availability"], "unknown")
        self.assertTrue(expired["stale"])

    def test_transient_without_definite_value_publishes_unknown(self):
        catalog = NeutralCatalogStub(
            error=voice_catalog.CatalogUpstreamError(
                "catalog_temporarily_unavailable", 503
            )
        )
        helper = cg._NeutralVoiceReadiness(
            catalog_service_getter=lambda: catalog,
            preset_id="fish-neutral-ja-v1",
            reference_id="0089dce5fefb4c6ba9b9f2f0debe1ddc",
            sleep=lambda _seconds: None,
            retry_attempts=1,
        )

        helper._refresh_now()

        state = helper.state()
        self.assertEqual(state["availability"], "unknown")
        self.assertIsNone(state["checked_at"])

    def test_hidden_result_is_definite_unavailable(self):
        catalog = NeutralCatalogStub(
            response={
                "provider": "fish",
                "scope": "public",
                "reference_id": "0089dce5fefb4c6ba9b9f2f0debe1ddc",
                "availability": "hidden",
            }
        )
        helper = cg._NeutralVoiceReadiness(
            catalog_service_getter=lambda: catalog,
            preset_id="fish-neutral-ja-v1",
            reference_id="0089dce5fefb4c6ba9b9f2f0debe1ddc",
            retry_attempts=1,
        )

        helper._refresh_now()

        self.assertEqual(helper.state()["availability"], "unavailable")

    def test_state_triggers_single_flight_refresh_when_stale(self):
        clock = MutableClock()
        entered = threading.Event()
        release = threading.Event()
        catalog = NeutralCatalogStub(entered=entered, release=release)
        helper = cg._NeutralVoiceReadiness(
            catalog_service_getter=lambda: catalog,
            preset_id="fish-neutral-ja-v1",
            reference_id="0089dce5fefb4c6ba9b9f2f0debe1ddc",
            clock=clock,
            now_iso=lambda: self._iso(clock),
            ttl_seconds=10,
            retry_attempts=1,
            initial_backoff_seconds=0,
        )

        helper.start()
        self.assertTrue(entered.wait(1))
        release.set()
        deadline = time.time() + 1
        while time.time() < deadline and helper.state()["availability"] != "available":
            time.sleep(0.01)
        self.assertEqual(catalog.calls, 1)

        clock.value += 11
        catalog.entered = threading.Event()
        catalog.release = threading.Event()
        results = []
        threads = [threading.Thread(target=lambda: results.append(helper.state())) for _ in range(4)]
        for thread in threads:
            thread.start()
        self.assertTrue(catalog.entered.wait(1))
        time.sleep(0.05)
        self.assertEqual(catalog.calls, 2)
        catalog.release.set()
        for thread in threads:
            thread.join(2)

        self.assertEqual(len(results), 4)
        self.assertTrue(all(result["availability"] in {"available", "unknown"} for result in results))


class NonClosingBytesIO(io.BytesIO):
    def close(self):
        pass


class MemorySocket:
    def __init__(self, request):
        self.input = io.BytesIO(request)
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


class FakeHttpService:
    def state(self):
        return {
            "availability": "available", "checked_at": "now", "voice_ready": True,
            "config_version": 1, "fillers_version": 1,
            "active": {"reference_id": "diagnostic-id", "display_metadata": {}},
            "filler": {
                "effective_status": "unavailable", "pack_id": None,
                "desired_text_version": "ft1-desired",
                "active_text_version": "ft1-active",
                "text_stale": True,
            },
            "last_known_good": {"available": False, "voice_available": False, "pack_available": False},
            "picker": {"capable": True, "supported_scopes": ["recommended", "all", "self"]},
            "engine": "fish",
        }

    def activate(self, payload, if_match):
        if if_match is None:
            raise voice_activation.ActivationError(
                "version_conflict", 409, details={"config_version": 1}
            )
        if if_match == 'W/"1"':
            raise voice_activation.ActivationError("invalid_if_match_header", 400)
        return {"ok": True, "action": payload.get("action", "activated"), "state": self.state()}

    def filler_audio(self):
        return {"status": "ready", "audio": b"ID3new-voice"}


class VoiceActivationHttpTest(unittest.TestCase):
    def setUp(self):
        self.old = (
            cg.CATY_TOKEN,
            cg.CATY_ADMIN_TOKEN,
            cg.VOICE_SCOPE_AUTHORIZER,
            cg._voice_activation_service,
            cg._neutral_voice_readiness,
        )
        cg.CATY_TOKEN = "member-secret"
        cg.CATY_ADMIN_TOKEN = ""
        cg.VOICE_SCOPE_AUTHORIZER = None
        cg._voice_activation_service = FakeHttpService()
        cg._neutral_voice_readiness = None

    def tearDown(self):
        (
            cg.CATY_TOKEN,
            cg.CATY_ADMIN_TOKEN,
            cg.VOICE_SCOPE_AUTHORIZER,
            cg._voice_activation_service,
            cg._neutral_voice_readiness,
        ) = self.old

    def request_raw(self, method, path, payload=None, headers=None):
        body = b"" if payload is None else json.dumps(payload).encode()
        headers = {"Host": "localhost", "Connection": "close", **(headers or {})}
        headers["Content-Length"] = str(len(body))
        request = [f"{method} {path} HTTP/1.1"]
        request.extend(f"{key}: {value}" for key, value in headers.items())
        sock = MemorySocket(("\r\n".join(request) + "\r\n\r\n").encode() + body)
        cg.Handler(sock, ("127.0.0.1", 0), MemoryServer())
        head, _, response = sock.output.getvalue().partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        status = int(lines[0].split()[1])
        response_headers = dict(
            line.split(": ", 1) for line in lines[1:] if ": " in line
        )
        length = int(response_headers.get("Content-Length", 0))
        return status, response_headers, response[:length]

    def request(self, method, path, payload=None, headers=None):
        status, _headers, body = self.request_raw(method, path, payload, headers)
        return status, json.loads(body)

    def test_new_read_and_write_routes_are_fail_closed_and_scoped(self):
        self.assertEqual(self.request("GET", "/tts/voice-state")[0], 401)
        self.assertEqual(
            self.request("POST", "/tts/voice-activations", {})[0], 401
        )
        auth = {"Authorization": "Bearer member-secret"}
        status, state = self.request("GET", "/tts/voice-state", headers=auth)
        self.assertEqual(status, 200)
        self.assertEqual(state["picker"]["supported_scopes"], ["recommended", "all", "self"])
        self.assertEqual(state["neutral"]["preset_id"], "fish-neutral-ja-v1")
        self.assertEqual(state["filler"]["desired_text_version"], "ft1-desired")
        self.assertEqual(state["filler"]["active_text_version"], "ft1-active")
        self.assertTrue(state["filler"]["text_stale"])

    def test_activation_http_preserves_cas_error_shapes_and_leaks_no_secrets(self):
        auth = {"Authorization": "Bearer member-secret"}
        status, body = self.request("POST", "/tts/voice-activations", {}, headers=auth)
        self.assertEqual(body, {"ok": False, "error": "version_conflict", "config_version": 1})
        self.assertEqual(status, 409)
        status, body = self.request(
            "POST", "/tts/voice-activations", {},
            headers={**auth, "If-Match": 'W/"1"'},
        )
        self.assertEqual((status, body), (400, {"ok": False, "error": "invalid_if_match_header"}))
        status, body = self.request("GET", "/tts/voice-state", headers=auth)
        self.assertEqual(status, 200)
        encoded = json.dumps(body)
        self.assertNotIn("member-secret", encoded)
        self.assertNotIn("provenance", encoded)
        self.assertNotIn("license_metadata", encoded)

    def test_voice_state_degrades_neutral_only_when_neutral_builder_raises(self):
        class ExplodingReadiness:
            def state(self):
                raise RuntimeError("boom")

        cg._neutral_voice_readiness = ExplodingReadiness()
        status, body = self.request(
            "GET",
            "/tts/voice-state",
            headers={"Authorization": "Bearer member-secret"},
        )

        self.assertEqual(status, 200)
        self.assertEqual(body["availability"], "available")
        self.assertEqual(body["neutral"]["availability"], "unknown")

    def test_voice_state_neutral_degrade_survives_missing_preset_registry_row(self):
        class ExplodingReadiness:
            def state(self):
                raise RuntimeError("boom")

        cg._neutral_voice_readiness = ExplodingReadiness()
        with mock.patch.dict(voice_presets.PRESETS, {}, clear=True):
            status, body = self.request(
                "GET",
                "/tts/voice-state",
                headers={"Authorization": "Bearer member-secret"},
            )

        self.assertEqual(status, 200)
        self.assertEqual(body["neutral"]["availability"], "unknown")
        self.assertEqual(
            body["neutral"]["reference_id"],
            "0089dce5fefb4c6ba9b9f2f0debe1ddc",
        )

    def test_voice_error_details_cannot_override_core_response_keys(self):
        class MaliciousDetailsService(FakeHttpService):
            def activate(self, _payload, _if_match):
                raise voice_activation.ActivationError(
                    "safe_error", 503, retryable=True,
                    details={
                        "ok": True, "error": "overwritten", "retryable": False,
                        "availability": "unknown", "state_unchanged": True,
                    },
                )

        cg._voice_activation_service = MaliciousDetailsService()
        status, body = self.request(
            "POST", "/tts/voice-activations", {},
            headers={"Authorization": "Bearer member-secret", "If-Match": "1"},
        )
        self.assertEqual(status, 503)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "safe_error")
        self.assertTrue(body["retryable"])
        self.assertTrue(body["state_unchanged"])

    def test_unexpected_activation_errors_report_state_change_without_guessing(self):
        auth = {"Authorization": "Bearer member-secret", "If-Match": "1"}
        cg._voice_activation_service = None
        with mock.patch.object(
            cg, "_get_voice_activation_service", side_effect=RuntimeError("init failed")
        ):
            status, body = self.request(
                "POST", "/tts/voice-activations", {}, headers=auth
            )
        self.assertEqual(status, 503)
        self.assertTrue(body["retryable"])
        self.assertTrue(body["state_unchanged"])

        class UnexpectedService(FakeHttpService):
            def activate(self, _payload, _if_match):
                raise RuntimeError("unexpected")

        for versions, unchanged in (((7, 7), True), ((7, 8), False)):
            with self.subTest(versions=versions):
                cg._voice_activation_service = UnexpectedService()
                config = mock.Mock()
                config.get.side_effect = [
                    {"config_version": versions[0]},
                    {"config_version": versions[1]},
                ]
                with mock.patch.object(cg, "CONFIG", config):
                    status, body = self.request(
                        "POST", "/tts/voice-activations", {}, headers=auth
                    )
                self.assertEqual(status, 503)
                self.assertTrue(body["retryable"])
                self.assertEqual(body["state_unchanged"], unchanged)

    def test_lazy_filler_handler_resolves_one_managed_snapshot_across_legacy_commit(self):
        with tempfile.TemporaryDirectory(prefix="filler-snapshot-race-") as tmp:
            previous_dir = os.environ.get("CATY_CONFIG_DIR")
            os.environ["CATY_CONFIG_DIR"] = tmp
            try:
                config = InterleavingConfig({"voice_id": "bundled-default"})
                catalog = FakeCatalog()
                registry = FakeRegistry(Path(tmp) / "test-member" / "filler-packs")
                registry.packs["managed-pack"] = {
                    "status": "ready", "provider": "fish",
                    "reference_id": "managed-voice", "preset_id": None,
                    "audio": b"ID3managed-snapshot",
                }
                bootstrap = voice_activation.VoiceActivationService(
                    config, catalog, registry,
                    member_id="test-member",
                    synthesizer=lambda _text, _reference: b"ID3audio",
                    inference_contract_version=lambda: "fish-tts-v1-test",
                    engine_truth=lambda: "fish",
                )
                target = {
                    "catalog_id": voice_catalog.make_catalog_id(
                        "all", "managed-voice", "v1"
                    ),
                    "preset_id": "", "reference_id": "managed-voice",
                    "provider": "fish", "display_metadata": {},
                    "availability": "available", "checked_at": "now",
                }
                fields = bootstrap._active_fields(
                    target, "managed-pack", "ready", config.get()
                )
                config.commit_voice_pointers(fields, "1")
                config.snapshot_gets = 0
                config.interleave_on_get = True

                def lazy_service():
                    service = voice_activation.VoiceActivationService(
                        config, catalog, registry,
                        member_id="test-member",
                        synthesizer=lambda _text, _reference: b"ID3audio",
                        inference_contract_version=lambda: "fish-tts-v1-test",
                        engine_truth=lambda: "fish",
                    )
                    cg._voice_activation_service = service
                    return service

                cg._voice_activation_service = None
                with mock.patch.object(cg, "CONFIG", config), \
                        mock.patch.object(
                            cg, "_get_voice_activation_service", side_effect=lazy_service
                        ) as get_service, \
                        mock.patch.object(
                            cg, "FILLERS", [(b"ID3bundled", "bundled.mp3")]
                        ):
                    status, headers, body = self.request_raw(
                        "GET", "/filler",
                        headers={"Authorization": "Bearer member-secret"},
                    )

                self.assertTrue(config.interleaved)
                self.assertEqual(config.snapshot_gets, 1)
                get_service.assert_called_once_with()
                with config._lock:
                    committed = config._merged_unlocked()
                self.assertEqual(committed["voice_management_state"], "legacy")
                self.assertEqual(status, 200)
                self.assertEqual(headers["Content-Type"], "audio/mpeg")
                self.assertEqual(body, b"ID3managed-snapshot")
            finally:
                if previous_dir is None:
                    os.environ.pop("CATY_CONFIG_DIR", None)
                else:
                    os.environ["CATY_CONFIG_DIR"] = previous_dir

    def test_default_reset_round_trip_serves_bundled_filler_through_http(self):
        with tempfile.TemporaryDirectory(prefix="raw-reset-http-") as tmp:
            previous_dir = os.environ.get("CATY_CONFIG_DIR")
            os.environ["CATY_CONFIG_DIR"] = tmp
            try:
                config = CountingConfig({"voice_id": "bundled-default"})
                catalog = FakeCatalog()
                registry = FakeRegistry(Path(tmp) / "test-member" / "filler-packs")
                service = voice_activation.VoiceActivationService(
                    config, catalog, registry,
                    member_id="test-member",
                    synthesizer=lambda _text, _reference: b"ID3audio",
                    inference_contract_version=lambda: "fish-tts-v1-test",
                    engine_truth=lambda: "fish",
                )
                service.activate({
                    "catalog_id": voice_catalog.make_catalog_id("all", "managed", "v1"),
                    "filler_policy": "require_matching",
                }, "1")
                config.update({"voice_id": "raw"}, "2")
                config.update({"voice_id": "bundled-default"}, "3")
                cg._voice_activation_service = service
                with mock.patch.object(cg, "FILLERS", [(b"ID3bundled", "bundled.mp3")]):
                    status, _headers, body = self.request_raw(
                        "GET", "/filler",
                        headers={"Authorization": "Bearer member-secret"},
                    )
                self.assertEqual(status, 200)
                self.assertEqual(body, b"ID3bundled")
            finally:
                if previous_dir is None:
                    os.environ.pop("CATY_CONFIG_DIR", None)
                else:
                    os.environ["CATY_CONFIG_DIR"] = previous_dir

    def test_identity_advertises_activation_only_when_picker_capable(self):
        identity_config = {
            "name": "Test", "accent_color": "#000000", "assets_version": 1,
        }
        with mock.patch.object(cg, "resolved_config", return_value=identity_config), \
                mock.patch.object(cg, "_voice_engine_truth", return_value="openclaw"):
            voice = cg.identity_payload()["voice"]
        self.assertFalse(voice["picker"])
        self.assertNotIn("activation_api", voice)
        with mock.patch.object(cg, "resolved_config", return_value=identity_config), \
                mock.patch.object(cg, "_voice_engine_truth", return_value="fish"):
            voice = cg.identity_payload()["voice"]
        self.assertEqual(voice["activation_api"], "/tts/voice-activations")


class ManagedFillerReadTest(unittest.TestCase):
    def test_registry_read_revalidates_voice_and_hash_before_returning_bytes(self):
        with tempfile.TemporaryDirectory(prefix="managed-filler-read-") as tmp:
            registry = filler_pack.FillerPackRegistry(os.path.join(tmp, "registry"))
            audio = b"\xff\xfb\x90\x64" + b"x" * filler_pack.MIN_AUDIO_BYTES
            manifest = registry.stage_pack(
                pack_id="read-pack",
                generated_for_provider="fish",
                generated_for_reference_id="voice-a",
                filler_text_version="v1",
                texts={kind: [kind] for kind in filler_pack.REQUIRED_KINDS},
                synthesizer=lambda _text, _reference: audio,
                inference_contract_version="fish-tts-v1-test",
            )
            matching = registry.read_audio(
                manifest["pack_id"],
                active_provider="fish", active_reference_id="voice-a",
            )
            self.assertEqual(matching, {"status": "ready", "audio": audio})
            mismatch = registry.read_audio(
                manifest["pack_id"],
                active_provider="fish", active_reference_id="voice-b",
            )
            self.assertEqual(mismatch, {"status": "stale", "audio": None})

            manifest_path = registry.packs_dir / manifest["pack_id"] / "manifest.json"
            stored = json.loads(manifest_path.read_text(encoding="utf-8"))
            stored["status"] = "staged"
            manifest_path.write_text(json.dumps(stored), encoding="utf-8")
            staged = registry.read_audio(
                manifest["pack_id"],
                active_provider="fish", active_reference_id="voice-a",
            )
            self.assertEqual(staged, {"status": "unavailable", "audio": None})
            stored["status"] = "ready"
            manifest_path.write_text(json.dumps(stored), encoding="utf-8")

            path = registry.packs_dir / manifest["pack_id"] / "files" / "thinking-0.mp3"
            path.write_bytes(audio + b"tampered")
            corrupt = registry.read_audio(
                manifest["pack_id"],
                active_provider="fish", active_reference_id="voice-a",
            )
            self.assertEqual(corrupt, {"status": "unavailable", "audio": None})


if __name__ == "__main__":
    unittest.main()
