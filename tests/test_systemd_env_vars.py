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


import pathlib
import plistlib
import sys
import pytest
from caty_gateway.setup_orchestrator import SetupOrchestrator


def test_systemd_environment_file_is_absolute(tmp_path, monkeypatch):
    monkeypatch.setattr("caty_gateway.setup_orchestrator.platform.system", lambda: "Linux")
    orch = SetupOrchestrator(["--member", "fake-member"], env={"HOME": str(tmp_path)})
    unit = orch._expected_systemd_unit().decode("utf-8")
    line = next(line for line in unit.splitlines() if line.startswith("EnvironmentFile="))
    assert line == 'EnvironmentFile="' + str(tmp_path / ".config/caty-gateway/fake-member.env") + '"'
    assert "%h" not in line and "%i" not in line
    assert orch.service_name == "caty-gateway-fake-member.service"


@pytest.mark.parametrize("system", ["Linux", "Darwin"])
@pytest.mark.parametrize("backend", ["claude", "codex", "openclaw", "hermes", "openai-compat", "openai_compat"])
def test_renderer_persists_runtime_env_auth_history_and_module(tmp_path, monkeypatch, system, backend):
    monkeypatch.setattr("caty_gateway.setup_orchestrator.platform.system", lambda: system)
    home = tmp_path / 'fake home & "quoted" %i'
    home.mkdir()
    state = tmp_path / "fake-state"
    env = {"HOME": str(home), "PYTHON": sys.executable, "XDG_STATE_HOME": str(state),
           "CATY_HERMES_API_KEY": "fake-hermes-key", "CATY_OPENAI_API_KEY": "fake-api-key",
           "PATH": "/fake-custom-bin:/usr/bin:/bin", "ANTHROPIC_API_KEY": "fake-provider-key",
           "POYO_API_KEY": "fake-image-key",
           "CATY_CLAUDE_CWD": str(home), "CATY_REQUIRE_AUTH": "0", "CATY_LANG": "th"}
    orch = SetupOrchestrator(["--member", "fake-member", "--backend", backend,
                              "--name", 'Fake "Name" & $value', "--public-url", "http://127.0.0.1:8788"], env=env)
    orch.config = orch._resolved_config()
    orch._start_state()
    orch._install()
    installed = orch._installed_env()
    assert installed["CATY_BACKEND"] == backend.replace("_", "-")
    assert installed["CATY_NAME"] == 'Fake "Name" & $value'
    assert installed["CATY_REQUIRE_AUTH"] == "1"
    assert installed["CATY_LANG"] == "th"
    assert "/fake-custom-bin" in installed["PATH"].split(":")
    assert installed["ANTHROPIC_API_KEY"] == "fake-provider-key"
    assert installed["POYO_API_KEY"] == "fake-image-key"
    assert installed["CATY_HISTORY_DIR"] == str(state / "caty-gateway/history/fake-member")
    assert pathlib.Path(installed["CATY_HISTORY_DIR"]).is_dir()
    assert installed["CATY_HERMES_API_KEY"] == "fake-hermes-key"
    assert installed["CATY_OPENAI_API_KEY"] == "fake-api-key"
    assert len(bytes.fromhex(installed["CATY_TOKEN"])) == 24
    assert orch.artifact_path.stat().st_mode & 0o777 == 0o600
    assert (home / ".local/share/caty-gateway/fake-member").is_dir()
    if system == "Linux":
        unit = home / ".config/systemd/user" / orch.service_name
        assert unit.read_bytes() == orch._expected_systemd_unit()
        assert ' -m caty_gateway.caty_gateway' in unit.read_text()
        assert "__" not in unit.read_text()
        assert "%%i" in unit.read_text()
        env_line = next(line for line in unit.read_text().splitlines() if line.startswith("EnvironmentFile="))
        escaped_path = str(orch.artifact_path).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
        assert env_line == 'EnvironmentFile="' + escaped_path + '"'
    else:
        payload = plistlib.loads(orch.artifact_path.read_bytes())
        assert payload["ProgramArguments"] == [sys.executable, "-m", "caty_gateway.caty_gateway"]
        assert payload["WorkingDirectory"] == str(home)
        assert payload["EnvironmentVariables"]["PATH"].split(":")[0] == str(home / ".local/bin")
        assert payload["Label"] == "ai.caty.gateway.fake-member"
        assert "__" not in orch.artifact_path.read_text()


@pytest.mark.parametrize("system", ["Linux", "Darwin"])
def test_no_history_omits_inherited_history_setting(tmp_path, monkeypatch, system):
    monkeypatch.setattr("caty_gateway.setup_orchestrator.platform.system", lambda: system)
    orch = SetupOrchestrator(["--member", "fake-member", "--no-history"],
        env={"HOME": str(tmp_path), "CATY_HISTORY_DIR": str(tmp_path / "old-history")})
    orch._render_service("fake-token")
    assert "CATY_HISTORY_DIR" not in orch._installed_env()
    assert not (tmp_path / "old-history").exists()


@pytest.mark.parametrize("credential", ["generated", "wrong", "missing"])
def test_setup_health_uses_generated_auth_against_real_handler(tmp_path, monkeypatch, credential):
    from urllib import error, parse
    from caty_gateway import caty_gateway as gateway
    from caty_gateway import setup_orchestrator
    from tests.test_config_api import MemoryServer, MemorySocket

    monkeypatch.setattr(setup_orchestrator.platform, "system", lambda: "Linux")
    orch = SetupOrchestrator(["--member", "fake-member"], env={"HOME": str(tmp_path)})
    orch._render_service("fake-generated")
    installed = orch._installed_env()
    monkeypatch.setenv("CATY_REQUIRE_AUTH", installed["CATY_REQUIRE_AUTH"])
    monkeypatch.setattr(gateway, "CATY_TOKEN", installed["CATY_TOKEN"])
    monkeypatch.setattr(gateway, "CATY_ADMIN_TOKEN", "")
    calls = []

    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    def urlopen(request, timeout):
        calls.append(request.get_header("Authorization"))
        path = parse.urlsplit(request.full_url).path
        raw = ("GET %s HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\nAuthorization: %s\r\n\r\n"
               % (path, request.get_header("Authorization", ""))).encode()
        sock = MemorySocket(raw)
        gateway.Handler(sock, ("127.0.0.1", 0), MemoryServer())
        status = int(sock.output.getvalue().split(b" ", 2)[1])
        if status != 200:
            raise error.HTTPError(request.full_url, status, "unauthorized", {}, None)
        return Response()

    monkeypatch.setattr(setup_orchestrator.urllib.request, "urlopen", urlopen)
    if credential == "generated":
        orch._health()
        assert calls == ["Bearer fake-generated"]
    else:
        orch._identity_token = lambda: "fake-wrong" if credential == "wrong" else ""
        times = iter([0, 0, 31])
        monkeypatch.setattr(setup_orchestrator.time, "monotonic", lambda: next(times, 31))
        monkeypatch.setattr(setup_orchestrator.time, "sleep", lambda seconds: None)
        orch._diagnostics = lambda: "fake diagnostics"
        with pytest.raises(setup_orchestrator.SetupError, match="health timed out|no CATY_TOKEN"):
            orch._health()
        assert calls == (["Bearer fake-wrong"] if credential == "wrong" else [])
