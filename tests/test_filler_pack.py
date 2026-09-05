import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from caty_gateway import filler_pack
from caty_gateway import tts_fish


KINDS = filler_pack.REQUIRED_KINDS


def valid_mp3(marker=b""):
    header = b"\xff\xfb\x90\x64"
    payload = header + marker
    return payload + b"\x00" * (filler_pack.MIN_AUDIO_BYTES - len(payload))


def texts(version="v1"):
    return {kind: [f"{version}-{kind}"] for kind in KINDS}


class FakeSynthesizer:
    def __init__(self, invalid_kind=None):
        self.invalid_kind = invalid_kind
        self.calls = []

    def __call__(self, text, reference_id):
        self.calls.append((text, reference_id))
        kind = text.rsplit("-", 1)[-1]
        if kind == self.invalid_kind:
            return b"not-mp3"
        return valid_mp3(reference_id.encode("utf-8") + b":" + text.encode("utf-8"))


class Faults:
    def __init__(self, point, action=None, occurrence=1):
        self.point = point
        self.action = action
        self.occurrence = occurrence
        self.seen = 0

    def __call__(self, point, context):
        if point != self.point:
            return
        self.seen += 1
        if self.seen != self.occurrence:
            return
        if self.action is not None:
            self.action(context)
        raise RuntimeError(f"fault:{point}")


class FillerPackRegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="caty-filler-pack-")
        self.root = Path(self.tmp.name) / "registry"

    def tearDown(self):
        self.tmp.cleanup()

    def registry(self, **kwargs):
        return filler_pack.FillerPackRegistry(self.root, **kwargs)

    def stage(self, registry=None, pack_id="pack-a", reference="voice-a", version="v1", **kwargs):
        registry = registry or self.registry()
        synth = kwargs.pop("synthesizer", FakeSynthesizer())
        stage_texts = kwargs.pop("texts", texts(version))
        provenance = kwargs.pop(
            "provenance", {"source": "bundled-text-v1", "generator": "local"}
        )
        license_metadata = kwargs.pop(
            "license_metadata", {"audio_redistribution": False}
        )
        manifest = registry.stage_pack(
            pack_id=pack_id,
            generated_for_provider="fish",
            generated_for_reference_id=reference,
            preset_id="warm",
            preset_version=1,
            filler_text_version=version,
            texts=stage_texts,
            synthesizer=synth,
            inference_contract_version="fish-tts-v1-s1",
            provenance=provenance,
            license_metadata=license_metadata,
            generated_at="2026-08-08T12:00:00Z",
            **kwargs,
        )
        return manifest, synth

    def test_stage_uses_explicit_target_and_publishes_complete_manifest(self):
        registry = self.registry()
        active_voice = "voice-current"
        manifest, synth = self.stage(registry, reference="voice-target")

        self.assertEqual(active_voice, "voice-current")
        self.assertEqual([reference for _, reference in synth.calls], ["voice-target"] * 5)
        self.assertEqual(manifest["status"], "ready")
        self.assertEqual(manifest["generated_for_provider"], "fish")
        self.assertEqual(manifest["generated_for_reference_id"], "voice-target")
        self.assertEqual(manifest["preset_id"], "warm")
        self.assertEqual(manifest["preset_version"], 1)
        self.assertEqual(manifest["filler_text_version"], "v1")
        self.assertEqual(manifest["inference_contract_version"], "fish-tts-v1-s1")
        self.assertEqual(manifest["kinds"], sorted(KINDS))
        self.assertEqual(manifest["provenance"]["generator"], "local")
        self.assertFalse(manifest["license_metadata"]["audio_redistribution"])
        self.assertRegex(manifest["generated_at"], r"Z$")

        pack = self.root / "packs" / "pack-a"
        self.assertTrue((pack / "manifest.json").is_file())
        self.assertTrue((pack / "texts.json").is_file())
        for relative, expected_hash in manifest["files_sha256"].items():
            data = (pack / relative).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), expected_hash)

    def test_stage_synthesizes_every_text_and_validates_text_audio_parity(self):
        registry = self.registry()
        multi_texts = {
            kind: [f"first-{kind}", f"second-{kind}"] for kind in KINDS
        }
        synth = FakeSynthesizer()
        manifest = registry.stage_pack(
            pack_id="multi",
            generated_for_provider="fish",
            generated_for_reference_id="voice-a",
            filler_text_version="multi-v1",
            texts=multi_texts,
            synthesizer=synth,
            inference_contract_version="contract",
        )
        self.assertEqual(len(synth.calls), 10)
        for kind in KINDS:
            self.assertEqual(len(manifest["files"][kind]), 2)

        sidecar = self.root / "packs" / "multi" / "texts.json"
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        payload["texts"]["thinking"].pop()
        sidecar.write_text(json.dumps(payload), encoding="utf-8")
        result = registry.resolve(
            "multi", active_provider="fish", active_reference_id="voice-a"
        )
        self.assertEqual(result["status"], "unavailable")

    def test_optional_announce_pack_is_valid_but_playback_draws_required_kinds_only(self):
        registry = self.registry()
        pack_texts = texts()
        pack_texts["announce"] = ["ready-announce"]
        manifest, synth = self.stage(
            registry, pack_id="with-announce", texts=pack_texts
        )

        self.assertEqual(manifest["kinds"], sorted(pack_texts))
        self.assertEqual(len(synth.calls), 6)
        sidecar = registry.read_texts("with-announce")
        self.assertEqual(sidecar["texts"]["announce"], ["ready-announce"])
        resolved = registry.resolve(
            "with-announce",
            active_provider="fish",
            active_reference_id="voice-a",
        )
        self.assertIn("announce", resolved["files"])

        choices = []
        with mock.patch.object(
            filler_pack.secrets,
            "choice",
            side_effect=lambda values: choices.extend(values) or values[0],
        ):
            audio = registry.read_audio(
                "with-announce",
                active_provider="fish",
                active_reference_id="voice-a",
            )
        self.assertEqual(audio["status"], "ready")
        self.assertTrue(choices)
        self.assertFalse(any("announce" in relative for relative in choices))

    def test_resolve_can_report_text_stale_without_changing_voice_match(self):
        registry = self.registry()
        self.stage(registry, pack_id="text-v1", version="text-v1")

        stale = registry.resolve(
            "text-v1",
            active_provider="fish",
            active_reference_id="voice-a",
            expected_text_version="text-v2",
        )
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["reason"], "text")
        self.assertEqual(stale["files"], {})
        ready = registry.resolve(
            "text-v1",
            active_provider="fish",
            active_reference_id="voice-a",
            expected_text_version="text-v1",
        )
        self.assertEqual(ready["status"], "ready")

    def test_missing_kind_and_invalid_mp3_never_publish(self):
        registry = self.registry()
        incomplete = texts()
        incomplete.pop("fail")
        with self.assertRaisesRegex(filler_pack.FillerPackError, "kinds are incomplete"):
            registry.stage_pack(
                pack_id="missing-kind",
                generated_for_provider="fish",
                generated_for_reference_id="voice-a",
                filler_text_version="v1",
                texts=incomplete,
                synthesizer=FakeSynthesizer(),
                inference_contract_version="contract",
            )
        self.assertFalse((self.root / "packs" / "missing-kind").exists())

        with self.assertRaisesRegex(filler_pack.FillerPackError, "invalid synthesized MP3"):
            self.stage(registry, pack_id="invalid-mp3", synthesizer=FakeSynthesizer("large"))
        self.assertFalse((self.root / "packs" / "invalid-mp3").exists())

    def test_mp3_validation_rejects_tiny_magic_and_accepts_id3_with_frame(self):
        registry = self.registry()
        malformed_with_sync = b"x" * 100 + b"\xff\xe0\x00\x00" + b"x" * 920
        for index, data in enumerate(
            (b"ID3", b"ID3" + b"x" * 1021, malformed_with_sync)
        ):
            with self.subTest(data=index), self.assertRaisesRegex(
                filler_pack.FillerPackError, "invalid synthesized MP3"
            ):
                self.stage(
                    registry,
                    pack_id=f"tiny-magic-{index}",
                    synthesizer=lambda _text, _reference, data=data: data,
                )
        id3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + valid_mp3()
        manifest, _ = self.stage(
            registry,
            pack_id="id3-frame",
            synthesizer=lambda _text, _reference: id3,
        )
        self.assertEqual(manifest["status"], "ready")

    def test_text_count_limit_fails_before_paid_synthesis(self):
        synth = FakeSynthesizer()
        too_many = {kind: [f"{kind}-{index}" for index in range(3)] for kind in KINDS}
        with self.assertRaisesRegex(filler_pack.FillerPackError, "too many filler texts"):
            self.registry(max_texts_per_kind=2).stage_pack(
                pack_id="too-many",
                generated_for_provider="fish",
                generated_for_reference_id="voice-a",
                filler_text_version="v1",
                texts=too_many,
                synthesizer=synth,
                inference_contract_version="contract",
            )
        self.assertEqual(synth.calls, [])

    def test_text_character_limit_is_invalid_not_too_many(self):
        synth = FakeSynthesizer()
        too_long = texts()
        too_long["thinking"] = ["x" * 41]

        with self.assertRaisesRegex(
            filler_pack.FillerPackError, "^invalid filler text$"
        ):
            self.registry().stage_pack(
                pack_id="too-long",
                generated_for_provider="fish",
                generated_for_reference_id="voice-a",
                filler_text_version="v1",
                texts=too_long,
                synthesizer=synth,
                inference_contract_version="contract",
            )

        self.assertEqual(synth.calls, [])

    def test_ready_pack_remains_available_when_stored_text_exceeds_current_limit(self):
        registry = self.registry()
        self.stage(registry, pack_id="grandfathered-ready")
        sidecar_path = registry.packs_dir / "grandfathered-ready" / "texts.json"
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        sidecar["texts"]["thinking"] = ["x" * 41]
        sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")

        resolved = registry.resolve(
            "grandfathered-ready",
            active_provider="fish",
            active_reference_id="voice-a",
        )

        self.assertEqual(resolved["status"], "ready")

    def test_hash_mismatch_and_sidecar_failure_never_publish(self):
        def corrupt_audio(context):
            (Path(context["stage_dir"]) / "files" / "thinking-0.mp3").write_bytes(
                valid_mp3(b"tampered")
            )

        def corrupt_before_validate(point, context):
            if point == "before_validate":
                corrupt_audio(context)

        corrupt = self.registry(fault_injector=corrupt_before_validate)
        with self.assertRaisesRegex(filler_pack.FillerPackError, "hash mismatch"):
            self.stage(corrupt, pack_id="bad-hash")
        self.assertFalse((self.root / "packs" / "bad-hash").exists())

        # Remove the sidecar immediately before validation; the stage may remain
        # for recovery, but it is never visible under packs/.
        def remove_sidecar(context):
            (Path(context["stage_dir"]) / "texts.json").unlink()

        def remove_before_validate(point, context):
            if point == "before_validate":
                remove_sidecar(context)

        sidecar = filler_pack.FillerPackRegistry(
            Path(self.tmp.name) / "sidecar-registry", fault_injector=remove_before_validate
        )
        with self.assertRaisesRegex(filler_pack.FillerPackError, "metadata unavailable"):
            self.stage(sidecar, pack_id="bad-sidecar")
        self.assertFalse((sidecar.packs_dir / "bad-sidecar").exists())

    def test_published_hash_corruption_resolves_unavailable(self):
        registry = self.registry()
        self.stage(registry)
        path = self.root / "packs" / "pack-a" / "files" / "thinking-0.mp3"
        path.write_bytes(valid_mp3(b"changed"))

        result = registry.resolve(
            "pack-a", active_provider="fish", active_reference_id="voice-a"
        )
        self.assertEqual(result, {"pack_id": "pack-a", "status": "unavailable", "files": {}})

    def test_mid_stage_crash_recovery_is_idempotent_and_does_not_touch_lkg(self):
        registry = self.registry()
        self.stage(registry, pack_id="old")
        registry.pin_lkg("old")

        crashing = filler_pack.FillerPackRegistry(
            self.root, fault_injector=Faults("stage_after_kind", occurrence=2)
        )
        with self.assertRaisesRegex(RuntimeError, "fault:stage_after_kind"):
            self.stage(crashing, pack_id="partial")
        self.assertFalse((self.root / "packs" / "partial").exists())
        self.assertEqual(registry.lkg_pack_id(), "old")

        first = registry.recover_staging()
        second = registry.recover_staging()
        self.assertTrue(first["removed_stages"])
        self.assertEqual(second["removed_stages"], [])
        self.assertTrue((self.root / "packs" / "old").is_dir())
        self.assertEqual(registry.lkg_pack_id(), "old")

    def test_pre_publish_crash_recovers_ready_pack_without_changing_lkg(self):
        registry = self.registry()
        self.stage(registry, pack_id="old")
        registry.pin_lkg("old")
        crashing = filler_pack.FillerPackRegistry(
            self.root, fault_injector=Faults("before_publish")
        )
        with self.assertRaisesRegex(RuntimeError, "fault:before_publish"):
            self.stage(crashing, pack_id="new", reference="voice-new")
        self.assertFalse((self.root / "packs" / "new").exists())
        self.assertEqual(registry.lkg_pack_id(), "old")

        report = registry.recover_staging()
        self.assertEqual(report["published"], ["new"])
        self.assertEqual(registry.lkg_pack_id(), "old")
        self.assertTrue((self.root / "packs" / "old").is_dir())
        self.assertEqual(registry.recover_staging()["published"], [])

    def test_recovery_republishes_valid_stage_over_corrupt_published_pack(self):
        registry = self.registry()
        self.stage(registry, pack_id="repair")
        published = self.root / "packs" / "repair"
        stage = self.root / "staging" / "repair.ready"
        shutil.copytree(published, stage)
        (published / "files" / "thinking-0.mp3").write_bytes(b"broken")

        report = registry.recover_staging()

        self.assertEqual(report["published"], ["repair"])
        result = registry.resolve(
            "repair", active_provider="fish", active_reference_id="voice-a"
        )
        self.assertEqual(result["status"], "ready")
        self.assertFalse(stage.exists())

    def test_resolve_rejects_staged_status_and_published_parity_mismatch(self):
        registry = self.registry()
        self.stage(registry, pack_id="staged-in-packs")
        manifest_path = self.root / "packs" / "staged-in-packs" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["status"] = "staged"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual(
            registry.resolve(
                "staged-in-packs",
                active_provider="fish",
                active_reference_id="voice-a",
            )["status"],
            "unavailable",
        )

        self.stage(registry, pack_id="published-parity")
        sidecar_path = self.root / "packs" / "published-parity" / "texts.json"
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        sidecar["texts"]["thinking"] = []
        sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")
        self.assertEqual(
            registry.resolve(
                "published-parity",
                active_provider="fish",
                active_reference_id="voice-a",
            )["status"],
            "unavailable",
        )

    @unittest.skipIf(filler_pack.fcntl is None, "flock unavailable")
    def test_registry_lock_is_reentrant_and_inner_exit_keeps_outer_lock(self):
        first = self.registry()
        second = self.registry()
        entered = threading.Event()
        released = threading.Event()

        def competitor():
            entered.set()
            with second._locked():
                released.set()

        with first._locked():
            with first._locked():
                thread = threading.Thread(target=competitor)
                thread.start()
                self.assertTrue(entered.wait(1))
            self.assertFalse(released.wait(0.1))
        self.assertTrue(released.wait(1))
        thread.join(1)
        self.assertFalse(thread.is_alive())

    def test_generation_does_not_block_resolve_and_same_pack_race_never_clobbers(self):
        registry = self.registry()
        self.stage(registry, pack_id="serving")
        synthesis_started = threading.Event()
        continue_synthesis = threading.Event()

        def slow_synth(text, reference_id):
            synthesis_started.set()
            self.assertTrue(continue_synthesis.wait(2))
            return valid_mp3(text.encode("utf-8"))

        worker = threading.Thread(
            target=lambda: self.stage(
                registry,
                pack_id="building",
                synthesizer=slow_synth,
            )
        )
        worker.start()
        self.assertTrue(synthesis_started.wait(1))
        started = time.monotonic()
        resolved = registry.resolve(
            "serving", active_provider="fish", active_reference_id="voice-a"
        )
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertEqual(resolved["status"], "ready")
        continue_synthesis.set()
        worker.join(3)
        self.assertFalse(worker.is_alive())

        barrier = threading.Barrier(2)
        outcomes = []

        def racing_synth(text, reference_id):
            if text.endswith("thinking"):
                barrier.wait(2)
            return valid_mp3(text.encode("utf-8"))

        def publish_same_pack():
            try:
                self.stage(
                    registry,
                    pack_id="same-id",
                    synthesizer=racing_synth,
                )
                outcomes.append("ready")
            except filler_pack.FillerPackError as exc:
                outcomes.append(str(exc))

        racers = [threading.Thread(target=publish_same_pack) for _ in range(2)]
        for racer in racers:
            racer.start()
        for racer in racers:
            racer.join(3)
        self.assertEqual(outcomes.count("ready"), 1)
        self.assertEqual(outcomes.count("pack id already exists"), 1)
        self.assertEqual(
            registry.resolve(
                "same-id", active_provider="fish", active_reference_id="voice-a"
            )["status"],
            "ready",
        )
        self.assertEqual(list(registry.staging_dir.iterdir()), [])
        self.assertEqual(registry.recover_staging()["published"], [])

    def test_recovery_between_mkdtemp_and_stage_lock_preserves_young_stage(self):
        registry = self.registry()
        original_mkdtemp = filler_pack.tempfile.mkdtemp
        raced = threading.Event()

        def racing_mkdtemp(*args, **kwargs):
            path = original_mkdtemp(*args, **kwargs)
            if not raced.is_set():
                raced.set()
                self.registry().recover_staging()
            return path

        with mock.patch.object(
            filler_pack.tempfile, "mkdtemp", side_effect=racing_mkdtemp
        ):
            manifest, _ = self.stage(registry, pack_id="mkdtemp-race")

        self.assertEqual(manifest["status"], "ready")
        self.assertEqual(
            registry.resolve(
                "mkdtemp-race", active_provider="fish", active_reference_id="voice-a"
            )["status"],
            "ready",
        )

    @unittest.skipIf(filler_pack.fcntl is None, "flock unavailable")
    def test_recovery_during_stage_write_skips_locked_stage_and_publish_still_succeeds(self):
        registry = self.registry()
        original_write_bytes = filler_pack._write_bytes
        recovery_done = threading.Event()
        recovery_thread = None

        def interleaved_write(path, data):
            nonlocal recovery_thread
            if Path(path).name == "thinking-0.mp3" and recovery_thread is None:
                recovery_thread = threading.Thread(
                    target=lambda: (self.registry().recover_staging(), recovery_done.set())
                )
                recovery_thread.start()
                self.assertTrue(recovery_done.wait(1))
            return original_write_bytes(path, data)

        with mock.patch.object(
            filler_pack, "_write_bytes", side_effect=interleaved_write
        ):
            manifest, _ = self.stage(registry, pack_id="write-race")

        if recovery_thread is not None:
            recovery_thread.join(1)
            self.assertFalse(recovery_thread.is_alive())
        self.assertEqual(manifest["status"], "ready")
        self.assertEqual(
            registry.resolve(
                "write-race", active_provider="fish", active_reference_id="voice-a"
            )["status"],
            "ready",
        )

    def test_lockless_young_invalid_stage_is_skipped_but_old_stage_is_reaped(self):
        registry = self.registry()
        young = registry.staging_dir / "young-lockless"
        old = registry.staging_dir / "old-lockless"
        young.mkdir()
        old.mkdir()
        old_age = time.time() - registry._lockless_stage_grace_seconds - 5
        os.utime(old, (old_age, old_age))

        report = registry.recover_staging()

        self.assertTrue(young.exists())
        self.assertFalse(old.exists())
        self.assertNotIn(young.name, report["removed_stages"])
        self.assertIn(old.name, report["removed_stages"])

    def test_stage_write_oserror_is_sanitized(self):
        registry = self.registry()
        leaked_path = str(self.root / "staging" / "sensitive" / "thinking-0.mp3")

        def explode(*_args, **_kwargs):
            raise FileNotFoundError(2, "No such file or directory", leaked_path)

        with mock.patch.object(filler_pack, "_write_bytes", side_effect=explode):
            with self.assertRaisesRegex(
                filler_pack.FillerPackError, "pack staging failed"
            ) as caught:
                self.stage(registry, pack_id="sanitized-stage-error")

        self.assertNotIn(leaked_path, str(caught.exception))
        self.assertFalse((registry.packs_dir / "sanitized-stage-error").exists())

    def test_stage_lock_open_file_not_found_is_sanitized(self):
        registry = self.registry()
        original_open = filler_pack.Path.open
        leaked_path = str(registry.staging_dir / "lock-missing" / ".stage.lock")

        def patched_open(path_obj, *args, **kwargs):
            if path_obj.name == ".stage.lock":
                raise FileNotFoundError(2, "No such file or directory", leaked_path)
            return original_open(path_obj, *args, **kwargs)

        with mock.patch.object(filler_pack.Path, "open", autospec=True, side_effect=patched_open):
            with self.assertRaisesRegex(
                filler_pack.FillerPackError, "pack staging failed"
            ) as caught:
                self.stage(registry, pack_id="lock-open-failure")

        self.assertNotIn(leaked_path, str(caught.exception))
        self.assertFalse((registry.packs_dir / "lock-open-failure").exists())

    @unittest.skipIf(filler_pack.fcntl is None, "flock unavailable")
    def test_recovery_cannot_steal_a_ready_stage_from_its_publisher(self):
        before_publish = threading.Event()
        continue_publish = threading.Event()
        recovered = threading.Event()

        def pause_publish(point, _context):
            if point == "before_publish":
                before_publish.set()
                self.assertTrue(continue_publish.wait(2))

        registry = self.registry(fault_injector=pause_publish)
        publisher = threading.Thread(
            target=lambda: self.stage(registry, pack_id="owned-stage")
        )
        publisher.start()
        self.assertTrue(before_publish.wait(1))

        def recover():
            self.registry().recover_staging()
            recovered.set()

        recovery = threading.Thread(target=recover)
        recovery.start()
        self.assertFalse(recovered.wait(0.1))
        continue_publish.set()
        publisher.join(3)
        recovery.join(3)
        self.assertFalse(publisher.is_alive())
        self.assertFalse(recovery.is_alive())
        self.assertEqual(
            registry.resolve(
                "owned-stage", active_provider="fish", active_reference_id="voice-a"
            )["status"],
            "ready",
        )

    def test_reference_or_provider_mismatch_is_stale_and_returns_no_audio(self):
        registry = self.registry()
        self.stage(registry)
        for provider, reference in (("fish", "voice-b"), ("other", "voice-a")):
            with self.subTest(provider=provider, reference=reference):
                result = registry.resolve(
                    "pack-a", active_provider=provider, active_reference_id=reference
                )
                self.assertEqual(result["status"], "stale")
                self.assertEqual(result["files"], {})

        ready = registry.resolve(
            "pack-a", active_provider="fish", active_reference_id="voice-a"
        )
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(set(ready["files"]), set(KINDS))

    def test_successful_predecessor_remains_resolvable_lkg(self):
        registry = self.registry()
        self.stage(registry, pack_id="previous")
        self.stage(registry, pack_id="next")
        registry.pin_lkg("previous")

        rollback = registry.resolve_lkg(
            active_provider="fish", active_reference_id="voice-a"
        )
        self.assertEqual(rollback["pack_id"], "previous")
        self.assertEqual(rollback["status"], "ready")
        self.assertTrue((self.root / "packs" / "next").is_dir())

    def test_resolve_lkg_never_returns_staged_audio(self):
        registry = self.registry()
        self.stage(registry, pack_id="previous")
        registry.pin_lkg("previous")
        manifest_path = registry.packs_dir / "previous" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["status"] = "staged"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        rollback = registry.resolve_lkg(
            active_provider="fish", active_reference_id="voice-a"
        )

        self.assertEqual(rollback["status"], "unavailable")
        self.assertEqual(rollback["files"], {})

    def test_gc_crash_never_moves_active_or_lkg_and_recovery_is_idempotent(self):
        registry = self.registry()
        for pack_id in ("active", "lkg", "doomed"):
            self.stage(registry, pack_id=pack_id)
        registry.pin_lkg("lkg")
        crashing = filler_pack.FillerPackRegistry(
            self.root, fault_injector=Faults("gc_after_move")
        )
        with self.assertRaisesRegex(RuntimeError, "fault:gc_after_move"):
            crashing.garbage_collect(
                ["active", "lkg", "doomed"], protected_pack_ids={"active"}
            )

        self.assertTrue((self.root / "packs" / "active").is_dir())
        self.assertTrue((self.root / "packs" / "lkg").is_dir())
        self.assertFalse((self.root / "packs" / "doomed").exists())
        self.assertTrue(any((self.root / "trash").iterdir()))
        self.assertEqual(registry.lkg_pack_id(), "lkg")

        first = registry.recover_staging()
        second = registry.recover_staging()
        self.assertTrue(first["removed_trash"])
        self.assertEqual(second["removed_trash"], [])
        self.assertTrue((self.root / "packs" / "active").is_dir())
        self.assertTrue((self.root / "packs" / "lkg").is_dir())

    def test_gc_only_collects_explicit_unprotected_candidates(self):
        registry = self.registry()
        for pack_id in ("keep", "remove", "unlisted"):
            self.stage(registry, pack_id=pack_id)
        result = registry.garbage_collect(["keep", "remove"], protected_pack_ids={"keep"})
        self.assertEqual(result["removed"], ["remove"])
        self.assertTrue((self.root / "packs" / "keep").is_dir())
        self.assertTrue((self.root / "packs" / "unlisted").is_dir())

    def test_gc_requires_explicit_protected_pack_ids(self):
        registry = self.registry()
        self.stage(registry, pack_id="active")
        with self.assertRaises(TypeError):
            registry.garbage_collect(["active"])
        self.assertTrue((self.root / "packs" / "active").is_dir())

    def test_gc_fails_closed_when_lkg_pointer_is_corrupt(self):
        registry = self.registry()
        self.stage(registry, pack_id="candidate")
        registry.lkg_path.write_text("not-json", encoding="utf-8")

        with self.assertRaisesRegex(filler_pack.FillerPackError, "metadata unavailable"):
            registry.garbage_collect(["candidate"], protected_pack_ids=set())
        self.assertTrue((self.root / "packs" / "candidate").is_dir())

    def test_recovery_completes_gc_journal_and_sweeps_state_temp_files(self):
        registry = self.registry()
        self.stage(registry, pack_id="doomed")
        run_id = "a" * 32
        trash = registry.trash_dir / f"{run_id}.doomed"
        os.replace(registry.packs_dir / "doomed", trash)
        journal = registry.state_dir / f"gc-{run_id}.json"
        journal.write_text(
            json.dumps({"run_id": run_id, "pack_ids": ["doomed"]}),
            encoding="utf-8",
        )
        temporary = registry.state_dir / ".lkg.json.crash"
        temporary.write_text("partial", encoding="utf-8")

        report = registry.recover_staging()

        self.assertIn(trash.name, report["removed_trash"])
        self.assertIn(journal.name, report["removed_journals"])
        self.assertIn(temporary.name, report["removed_state_temps"])
        self.assertFalse(trash.exists())
        self.assertFalse(journal.exists())
        self.assertFalse(temporary.exists())

    def test_recovery_quarantine_survives_same_sweep_then_expires(self):
        registry = self.registry()
        self.stage(registry, pack_id="repair")
        published = registry.packs_dir / "repair"
        stage = registry.staging_dir / "repair.ready"
        shutil.copytree(published, stage)
        expired = time.time() - registry._recovery_quarantine_retention_seconds - 5
        os.utime(published, (expired, expired))
        (published / "files" / "thinking-0.mp3").write_bytes(b"broken")

        report = registry.recover_staging()

        quarantined = sorted(registry.trash_dir.glob("recovery-*.repair"))
        self.assertEqual(report["published"], ["repair"])
        self.assertEqual(report["removed_trash"], [])
        self.assertEqual(len(quarantined), 1)

        os.utime(quarantined[0], (expired, expired))
        second = registry.recover_staging()
        self.assertIn(quarantined[0].name, second["removed_trash"])
        self.assertFalse(quarantined[0].exists())

    def test_recovery_publish_strips_stage_lock_from_published_pack(self):
        registry = self.registry()
        self.stage(registry, pack_id="clean-target")
        published = registry.packs_dir / "clean-target"
        self.assertEqual(
            {child.name for child in published.iterdir()},
            {"files", "manifest.json", "texts.json"},
        )
        stage = registry.staging_dir / "clean-target.ready"
        shutil.copytree(published, stage)
        (stage / ".stage.lock").write_text("", encoding="utf-8")
        shutil.rmtree(published)

        report = registry.recover_staging()

        self.assertEqual(report["published"], ["clean-target"])
        self.assertFalse((registry.packs_dir / "clean-target" / ".stage.lock").exists())
        self.assertEqual(
            {child.name for child in (registry.packs_dir / "clean-target").iterdir()},
            {"files", "manifest.json", "texts.json"},
        )

    def test_loser_stage_marker_blocks_recovery_republish_when_cleanup_fails(self):
        registry = self.registry()
        self.stage(registry, pack_id="same-id")
        loser = registry.staging_dir / "same-id.ready"
        shutil.copytree(registry.packs_dir / "same-id", loser)
        (loser / filler_pack._STAGE_LOSER_MARKER).write_text("", encoding="utf-8")

        (registry.packs_dir / "same-id" / "files" / "thinking-0.mp3").write_bytes(b"broken")
        report = registry.recover_staging()

        self.assertEqual(report["published"], [])
        self.assertIn(loser.name, report["removed_stages"])
        self.assertEqual(
            registry.resolve(
                "same-id", active_provider="fish", active_reference_id="voice-a"
            )["status"],
            "unavailable",
        )

    def test_list_packs_returns_status_and_generated_for_without_layout_access(self):
        registry = self.registry()
        self.stage(registry, pack_id="listed", reference="voice-listed")
        self.stage(registry, pack_id="corrupt")
        (registry.packs_dir / "corrupt" / "manifest.json").write_text(
            "not-json", encoding="utf-8"
        )

        self.assertEqual(
            registry.list_packs(),
            [
                {"pack_id": "corrupt", "status": "unavailable", "generated_for": None},
                {
                    "pack_id": "listed",
                    "status": "ready",
                    "generated_for": {
                        "provider": "fish",
                        "reference_id": "voice-listed",
                    },
                },
            ],
        )

    def test_legacy_import_copies_without_mutating_and_stales_after_voice_change(self):
        legacy = Path(self.tmp.name) / "legacy"
        legacy.mkdir()
        before = {}
        sidecar = {}
        for index in range(1):
            name = f"filler{index}.mp3"
            data = valid_mp3(b"legacy-" + str(index).encode("ascii"))
            (legacy / name).write_bytes(data)
            before[name] = data
            sidecar[name] = f"legacy text {index}"
        sidecar_path = Path(self.tmp.name) / "legacy-texts.json"
        sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")

        registry = self.registry()
        manifest = registry.import_legacy(
            legacy,
            pack_id="legacy-pack",
            generated_for_provider="fish",
            generated_for_reference_id="voice-old",
            texts_path=sidecar_path,
            provenance={"source": "explicit-legacy-import"},
            generated_at="2026-08-08T12:00:00Z",
        )
        self.assertEqual(manifest["status"], "legacy-unknown")
        self.assertEqual({path.name: path.read_bytes() for path in legacy.iterdir()}, before)
        copied = []
        for relative in manifest["files_sha256"]:
            copied.append((self.root / "packs" / "legacy-pack" / relative).read_bytes())
        self.assertCountEqual(copied, list(before.values()))

        current = registry.resolve(
            "legacy-pack", active_provider="fish", active_reference_id="voice-old"
        )
        self.assertEqual(current["status"], "legacy-unknown")
        stale = registry.resolve(
            "legacy-pack", active_provider="fish", active_reference_id="voice-new"
        )
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["files"], {})

    def test_legacy_import_preserves_flat_pool_for_every_kind_and_pool_size(self):
        for count in (1, 4, 7):
            with self.subTest(count=count):
                legacy = Path(self.tmp.name) / f"legacy-{count}"
                legacy.mkdir()
                sidecar = {}
                for index in range(count):
                    name = "silence1s.mp3" if index == count - 1 else f"filler{index}.mp3"
                    (legacy / name).write_bytes(valid_mp3(name.encode("utf-8")))
                    sidecar[name] = f"legacy text {index}"
                sidecar_path = Path(self.tmp.name) / f"legacy-{count}-texts.json"
                sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")
                registry = filler_pack.FillerPackRegistry(
                    Path(self.tmp.name) / f"registry-{count}"
                )
                manifest = registry.import_legacy(
                    legacy,
                    pack_id=f"legacy-{count}",
                    generated_for_provider="fish",
                    generated_for_reference_id="voice-old",
                    texts_path=sidecar_path,
                )
                pools = [tuple(manifest["files"][kind]) for kind in KINDS]
                self.assertTrue(all(pool == pools[0] for pool in pools))
                self.assertEqual(len(pools[0]), count)
                self.assertEqual(len(manifest["files_sha256"]), count)
                resolved = registry.resolve(
                    f"legacy-{count}",
                    active_provider="fish",
                    active_reference_id="voice-old",
                )
                self.assertEqual(resolved["status"], "legacy-unknown")
                self.assertTrue(all(len(resolved["files"][kind]) == count for kind in KINDS))

    def test_legacy_import_accepts_missing_and_duplicate_labels(self):
        legacy = Path(self.tmp.name) / "legacy-permissive-labels"
        legacy.mkdir()
        for name in ("missing.mp3", "same-a.mp3", "same-b.mp3"):
            (legacy / name).write_bytes(valid_mp3(name.encode("utf-8")))
        sidecar_path = Path(self.tmp.name) / "legacy-permissive-labels.json"
        sidecar_path.write_text(
            json.dumps({"same-a.mp3": "same", "same-b.mp3": "same"}),
            encoding="utf-8",
        )
        registry = self.registry()

        try:
            manifest = registry.import_legacy(
                legacy,
                pack_id="legacy-permissive-labels",
                generated_for_provider="fish",
                generated_for_reference_id="voice-old",
                texts_path=sidecar_path,
            )
        except filler_pack.FillerPackError as error:
            self.fail(f"import_legacy raised {error!r}")

        self.assertEqual(manifest["status"], "legacy-unknown")
        self.assertEqual(registry.inspect(manifest["pack_id"])["status"], "legacy-unknown")
        self.assertEqual(
            registry.resolve(
                manifest["pack_id"],
                active_provider="fish",
                active_reference_id="voice-old",
            )["status"],
            "legacy-unknown",
        )
        audio = registry.read_audio(
            manifest["pack_id"],
            active_provider="fish",
            active_reference_id="voice-old",
        )
        self.assertEqual(audio["status"], "legacy-unknown")
        self.assertTrue(audio["audio"])

    def test_preexisting_legacy_pack_with_blank_sidecar_texts_stays_available(self):
        legacy = Path(self.tmp.name) / "legacy-blank-sidecar"
        legacy.mkdir()
        (legacy / "filler.mp3").write_bytes(valid_mp3(b"legacy-blank"))
        sidecar_path = Path(self.tmp.name) / "legacy-blank-label.json"
        sidecar_path.write_text(
            json.dumps({"filler.mp3": "placeholder"}), encoding="utf-8"
        )
        registry = self.registry()
        registry.import_legacy(
            legacy,
            pack_id="legacy-blank-sidecar",
            generated_for_provider="fish",
            generated_for_reference_id="voice-old",
            texts_path=sidecar_path,
        )
        stored_sidecar_path = (
            registry.packs_dir / "legacy-blank-sidecar" / "texts.json"
        )
        stored_sidecar = json.loads(stored_sidecar_path.read_text(encoding="utf-8"))
        stored_sidecar["texts"] = {kind: [""] for kind in KINDS}
        stored_sidecar_path.write_text(json.dumps(stored_sidecar), encoding="utf-8")

        self.assertEqual(
            registry.inspect("legacy-blank-sidecar")["status"], "legacy-unknown"
        )
        read = registry.read_texts("legacy-blank-sidecar")
        self.assertEqual(read["texts"], {kind: [""] for kind in KINDS})

    def test_legacy_import_missing_texts_path_is_explicit_error(self):
        legacy = Path(self.tmp.name) / "legacy-missing-texts"
        legacy.mkdir()
        (legacy / "filler.mp3").write_bytes(valid_mp3())
        with self.assertRaisesRegex(
            filler_pack.FillerPackError, "legacy filler texts unavailable"
        ):
            self.registry().import_legacy(
                legacy,
                pack_id="legacy-missing-texts",
                generated_for_provider="fish",
                generated_for_reference_id="voice-old",
                texts_path=Path(self.tmp.name) / "missing.json",
            )

    def test_secret_and_private_provenance_are_rejected_before_artifacts(self):
        registry = self.registry()
        cases = (
            {"api_key": "not-allowed"},
            {"source": "http://127.0.0.1/private"},
            {"source": "Bearer private-value"},
            {"source": "https://example.com/fillers?token=abc123"},
            {"source": "https://example.com/fillers#signed-fragment"},
            {"source": "generated with token=abc123"},
        )
        with mock.patch.dict(os.environ, {"FISH_API_KEY": "fish-super-secret"}):
            cases += ({"source": "fish-super-secret"},)
            for index, provenance in enumerate(cases):
                with self.subTest(provenance=provenance), self.assertRaisesRegex(
                    filler_pack.FillerPackError, "unsafe provenance"
                ):
                    registry.stage_pack(
                        pack_id=f"secret-{index}",
                        generated_for_provider="fish",
                        generated_for_reference_id="voice-a",
                        filler_text_version="v1",
                        texts=texts(),
                        synthesizer=FakeSynthesizer(),
                        inference_contract_version="contract",
                        provenance=provenance,
                    )
        self.assertEqual(list((self.root / "packs").iterdir()), [])

    def test_embedded_environment_secrets_are_rejected_in_metadata_and_texts(self):
        secret = "fish-secret-1234"
        registry = self.registry()
        with mock.patch.dict(os.environ, {"FISH_API_KEY": secret}, clear=False):
            with self.assertRaisesRegex(filler_pack.FillerPackError, "unsafe provenance"):
                self.stage(
                    registry,
                    pack_id="embedded-provenance",
                    provenance={"source": f"generated-with-{secret}-locally"},
                )
            secret_texts = texts()
            secret_texts["thinking"] = f"prefix-{secret}-suffix"
            with self.assertRaisesRegex(filler_pack.FillerPackError, "unsafe texts"):
                registry.stage_pack(
                    pack_id="embedded-text",
                    generated_for_provider="fish",
                    generated_for_reference_id="voice-a",
                    filler_text_version="v1",
                    texts=secret_texts,
                    synthesizer=FakeSynthesizer(),
                    inference_contract_version="contract",
                )
            with self.assertRaisesRegex(filler_pack.FillerPackError, "unsafe manifest"):
                registry.stage_pack(
                    pack_id="embedded-reference",
                    generated_for_provider="fish",
                    generated_for_reference_id=f"voice-{secret}",
                    filler_text_version="v1",
                    texts=texts(),
                    synthesizer=FakeSynthesizer(),
                    inference_contract_version="contract",
                )
        self.assertEqual(list(registry.packs_dir.iterdir()), [])

    def test_short_environment_secret_does_not_contaminate_published_pack_validation(self):
        registry = self.registry()
        self.stage(registry, pack_id="short-secret")

        with mock.patch.dict(os.environ, {"FISH_API_KEY": "fish"}, clear=False):
            resolved = registry.resolve(
                "short-secret", active_provider="fish", active_reference_id="voice-a"
            )

        self.assertEqual(resolved["status"], "ready")

    def test_synthesizer_runtime_error_is_sanitized(self):
        registry = self.registry()

        def boom(_text, _reference):
            raise RuntimeError("private /tmp/secret boom")

        with self.assertRaisesRegex(
            filler_pack.FillerPackError, "filler synthesis failed"
        ) as caught:
            self.stage(registry, pack_id="runtime-boom", synthesizer=boom)

        self.assertNotIn("/tmp/secret", str(caught.exception))
        self.assertFalse((registry.packs_dir / "runtime-boom").exists())

    def test_synthesizer_iterator_runtime_error_is_sanitized(self):
        registry = self.registry()

        def boom_stream(_text, _reference):
            yield valid_mp3()[:32]
            raise RuntimeError("private /tmp/iter boom")

        with self.assertRaisesRegex(
            filler_pack.FillerPackError, "filler synthesis failed"
        ) as caught:
            self.stage(registry, pack_id="iter-runtime-boom", synthesizer=boom_stream)

        self.assertNotIn("/tmp/iter", str(caught.exception))
        self.assertFalse((registry.packs_dir / "iter-runtime-boom").exists())

    def test_registry_rejects_world_writable_or_symlink_root(self):
        insecure = Path(self.tmp.name) / "insecure"
        insecure.mkdir()
        insecure.chmod(0o777)
        with self.assertRaisesRegex(filler_pack.FillerPackError, "group/world writable"):
            filler_pack.FillerPackRegistry(insecure)

        private = Path(self.tmp.name) / "private"
        private.mkdir()
        linked = Path(self.tmp.name) / "linked"
        linked.symlink_to(private, target_is_directory=True)
        with self.assertRaisesRegex(filler_pack.FillerPackError, "directory unavailable"):
            filler_pack.FillerPackRegistry(linked)

    def test_fish_target_synthesis_isolated_from_conversation_health(self):
        chunks = iter((b"ID3", b"audio"))
        with mock.patch.dict(os.environ, {"FISH_API_KEY": "fish-test-key"}, clear=True), \
                mock.patch.object(tts_fish, "_preview_stream_response", return_value=chunks) as stream, \
                mock.patch.object(tts_fish, "_check_health") as check, \
                mock.patch.object(tts_fish, "_record_unhealthy") as record:
            result = tts_fish.synthesize_filler("hello", "voice-target")
            self.assertEqual(list(result), [b"ID3", b"audio"])

        check.assert_not_called()
        record.assert_not_called()
        body = json.loads(stream.call_args.args[1])
        self.assertEqual(body["reference_id"], "voice-target")
        self.assertEqual(body["text"], "hello")
        self.assertTrue(stream.call_args.kwargs["sanitize_errors"])
        self.assertEqual(
            stream.call_args.kwargs["attempts_env"],
            "CATY_VOICE_FILLER_TTS_ATTEMPTS",
        )
        self.assertEqual(
            stream.call_args.kwargs["timeout_env"],
            "CATY_VOICE_FILLER_TTS_TIMEOUT_SECONDS",
        )

    def test_real_filler_generator_stream_stages_as_ready_pack(self):
        audio = valid_mp3(b"fish-stream")

        def stream_response(*_args, **_kwargs):
            return iter((audio[:512], audio[512:]))

        with mock.patch.dict(os.environ, {"FISH_API_KEY": "fish-test-key"}, clear=True), \
                mock.patch.object(
                    tts_fish,
                    "_preview_stream_response",
                    side_effect=stream_response,
                ):
            manifest, _ = self.stage(
                self.registry(),
                pack_id="real-adapter",
                synthesizer=tts_fish.synthesize_filler,
            )
        self.assertEqual(manifest["status"], "ready")

    def test_fish_target_synthesis_does_not_expose_raw_upstream_error(self):
        connection = mock.Mock()
        connection.request.side_effect = RuntimeError(
            "private https://127.0.0.1/upstream?token=raw-secret"
        )
        with mock.patch.dict(os.environ, {"FISH_API_KEY": "fish-test-key"}, clear=True), \
                mock.patch.object(tts_fish, "_connection", return_value=connection):
            with self.assertRaisesRegex(RuntimeError, "filler synthesis failed") as caught:
                list(tts_fish.synthesize_filler("hello", "voice-target"))
        self.assertNotIn("raw-secret", str(caught.exception))
        self.assertNotIn("127.0.0.1", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
