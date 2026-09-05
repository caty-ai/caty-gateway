import re
import unittest

from caty_gateway import voice_presets


class VoicePresetsTest(unittest.TestCase):
    def test_preset_mapping_is_pinned(self):
        self.assertEqual(
            voice_presets.PRESETS,
            {
                "fish-neutral-ja-v1": {
                    "provider": "fish",
                    "reference_id": "0089dce5fefb4c6ba9b9f2f0debe1ddc",
                    "display_name_ja": "おまかせ — 落ち着いた日本語",
                }
            },
        )

    def test_public_reference_identifier_shape(self):
        reference = voice_presets.PRESETS["fish-neutral-ja-v1"]["reference_id"]
        self.assertRegex(reference, re.compile(r"[0-9a-f]{32}\Z"))


if __name__ == "__main__":
    unittest.main()
