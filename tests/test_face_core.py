import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

from PIL import Image


from caty_gateway import face_core


class FaceCoreTests(unittest.TestCase):
    def make_retry_config(self, retry_count=1):
        return face_core.MemberConfig(
            name="Test",
            base_image=Path("/tmp/base.png"),
            character_description="test character",
            frames={
                slot: face_core.FrameConfig(prompt="Edit {slot} for {character_description}. {keep_prompt}")
                for slot in face_core.EXPRESSION_SLOTS
            },
            thresholds=face_core.Thresholds(size_floor_bytes=1, retry_count=retry_count),
        )

    def test_member_config_validates_prompt_templates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "member.json"
            path.write_text(
                json.dumps(
                    {
                        "name": "Test Member",
                        "base_image": "/tmp/base.png",
                        "character_description": "test character",
                        "frames": {
                            "idle": {
                                "prompt": (
                                    "Edit {character_description}; keep {keep_prompt}; "
                                    "member={member_name}; slot={slot}"
                                )
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            config = face_core.load_member_config(path)

        self.assertEqual(config.slug, "test-member")
        self.assertIn("{member_name}", config.frames["idle"].prompt)

    def test_rejects_unknown_prompt_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "member.json"
            path.write_text(
                json.dumps(
                    {
                        "name": "Test Member",
                        "base_image": "/tmp/base.png",
                        "character_description": "test character",
                        "frames": {"idle": {"prompt": "Edit {unknown}"}},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(face_core.ConfigError):
                face_core.load_member_config(path)

    def test_postprocess_and_icon_have_distinct_alpha_behavior(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.png"
            expr = Path(tmp) / "idle.png"
            icon = Path(tmp) / "icon.png"
            image = Image.new("RGBA", (6, 4), (255, 255, 255, 255))
            for x in (2, 3):
                for y in (1, 2):
                    image.putpixel((x, y), (20, 30, 40, 255))
            image.save(src)

            expr_stats = face_core.postprocess_expression(src, expr, size=8)
            icon_stats = face_core.make_icon(src, icon, size=8)
            expr_out = Image.open(expr).convert("RGBA")
            icon_out = Image.open(icon).convert("RGBA")

        self.assertAlmostEqual(expr_stats["clear_rate_pct"], 100.0 * 20 / 24)
        self.assertEqual(expr_out.getpixel((0, 0))[3], 0)
        self.assertEqual(icon_stats["crop_box"], (1, 0, 5, 4))
        self.assertEqual(icon_out.getchannel("A").getextrema(), (255, 255))

    def test_retry_accepts_largest_fallback_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            config = face_core.MemberConfig(
                name="Test",
                base_image=Path("/tmp/base.png"),
                character_description="test character",
                frames={
                    slot: face_core.FrameConfig(prompt="Edit {slot} for {character_description}. {keep_prompt}")
                    for slot in face_core.EXPRESSION_SLOTS
                },
                thresholds=face_core.Thresholds(size_floor_bytes=1_000_000, retry_count=1),
            )

            def generate_candidate(slot, prompt, raw_path, attempt, candidate):
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (4, 4), "white").save(raw_path)
                with raw_path.open("ab") as handle:
                    handle.write(b"x" * (300 if candidate == 2 else 10))

            record = face_core.generate_frame_with_retries(
                config,
                "talk1",
                output_dir,
                [],
                generate_candidate,
            )

        self.assertTrue(record["fallback"])
        self.assertEqual(record["attempt"], 2)
        self.assertEqual(record["candidate"], 2)
        self.assertIn("accepted largest candidate", record["reason"])

    def test_f7_one_candidate_transport_error_continues_ladder(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            decisions = []

            def generate_candidate(slot, prompt, raw_path, attempt, candidate):
                calls.append((attempt, candidate))
                if len(calls) == 1:
                    raise urllib.error.URLError("connection reset")
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (4, 4), "white").save(raw_path)

            record = face_core.generate_frame_with_retries(
                self.make_retry_config(),
                "talk1",
                Path(tmp),
                [],
                generate_candidate,
                decisions.append,
            )

        self.assertEqual(calls, [(1, 1), (2, 1), (2, 2)])
        self.assertTrue(record["accepted"])
        self.assertEqual(record["attempt"], 2)
        self.assertEqual(decisions[0]["accepted"], False)
        self.assertIn("transport error", decisions[0]["reason"])

    def test_f7_all_candidates_transport_error_fails_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            def generate_candidate(slot, prompt, raw_path, attempt, candidate):
                calls.append((attempt, candidate))
                raise TimeoutError("transport timeout")

            with self.assertRaisesRegex(RuntimeError, "all avatar candidates failed"):
                face_core.generate_frame_with_retries(
                    self.make_retry_config(), "talk1", Path(tmp), [], generate_candidate
                )

        self.assertEqual(calls, [(1, 1), (2, 1), (2, 2)])

    def test_f7_http_402_aborts_ladder_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            def generate_candidate(slot, prompt, raw_path, attempt, candidate):
                calls.append((attempt, candidate))
                raise urllib.error.HTTPError(
                    "https://api.caty.talk/v1/avatar/api/generate/submit",
                    402,
                    "budget exhausted",
                    hdrs={},
                    fp=None,
                )

            with self.assertRaises(urllib.error.HTTPError) as raised:
                face_core.generate_frame_with_retries(
                    self.make_retry_config(), "talk1", Path(tmp), [], generate_candidate
                )

        self.assertEqual(raised.exception.code, 402)
        self.assertEqual(calls, [(1, 1)])


if __name__ == "__main__":
    unittest.main()
