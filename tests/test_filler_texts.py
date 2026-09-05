import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from caty_gateway import filler_pack
from caty_gateway import filler_texts


EXPECTED_DEFAULT = {
    "thinking": ["んー", "えっと"],
    "wait": ["少し待ってね", "ちょっと確認しますね"],
    "large": ["少し時間がかかりそう", "大きめの作業みたい"],
    "alive": ["まだやってるよ", "もう少しね"],
    "fail": ["ごめんね、もう一回お願い"],
}

LEGACY_V1_TEXTS = {
    "thinking": ["うん、ちょっと考えるね。"],
    "wait": ["少し待ってね。"],
    "large": ["もう少しかかりそう。待っててね。"],
    "alive": ["ちゃんと考えてるよ。"],
    "fail": ["ごめんね、もう一度お願い。"],
}


class FillerTextsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="caty-filler-texts-")
        self.root = Path(self.tmp.name)
        self.member = "test-member"

    def tearDown(self):
        self.tmp.cleanup()

    def write_override(self, kinds=None, raw=None):
        path = filler_texts.override_path(self.member, self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        if raw is not None:
            path.write_text(raw, encoding="utf-8")
        else:
            path.write_text(
                json.dumps({
                    "schema": filler_texts.SCHEMA,
                    "language": "ja",
                    "kinds": kinds,
                }, ensure_ascii=False),
                encoding="utf-8",
            )
        return path

    def test_bundled_json_matches_owner_approved_neutral_set(self):
        self.assertEqual(filler_texts.load_default(), EXPECTED_DEFAULT)

    def test_bundled_default_version_differs_from_v1_five_sentences(self):
        self.assertNotEqual(
            filler_texts.text_version(filler_texts.load_default()),
            filler_texts.text_version(LEGACY_V1_TEXTS),
        )

    def test_defaults_only_always_contains_required_kinds(self):
        result = filler_texts.effective(self.member, self.root)
        self.assertEqual(result.texts, EXPECTED_DEFAULT)
        self.assertEqual(set(result.texts), set(filler_texts.REQUIRED_KINDS))
        self.assertEqual(result.override_status, "none")
        self.assertEqual(set(result.sources.values()), {"default"})

    def test_full_and_partial_overrides_replace_whole_kinds(self):
        full = {kind: [f"new-{kind}"] for kind in filler_texts.REQUIRED_KINDS}
        full["announce"] = ["できたよ"]
        self.write_override(full)
        result = filler_texts.effective(self.member, self.root)
        self.assertEqual(result.texts, filler_texts.normalize(full))
        self.assertEqual(result.override_status, "ok")
        self.assertEqual(set(result.sources.values()), {"override"})

        self.write_override({"wait": ["待って"]})
        partial = filler_texts.effective(self.member, self.root)
        self.assertEqual(partial.texts["wait"], ["待って"])
        self.assertEqual(partial.texts["thinking"], EXPECTED_DEFAULT["thinking"])
        self.assertEqual(set(filler_texts.REQUIRED_KINDS) - set(partial.texts), set())

    def test_invalid_override_variants_fall_back_whole_file_and_warn_once(self):
        invalids = {
            "broken": (None, "{"),
            "empty-string": ({"wait": ["   "]}, None),
            "empty-array": ({"wait": []}, None),
            "too-long": ({"wait": ["x" * 41]}, None),
            "unknown-kind": ({"unknown": ["x"]}, None),
            "too-many": ({"wait": [str(index) for index in range(filler_texts.MAX_PER_KIND + 1)]}, None),
            "secret": ({"wait": ["Bearer token"]}, None),
        }
        for label, (kinds, raw) in invalids.items():
            with self.subTest(label=label):
                self.write_override(kinds, raw)
                with self.assertLogs("caty_gateway.filler_texts", level="WARNING") as logs:
                    result = filler_texts.effective(self.member, self.root)
                self.assertEqual(len(logs.records), 1)
                self.assertIn(self.member, logs.output[0])
                self.assertTrue(result.override_status.startswith("invalid: "))
                self.assertEqual(result.texts, EXPECTED_DEFAULT)

    def test_normalization_is_idempotent_and_version_ignores_whitespace_duplicates_and_kind_order(self):
        first = {
            "wait": [" 待って ", "待って"],
            "thinking": [" 考える "],
        }
        second = {"thinking": ["考える"], "wait": ["待って"]}
        normalized = filler_texts.normalize(first)
        self.assertEqual(filler_texts.normalize(normalized), normalized)
        self.assertEqual(filler_texts.text_version(first), filler_texts.text_version(second))

    def test_one_character_change_changes_version(self):
        first = filler_texts.load_default()
        second = {kind: list(values) for kind, values in first.items()}
        second["wait"] = [second["wait"][0] + "ね"]
        self.assertNotEqual(filler_texts.text_version(first), filler_texts.text_version(second))

    def test_per_kind_limit_is_evaluated_from_environment_at_call_time(self):
        with mock.patch.dict(
            os.environ, {"CATY_VOICE_FILLER_MAX_TEXTS_PER_KIND": "2"}
        ):
            self.assertEqual(filler_texts.max_per_kind(), 2)
            errors = filler_texts.validate({"wait": ["one", "two", "three"]})

        self.assertIn("kind must contain at most 2 texts", errors["wait"])
        for invalid in ("invalid", "0", "-1"):
            with self.subTest(invalid=invalid), mock.patch.dict(
                os.environ, {"CATY_VOICE_FILLER_MAX_TEXTS_PER_KIND": invalid}
            ):
                self.assertEqual(
                    filler_texts.max_per_kind(), filler_texts.MAX_PER_KIND
                )

    def test_lockless_platform_still_reads_writes_and_deletes_override(self):
        with mock.patch.object(filler_texts, "fcntl", None):
            saved = filler_texts.save_override(
                self.member, {"wait": ["first"]}, data_root=self.root
            )
            self.assertEqual(
                filler_texts.effective(self.member, self.root).version,
                saved.version,
            )
            deleted = filler_texts.delete_override(
                self.member, if_match=saved.version, data_root=self.root
            )

        self.assertEqual(deleted.override_status, "none")

    def test_save_override_is_atomic_and_enforces_if_match(self):
        with self.assertRaises(filler_texts.ConflictError):
            filler_texts.save_override(
                self.member, {"wait": ["first"]},
                if_match="wrong", data_root=self.root,
            )
        created = filler_texts.save_override(
            self.member, {"wait": [" first ", "first"]}, data_root=self.root
        )
        path = filler_texts.override_path(self.member, self.root)
        self.assertEqual(created.texts["wait"], ["first"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        original = path.read_bytes()

        with self.assertRaises(filler_texts.ConflictError) as conflict:
            filler_texts.save_override(
                self.member, {"wait": ["second"]}, if_match="wrong", data_root=self.root
            )
        self.assertEqual(conflict.exception.current.version, created.version)
        self.assertEqual(path.read_bytes(), original)

        with mock.patch("caty_gateway.filler_texts.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                filler_texts.save_override(
                    self.member, {"wait": ["second"]},
                    if_match=created.version, data_root=self.root,
                )
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.glob(".filler-texts-*.json")), [])

    def test_delete_override_requires_matching_version(self):
        created = filler_texts.save_override(
            self.member, {"wait": ["first"]}, data_root=self.root
        )
        with self.assertRaises(filler_texts.ConflictError):
            filler_texts.delete_override(self.member, data_root=self.root)
        result = filler_texts.delete_override(
            self.member, if_match=created.version, data_root=self.root
        )
        self.assertEqual(result.override_status, "none")
        self.assertFalse(filler_texts.override_path(self.member, self.root).exists())

    def test_validate_returns_per_kind_errors_without_saving(self):
        errors = filler_texts.validate({"wait": [], "bogus": ["ok"]})
        self.assertIn("wait", errors)
        self.assertIn("bogus", errors)
        with self.assertRaises(filler_texts.ValidationError):
            filler_texts.save_override(
                self.member, {"wait": []}, data_root=self.root
            )
        self.assertFalse(filler_texts.override_path(self.member, self.root).exists())

    def test_optional_announce_is_valid_but_not_required(self):
        self.assertEqual(filler_texts.validate({"announce": ["できた"]}), {})
        self.assertNotIn("announce", filler_texts.load_default())

    def test_secret_marker_tuple_matches_pack_guard(self):
        self.assertEqual(filler_texts._SECRET_KEY_PARTS, filler_pack._SECRET_KEY_PARTS)

    def test_data_root_matches_registry_member_layout(self):
        registry = filler_pack.FillerPackRegistry.for_member(
            self.member, data_root=self.root
        )
        self.assertEqual(
            filler_texts.override_path(self.member, self.root).parent,
            registry.root.parent,
        )


if __name__ == "__main__":
    unittest.main()
