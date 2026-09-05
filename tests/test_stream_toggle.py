import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


from caty_gateway import caty_config
from caty_gateway import caty_gateway as cg


class FakeBackend:
    agent = "fake-agent"

    def __init__(self, supported, stream_entered=None, stream_release=None):
        self.supported = supported
        self.stream_entered = stream_entered
        self.stream_release = stream_release

    def supports_stream(self):
        return self.supported

    def stream(self, _text, _session_id=None, _brain_timeout=180, route=None):
        if self.stream_entered is not None:
            self.stream_entered.set()
        if self.stream_release is not None:
            self.stream_release.wait(timeout=2)
        yield "stream reply"

    def generate(self, _text, _session_id=None, _brain_timeout=180, route=None):
        return "legacy reply"


class StreamToggleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="caty-stream-toggle-")
        self.old_config = cg.CONFIG
        self.old_backend = cg.BACKEND
        self.old_env = {
            key: os.environ.get(key)
            for key in ("CATY_CONFIG_DIR", "CATY_STREAM_TTS")
        }
        os.environ["CATY_CONFIG_DIR"] = self.tmp.name
        os.environ.pop("CATY_STREAM_TTS", None)
        cg.CONFIG = caty_config.OverlayConfig(cg._config_defaults)

    def tearDown(self):
        cg.CONFIG = self.old_config
        cg.BACKEND = self.old_backend
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def reset_overlay(self):
        try:
            os.remove(cg.CONFIG.path())
        except FileNotFoundError:
            pass

    def set_overlay(self, value):
        if value is not None:
            cg.CONFIG.update({"stream_tts": value}, if_match=1)

    def test_effective_state_full_precedence_matrix(self):
        for supported in (False, True):
            for env_enabled in (False, True):
                for overlay in (None, "on", "off", ""):
                    with self.subTest(
                        supported=supported,
                        env_enabled=env_enabled,
                        overlay=overlay,
                    ):
                        self.reset_overlay()
                        if env_enabled:
                            os.environ["CATY_STREAM_TTS"] = "1"
                        else:
                            os.environ.pop("CATY_STREAM_TTS", None)
                        self.set_overlay(overlay)
                        cg.BACKEND = FakeBackend(supported)

                        payload = cg.config_payload()

                        if not supported:
                            expected_effective = "off"
                            expected_reason = "unsupported-backend"
                        elif overlay in ("on", "off"):
                            expected_effective = overlay
                            expected_reason = "runtime-override"
                        elif env_enabled:
                            expected_effective = "on"
                            expected_reason = "legacy-env"
                        else:
                            expected_effective = "off"
                            expected_reason = "default-off"
                        self.assertEqual(payload["stream_tts"], overlay or "")
                        self.assertEqual(
                            payload["stream_tts_effective"], expected_effective
                        )
                        self.assertIs(
                            payload["stream_tts_supported"], supported
                        )
                        self.assertEqual(
                            payload["stream_tts_reason"], expected_reason
                        )

    def test_turn_uses_entry_snapshot_and_next_turn_sees_runtime_update(self):
        cg.CONFIG.update({"stream_tts": "on"}, if_match=1)
        stream_entered = threading.Event()
        stream_release = threading.Event()
        cg.BACKEND = FakeBackend(True, stream_entered, stream_release)
        job = cg.Job("user text", session_id="snapshot-session")

        def push_audio(_text, target_job):
            target_job.push(b"mp3")
            return 3

        with mock.patch.object(
            cg, "tts_stream_to_job", side_effect=push_audio
        ), mock.patch.object(cg.history_store, "append_turn"):
            thread = threading.Thread(
                target=cg.stream_pipeline,
                args=(job, "user text", time.time()),
            )
            thread.start()
            try:
                self.assertTrue(
                    stream_entered.wait(timeout=1),
                    "turn did not enter the snapshotted streaming path",
                )
                cg.CONFIG.update({"stream_tts": "off"}, if_match=2)
                next_enabled, next_supported, next_reason = (
                    cg.stream_tts_effective_state()
                )
                self.assertFalse(next_enabled)
                self.assertTrue(next_supported)
                self.assertEqual(next_reason, "runtime-override")
            finally:
                stream_release.set()
                thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertTrue(job.stream_enabled)
        self.assertTrue(job.done)
        self.assertEqual(job.reply, "stream reply")
        self.assertEqual(job.chunks, [b"mp3"])


if __name__ == "__main__":
    unittest.main()
