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


from importlib import resources
from pathlib import Path
from caty_gateway.setup_orchestrator import SetupOrchestrator


def test_renderer_seeds_packaged_assets_without_overwriting_member_files(tmp_path, monkeypatch):
    monkeypatch.setattr("caty_gateway.setup_orchestrator.platform.system", lambda: "Linux")
    orch = SetupOrchestrator(["--member", "fake-member"], env={"HOME": str(tmp_path)})
    orch._render_service("fake-token")
    values = orch._installed_env()
    assets = Path(values["CATY_ASSET_DIR"])
    assert assets == tmp_path / ".local/share/caty-gateway/fake-member/assets"
    bundled = [item for item in resources.files("caty_gateway").joinpath("assets").iterdir() if item.name.endswith(".png")]
    assert bundled
    for item in bundled:
        assert (assets / item.name).read_bytes() == item.read_bytes()
    customized = assets / bundled[0].name
    customized.write_bytes(b"fake-member-custom-image")
    orch._render_service("fake-token")
    assert customized.read_bytes() == b"fake-member-custom-image"
    assert Path(values["CATY_FILLER_DIR"]).is_dir()
    assert Path(values["CATY_CONFIG_DIR"]).is_dir()


def test_renderer_keeps_explicit_member_asset_directory(tmp_path, monkeypatch):
    monkeypatch.setattr("caty_gateway.setup_orchestrator.platform.system", lambda: "Linux")
    target = tmp_path / "fake-custom-assets"
    orch = SetupOrchestrator(["--member", "fake-member"],
        env={"HOME": str(tmp_path), "CATY_ASSET_DIR": str(target)})
    orch._render_service("fake-token")
    assert orch._installed_env()["CATY_ASSET_DIR"] == str(target)
    assert list(target.glob("*.png"))
