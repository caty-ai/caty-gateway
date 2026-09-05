"""Passive doctor checks use fake processes and HTTP responses throughout."""
import json
from pathlib import Path
import socket
import subprocess

import pytest

from caty_gateway import doctor


@pytest.fixture
def harness(tmp_path):
    commands, requests, lines = [], [], []
    env = {
        "HOME": str(tmp_path), "PATH": "/fake-bin", "CATY_AGENT": "fake-agent",
        "CATY_GATEWAY_TOKEN": "fake-gateway-token", "CATY_HERMES_API_KEY": "fake-hermes-key",
        "CATY_OPENAI_BASE_URL": "http://127.0.0.1:12345/v1", "CATY_OPENAI_MODEL": "fake-model",
    }

    def runner(command, *, env, timeout):
        commands.append(command)
        assert timeout == doctor.TIMEOUT
        if "-c" in command:
            output = "3.10\n"
        elif command[-2:] == ["ip", "-4"]:
            # Reserved fixture address within the synthetic tailnet test range.
            output = "100.64.0.1\n"
        elif command[-2:] == ["agents", "list"]:
            output = "Agents:\n- fake-agent (default)\n"
        else:
            output = "fake version or status\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    def get_json(url, headers, timeout):
        assert timeout == doctor.TIMEOUT
        requests.append((url, headers, timeout))
        return 200, {"data": [{"id": "fake-model"}]}

    options = dict(env=env, home=tmp_path, system="Linux", runner=runner, get_json=get_json,
                   command_path=lambda value: "/fake-bin/" + Path(value).name,
                   resolver=lambda host: ["127.0.0.1"], port_available=lambda port: True,
                   writable=lambda path: True, emit=lines.append)
    return options, commands, requests, lines


@pytest.mark.parametrize("backend", doctor.BACKENDS)
def test_every_backend_passes_without_prompts(harness, backend):
    options, commands, requests, lines = harness
    instance = doctor.Doctor(backend=backend, **options)
    assert instance.run()
    assert instance.tailscale_ip == "100.64.0.1"
    assert instance.public_url == "http://100.64.0.1:8788"
    assert all(check.status in {"PASS", "WARN"} and check.hint for check in instance.checks)
    assert all(line.startswith(("PASS ", "WARN ")) for line in lines)
    assert lines == ["PASS " + check.name if check.status == "PASS"
                     else "WARN " + check.name + ": " + check.hint
                     for check in instance.checks]
    allowed = [["status"], ["ip", "-4"], ["--version"], ["login", "status"], ["agents", "list"]]
    assert all(command[1:] in allowed or command[1] == "-c" for command in commands)
    assert all(url.endswith(("/models", ":18789")) for url, _, _ in requests)
    assert not any("prompt" in command or "exec" in command for command in commands)


@pytest.mark.parametrize("status", ["PASS", "FAIL", "WARN"])
def test_repair_hint_is_printed_only_for_failures_and_warnings(status):
    check = doctor.Check(status, "Python", "install Python 3.10+")
    expected = "PASS Python" if status == "PASS" else status + " Python: install Python 3.10+"
    assert str(check) == expected


def test_normalization_and_exact_rejection():
    assert doctor.normalize_backend("openai_compat") == "openai-compat"
    with pytest.raises(ValueError) as caught:
        doctor.normalize_backend("fake-backend")
    assert str(caught.value) == ("post-release: backend 'fake-backend' is not supported in this release; "
                                 "supported: claude, codex, openclaw, hermes, openai-compat")


@pytest.mark.parametrize("version, expected", [("3.9", False), ("3.10", True), ("3.12", True), ("garbage", False)])
def test_python_floor(harness, version, expected):
    options, _, _, _ = harness
    old_runner = options["runner"]
    options["runner"] = lambda command, **kw: subprocess.CompletedProcess(command, 0, version, "") if "-c" in command else old_runner(command, **kw)
    instance = doctor.Doctor(backend="codex", **options)
    assert instance.run() is expected
    check = next(c for c in instance.checks if c.name == "Python")
    assert check.status == ("PASS" if expected else "FAIL")


@pytest.mark.parametrize("name", ["ffmpeg", "ffprobe", "tailscale", "codex"])
def test_missing_required_binaries_fail(harness, name):
    options, _, _, _ = harness
    options["command_path"] = lambda value: None if value == name else "/fake-bin/" + value
    assert not doctor.Doctor(backend="codex", **options).run()


