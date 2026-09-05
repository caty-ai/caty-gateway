from importlib import resources
import unittest


class ServiceTemplateContractTest(unittest.TestCase):
    def template(self, name):
        return resources.files("caty_gateway").joinpath("templates", name).read_text(encoding="utf-8")

    def test_launchd_uses_public_label(self):
        text = self.template("launchd.plist")
        self.assertIn("ai.caty.gateway.__MEMBER_ID__", text)
        self.assertNotIn("ai." + "openclaw", text)

    def test_systemd_runs_installed_module(self):
        text = self.template("systemd.service")
        self.assertIn("python", text.lower())
        self.assertIn("caty_gateway.caty_gateway", text)


import sys
import pytest
from caty_gateway.setup_orchestrator import SetupError, SetupOrchestrator


@pytest.mark.parametrize("system", ["Linux", "Darwin"])
def test_renderer_resume_keeps_existing_token_and_configuration(tmp_path, monkeypatch, system):
    monkeypatch.setattr("caty_gateway.setup_orchestrator.platform.system", lambda: system)
    orch = SetupOrchestrator(["--member", "fake-member"], env={"HOME": str(tmp_path)})
    orch.config = orch._resolved_config()
    orch._start_state()
    orch._install()
    original = (orch.artifact_path.read_bytes(), orch.artifact_path.stat().st_mtime_ns)
    orch._install()
    assert (orch.artifact_path.read_bytes(), orch.artifact_path.stat().st_mtime_ns) == original
    assert orch._owned_artifact()


def test_renderer_refuses_foreign_unit_without_writing_env(tmp_path, monkeypatch):
    monkeypatch.setattr("caty_gateway.setup_orchestrator.platform.system", lambda: "Linux")
    orch = SetupOrchestrator(["--member", "fake-member"], env={"HOME": str(tmp_path)})
    unit = tmp_path / ".config/systemd/user" / orch.service_name
    unit.parent.mkdir(parents=True)
    unit.write_bytes(b"foreign service\n")
    with pytest.raises(SetupError, match="foreign service unit"):
        orch._render_service("fake-token")
    assert unit.read_bytes() == b"foreign service\n"
    assert not orch.artifact_path.exists()


def test_python_selection_ignores_checkout_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTHON", raising=False)
    checkout = tmp_path / "fake-checkout"
    checkout.mkdir()
    orch = SetupOrchestrator(["--member", "fake-member"], env={"HOME": str(tmp_path)}, workdir=checkout)
    assert orch.service_python == sys.executable
    override = SetupOrchestrator(["--member", "fake-member"],
        env={"HOME": str(tmp_path), "PYTHON": "/fake/python"}, workdir=checkout)
    assert override.service_python == "/fake/python"
    assert list(checkout.iterdir()) == []


def test_renderer_rejects_multiline_env_before_writing(tmp_path, monkeypatch):
    monkeypatch.setattr("caty_gateway.setup_orchestrator.platform.system", lambda: "Linux")
    orch = SetupOrchestrator(["--member", "fake-member"],
        env={"HOME": str(tmp_path), "CATY_NAME": "fake\nCATY_REQUIRE_AUTH=0"})
    with pytest.raises(SetupError, match="newlines"):
        orch._render_service("fake-token")
    assert not orch.artifact_path.exists()
