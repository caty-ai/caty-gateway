import os
import tempfile
import unittest
from unittest import mock

from caty_gateway.avatar_engine import AvatarEngine, AvatarEngineDisabled


class AvatarProvisioningContractTest(unittest.TestCase):
    def test_style_reference_has_no_checkout_default(self):
        with tempfile.TemporaryDirectory() as work, mock.patch.dict(os.environ, {}, clear=True):
            engine = AvatarEngine(work_dir=work)
            self.assertIsNone(engine.style_ref_path)
            with self.assertRaisesRegex(AvatarEngineDisabled, "CATY_AVATAR_STYLE_REF"):
                engine.start_stylize(b"image")

    def test_explicit_style_reference_is_preserved(self):
        with tempfile.TemporaryDirectory() as work:
            style = os.path.join(work, "style.png")
            engine = AvatarEngine(work_dir=work, style_ref_path=style)
            self.assertEqual(str(engine.style_ref_path), style)