@pytest.mark.parametrize("name", ["ffmpeg", "ffprobe"])
@pytest.mark.parametrize("state", ["missing", "non-executable", "executable"])
def test_media_binary_absolute_override_is_authoritative(harness, tmp_path, name, state):
    options, _, _, _ = harness
    options.pop("command_path")  # Exercise real filesystem lookup.
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    fallback = bin_dir / name
    fallback.write_text("#!/bin/sh\nexit 0\n")
    fallback.chmod(0o755)
    override = tmp_path / name
    if state != "missing":
        override.write_text("#!/bin/sh\nexit 0\n")
        override.chmod(0o755 if state == "executable" else 0o644)
    options["env"].update(PATH=str(bin_dir), **{name.upper() + "_BIN": str(override)})
    instance = doctor.Doctor(backend="codex", **options)
    instance._common()
    assert next(c.status for c in instance.checks if c.name == name) == (
        "PASS" if state == "executable" else "FAIL"
    )


@pytest.mark.parametrize("name", ["ffmpeg", "ffprobe"])
@pytest.mark.parametrize("on_path", [False, True])
def test_media_binary_relative_override_resolves_only_on_path(
    harness, tmp_path, monkeypatch, name, on_path
):
    options, _, _, _ = harness
    options.pop("command_path")
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    command = "fake-custom-" + name
    local = tmp_path / command
    local.write_text("#!/bin/sh\nexit 0\n")
    local.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    if on_path:
        (bin_dir / command).symlink_to(local)
    options["env"].update(PATH=str(bin_dir), **{name.upper() + "_BIN": command})
    instance = doctor.Doctor(backend="codex", **options)
    instance._common()
    assert next(c.status for c in instance.checks if c.name == name) == (
        "PASS" if on_path else "FAIL"
    )


@pytest.mark.parametrize("failure", ["login", "ipv4", "codex-login", "timeout"])
def test_failed_passive_commands_fail(harness, failure):
    options, _, _, _ = harness
    old_runner = options["runner"]

    def runner(command, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 10)
        if ((failure == "login" and command[1:] == ["status"]) or
                (failure == "ipv4" and command[1:] == ["ip", "-4"]) or
                (failure == "codex-login" and command[1:] == ["login", "status"])):
            return subprocess.CompletedProcess(command, 1, "", "fake error")
        return old_runner(command, **kwargs)

    options["runner"] = runner
    assert not doctor.Doctor(backend="codex", **options).run()


@pytest.mark.parametrize("url,addresses,valid", [
    ("http://localhost:8788", ["127.0.0.1", "::1"], True),
    ("https://example.invalid", ["100.64.0.1"], True),
    ("https://example.invalid", ["fd7a:115c:a1e0::1"], True),
    ("https://example.invalid", ["127.0.0.1", "192.0.2.1"], False),
    ("http://192.168.1.1", ["192.168.1.1"], False),
    ("https://example.invalid", [], False),
    ("ftp://localhost", ["127.0.0.1"], False),
    ("http://fake-user:fake-password@localhost", ["127.0.0.1"], False),
    ("http://localhost:bad", ["127.0.0.1"], False),
    ("http://[invalid", ["127.0.0.1"], False),
])
def test_public_url_requires_only_loopback_or_tailnet(harness, url, addresses, valid):
    options, _, _, _ = harness
    options["resolver"] = lambda host: addresses
    assert doctor.Doctor(backend="codex", public_url=url, **options).run() is valid


def test_dns_failure_has_hint_no_traceback(harness):
    options, _, _, lines = harness

    def resolver(host):
        raise socket.gaierror("fake resolution failure")

    options["resolver"] = resolver
    assert not doctor.Doctor(backend="codex", **options).run()
    assert any("FAIL public URL:" in line and "set --public-url" in line for line in lines)


@pytest.mark.parametrize("overrides", [dict(system="Windows"), dict(port_available=lambda port: False), dict(writable=lambda path: False)])
def test_os_port_and_write_failures(harness, overrides):
    options, _, _, _ = harness
    options.update(overrides)
    assert not doctor.Doctor(backend="codex", **options).run()


def test_state_directory_obeys_xdg(harness, tmp_path):
    options, _, _, _ = harness
    paths = []
    options["env"]["XDG_STATE_HOME"] = str(tmp_path / "fake-state")
    options["writable"] = lambda path: paths.append(path) or True
    assert doctor.Doctor(backend="codex", member="fake-member", **options).run()
    assert tmp_path / "fake-state" / "caty-gateway" in paths
    assert tmp_path / ".local" / "share" / "caty-gateway" / "fake-member" in paths


