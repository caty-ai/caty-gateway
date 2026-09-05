import os
import sys
import unittest
from unittest import mock


from caty_gateway import caty_gateway as cg


class RuntimeKindTest(unittest.TestCase):
    def test_runtime_kind_mapping_and_normalization(self):
        cases = (
            ("openclaw", "openclaw"),
            ("hermes", "hermes"),
            ("claude", "claude-code"),
            ("codex", "codex-cli"),
            ("openai-compat", "local-llm"),
            ("openai_compat", "local-llm"),
            ("unsupported", "unknown"),
            ("generic", "unknown"),
            ("", "unknown"),
            (None, "unknown"),
            ("gemini", "unknown"),
            (" Claude ", "claude-code"),
        )

        for backend_name, expected in cases:
            with self.subTest(backend_name=backend_name):
                self.assertEqual(
                    cg.runtime_kind_for_backend(backend_name),
                    expected,
                )

    def test_runtime_kind_output_is_closed(self):
        self.assertTrue(set(cg._RUNTIME_KIND_BY_BACKEND.values()) <= cg.RUNTIME_KINDS)
        self.assertIn("unknown", cg.RUNTIME_KINDS)
        self.assertEqual(len(cg.RUNTIME_KINDS), 6)

        backend_names = (
            "openclaw", "hermes", "claude", "codex", "openai-compat",
            "openai_compat", "unsupported", "generic", "",
            None, "gemini", " Claude ", "future-backend",
        )

        for backend_name in backend_names:
            with self.subTest(backend_name=backend_name):
                self.assertIn(
                    cg.runtime_kind_for_backend(backend_name),
                    cg.RUNTIME_KINDS,
                )

    def test_identity_payload_reads_current_backend_name(self):
        config = {
            "name": "Caty",
            "accent_color": "#FF8FB1",
            "assets_version": 1,
        }
        with mock.patch.object(cg, "resolved_config", return_value=config):
            with mock.patch.object(cg, "_voice_engine_truth", return_value="proxy"):
                with mock.patch.object(cg, "backend_available", return_value=True):
                    with mock.patch.object(cg, "BACKEND_NAME", "claude"):
                        payload = cg.identity_payload()
                        self.assertEqual(payload["runtime_kind"], "claude-code")
                        self.assertEqual(payload["protocol_version"], 1)

                    with mock.patch.object(cg, "BACKEND_NAME", "unsupported"):
                        payload = cg.identity_payload()
                        self.assertEqual(payload["runtime_kind"], "unknown")
                        self.assertEqual(payload["protocol_version"], 1)

    def test_config_payload_reads_current_backend_name_and_keeps_raw_backend(self):
        with mock.patch.object(
            cg,
            "_stream_tts_effective_state",
            return_value=(False, True, "default-off"),
        ):
            for backend_name, expected in (
                ("claude", "claude-code"),
                ("openai_compat", "local-llm"),
                ("unsupported", "unknown"),
            ):
                with self.subTest(backend_name=backend_name):
                    with mock.patch.object(cg, "BACKEND_NAME", backend_name):
                        payload = cg.config_payload(cg._config_defaults())
                    self.assertEqual(payload["runtime_kind"], expected)
                    self.assertEqual(payload["backend"], backend_name)


if __name__ == "__main__":
    unittest.main()
