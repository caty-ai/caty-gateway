import contextlib
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

from caty_gateway import caty_gateway as cg


class FillerResolutionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="caty-filler-resolution-")
        self.old_dir = cg.FILLER_DIR
        self.old_fillers = list(cg.FILLERS)
        self.old_metadata = list(cg.FILLER_METADATA)
        self.old_silence = cg.SILENCE_1S
        self.old_status = cg.FILLER_DIR_STATUS

    def tearDown(self):
        cg.FILLER_DIR = self.old_dir
        cg.FILLERS[:] = self.old_fillers
        cg.FILLER_METADATA[:] = self.old_metadata
        cg.SILENCE_1S = self.old_silence
        cg.FILLER_DIR_STATUS = self.old_status
        shutil.rmtree(self.tmp)

    def resolved(self, member="member-a", configured=None, *, set_configured=False):
        env = {"CATY_ID": member}
        if set_configured:
            env["CATY_FILLER_DIR"] = configured
        with mock.patch.dict(os.environ, env, clear=True):
            return cg._resolve_filler_dir()

    def test_every_member_uses_its_runtime_directory_when_unset(self):
        for member in ("caty", "member-a", "member-b"):
            with self.subTest(member=member):
                self.assertEqual(
                    self.resolved(member),
                    os.path.expanduser(f"~/.local/share/caty-gateway/{member}/fillers"),
                )

    def test_explicit_directory_and_explicit_disable_are_preserved(self):
        explicit = os.path.join(self.tmp, "fillers")
        self.assertEqual(self.resolved(configured=explicit, set_configured=True), explicit)
        self.assertEqual(self.resolved(configured="", set_configured=True), "")

    def test_invalid_member_cannot_escape_runtime_root(self):
        for member in ("../../caty", ".", ".."):
            with self.subTest(member=member), self.assertRaises(ValueError):
                self.resolved(member)

    def test_missing_runtime_directory_is_created_without_packaged_fallback(self):
        cg.FILLER_DIR = os.path.join(self.tmp, "missing")
        with contextlib.redirect_stdout(io.StringIO()):
            cg.load_fillers()
        self.assertTrue(os.path.isdir(cg.FILLER_DIR))
        self.assertEqual(cg.FILLERS, [])
        self.assertEqual(cg.FILLER_METADATA, [])
        self.assertIsNone(cg.SILENCE_1S)

    def test_disabled_directory_loads_nothing(self):
        cg.FILLER_DIR = ""
        with contextlib.redirect_stdout(io.StringIO()):
            cg.load_fillers()
        self.assertEqual(cg.FILLERS, [])
        self.assertEqual(cg.FILLER_DIR_STATUS, "unavailable")


if __name__ == "__main__":
    unittest.main()