def test_claude_cwd_missing_fails_and_credentials_only_warn(harness, tmp_path):
    options, _, _, _ = harness
    instance = doctor.Doctor(backend="claude", **options)
    assert instance.run()
    assert next(c.status for c in instance.checks if c.name == "claude credentials") == "WARN"
    options["env"]["CATY_CLAUDE_CWD"] = str(tmp_path / "fake-missing")
    assert not doctor.Doctor(backend="claude", **options).run()


def test_openclaw_config_token_and_exact_agent(harness, tmp_path):
    options, _, requests, _ = harness
    options["env"].pop("CATY_GATEWAY_TOKEN")
    config = tmp_path / ".openclaw" / "openclaw.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"gateway": {"auth": {"token": "fake-config"}}}))
    assert doctor.Doctor(backend="openclaw", **options).run()
    assert requests[-1][1] == {"Authorization": "Bearer fake-config"}
    options["env"]["CATY_AGENT"] = "agent"
    assert not doctor.Doctor(backend="openclaw", **options).run()


@pytest.mark.parametrize("backend,key", [("hermes", "CATY_HERMES_API_KEY"), ("openclaw", "CATY_GATEWAY_TOKEN")])
def test_required_tokens_fail_without_disclosing_secrets(harness, backend, key):
    options, _, _, lines = harness
    options["env"][key] = "   "
    assert not doctor.Doctor(backend=backend, **options).run()
    assert "fake-gateway-token" not in "\n".join(lines)


@pytest.mark.parametrize("backend,path", [("hermes", "/v1/models"), ("openai-compat", "/v1/models")])
def test_models_gets_are_exact(harness, backend, path):
    options, _, requests, _ = harness
    assert doctor.Doctor(backend=backend, **options).run()
    assert requests[-1][0].endswith(path)


@pytest.mark.parametrize("result", [(503, {}), (401, {}), (200, {"data": [{"id": "fake-alternative"}]})])
def test_models_response_failures_and_missing_model_warning(harness, result):
    options, _, _, lines = harness
    options["get_json"] = lambda *args: result
    assert doctor.Doctor(backend="openai-compat", **options).run() is (result[0] == 200)
    if result[0] == 200:
        assert any("WARN openai-compat model" in line and "fake-alternative" in line for line in lines)


def test_network_error_is_not_printed_or_raised(harness):
    options, _, _, lines = harness

    def get_json(*args):
        raise OSError("fake-sensitive-token")

    options["get_json"] = get_json
    assert not doctor.Doctor(backend="hermes", **options).run()
    assert "fake-sensitive-token" not in "\n".join(lines)


def test_main_unsupported_and_probe_never_run_checks(monkeypatch, capsys):
    monkeypatch.setattr(doctor.Doctor, "run", lambda self: pytest.fail("unexpected check"))
    assert doctor.main(["--backend", "fake-backend"]) == 2
    assert "post-release: backend 'fake-backend'" in capsys.readouterr().err
    assert doctor.main(["--backend", "codex", "--probe"]) == 2
    assert "passive checks only" in capsys.readouterr().err


def test_member_traversal_never_checks_filesystem(harness):
    options, commands, _, _ = harness
    assert not doctor.Doctor(backend="codex", member="../fake-member", **options).run()
    assert not commands


def test_writable_probe_leaves_no_artifacts(tmp_path):
    assert doctor._writable(tmp_path / "fake-new" / "nested")
    assert not list(tmp_path.iterdir())
    blocked = tmp_path / "fake-file"
    blocked.write_text("fixture")
    assert not doctor._writable(blocked / "nested")


def test_model_output_is_one_line_and_redacted(harness):
    options, _, _, lines = harness
    options["get_json"] = lambda *args: (200, {"data": [{"id": "fake-gateway-token\nFAIL forged"}]})
    assert doctor.Doctor(backend="openai-compat", **options).run()
    assert all("\n" not in line and "fake-gateway-token" not in line for line in lines)


def test_claude_uses_service_binary_override(harness):
    options, commands, _, _ = harness
    options["env"]["CATY_CLAUDE_BIN"] = "/fake-custom/claude-special"
    assert doctor.Doctor(backend="claude", **options).run()
    assert ["/fake-bin/claude-special", "--version"] in commands


def test_invalid_port_env_has_repair_hint(monkeypatch, capsys):
    monkeypatch.setenv("CATY_GATEWAY_PORT", "fake-invalid")
    assert doctor.main(["--backend", "codex"]) == 2
    assert "FAIL port: set CATY_GATEWAY_PORT" in capsys.readouterr().err
