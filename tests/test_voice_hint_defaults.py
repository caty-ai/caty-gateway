import os
import tempfile
import unittest
from unittest import mock

from caty_gateway import caty_gateway as cg


class VoiceHintDefaultsTest(unittest.TestCase):
    def test_voice_prompt_is_channel_neutral(self):
        for name in ("Sl" + "ack", "Dis" + "cord", "Tele" + "gram"):
            self.assertNotIn(name, cg.DEFAULT_VOICE_HINT)

    def test_openclaw_appends_installed_push_helper(self):
        with mock.patch.object(cg, "BACKEND_NAME", "openclaw"):
            self.assertIn("caty_gateway.caty_push", str(cg.LiveVoiceHint()))

    def test_member_backends_use_thin_default(self):
        for backend in ("hermes", "claude"):
            with self.subTest(backend=backend), mock.patch.object(cg, "BACKEND_NAME", backend):
                self.assertEqual(str(cg.LiveVoiceHint()), cg.THIN_MEMBER_VOICE_HINT)

    def test_user_name_is_read_at_call_time(self):
        with mock.patch.dict(os.environ, {"CATY_USER_NAME": "Member A"}, clear=False):
            self.assertIn("Member A", cg._screen_push_hint())
        with mock.patch.dict(os.environ, {"CATY_USER_NAME": "Member B"}, clear=False):
            self.assertIn("Member B", cg._screen_push_hint())

    def test_custom_voice_hint_remains_editable(self):
        with mock.patch.object(cg, "resolved_config", return_value={"voice_hint": "custom"}), mock.patch.object(cg, "BACKEND_NAME", "openclaw"):
            self.assertTrue(str(cg.LiveVoiceHint()).startswith("custom"))


if __name__ == "__main__":
    unittest.main()
