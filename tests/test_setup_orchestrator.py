import builtins
import datetime
import os
import json
import stat
import textwrap
import pathlib
import plistlib
import re
import signal
import subprocess
import sys
import threading
import time

import pytest


from caty_gateway import setup_orchestrator
from caty_gateway import setup_redaction
from caty_gateway import setup_supervisor


@pytest.fixture
def fake_home(tmp_path: pathlib.Path) -> pathlib.Path:
    home = tmp_path / "home"
    (home / ".config/caty-gateway").mkdir(parents=True)
    (home / ".local/state").mkdir(parents=True)
    return home


def _write_exec(path: pathlib.Path, body: str) -> None:
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _prepare_fake_commands(tmp_path: pathlib.Path) -> pathlib.Path:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    _write_exec(
        bin_dir / "tailscale",
        """
        #!/usr/bin/env sh
        if [ "$1" = "status" ]; then
          exit 0
        fi
        if [ "$1" = "ip" ]; then
          echo "100.1.1.1"
          exit 0
        fi
        exit 1
        """,
    )
    _write_exec(bin_dir / "ffmpeg", "#!/usr/bin/env sh\nexit 0\n")
    _write_exec(
        bin_dir / "systemctl",
        "#!/usr/bin/env sh\nexit 0\n",
    )
    _write_exec(
        bin_dir / "journalctl",
        "#!/usr/bin/env sh\necho journal line\n",
    )
    _write_exec(bin_dir / "loginctl", "#!/usr/bin/env sh\nexit 0\n")
    _write_exec(bin_dir / "openclaw", "#!/usr/bin/env sh\nexit 0\n")
    return bin_dir


def _prepare_fake_gateway_files(workdir: pathlib.Path) -> None:
    _write_exec(
        workdir / "install-member-service-systemd.sh",
        """
        #!/usr/bin/env sh
        set -eu
        while [ "$1" = "--yes" ] || [ "$1" = "--dry-run" ]; do
          shift
        done
        member="$1"
        env_path="$HOME/.config/caty-gateway/$member.env"
        mkdir -p "$(dirname "$env_path")"
        printf 'CATY_GATEWAY_PORT=%s\n' "$PORT" > "$env_path"
        printf 'CATY_ID=%s\n' "$MEMBER_ID" >> "$env_path"
        printf 'CATY_TOKEN=%s\n' "$TOKEN" >> "$env_path"
        """,
    )
    _write_exec(
        workdir / "install-member-service.sh",
        "#!/usr/bin/env sh\necho launcher install\n",
    )
    _write_exec(
        workdir / "caty_gateway.py",
        """
        #!/usr/bin/env python3
        import os
        import sys
        if len(sys.argv) > 1 and sys.argv[1] == "qr":
            print(os.environ.get("CATY_TOKEN", ""))
            print("deadbeef.0123456789abcdef0123456789abcdef")
            raise SystemExit(0)
        print("ok")
        """,
    )

    # qrcode import shim so orchestrator won't try to provision the .venv path in tests.
    package_root = workdir / "fake_pyqrcode" / "qrcode"
    package_root.mkdir(parents=True, exist_ok=True)
    (package_root / "__init__.py").write_text("VERSION = 'stub'\n", encoding="utf-8")

    template = workdir / "systemd" / "caty-gateway.service.template"
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_text(
        "[Service]\n"
        "WorkingDirectory=__WORKDIR__\n"
        "Environment=HOME=%h\n"
        "EnvironmentFile=%h/.config/caty-gateway/%i.env\n"
        "ExecStart=__PYTHON__ __WORKDIR__/caty_gateway.py\n",
        encoding="utf-8",
    )


def _make_orch(
    fake_home_dir: pathlib.Path,
    workdir: pathlib.Path,
    monkeypatch,
    *argv: str,
    extra_env=None,
    disable_collision_scan: bool = True,
):
    argv_list = list(argv)
    bin_dir = _prepare_fake_commands(workdir)
    env = {
        "HOME": str(fake_home_dir),
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "CATY_BACKEND": "openclaw",
        "PYTHONPATH": str(workdir / "fake_pyqrcode"),
        "XDG_RUNTIME_DIR": str(fake_home_dir / "xdg-runtime"),
    }
    if extra_env:
        env.update(extra_env)
    _prepare_fake_gateway_files(workdir)
    monkeypatch.setattr(setup_orchestrator.platform, "system", lambda: "Linux")
    process_start_time = setup_orchestrator.process_start_time
    monkeypatch.setattr(
        setup_orchestrator,
        "process_start_time",
        lambda pid, system=None: (
            "test-current-process" if pid == os.getpid() else process_start_time(pid, system)
        ),
    )
    orch = setup_orchestrator.SetupOrchestrator(["--member", "alice", *argv_list], env=env, workdir=workdir)
    # Existing orchestrator tests predate the backend capability phase and are
    # concerned with later phases.  Backend-specific tests override this probe.
    orch._probe_backend = lambda: True
    orch._voice = lambda: None
    if disable_collision_scan:
        orch._collision_with_other_members = lambda: []
    return orch


def _clear_state(orch: setup_orchestrator.SetupOrchestrator) -> None:
    if orch.state_path.exists():
        orch.state_path.unlink()


def test_plan_only_is_side_effect_free(fake_home, tmp_path, monkeypatch):
    marker = fake_home / "operator-marker"
    marker.write_bytes(b"keep-byte-identical\n")

    def snapshot(root):
        return {
            str(path.relative_to(root)): (
                path.stat().st_mode,
                path.stat().st_mtime_ns,
                path.read_bytes() if path.is_file() else None,
            )
            for path in [root, *sorted(root.rglob("*"))]
        }

    orch = _make_orch(fake_home, tmp_path, monkeypatch, "--plan-only", "--yes", "--public-url", "http://100.1.1.1:8788")
    orch._voice = lambda: (_ for _ in ()).throw(
        AssertionError("plan-only must not probe voice readiness")
    )
    before = snapshot(fake_home)
    worktree_before = snapshot(tmp_path)
    code = orch.run()
    assert code == 0
    assert not orch.state_path.exists()
    assert not (fake_home / ".config/caty-gateway/alice.env").exists()
    assert snapshot(fake_home) == before
    assert snapshot(tmp_path) == worktree_before


def test_preflight_aggregates_failures(fake_home, tmp_path, monkeypatch, capsys):
    orch = _make_orch(fake_home, tmp_path, monkeypatch, "--plan-only", "--yes", "--health-timeout", "30")
    # remove one required command and public url
    bin_dir = pathlib.Path(orch.env["PATH"].split(":", 1)[0])
    (bin_dir / "ffmpeg").unlink()
    _write_exec(
        bin_dir / "tailscale",
        """
        #!/usr/bin/env sh
        if [ "$1" = "ip" ]; then
          exit 1
        fi
        if [ "$1" = "status" ]; then
          exit 0
        fi
        exit 0
        """,
    )
    orch.args.public_url = ""
    orch.public_url = ""
    with pytest.raises(setup_orchestrator.SetupError):
        orch._preflight()
    output = capsys.readouterr().err
    assert "Preflight failed" in output
    assert "tailscale missing" not in output
    assert "ffmpeg missing" in output
    assert "Cannot infer --public-url" in output


def test_preflight_warns_but_does_not_fail_when_ffprobe_is_missing(
    fake_home, tmp_path, monkeypatch, capsys
):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--plan-only",
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
    )
    orch._preflight()
    output = capsys.readouterr().out
    assert "WARN: ffprobe is missing" in output


def test_public_url_with_userinfo_is_rejected(fake_home, tmp_path, monkeypatch, capsys):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--plan-only",
        "--yes",
        "--public-url",
        "http://user:password@100.1.1.1:8788",
    )
    with pytest.raises(setup_orchestrator.SetupError, match="preflight failed"):
        orch._preflight()
    assert "public URL must not contain userinfo" in capsys.readouterr().err


def test_ttl_env_validation_and_clamp(fake_home, tmp_path, monkeypatch):
    with pytest.raises(setup_orchestrator.SetupError):
        _make_orch(
            fake_home,
            tmp_path,
            monkeypatch,
            "--plan-only",
            "--yes",
            extra_env={"CATY_SETUP_RESUME_TTL_SECONDS": "nonsense"},
        )

    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--plan-only",
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
        extra_env={"CATY_SETUP_RESUME_TTL_SECONDS": "99999999"},
    )
    assert orch.resume_ttl == 24 * 60 * 60

    with pytest.raises(setup_orchestrator.SetupError):
        _make_orch(fake_home, tmp_path, monkeypatch, extra_env={"CATY_SETUP_RESUME_TTL_SECONDS": "0"})


@pytest.mark.parametrize("value", ["nonsense", "0", "-1", "nan", "inf"])
def test_qr_timeout_is_validated_when_configuration_is_read(
    fake_home, tmp_path, monkeypatch, value
):
    with pytest.raises(setup_orchestrator.SetupError, match="CATY_SETUP_QR_TIMEOUT_SECONDS"):
        _make_orch(
            fake_home,
            tmp_path,
            monkeypatch,
            "--plan-only",
            "--yes",
            extra_env={"CATY_SETUP_QR_TIMEOUT_SECONDS": value},
        )


def test_backend_inferred_from_env_is_validated(fake_home, tmp_path, monkeypatch):
    with pytest.raises(setup_orchestrator.SetupError, match="CATY_BACKEND"):
        _make_orch(
            fake_home,
            tmp_path,
            monkeypatch,
            "--plan-only",
            "--yes",
            extra_env={"CATY_BACKEND": "unknown-backend"},
        )


def test_collision_member_port_and_resume_ownership(fake_home, tmp_path, monkeypatch):
    # foreign artifact collision
    collision_port = 8788
    foreign = fake_home / ".config/caty-gateway/alice.env"
    foreign.write_text(f"CATY_GATEWAY_PORT={collision_port}\nCATY_TOKEN=foreign\n", encoding="utf-8")
    before_bytes = foreign.read_bytes()
    before = foreign.stat().st_mtime_ns

    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--port",
        str(collision_port),
        "--public-url",
        f"http://100.1.1.1:{collision_port}",
        disable_collision_scan=False,
    )
    with pytest.raises(setup_orchestrator.SetupError):
        orch.run()
    assert foreign.stat().st_mtime_ns == before
    assert foreign.read_bytes() == before_bytes

    # other member port collision
    (fake_home / ".config/caty-gateway/bob.env").write_text(f"CATY_GATEWAY_PORT={collision_port}\n", encoding="utf-8")
    _clear_state(orch)
    foreign.unlink()
    with pytest.raises(setup_orchestrator.SetupError):
        _make_orch(
            fake_home,
            tmp_path,
            monkeypatch,
            "--plan-only",
            "--yes",
            "--port",
            str(collision_port),
            "--public-url",
            f"http://100.1.1.1:{collision_port}",
            disable_collision_scan=False,
        ).run()


def test_resume_from_failed_phase_then_success(fake_home, tmp_path, monkeypatch):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--port",
        "8788",
        "--public-url",
        "http://100.1.1.1:8788",
    )

    orch._health = lambda: (_ for _ in ()).throw(
        setup_orchestrator.SetupError("temporary service failure")
    )
    orch._identity = lambda: None
    orch._qr = lambda: None

    with pytest.raises(setup_orchestrator.SetupError):
        orch.run()

    assert orch.state is not None
    assert "install" in orch.state.completed_phases
    assert "health" not in orch.state.completed_phases
    artifact = fake_home / ".config/caty-gateway/alice.env"
    artifact_before = (artifact.read_bytes(), artifact.stat().st_mtime_ns)

    orch2 = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
    )
    orch2._health = lambda: None
    orch2._identity = lambda: None
    orch2._qr = lambda: None
    code = orch2.run()

    assert code == 0
    assert not orch2.state_path.exists()
    assert (artifact.read_bytes(), artifact.stat().st_mtime_ns) == artifact_before


def test_install_persists_ownership_before_phase_checkpoint(fake_home, tmp_path, monkeypatch):
    orch = _make_orch(fake_home, tmp_path, monkeypatch, "--yes", "--public-url", "http://100.1.1.1:8788")
    orch._preflight()
    orch._start_state()
    orch._install()  # simulate a kill immediately after this returns, before _mark("install")
    payload = json.loads(orch.state_path.read_text(encoding="utf-8"))
    assert payload["env_file_created_by_us"] is True
    assert len(payload["env_file_sha256"]) == 64
    assert payload["token_digest"] == ""
    resumed = _make_orch(fake_home, tmp_path, monkeypatch, "--yes", "--public-url", "http://100.1.1.1:8788")
    resumed.state = resumed._read_state(validate_fingerprint=False)
    assert resumed._owned_artifact()


def test_state_parent_directories_are_private(fake_home, tmp_path, monkeypatch):
    orch = _make_orch(fake_home, tmp_path, monkeypatch, "--yes", "--public-url", "http://100.1.1.1:8788")
    orch.config = orch._resolved_config()
    orch._start_state()
    assert stat.S_IMODE(orch.state_path.parent.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(orch.state_path.parent.stat().st_mode) == 0o700


def test_kill_between_installer_write_and_state_write_is_adopted(fake_home, tmp_path, monkeypatch):
    """Kill window: the installer wrote the artifact but the orchestrator died
    before recording ownership. The persisted token digest proves the artifact
    is ours, so a rerun resumes instead of reporting a false collision."""
    orch = _make_orch(fake_home, tmp_path, monkeypatch, "--yes", "--public-url", "http://100.1.1.1:8788")
    orch._preflight()
    orch._start_state()
    token = "aa" * 24
    orch.state.token_digest = orch._token_digest(token)
    orch._write_state()
    artifact = fake_home / ".config/caty-gateway/alice.env"
    artifact.write_text(
        "CATY_GATEWAY_PORT=8788\nCATY_ID=alice\nCATY_TOKEN=%s\n" % token, encoding="utf-8"
    )

    resumed = _make_orch(fake_home, tmp_path, monkeypatch, "--yes", "--public-url", "http://100.1.1.1:8788")
    resumed._health = lambda: None
    resumed._identity = lambda: None
    resumed._qr = lambda: None
    before = (artifact.read_bytes(), artifact.stat().st_mtime_ns)
    assert resumed.run() == 0
    assert (artifact.read_bytes(), artifact.stat().st_mtime_ns) == before

    # A foreign artifact whose token does not match the digest still aborts.
    _clear_state(orch)
    orch2 = _make_orch(fake_home, tmp_path, monkeypatch, "--yes", "--public-url", "http://100.1.1.1:8788")
    orch2._start_state()
    orch2.state.token_digest = orch2._token_digest("bb" * 24)
    orch2._write_state()
    foreign_before = (artifact.read_bytes(), artifact.stat().st_mtime_ns)
    orch3 = _make_orch(fake_home, tmp_path, monkeypatch, "--yes", "--public-url", "http://100.1.1.1:8788")
    with pytest.raises(setup_orchestrator.SetupError):
        orch3.run()
    assert (artifact.read_bytes(), artifact.stat().st_mtime_ns) == foreign_before


def test_adoption_window_listener_is_ours_but_foreign_artifact_still_collides(
    fake_home, tmp_path, monkeypatch
):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
        disable_collision_scan=False,
    )
    # Pin the port probe: the host running the tests may itself serve 8788.
    monkeypatch.setattr(orch, "_port_is_listening", lambda: False)
    orch._preflight()
    orch._start_state()
    token = "ab" * 24
    orch.state.token_digest = orch._token_digest(token)
    orch._write_state()
    orch.artifact_path.write_text(
        "CATY_GATEWAY_PORT=8788\nCATY_ID=alice\nCATY_TOKEN=%s\n" % token,
        encoding="utf-8",
    )

    resumed = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
        disable_collision_scan=False,
    )
    resumed.state = resumed._read_state(validate_fingerprint=False)
    monkeypatch.setattr(resumed, "_port_is_listening", lambda: True)
    resumed._preflight()

    resumed.artifact_path.write_text(
        "CATY_GATEWAY_PORT=8788\nCATY_ID=alice\nCATY_TOKEN=foreign\n",
        encoding="utf-8",
    )
    foreign = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
        disable_collision_scan=False,
    )
    foreign.state = foreign._read_state(validate_fingerprint=False)
    monkeypatch.setattr(foreign, "_port_is_listening", lambda: True)
    with pytest.raises(setup_orchestrator.SetupError, match="preflight failed"):
        foreign._preflight()


def test_plan_only_does_not_commit_adoption_window_state(fake_home, tmp_path, monkeypatch):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
    )
    orch._preflight()
    orch._start_state()
    token = "cd" * 24
    orch.state.token_digest = orch._token_digest(token)
    orch._write_state()
    orch.artifact_path.write_text(
        "CATY_GATEWAY_PORT=8788\nCATY_ID=alice\nCATY_TOKEN=%s\n" % token,
        encoding="utf-8",
    )
    state_before = orch.state_path.read_bytes()

    plan = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--plan-only",
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
    )
    assert plan.run() == 0
    assert plan.state_path.read_bytes() == state_before


def test_plan_only_hermes_never_prompts_and_resume_reuses_installed_key(
    fake_home, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        setup_orchestrator.getpass,
        "getpass",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("plan-only prompted")),
    )
    absent = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--backend",
        "hermes",
        "--plan-only",
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
    )
    with pytest.raises(setup_orchestrator.SetupError, match="preflight failed"):
        absent._preflight()

    absent.config = absent._resolved_config()
    absent._start_state()
    token = "12" * 24
    absent.state.token_digest = absent._token_digest(token)
    absent._write_state()
    absent.artifact_path.write_text(
        "CATY_GATEWAY_PORT=8788\nCATY_ID=alice\nCATY_TOKEN=%s\nCATY_HERMES_API_KEY=installed-key\n"
        % token,
        encoding="utf-8",
    )
    resumed = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--backend",
        "hermes",
        "--plan-only",
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
    )
    assert resumed.run() == 0
    assert resumed.env["CATY_HERMES_API_KEY"] == "installed-key"


def test_resume_allows_different_health_timeout_and_restarts_service(
    fake_home, tmp_path, monkeypatch
):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--health-timeout",
        "30",
        "--public-url",
        "http://100.1.1.1:8788",
    )
    orch._health = lambda: (_ for _ in ()).throw(setup_orchestrator.SetupError("temporary health failure"))
    with pytest.raises(setup_orchestrator.SetupError, match="temporary health failure"):
        orch.run()
    assert orch.state is not None
    assert "start" in orch.state.completed_phases
    assert "health" not in orch.state.completed_phases

    resumed = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--health-timeout",
        "7",
        "--public-url",
        "http://100.1.1.1:8788",
    )
    starts = []
    resumed._start = lambda: starts.append("start")
    resumed._health = lambda: None
    resumed._identity = lambda: None
    resumed._qr = lambda: None
    assert resumed.run() == 0
    assert starts == ["start"]


def test_digest_window_unit_requires_exact_installer_render(fake_home, tmp_path, monkeypatch):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
    )
    orch._preflight()
    orch._start_state()
    orch.state.token_digest = orch._token_digest("ef" * 24)
    orch._write_state()

    unit = fake_home / ".config" / "systemd" / "user" / orch.service_name
    unit.parent.mkdir(parents=True, exist_ok=True)
    template = (tmp_path / "systemd" / "caty-gateway.service.template").read_text(encoding="utf-8")
    expected = template.replace("__WORKDIR__", str(tmp_path.resolve()))
    expected = expected.replace("__PYTHON__", orch.service_python)
    expected = expected.replace("%h", str(fake_home))
    expected = expected.replace("%i", "alice")
    unit.write_bytes((expected.rstrip("\n") + "\n").encode("utf-8"))
    assert orch._member_collision() is None

    unit.write_bytes(b"foreign unit\n")
    collision = orch._member_collision()
    assert collision is not None
    assert "member unit already exists" in collision


def test_installer_env_forces_member_language_not_shell_locale(fake_home, tmp_path, monkeypatch):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
        extra_env={"LANG": "en_US.UTF-8"},
    )
    # Linux: the systemd installer reads CATY_LANG directly; the shell locale stays valid.
    assert orch._installer_env("token")["LANG"] == "en_US.UTF-8"
    # macOS: the launchd installer renders __LANG__ from $LANG, so it must be pinned.
    orch.system = "Darwin"
    assert orch._installer_env("token")["LANG"] == "ja"
    orch.env["CATY_LANG"] = "th"
    assert orch._installer_env("token")["LANG"] == "th"


def test_post_success_rerun_explains_how_to_show_qr(fake_home, tmp_path, monkeypatch):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
    )
    orch.artifact_path.write_text("CATY_TOKEN=live-token\n", encoding="utf-8")
    message = orch._member_collision()
    assert message is not None
    assert "member appears already set up at %s" % orch.artifact_path in message
    assert "python3 caty_gateway.py qr" in message
    assert "move the file aside / choose another --member" in message


def test_qr_uses_unbuffered_stdio(fake_home, tmp_path, monkeypatch):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
    )
    orch._install = lambda: None
    orch._start = lambda: None
    orch._linger = lambda: None
    orch._health = lambda: None
    orch._identity = lambda: None
    orch._identity_token = lambda: "token"

    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    original_run = subprocess.run

    def fake_run(cmd, **kwargs):
        calls.append((tuple(cmd), kwargs))

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    orch._qr()
    assert calls, "orchestrator must call subprocess for qr"
    assert calls[-1][0][-2:] == ("--qr-delivery", "auto")
    call_kwargs = calls[-1][1]
    assert call_kwargs.get("stdout") is None
    assert call_kwargs.get("stderr") is None

    monkeypatch.setattr(subprocess, "run", original_run)


def test_qr_delivery_cli_wins_over_env_and_is_fingerprinted(fake_home, tmp_path, monkeypatch, capsys):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--plan-only",
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
        "--qr-delivery",
        "tty",
        extra_env={"CATY_QR_DELIVERY": "url"},
    )
    orch._preflight()
    assert orch.qr_delivery == "tty"
    assert orch._resolved_config()["qr_delivery"] == "tty"
    orch._print_plan()
    output = capsys.readouterr().out
    assert "qr_delivery: tty" in output
    assert "qr --qr-delivery tty with inherited terminal streams" in output

    env_only = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--plan-only",
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
        extra_env={"CATY_QR_DELIVERY": "url"},
    )
    assert env_only.qr_delivery == "url"
    assert env_only._config_fingerprint() != orch._config_fingerprint()


def test_qr_child_canaries_are_inherited_not_managed(fake_home, tmp_path, monkeypatch, capfd):
    fake_token = ("0123456789abcdef") * 3  # gitleaks:allow — deliberate test canary, not a credential (parens keep the family secret-guard quiet)
    fake_pair = "deadbeef.0123456789abcdef0123456789abcdef"  # gitleaks:allow — deliberate test canary
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
    )
    orch._preflight()
    orch._start_state()
    artifact = fake_home / ".config/caty-gateway/alice.env"
    artifact.write_text(
        "CATY_GATEWAY_PORT=8788\nCATY_ID=alice\nCATY_TOKEN=%s\n" % fake_token,
        encoding="utf-8",
    )

    managed_prints: list[str] = []
    real_print = builtins.print

    def observe_managed_print(*args, **kwargs):
        managed_prints.append(" ".join(str(value) for value in args))
        return real_print(*args, **kwargs)

    monkeypatch.setattr(builtins, "print", observe_managed_print)
    orch._qr()

    inherited = capfd.readouterr().out
    managed = "\n".join(managed_prints) + orch.state_path.read_text(encoding="utf-8")
    assert fake_token in inherited
    assert fake_pair in inherited
    assert fake_token not in managed
    assert fake_pair not in managed


def test_qr_nonzero_is_actionable(fake_home, tmp_path, monkeypatch):
    orch = _make_orch(fake_home, tmp_path, monkeypatch, "--yes", "--public-url", "http://100.1.1.1:8788")
    artifact = fake_home / ".config/caty-gateway/alice.env"
    artifact.write_text("CATY_TOKEN=secret\nCATY_ID=alice\n", encoding="utf-8")

    def fail_qr(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 9)

    monkeypatch.setattr(subprocess, "run", fail_qr)
    with pytest.raises(setup_orchestrator.SetupError, match="non-zero"):
        orch._qr()


def test_qr_failure_does_not_claim_setup_success(fake_home, tmp_path, monkeypatch, capsys):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
        "--qr-delivery",
        "url",
    )
    orch._preflight = lambda: setattr(orch, "config", orch._resolved_config())
    orch._install = lambda: None
    orch._start = lambda: None
    orch._linger = lambda: None
    orch._health = lambda: None
    orch._identity = lambda: None
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 7),
    )
    with pytest.raises(setup_orchestrator.SetupError, match="non-zero"):
        orch.run()
    assert "Setup complete" not in capsys.readouterr().out


def test_secrets_are_not_emitted(fake_home, tmp_path, monkeypatch, capsys):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--plan-only",
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
        extra_env={"CATY_TOKEN": "deadbeef-abc"},
    )
    orch.run()
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert "deadbeef" not in out
    assert "[REDACTED]" in out

    # verify state never stores token value or bearer/pair payload
    fake_art = fake_home / ".config/caty-gateway" / "alice.env"
    assert not fake_art.exists()


def test_resume_state_and_managed_output_exclude_secret_canaries(fake_home, tmp_path, monkeypatch, capsys):
    fake_token = ("0123456789abcdef") * 3  # gitleaks:allow — deliberate test canary, not a credential (parens keep the family secret-guard quiet)
    fake_pair = "deadbeef.0123456789abcdef0123456789abcdef"  # gitleaks:allow — deliberate test canary
    monkeypatch.setattr(setup_orchestrator.secrets, "token_hex", lambda size: fake_token)
    orch = _make_orch(fake_home, tmp_path, monkeypatch, "--yes", "--public-url", "http://100.1.1.1:8788")
    orch._start = lambda: (_ for _ in ()).throw(setup_orchestrator.SetupError("stop after install"))
    with pytest.raises(setup_orchestrator.SetupError):
        orch.run()
    captured = capsys.readouterr()
    managed = captured.out + captured.err + orch.state_path.read_text(encoding="utf-8")
    assert fake_token not in managed
    assert fake_pair not in managed


def test_local_redaction_covers_pair_bearer_and_secret_env():
    bare_token = "a" * 48
    sha256 = "b" * 64
    raw = (
        "deadbeef.0123456789abcdef0123456789abcdef\n"
        "Authorization: Bearer abc.def\n"
        "CATY_TOKEN=topsecret\nCATY_HERMES_API_KEY='also-secret'\nPASSWORD=hunter2\n"
        '"token": "json-secret"\n'
        "'api_key': 'single-json-secret'\n"
        "<key>Caty_Hermes_API_Key</key>\n<string>plist-secret</string>\n"
        "orchestrator credential %s\n" % bare_token
        + "pairing_invalid=visible\n"
        + "sha256=%s\n" % sha256
    )
    cleaned = setup_orchestrator.redact(raw)
    assert "0123456789abcdef" not in cleaned
    assert "abc.def" not in cleaned
    assert "topsecret" not in cleaned
    assert "also-secret" not in cleaned
    assert "hunter2" not in cleaned
    assert "json-secret" not in cleaned
    assert "single-json-secret" not in cleaned
    assert "plist-secret" not in cleaned
    assert bare_token not in cleaned
    assert "<key>Caty_Hermes_API_Key</key>\n<string>[REDACTED]</string>" in cleaned
    assert "pairing_invalid=visible" in cleaned
    assert sha256 in cleaned


def test_setup_paths_share_one_redaction_implementation():
    assert setup_orchestrator.redact is setup_redaction.redact
    assert setup_supervisor.redact is setup_redaction.redact


def test_identity_rejects_non_object_json(fake_home, tmp_path, monkeypatch):
    orch = _make_orch(fake_home, tmp_path, monkeypatch, "--yes", "--public-url", "http://100.1.1.1:8788")
    orch.artifact_path.write_text("CATY_TOKEN=token\n", encoding="utf-8")

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        @staticmethod
        def read():
            return b"[]"

    monkeypatch.setattr(setup_orchestrator.urllib.request, "urlopen", lambda *args, **kwargs: Response())
    with pytest.raises(setup_orchestrator.SetupError, match="non-object JSON body"):
        orch._identity()


def test_voice_phase_accepts_fresh_available_neutral_and_ignores_filler_status(
    fake_home, tmp_path, monkeypatch
):
    orch = _make_orch(fake_home, tmp_path, monkeypatch, "--yes", "--public-url", "http://100.1.1.1:8788")
    orch.artifact_path.write_text("CATY_TOKEN=token\n", encoding="utf-8")
    now = time.time()

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        @staticmethod
        def read():
            return json.dumps(
                {
                    "engine": "fish",
                    "neutral": {
                        "availability": "available",
                        "checked_at": datetime.datetime.fromtimestamp(
                            now, tz=datetime.timezone.utc
                        ).isoformat().replace("+00:00", "Z"),
                    },
                    "filler": {"effective_status": "unavailable"},
                }
            ).encode("utf-8")

    monkeypatch.setattr(setup_orchestrator.time, "time", lambda: now)
    monkeypatch.setattr(
        setup_orchestrator.urllib.request, "urlopen", lambda *args, **kwargs: Response()
    )
    orch._voice = setup_orchestrator.SetupOrchestrator._voice.__get__(orch)

    orch._voice()


def test_voice_phase_skips_non_fish_engines(fake_home, tmp_path, monkeypatch):
    orch = _make_orch(fake_home, tmp_path, monkeypatch, "--yes", "--public-url", "http://100.1.1.1:8788")
    orch.artifact_path.write_text("CATY_TOKEN=token\n", encoding="utf-8")

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        @staticmethod
        def read():
            return b'{"engine":"openclaw"}'

    monkeypatch.setattr(
        setup_orchestrator.urllib.request, "urlopen", lambda *args, **kwargs: Response()
    )
    orch._voice = setup_orchestrator.SetupOrchestrator._voice.__get__(orch)

    orch._voice()


def test_voice_phase_blocks_stale_available_neutral(
    fake_home, tmp_path, monkeypatch, capsys
):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
        "--health-timeout",
        "1",
    )
    orch.artifact_path.write_text("CATY_TOKEN=redactme-tok\n", encoding="utf-8")
    orch._diagnostics = lambda: "log tail"
    now = time.time()

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        @staticmethod
        def read():
            return json.dumps(
                {
                    "engine": "fish",
                    "neutral": {
                        "availability": "available",
                        "checked_at": datetime.datetime.fromtimestamp(
                            now, tz=datetime.timezone.utc
                        ).isoformat().replace("+00:00", "Z"),
                        "stale": True,
                    },
                }
            ).encode("utf-8")

    monotonic_values = iter([0.0, 1.0, 51.0])
    monkeypatch.setattr(setup_orchestrator.time, "time", lambda: now)
    monkeypatch.setattr(
        setup_orchestrator.time,
        "monotonic",
        lambda: next(monotonic_values),
    )
    monkeypatch.setattr(setup_orchestrator.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        setup_orchestrator.urllib.request,
        "urlopen",
        lambda *args, **kwargs: Response(),
    )
    orch._voice = setup_orchestrator.SetupOrchestrator._voice.__get__(orch)

    with pytest.raises(setup_orchestrator.SetupError, match="neutral voice readiness"):
        orch._voice()

    output = capsys.readouterr().out
    assert "Voice diagnostics (redacted):" in output
    assert "redactme-tok" not in output


def test_voice_phase_blocks_unavailable_or_future_skewed_neutral(fake_home, tmp_path, monkeypatch, capsys):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
        "--health-timeout",
        "1",
    )
    orch.artifact_path.write_text("CATY_TOKEN=redactme-tok\n", encoding="utf-8")
    orch._diagnostics = lambda: "log tail"
    now = time.time()
    payloads = iter(
        [
            {
                "engine": "fish",
                "neutral": {"availability": "unavailable", "checked_at": None},
            },
            {
                "engine": "fish",
                "neutral": {
                    "availability": "available",
                    "checked_at": datetime.datetime.fromtimestamp(
                        now + setup_orchestrator.VOICE_STATE_MAX_FUTURE_SKEW_SECONDS + 120,
                        tz=datetime.timezone.utc,
                    ).isoformat().replace("+00:00", "Z"),
                },
            },
        ]
    )

    class Response:
        status = 200

        def __init__(self, payload):
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(self._payload).encode("utf-8")

    last_payload = {
        "engine": "fish",
        "neutral": {"availability": "unavailable", "checked_at": None},
    }
    monotonic_values = iter([0.0, 1.0, 2.0, 51.0])

    def urlopen(request, timeout):
        try:
            payload = next(payloads)
        except StopIteration:
            payload = last_payload
        return Response(payload)

    monkeypatch.setattr(setup_orchestrator.time, "time", lambda: now)
    monkeypatch.setattr(setup_orchestrator.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(setup_orchestrator.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(setup_orchestrator.urllib.request, "urlopen", urlopen)
    orch._voice = setup_orchestrator.SetupOrchestrator._voice.__get__(orch)

    with pytest.raises(setup_orchestrator.SetupError, match="neutral voice readiness"):
        orch._voice()

    output = capsys.readouterr().out
    assert "Voice diagnostics (redacted):" in output
    assert "redactme-tok" not in output


def test_resume_from_old_phase_list_runs_new_voice_phase_without_reinstall(
    fake_home, tmp_path, monkeypatch
):
    orch = _make_orch(fake_home, tmp_path, monkeypatch, "--yes", "--public-url", "http://100.1.1.1:8788")
    orch.config = orch._resolved_config()
    orch._start_state()
    orch.state.completed_phases = [
        "preflight",
        "plan",
        "backend",
        "install",
        "start",
        "linger",
        "health",
        "identity",
        "qr",
    ]
    orch._write_state()

    resumed = _make_orch(fake_home, tmp_path, monkeypatch, "--yes", "--public-url", "http://100.1.1.1:8788")
    ran = []
    starts = []
    resumed._start = lambda: starts.append("start")
    resumed._voice = lambda: ran.append("voice")
    resumed._qr = lambda: ran.append("qr")
    assert resumed.run() == 0
    assert starts == ["start"]
    assert ran == ["voice"]


def test_resume_restarts_service_when_voice_phase_is_pending_after_start(
    fake_home, tmp_path, monkeypatch
):
    orch = _make_orch(fake_home, tmp_path, monkeypatch, "--yes", "--public-url", "http://100.1.1.1:8788")
    orch.config = orch._resolved_config()
    orch._start_state()
    orch.state.completed_phases = [
        "preflight",
        "plan",
        "backend",
        "install",
        "start",
        "linger",
        "health",
        "identity",
    ]
    orch._write_state()

    resumed = _make_orch(fake_home, tmp_path, monkeypatch, "--yes", "--public-url", "http://100.1.1.1:8788")
    starts = []
    resumed._start = lambda: starts.append("start")
    resumed._voice = lambda: None
    resumed._qr = lambda: None
    assert resumed.run() == 0
    assert starts == ["start"]


def test_debug_traceback_is_redacted(monkeypatch, capsys):
    secret = "9" * 48
    monkeypatch.setenv("CATY_SETUP_DEBUG", "1")
    monkeypatch.setenv("CATY_BACKEND", "openclaw")
    monkeypatch.setattr(
        setup_orchestrator.SetupOrchestrator,
        "run",
        lambda self: (_ for _ in ()).throw(RuntimeError(secret)),
    )
    assert setup_orchestrator.main(["--member", "alice"]) == 1
    error = capsys.readouterr().err
    assert "Traceback" in error
    assert secret not in error
    assert "[REDACTED]" in error


def test_macos_matching_owned_plist_is_resumable(fake_home, tmp_path, monkeypatch):
    _prepare_fake_gateway_files(tmp_path)
    monkeypatch.setattr(setup_orchestrator.platform, "system", lambda: "Darwin")
    env = {"HOME": str(fake_home), "PATH": "/usr/bin:/bin"}
    orch = setup_orchestrator.SetupOrchestrator(
        ["--member", "alice", "--yes", "--public-url", "http://100.1.1.1:8788"],
        env=env,
        workdir=tmp_path,
    )
    orch.artifact_path.parent.mkdir(parents=True, exist_ok=True)
    orch.artifact_path.write_bytes(
        plistlib.dumps({"EnvironmentVariables": {"CATY_GATEWAY_PORT": "8788", "CATY_TOKEN": "secret"}})
    )
    now = time.time()
    orch.state = setup_orchestrator.ResumeState(
        1,
        "alice",
        now,
        now,
        ["install"],
        {},
        "fingerprint",
        True,
        orch._hash_file(orch.artifact_path),
    )
    assert orch._member_collision() is None


def test_linger_polkit_denial_prints_single_sudo_action(fake_home, tmp_path, monkeypatch, capsys):
    orch = _make_orch(fake_home, tmp_path, monkeypatch, "--yes", "--public-url", "http://100.1.1.1:8788")
    monkeypatch.setattr(
        orch,
        "_run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "polkit denied"),
    )
    with pytest.raises(setup_orchestrator.SetupError):
        orch._linger()
    output = capsys.readouterr().out
    assert output.count("sudo loginctl enable-linger") == 1


def test_ttl_expired_treated_as_absent_then_resume(fake_home, tmp_path, monkeypatch):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
        extra_env={"CATY_SETUP_RESUME_TTL_SECONDS": "1"},
    )
    orch._preflight()
    stale = {
        "schema_version": setup_orchestrator.SCHEMA_VERSION,
        "member": "alice",
        "created_at": time.time() - 10,
        "updated_at": time.time() - 10,
        "completed_phases": ["preflight", "plan"],
        "resolved_config": orch.config,
        "config_fingerprint": orch._config_fingerprint(),
        "env_file_created_by_us": False,
        "env_file_sha256": "",
    }
    orch.state_path.parent.mkdir(parents=True, exist_ok=True)
    orch.state_path.write_text(json.dumps(stale), encoding="utf-8")

    # The stale state is logically absent; the read is side-effect-free.
    assert orch._read_state() is None
    orch2 = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
        extra_env={"CATY_SETUP_RESUME_TTL_SECONDS": "1"},
    )
    assert orch2._read_state(validate_fingerprint=False) is None


def test_v1_resume_state_requires_explicit_reset(fake_home, tmp_path, monkeypatch):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
    )
    orch.state_path.parent.mkdir(parents=True, exist_ok=True)
    orch.state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "member": "alice",
                "created_at": time.time(),
                "updated_at": time.time(),
                "completed_phases": [],
                "resolved_config": {},
                "config_fingerprint": "old",
            }
        ),
        encoding="utf-8",
    )
    message = (
        "state is from a different setup version — rerun with `--reset` after confirming "
        "no setup is mid-flight"
    )
    with pytest.raises(setup_orchestrator.SetupError, match=re.escape(message)):
        orch._read_state(validate_fingerprint=False)


def test_ttl_expired_incomplete_setup_gets_expired_message_not_already_set_up(
    fake_home, tmp_path, monkeypatch
):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
        extra_env={"CATY_SETUP_RESUME_TTL_SECONDS": "1"},
    )
    orch._preflight()
    stale = {
        "schema_version": setup_orchestrator.SCHEMA_VERSION,
        "member": "alice",
        "created_at": time.time() - 10,
        "updated_at": time.time() - 10,
        "completed_phases": ["preflight", "plan", "install"],
        "resolved_config": orch.config,
        "config_fingerprint": orch._config_fingerprint(),
        "env_file_created_by_us": True,
        "env_file_sha256": "0" * 64,
    }
    orch.state_path.parent.mkdir(parents=True, exist_ok=True)
    orch.state_path.write_text(json.dumps(stale), encoding="utf-8")
    orch.artifact_path.write_text("CATY_ID=alice\nCATY_TOKEN=whatever\n", encoding="utf-8")

    resumed = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
        extra_env={"CATY_SETUP_RESUME_TTL_SECONDS": "1"},
    )
    resumed.state = resumed._read_state(validate_fingerprint=False)
    assert resumed.state is None and resumed.state_expired
    message = resumed._member_collision()
    assert "expired" in message
    assert "already set up" not in message

    # Absent state (post-success) still gets the already-set-up guidance.
    fresh = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
    )
    _clear_state(fresh)
    fresh.state = fresh._read_state(validate_fingerprint=False)
    assert fresh.state is None and not fresh.state_expired
    assert "already set up" in fresh._member_collision()


def test_sha_mismatch_blocked_by_resume(fake_home, tmp_path, monkeypatch):
    # prepare artifact and compatible-looking state but mismatch hash
    fake_file = fake_home / ".config/caty-gateway/alice.env"
    fake_file.write_text("CATY_GATEWAY_PORT=8788\nCATY_TOKEN=good\n", encoding="utf-8")
    state_dir = fake_home / ".local/state/caty-gateway/setup"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "alice.json"

    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
    )
    payload = {
        "schema_version": setup_orchestrator.SCHEMA_VERSION,
        "member": "alice",
        "created_at": time.time(),
        "updated_at": time.time(),
        "completed_phases": ["install"],
        "config_fingerprint": orch._config_fingerprint(),
        "config": {},
        "env_file_created_by_us": True,
        "env_file_sha256": "0" * 64,
    }
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    mtime = fake_file.stat().st_mtime_ns

    with pytest.raises(setup_orchestrator.SetupError):
        _make_orch(
            fake_home,
            tmp_path,
            monkeypatch,
            "--yes",
            "--public-url",
            "http://100.1.1.1:8788",
        ).run()

    assert fake_file.stat().st_mtime_ns == mtime


@pytest.mark.parametrize("backend", ["openclaw", "hermes", "claude"])
@pytest.mark.parametrize("reachable", [True, False])
def test_backend_probe_matrix(fake_home, tmp_path, monkeypatch, backend, reachable):
    extra_env = {"CATY_CLAUDE_BIN": sys.executable} if backend == "claude" else {}
    if backend == "hermes":
        extra_env["CATY_HERMES_API_KEY"] = "test-key"
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--backend", backend,
        "--yes",
        "--public-url", "http://100.1.1.1:8788",
        extra_env=extra_env,
    )
    orch.__dict__.pop("_probe_backend")
    if backend == "claude":
        monkeypatch.setattr(
            orch,
            "_run",
            lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0 if reachable else 1, "", ""),
        )
    else:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def urlopen(*_args, **_kwargs):
            if reachable:
                return Response()
            raise setup_orchestrator.urllib.error.URLError("offline")

        monkeypatch.setattr(setup_orchestrator.urllib.request, "urlopen", urlopen)
    assert setup_orchestrator.SetupOrchestrator._probe_backend(orch) is reachable


def test_claude_enable_failure_rolls_back_without_restart(fake_home, tmp_path, monkeypatch):
    config = fake_home / ".claude-config"
    config.write_bytes(b"original")
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--backend", "claude",
        "--yes",
        "--public-url", "http://100.1.1.1:8788",
        extra_env={
            "CATY_CLAUDE_BIN": sys.executable,
            "CATY_BACKEND_ENABLE_CMD": "printf changed > '%s'" % config,
            "CATY_BACKEND_CONFIG_PATHS": str(config),
        },
    )
    orch._probe_backend = lambda: False
    orch.config = orch._resolved_config()
    orch._start_state()
    restarts = []
    monkeypatch.setattr(orch, "_linux_restart_target", lambda: restarts.append(True))
    with pytest.raises(setup_orchestrator.SetupError, match="Claude backend remained unavailable"):
        orch._backend()
    assert config.read_bytes() == b"original"
    assert restarts == []


def test_claude_default_enable_failure_says_nothing_was_restored(fake_home, tmp_path, monkeypatch):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--backend", "claude",
        "--yes",
        "--public-url", "http://100.1.1.1:8788",
        extra_env={
            "CATY_CLAUDE_BIN": sys.executable,
            "CATY_BACKEND_ENABLE_CMD": "exit 0",
        },
    )
    orch._probe_backend = lambda: False
    orch.config = orch._resolved_config()
    orch._start_state()
    with pytest.raises(setup_orchestrator.SetupError) as raised:
        orch._backend()
    assert "no brain configuration was declared for backup, so nothing was restored" in str(raised.value)


def test_claude_declared_absent_enable_failure_removes_created_file(fake_home, tmp_path, monkeypatch):
    config = fake_home / ".claude-config"
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--backend", "claude",
        "--yes",
        "--public-url", "http://100.1.1.1:8788",
        extra_env={
            "CATY_CLAUDE_BIN": sys.executable,
            "CATY_BACKEND_ENABLE_CMD": "printf changed > '%s'" % config,
            "CATY_BACKEND_CONFIG_PATHS": str(config),
        },
    )
    orch._probe_backend = lambda: False
    orch.config = orch._resolved_config()
    orch._start_state()
    with pytest.raises(setup_orchestrator.SetupError) as raised:
        orch._backend()
    assert not config.exists()
    message = str(raised.value)
    assert "declared brain configuration paths had no pre-enable content" in message
    assert "enable-created files were removed" in message
    assert "nothing was restored" not in message


def test_hermes_default_backup_roots_are_only_config_files(fake_home, tmp_path, monkeypatch):
    hermes_home = fake_home / "custom-hermes"
    extra = tmp_path / "extra.yaml"
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--backend", "hermes",
        "--yes",
        "--public-url", "http://100.1.1.1:8788",
        extra_env={
            "CATY_HERMES_API_KEY": "test-key",
            "HERMES_HOME": str(hermes_home),
            "CATY_BACKEND_CONFIG_PATHS": str(extra),
        },
    )
    assert orch._brain_config_paths() == [
        hermes_home / "config.yaml",
        hermes_home / "profile.yaml",
        extra,
    ]


def test_linux_cgroup_detection_same_different_and_undetectable(fake_home, tmp_path, monkeypatch):
    orch = _make_orch(fake_home, tmp_path, monkeypatch, "--yes", "--public-url", "http://100.1.1.1:8788")
    monkeypatch.setattr(orch, "_listener_pid_from_ss", lambda _port: 321)
    monkeypatch.setattr(orch, "_listener_pid_from_proc", lambda _port: None)
    units = {
        "/proc/321/cgroup": "openclaw.service",
        "/proc/self/cgroup": "openclaw.service",
    }
    monkeypatch.setattr(orch, "_cgroup_unit", lambda path: units.get(str(path)))
    assert orch._linux_restart_target() == ("openclaw.service", True)
    units["/proc/self/cgroup"] = "caty-gateway-alice.service"
    assert orch._linux_restart_target() == ("openclaw.service", False)
    units["/proc/self/cgroup"] = None  # normal interactive session.scope
    assert orch._linux_restart_target() == ("openclaw.service", False)
    monkeypatch.setattr(orch, "_listener_pid_from_ss", lambda _port: None)
    with pytest.raises(setup_orchestrator.SetupError, match="cannot be verified"):
        orch._linux_restart_target()


def test_same_unit_handoff_uses_collect_and_persists_restart_state(fake_home, tmp_path, monkeypatch, capsys):
    config = fake_home / ".openclaw" / "openclaw.json"
    config.parent.mkdir()
    config.write_bytes(b"original")
    record = tmp_path / "systemd-run.argv"
    fakebin = pathlib.Path(_prepare_fake_commands(tmp_path))
    _write_exec(
        fakebin / "systemd-run",
        """
        #!/usr/bin/env sh
        printf '%s\n' "$*" > "$SYSTEMD_RUN_RECORD"
        exit 0
        """,
    )
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes", "--public-url", "http://100.1.1.1:8788",
        extra_env={
            "CATY_BACKEND_ENABLE_CMD": "printf enabled > '%s'" % config,
            "SYSTEMD_RUN_RECORD": str(record),
        },
    )
    probes = iter([False, False])
    orch._probe_backend = lambda: next(probes)
    monkeypatch.setattr(orch, "_linux_restart_target", lambda: ("openclaw.service", True))
    monkeypatch.setattr(orch, "_wait_supervisor_ready", lambda: True)
    orch.config = orch._resolved_config()
    orch._start_state()

    assert orch._backend() is False
    invocation = record.read_text(encoding="utf-8")
    assert "--user --collect --unit=caty-setup-supervisor-alice" in invocation
    assert "caty_gateway.setup_supervisor.py" in invocation
    payload = json.loads(orch.state_path.read_text(encoding="utf-8"))
    assert payload["restart_target"] == "openclaw.service"
    assert payload["backend_enable_done"] is True
    assert pathlib.Path(payload["backup_manifest_path"]).is_file()
    output = capsys.readouterr().out
    assert "will restart now" in output
    assert "--status --member alice" in output


def test_handoff_uses_existing_rollback_point_after_interrupted_enable(fake_home, tmp_path, monkeypatch):
    config = fake_home / ".openclaw" / "openclaw.json"
    config.parent.mkdir()
    config.write_bytes(b"original")
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes", "--public-url", "http://100.1.1.1:8788",
        extra_env={"CATY_BACKEND_ENABLE_CMD": "exit 99"},
    )
    orch.config = orch._resolved_config()
    orch._start_state()
    manifest = orch._create_backend_backup()
    config.write_bytes(b"enabled")
    orch.state.backend_enable_done = True
    orch.state.backup_manifest_path = str(manifest)
    orch._write_state()
    orch._probe_backend = lambda: False
    monkeypatch.setattr(orch, "_linux_restart_target", lambda: ("openclaw.service", True))
    handoffs = []
    monkeypatch.setattr(orch, "_supervised_handoff", lambda target, backup: handoffs.append((target, backup)) or False)
    backup_root = manifest.parent.parent
    before = sorted(backup_root.iterdir())
    assert orch._backend() is False
    assert handoffs == [("openclaw.service", manifest)]
    assert sorted(backup_root.iterdir()) == before
    assert config.read_bytes() == b"enabled"


def test_precheckpoint_enable_crash_restores_without_second_enable(fake_home, tmp_path, monkeypatch):
    config = fake_home / ".openclaw" / "openclaw.json"
    config.parent.mkdir()
    config.write_bytes(b"original")
    marker = tmp_path / "enable-reran"
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes", "--public-url", "http://100.1.1.1:8788",
        extra_env={"CATY_BACKEND_ENABLE_CMD": "touch '%s'" % marker},
    )
    orch.config = orch._resolved_config()
    orch._start_state()
    manifest = orch._create_backend_backup()
    orch.state.backup_manifest_path = str(manifest)
    orch.state.backend_enable_done = False
    orch._write_state()
    config.write_bytes(b"partially-enabled")
    orch._probe_backend = lambda: (_ for _ in ()).throw(AssertionError("crash recovery must precede probing"))

    with pytest.raises(setup_orchestrator.SetupError, match="brain configuration was restored"):
        orch._backend()
    assert config.read_bytes() == b"original"
    assert not marker.exists()
    assert orch.state.backup_manifest_path == ""
    assert orch.state.backend_enable_done is False


def test_supervised_resume_probe_failure_never_recurses(fake_home, tmp_path, monkeypatch):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes", "--public-url", "http://100.1.1.1:8788",
        extra_env={"CATY_SETUP_SUPERVISED": "1", "CATY_BACKEND_ENABLE_CMD": "exit 0"},
    )
    orch.config = orch._resolved_config()
    orch._start_state()
    orch._probe_backend = lambda: False
    targets = []
    monkeypatch.setattr(orch, "_linux_restart_target", lambda: targets.append(True))
    with pytest.raises(setup_orchestrator.SetupError, match="remained unreachable"):
        orch._backend()
    assert targets == []


def test_supervisor_uses_verified_python_before_qrcode_venv_exists(fake_home, tmp_path, monkeypatch):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
        extra_env={"CATY_SETUP_QR_TIMEOUT_SECONDS": "123.5"},
    )
    orch.service_python = str(orch.venv_python)
    assert not orch.venv_python.exists()
    orch.config = orch._resolved_config()
    orch._start_state()
    manifest = orch._create_backend_backup()
    command = orch._supervisor_command("openclaw.service", manifest)
    assert pathlib.Path(command[0]).samefile(orch.probe_python)
    assert pathlib.Path(command[command.index("--python") + 1]).samefile(orch.probe_python)
    assert str(orch.venv_python) not in command
    assert float(command[command.index("--resume-timeout") + 1]) == 423.5


def test_macos_restart_target_requires_verified_launchd_label(fake_home, tmp_path, monkeypatch):
    orch = _make_orch(fake_home, tmp_path, monkeypatch, "--yes", "--public-url", "http://100.1.1.1:8788")
    orch.system = "Darwin"
    monkeypatch.setattr(orch, "_command_path", lambda value: "/usr/sbin/lsof" if value == "lsof" else value)

    def successful(command, **_kwargs):
        if command[0].endswith("lsof"):
            return subprocess.CompletedProcess(command, 0, "321\n", "")
        assert command == ["launchctl", "list"]
        return subprocess.CompletedProcess(
            command,
            0,
            "PID\tStatus\tLabel\n-\t0\tcom.apple.unrelated\n321\t0\tai.caty.backend\n",
            "",
        )

    monkeypatch.setattr(orch, "_run", successful)
    assert orch._macos_restart_target() == "ai.caty.backend"

    commands = []

    def listener_absent(command, **_kwargs):
        commands.append(command)
        if command[0].endswith("lsof"):
            return subprocess.CompletedProcess(command, 0, "321\n", "")
        return subprocess.CompletedProcess(
            command,
            0,
            "PID Status Label\n- 0 com.apple.unrelated\n654 0 ai.caty.other\n",
            "",
        )

    monkeypatch.setattr(orch, "_run", listener_absent)
    with pytest.raises(setup_orchestrator.SetupError, match="could not be found"):
        orch._macos_restart_target()
    assert ["launchctl", "procinfo", "321"] not in commands
    assert not any(command[:3] == ["launchctl", "kickstart", "-k"] for command in commands)


@pytest.mark.parametrize(
    "value",
    ["file:///tmp/backend.sock", "http://user:password@127.0.0.1:18789"],
)
def test_invalid_backend_probe_url_fails_before_network_or_supervisor_argv(
    fake_home, tmp_path, monkeypatch, value
):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes", "--public-url", "http://100.1.1.1:8788",
        extra_env={"CATY_GATEWAY_URL": value},
    )
    orch.__dict__.pop("_probe_backend")
    opened = []
    monkeypatch.setattr(setup_orchestrator.urllib.request, "urlopen", lambda *args, **kwargs: opened.append(args))
    with pytest.raises(setup_orchestrator.SetupError, match="absolute http"):
        setup_orchestrator.SetupOrchestrator._probe_backend(orch)
    assert opened == []
    orch.config = orch._resolved_config()
    orch._start_state()
    manifest = orch._create_backend_backup()
    with pytest.raises(setup_orchestrator.SetupError, match="absolute http"):
        orch._supervisor_command("openclaw.service", manifest)


def test_different_unit_restarts_inline_and_continues(fake_home, tmp_path, monkeypatch):
    config = fake_home / ".openclaw" / "openclaw.json"
    config.parent.mkdir()
    config.write_bytes(b"original")
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes", "--public-url", "http://100.1.1.1:8788",
        extra_env={"CATY_BACKEND_ENABLE_CMD": "printf enabled > '%s'" % config},
    )
    probes = iter([False, False])
    orch._probe_backend = lambda: next(probes)
    monkeypatch.setattr(orch, "_linux_restart_target", lambda: ("openclaw.service", False))
    calls = []
    monkeypatch.setattr(orch, "_inline_restart", lambda target, manifest: calls.append((target, manifest)))
    orch.config = orch._resolved_config()
    orch._start_state()
    assert orch._backend() is True
    assert calls[0][0] == "openclaw.service"
    assert calls[0][1].is_file()


@pytest.mark.parametrize("missing", [True, False])
def test_systemd_run_missing_or_failing_rolls_back_without_plain_detach(
    fake_home, tmp_path, monkeypatch, missing
):
    config = fake_home / ".openclaw" / "openclaw.json"
    config.parent.mkdir()
    config.write_bytes(b"original")
    orch = _make_orch(fake_home, tmp_path, monkeypatch, "--yes", "--public-url", "http://100.1.1.1:8788")
    orch.config = orch._resolved_config()
    orch._start_state()
    manifest = orch._create_backend_backup()
    config.write_bytes(b"changed")
    real_command_path = orch._command_path
    monkeypatch.setattr(
        orch,
        "_command_path",
        lambda value: None if value == "systemd-run" and missing else real_command_path(value),
    )
    if not missing:
        fake = pathlib.Path(orch.env["PATH"].split(os.pathsep)[0]) / "systemd-run"
        _write_exec(fake, "#!/usr/bin/env sh\nexit 7\n")
    detached_popens = []
    real_popen = subprocess.Popen

    def observe_popen(*args, **kwargs):
        if kwargs.get("start_new_session"):
            detached_popens.append((args, kwargs))
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", observe_popen)
    with pytest.raises(setup_orchestrator.SetupError, match="brain configuration was restored"):
        orch._supervised_handoff("openclaw.service", manifest)
    assert config.read_bytes() == b"original"
    assert detached_popens == []


def test_supervisor_handshake_timeout_stops_transient_and_rolls_back(fake_home, tmp_path, monkeypatch):
    config = fake_home / ".openclaw" / "openclaw.json"
    config.parent.mkdir()
    config.write_bytes(b"original")
    record = tmp_path / "commands.argv"
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes", "--public-url", "http://100.1.1.1:8788",
        extra_env={"COMMAND_RECORD": str(record)},
    )
    fakebin = pathlib.Path(orch.env["PATH"].split(os.pathsep)[0])
    script = "#!/usr/bin/env sh\nprintf '%s\\n' \"$0 $*\" >> \"$COMMAND_RECORD\"\nexit 0\n"
    _write_exec(fakebin / "systemd-run", script)
    _write_exec(fakebin / "systemctl", script)
    orch.config = orch._resolved_config()
    orch._start_state()
    manifest = orch._create_backend_backup()
    config.write_bytes(b"enabled")
    monkeypatch.setattr(orch, "_wait_supervisor_ready", lambda: False)
    with pytest.raises(setup_orchestrator.SetupError, match="environment handoff"):
        orch._supervised_handoff("openclaw.service", manifest)
    assert config.read_bytes() == b"original"
    assert "systemctl --user stop caty-setup-supervisor-alice" in record.read_text(encoding="utf-8")


def test_supervised_qr_forces_url_captures_safe_lines_and_never_prints_child(
    fake_home, tmp_path, monkeypatch, capsys
):
    token = ("0123456789abcdef") * 3
    pair = "deadbeef." + ("0123456789abcdef") * 2
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes", "--public-url", "http://100.1.1.1:8788",
        extra_env={"CATY_SETUP_SUPERVISED": "1"},
    )
    orch.artifact_path.write_text("CATY_TOKEN=%s\n" % token, encoding="utf-8")
    orch.caty_gateway.write_text(
        "import sys, time\n"
        "assert sys.argv[-1] == 'url'\n"
        "print('QR URL: http://100.1.1.1:4444/qr/random', flush=True)\n"
        "print('Expires: 2026-08-03T01:02:03Z (10 minutes remaining)', flush=True)\n"
        "print(%r, flush=True)\n"
        "print(%r, file=sys.stderr, flush=True)\n"
        "time.sleep(0.4)\n" % (pair, token),
        encoding="utf-8",
    )
    thread = threading.Thread(target=orch._qr)
    thread.start()
    deadline = time.time() + 2
    payload = {}
    while time.time() < deadline:
        if orch.status_path.exists():
            payload = json.loads(orch.status_path.read_text(encoding="utf-8"))
            if payload.get("qr_url"):
                break
        time.sleep(0.01)
    assert payload.get("qr_url"), "QR URL must reach status before the waiting child exits"
    assert thread.is_alive()
    thread.join(timeout=2)
    assert not thread.is_alive()
    status_text = orch.status_path.read_text(encoding="utf-8")
    payload = json.loads(status_text)
    assert payload["qr_url"].endswith("/qr/random")
    assert payload["expires_at"].startswith("2026-08-03")
    assert token not in status_text
    assert pair not in status_text
    captured = capsys.readouterr()
    assert token not in captured.out + captured.err
    assert pair not in captured.out + captured.err
    assert stat.S_IMODE(orch.status_path.stat().st_mode) == 0o600


def test_supervised_qr_nonzero_keeps_bounded_redacted_stderr_tail(
    fake_home, tmp_path, monkeypatch, capsys
):
    secret = "c" * 48
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes", "--public-url", "http://100.1.1.1:8788",
        extra_env={"CATY_SETUP_SUPERVISED": "1"},
    )
    orch.artifact_path.write_text("CATY_TOKEN=test\n", encoding="utf-8")
    orch.caty_gateway.write_text(
        "import sys\n"
        "sys.stderr.write('z' * 20000 + '\\n')\n"
        "sys.stderr.write('failing qr diagnostic TOKEN=%s\\n')\n"
        "raise SystemExit(7)\n" % secret,
        encoding="utf-8",
    )
    with pytest.raises(setup_orchestrator.SetupError, match="QR command exited non-zero"):
        orch._qr()
    payload = json.loads(orch.status_path.read_text(encoding="utf-8"))
    assert "failing qr diagnostic" in payload["qr_error_tail"]
    assert secret not in payload["qr_error_tail"]
    assert "[REDACTED]" in payload["qr_error_tail"]
    assert len(payload["qr_error_tail"].encode("utf-8")) <= 8192
    captured = capsys.readouterr()
    assert "failing qr diagnostic" not in captured.out + captured.err


def test_supervised_qr_keeps_truncated_partial_line_without_newline(
    fake_home, tmp_path, monkeypatch
):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes", "--public-url", "http://100.1.1.1:8788",
        extra_env={"CATY_SETUP_SUPERVISED": "1"},
    )
    orch.artifact_path.write_text("CATY_TOKEN=test\n", encoding="utf-8")
    orch.caty_gateway.write_text(
        "import sys\n"
        "sys.stderr.write('z' * 20000 + 'partial diagnostic tail')\n"
        "raise SystemExit(7)\n",
        encoding="utf-8",
    )

    with pytest.raises(setup_orchestrator.SetupError, match="QR command exited non-zero"):
        orch._qr()

    payload = json.loads(orch.status_path.read_text(encoding="utf-8"))
    assert payload["qr_error_tail"].endswith("partial diagnostic tail")
    assert len(payload["qr_error_tail"].encode("utf-8")) == 8192


def test_supervised_qr_does_not_overwrite_replaced_sigterm_handler(
    fake_home, tmp_path, monkeypatch
):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes", "--public-url", "http://100.1.1.1:8788",
        extra_env={"CATY_SETUP_SUPERVISED": "1"},
    )
    replacement = lambda *_args: None
    qr = tmp_path / "replace_handler.py"
    qr.write_text("raise SystemExit(0)\n", encoding="utf-8")
    original_queue = setup_orchestrator.queue.Queue
    replaced_during_wait = False

    class ReplaceHandlerOnWait(original_queue):
        def get(self, *args, **kwargs):
            nonlocal replaced_during_wait
            if not replaced_during_wait:
                assert signal.getsignal(signal.SIGTERM) is not previous
                signal.signal(signal.SIGTERM, replacement)
                replaced_during_wait = True
            return super().get(*args, **kwargs)

    previous = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(setup_orchestrator.queue, "Queue", ReplaceHandlerOnWait)
    try:
        assert orch._run_supervised_qr([sys.executable, str(qr)], orch.env) == 0
        assert replaced_during_wait
        assert signal.getsignal(signal.SIGTERM) is replacement
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_supervised_qr_restores_sigterm_handler_if_reader_start_fails(
    fake_home, tmp_path, monkeypatch
):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes", "--public-url", "http://100.1.1.1:8788",
        extra_env={"CATY_SETUP_SUPERVISED": "1"},
    )
    qr = tmp_path / "reader_start_failure.py"
    qr.write_text("raise SystemExit(0)\n", encoding="utf-8")
    previous = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(
        setup_orchestrator.threading.Thread,
        "start",
        lambda _self: (_ for _ in ()).throw(RuntimeError("reader start failed")),
    )

    with pytest.raises(RuntimeError, match="reader start failed"):
        orch._run_supervised_qr([sys.executable, str(qr)], orch.env)

    assert signal.getsignal(signal.SIGTERM) is previous


def test_supervised_qr_sigterm_kills_qr_process(fake_home, tmp_path, monkeypatch):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes", "--public-url", "http://100.1.1.1:8788",
        extra_env={"CATY_SETUP_SUPERVISED": "1"},
    )
    pid_file = tmp_path / "qr.pid"
    qr = tmp_path / "long_qr.py"
    qr.write_text(
        "import os, pathlib, time\n"
        "pathlib.Path(%r).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n" % str(pid_file),
        encoding="utf-8",
    )

    def terminate_when_started():
        deadline = time.time() + 5
        while time.time() < deadline and not pid_file.exists():
            time.sleep(0.01)
        if pid_file.exists():
            os.kill(os.getpid(), signal.SIGTERM)

    sender = threading.Thread(target=terminate_when_started, daemon=True)
    sender.start()
    with pytest.raises(SystemExit) as raised:
        orch._run_supervised_qr([sys.executable, str(qr)], orch.env)
    sender.join(timeout=1)
    assert raised.value.code == 143
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    with pytest.raises(OSError):
        os.kill(child_pid, 0)


def test_status_wait_and_single_flight_use_pid_start_time(fake_home, tmp_path, monkeypatch, capsys):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--status", "--wait", "--public-url", "http://100.1.1.1:8788",
        extra_env={"CATY_SETUP_STATUS_WAIT_SECONDS": "1"},
    )
    payloads = iter(
        [
            {"state": "resuming", "active": True},
            {"state": "resuming", "active": True, "qr_url": "http://100.1.1.1/qr/x", "expires_at": "soon"},
        ]
    )
    monkeypatch.setattr(orch, "_read_status", lambda: next(payloads))
    monkeypatch.setattr(setup_orchestrator.time, "sleep", lambda _seconds: None)
    assert orch.run() == 0
    assert "QR URL: http://100.1.1.1/qr/x" in capsys.readouterr().out

    follower = _make_orch(fake_home, tmp_path, monkeypatch, "--yes", "--public-url", "http://100.1.1.1:8788")
    status = {
        "state": "resuming",
        "active": True,
        "supervisor_pid": 4321,
        "supervisor_start_time": "777",
    }
    monkeypatch.setattr(follower, "_read_status", lambda: status)
    monkeypatch.setattr(setup_orchestrator, "process_start_time", lambda pid, system: "777")
    followed = []
    monkeypatch.setattr(follower, "_status", lambda wait: followed.append(wait) or 0)
    assert follower.run() == 0
    assert followed == [True]

    status["supervisor_start_time"] = "stale"
    assert follower._single_flight_active() is False


def test_dead_supervisor_live_orchestrator_remains_single_flight_owner(
    fake_home, tmp_path, monkeypatch, capsys
):
    owner_status = {
        "state": "resuming",
        "active": True,
        "supervisor_pid": 4100,
        "supervisor_start_time": "dead-supervisor",
        "orchestrator_pid": 4200,
        "orchestrator_start_time": "live-orchestrator",
    }
    monkeypatch.setattr(
        setup_orchestrator,
        "process_start_time",
        lambda pid, _system=None: "live-orchestrator" if pid == 4200 else None,
    )

    follower = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
    )
    monkeypatch.setattr(follower, "_read_status", lambda: owner_status)
    assert follower._single_flight_active() is True

    waiting = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--status",
        "--wait",
        "--public-url",
        "http://100.1.1.1:8788",
        extra_env={"CATY_SETUP_STATUS_WAIT_SECONDS": "1"},
    )
    payloads = iter(
        [
            owner_status,
            {**owner_status, "state": "waiting-qr", "qr_url": "http://100.1.1.1/qr/owned"},
        ]
    )
    monkeypatch.setattr(waiting, "_read_status", lambda: next(payloads))
    monkeypatch.setattr(setup_orchestrator.time, "sleep", lambda _seconds: None)
    assert waiting._status(True) == 0
    output = capsys.readouterr().out
    assert "QR URL: http://100.1.1.1/qr/owned" in output
    assert "rerun the setup command to resume" not in output


def test_dead_supervisor_and_orchestrator_release_single_flight(fake_home, tmp_path, monkeypatch):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
    )
    status = {
        "state": "running",
        "active": True,
        "supervisor_pid": 4100,
        "supervisor_start_time": "dead-supervisor",
        "orchestrator_pid": 4200,
        "orchestrator_start_time": "dead-orchestrator",
    }
    monkeypatch.setattr(orch, "_read_status", lambda: status)
    monkeypatch.setattr(setup_orchestrator, "process_start_time", lambda _pid, _system=None: None)
    assert orch._single_flight_active() is False


def test_old_status_without_orchestrator_fields_keeps_supervisor_behavior(
    fake_home, tmp_path, monkeypatch
):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
    )
    old_status = {
        "state": "resuming",
        "active": True,
        "supervisor_pid": 4100,
        "supervisor_start_time": "supervisor-start",
    }
    monkeypatch.setattr(orch, "_read_status", lambda: old_status)
    monkeypatch.setattr(
        setup_orchestrator,
        "process_start_time",
        lambda pid, _system=None: "supervisor-start" if pid == 4100 else None,
    )
    assert orch._single_flight_active() is True
    old_status["supervisor_start_time"] = "reused-pid-start"
    assert orch._single_flight_active() is False


def test_orchestrator_pid_reuse_does_not_hold_single_flight(fake_home, tmp_path, monkeypatch):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
    )
    status = {
        "state": "running",
        "active": True,
        "orchestrator_pid": 4200,
        "orchestrator_start_time": "original-start",
    }
    monkeypatch.setattr(orch, "_read_status", lambda: status)
    monkeypatch.setattr(
        setup_orchestrator,
        "process_start_time",
        lambda pid, _system=None: "reused-pid-start" if pid == 4200 else None,
    )
    assert orch._single_flight_active() is False


def test_status_wait_stops_after_three_dead_supervisor_polls(fake_home, tmp_path, monkeypatch, capsys):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--status", "--wait", "--public-url", "http://100.1.1.1:8788",
        extra_env={"CATY_SETUP_STATUS_WAIT_SECONDS": "1"},
    )
    dead = {
        "state": "resuming",
        "active": True,
        "supervisor_pid": 4321,
        "supervisor_start_time": "stale",
    }
    payloads = iter([dead, dead, dead])
    monkeypatch.setattr(orch, "_read_status", lambda: next(payloads))
    monkeypatch.setattr(orch, "_supervisor_is_live", lambda _payload: False)
    monkeypatch.setattr(setup_orchestrator.time, "sleep", lambda _seconds: None)
    assert orch._status(True) == 1
    output = capsys.readouterr().out
    assert "Setup status for alice: resuming" in output
    assert "restart supervisor is no longer running; setup did not complete" in output
    assert "rerun the setup command to resume" in output


def test_status_wait_keeps_polling_while_supervisor_is_live(fake_home, tmp_path, monkeypatch, capsys):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--status", "--wait", "--public-url", "http://100.1.1.1:8788",
        extra_env={"CATY_SETUP_STATUS_WAIT_SECONDS": "1"},
    )
    payloads = iter(
        [
            {
                "state": "resuming",
                "active": True,
                "supervisor_pid": 4321,
                "supervisor_start_time": "777",
            },
            {
                "state": "waiting-qr",
                "active": True,
                "supervisor_pid": 4321,
                "supervisor_start_time": "777",
                "qr_url": "http://100.1.1.1/qr/live",
            },
        ]
    )
    monkeypatch.setattr(orch, "_read_status", lambda: next(payloads))
    monkeypatch.setattr(orch, "_supervisor_is_live", lambda _payload: True)
    monkeypatch.setattr(setup_orchestrator.time, "sleep", lambda _seconds: None)
    assert orch._status(True) == 0
    assert "QR URL: http://100.1.1.1/qr/live" in capsys.readouterr().out


def test_single_flight_waits_briefly_for_handoff_pid(fake_home, tmp_path, monkeypatch):
    orch = _make_orch(fake_home, tmp_path, monkeypatch, "--yes", "--public-url", "http://100.1.1.1:8788")
    statuses = iter(
        [
            {"state": "handoff", "active": True},
            {
                "state": "validating",
                "active": True,
                "supervisor_pid": 99,
                "supervisor_start_time": "123",
            },
        ]
    )
    monkeypatch.setattr(orch, "_read_status", lambda: next(statuses))
    monkeypatch.setattr(setup_orchestrator.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(setup_orchestrator, "process_start_time", lambda pid, system: "123")
    assert orch._single_flight_active() is True


def test_single_flight_grace_covers_resumed_child_registration(fake_home, tmp_path, monkeypatch):
    orch = _make_orch(fake_home, tmp_path, monkeypatch, "--yes", "--public-url", "http://100.1.1.1:8788")
    statuses = iter(
        [
            {
                "state": "resuming",
                "active": True,
                "supervisor_pid": 99,
                "supervisor_start_time": "dead",
                "orchestrator_registration_pending": True,
            },
            {
                "state": "resuming",
                "active": True,
                "supervisor_pid": 99,
                "supervisor_start_time": "dead",
                "orchestrator_pid": 100,
                "orchestrator_start_time": "child-start",
            },
        ]
    )
    monkeypatch.setattr(orch, "_read_status", lambda: next(statuses))
    monkeypatch.setattr(setup_orchestrator.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        setup_orchestrator,
        "process_start_time",
        lambda pid, _system=None: "child-start" if pid == 100 else None,
    )
    assert orch._single_flight_active() is True


def test_supervised_child_registers_without_dropping_supervisor_owner(
    fake_home, tmp_path, monkeypatch
):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
        extra_env={"CATY_SETUP_SUPERVISED": "1"},
    )
    orch._update_status(
        state="resuming",
        active=True,
        supervisor_pid=99,
        supervisor_start_time="supervisor-start",
        orchestrator_registration_pending=True,
    )

    assert orch._single_flight_active() is False
    assert orch._claim_orchestrator_owner() is True
    payload = orch._read_status()
    assert payload["supervisor_pid"] == 99
    assert payload["supervisor_start_time"] == "supervisor-start"
    assert payload["orchestrator_pid"] == os.getpid()
    assert payload["orchestrator_start_time"] == "test-current-process"
    assert "orchestrator_registration_pending" not in payload


@pytest.mark.parametrize("assertion", ["follower", "preserves-owner"])
def test_supervised_child_yields_to_concurrent_manual_owner(
    fake_home, tmp_path, monkeypatch, assertion
):
    supervised = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
        extra_env={"CATY_SETUP_SUPERVISED": "1"},
    )
    supervised.orchestrator_pid = 22222
    supervised.orchestrator_start_time = "supervised-start"
    supervised._update_status(
        state="resuming",
        active=True,
        terminal=False,
        supervisor_pid=99,
        supervisor_start_time="dead-supervisor",
        orchestrator_registration_pending=True,
    )
    assert supervised._single_flight_active() is False

    manual = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
    )
    manual.orchestrator_pid = 11111
    manual.orchestrator_start_time = "manual-start"
    monkeypatch.setattr(
        setup_orchestrator,
        "process_start_time",
        lambda pid, _system=None: "manual-start" if pid == 11111 else None,
    )
    manual._claim_status = manual._read_status()
    assert manual._claim_orchestrator_owner() is True

    claimed = supervised._claim_orchestrator_owner()
    payload = supervised._read_status()
    if assertion == "follower":
        assert claimed is False
    else:
        assert payload["orchestrator_pid"] == 11111
        assert payload["orchestrator_start_time"] == "manual-start"


def test_supervised_child_replaces_stale_orchestrator_owner(
    fake_home, tmp_path, monkeypatch
):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
        extra_env={"CATY_SETUP_SUPERVISED": "1"},
    )
    orch._update_status(
        state="resuming",
        active=True,
        terminal=False,
        supervisor_pid=99,
        supervisor_start_time="dead-supervisor",
        orchestrator_pid=11111,
        orchestrator_start_time="stale-manual-start",
        orchestrator_registration_pending=True,
    )
    monkeypatch.setattr(setup_orchestrator, "process_start_time", lambda *_args: None)

    assert orch._single_flight_active() is False
    assert orch._claim_orchestrator_owner() is True
    payload = orch._read_status()
    assert payload["orchestrator_pid"] == os.getpid()
    assert payload["orchestrator_start_time"] == "test-current-process"


def test_yielded_supervised_child_follows_status_before_side_effects(
    fake_home, tmp_path, monkeypatch
):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
        extra_env={"CATY_SETUP_SUPERVISED": "1"},
    )
    monkeypatch.setattr(orch, "_claim_orchestrator_owner", lambda: False)
    monkeypatch.setattr(orch, "_status", lambda wait: 0 if wait else 1)
    monkeypatch.setattr(
        orch,
        "_preflight",
        lambda: pytest.fail("yielded child must stop before preflight"),
    )
    monkeypatch.setattr(
        orch,
        "_install",
        lambda: pytest.fail("yielded child must stop before install"),
    )
    monkeypatch.setattr(
        orch,
        "_qr",
        lambda: pytest.fail("yielded child must stop before QR"),
    )

    assert orch.run() == 0


def test_supervised_child_does_not_create_missing_status(fake_home, tmp_path, monkeypatch):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
        extra_env={"CATY_SETUP_SUPERVISED": "1"},
    )

    assert orch._single_flight_active() is False
    assert orch._claim_orchestrator_owner() is True
    assert not orch.status_path.exists()
    assert not orch.status_path.parent.exists()


def test_preflight_failure_does_not_create_status(fake_home, tmp_path, monkeypatch):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
    )
    orch._preflight = lambda: (_ for _ in ()).throw(
        setup_orchestrator.SetupError("preflight failed")
    )

    with pytest.raises(setup_orchestrator.SetupError, match="preflight failed"):
        orch.run()

    assert not orch.status_path.exists()
    assert not orch.status_path.parent.exists()


def test_manual_owner_registration_is_first_post_preflight_status_write(
    fake_home, tmp_path, monkeypatch
):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
    )
    events = []
    update_status = orch._update_status

    def preflight():
        events.append(("preflight", {}))
        orch.config = orch._resolved_config()

    def record_status_update(**changes):
        events.append(("status", dict(changes)))
        return update_status(**changes)

    orch._preflight = preflight
    monkeypatch.setattr(orch, "_update_status", record_status_update)
    orch._backend = lambda: True
    orch._install = lambda: None
    orch._start = lambda: None
    orch._linger = lambda: None
    orch._health = lambda: None
    orch._identity = lambda: None
    orch._qr = lambda: None

    assert orch.run() == 0
    first_status = next(
        (index, changes)
        for index, (kind, changes) in enumerate(events)
        if kind == "status"
    )
    assert events[0][0] == "preflight"
    assert first_status[0] > 0
    assert first_status[1]["state"] == "running"
    assert first_status[1]["orchestrator_pid"] == os.getpid()
    assert first_status[1]["orchestrator_start_time"] == "test-current-process"


def test_unavailable_orchestrator_identity_runs_without_registering_owner(
    fake_home, tmp_path, monkeypatch
):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
    )
    orch.orchestrator_start_time = None
    status_updates = []
    update_status = orch._update_status

    def record_status_update(**changes):
        status_updates.append(dict(changes))
        return update_status(**changes)

    monkeypatch.setattr(orch, "_update_status", record_status_update)
    orch._preflight = lambda: setattr(orch, "config", orch._resolved_config())
    orch._backend = lambda: True
    orch._install = lambda: None
    orch._start = lambda: None
    orch._linger = lambda: None
    orch._health = lambda: None
    orch._identity = lambda: None
    orch._qr = lambda: None

    assert orch.run() == 0
    assert status_updates
    assert all(
        "orchestrator_pid" not in update and "orchestrator_start_time" not in update
        for update in status_updates
    )
    payload = orch._read_status()
    assert payload["state"] == "succeeded"
    assert all(field not in payload for field in setup_orchestrator.OWNER_FIELDS)


def test_main_failure_marks_terminal_status(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(
        setup_orchestrator.SetupOrchestrator,
        "run",
        lambda self: (_ for _ in ()).throw(setup_orchestrator.SetupError("safe failure")),
    )
    assert setup_orchestrator.main(["--member", "alice"]) == 1
    status = json.loads((tmp_path / "state/caty-gateway/setup/alice.status.json").read_text(encoding="utf-8"))
    assert status["state"] == "failed"
    assert status["terminal"] is True


def test_orchestrator_owner_fields_clear_on_normal_completion(fake_home, tmp_path, monkeypatch):
    orch = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--public-url",
        "http://100.1.1.1:8788",
    )
    orch._preflight = lambda: setattr(orch, "config", orch._resolved_config())
    orch._backend = lambda: True
    orch._install = lambda: None
    orch._start = lambda: None
    orch._linger = lambda: None
    orch._health = lambda: None
    orch._identity = lambda: None
    orch._qr = lambda: None

    assert orch.run() == 0
    payload = orch._read_status()
    assert payload["state"] == "succeeded"
    assert payload["terminal"] is True
    assert all(field not in payload for field in setup_orchestrator.OWNER_FIELDS)


@pytest.mark.parametrize(
    ("raised", "expected_state"),
    [
        (setup_orchestrator.SetupError("terminal failure"), "failed"),
        (SystemExit(143), "interrupted"),
    ],
)
def test_main_terminal_paths_clear_orchestrator_owner(tmp_path, monkeypatch, raised, expected_state):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(
        setup_orchestrator,
        "process_start_time",
        lambda pid, _system=None: "current-start" if pid == os.getpid() else None,
    )

    def claim_then_fail(orchestrator):
        orchestrator._claim_status = orchestrator._read_status()
        assert orchestrator._claim_orchestrator_owner() is True
        raise raised

    monkeypatch.setattr(setup_orchestrator.SetupOrchestrator, "run", claim_then_fail)
    if isinstance(raised, SystemExit):
        with pytest.raises(SystemExit) as exit_error:
            setup_orchestrator.main(["--member", "alice", "--yes"])
        assert exit_error.value.code == 143
    else:
        assert setup_orchestrator.main(["--member", "alice", "--yes"]) == 1

    status_path = tmp_path / "state/caty-gateway/setup/alice.status.json"
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["state"] == expected_state
    assert payload["terminal"] is True
    assert all(field not in payload for field in setup_orchestrator.OWNER_FIELDS)


def test_main_terminal_path_does_not_clear_new_orchestrator_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(
        setup_orchestrator,
        "process_start_time",
        lambda pid, _system=None: (
            "current-start" if pid == os.getpid()
            else "new-owner-start" if pid == 33333
            else None
        ),
    )

    def lose_claim_then_fail(orchestrator):
        orchestrator._claim_status = orchestrator._read_status()
        assert orchestrator._claim_orchestrator_owner() is True
        orchestrator._update_status(
            state="running",
            active=True,
            terminal=False,
            orchestrator_pid=33333,
            orchestrator_start_time="new-owner-start",
        )
        raise setup_orchestrator.SetupError("old owner failed after losing ownership")

    monkeypatch.setattr(
        setup_orchestrator.SetupOrchestrator,
        "run",
        lose_claim_then_fail,
    )
    assert setup_orchestrator.main(["--member", "alice", "--yes"]) == 1

    status_path = tmp_path / "state/caty-gateway/setup/alice.status.json"
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["state"] == "running"
    assert payload["active"] is True
    assert payload["terminal"] is False
    assert payload["orchestrator_pid"] == 33333
    assert payload["orchestrator_start_time"] == "new-owner-start"


def test_status_lifecycle_clears_stale_ephemeral_fields(fake_home, tmp_path, monkeypatch):
    orch = _make_orch(fake_home, tmp_path, monkeypatch, "--yes", "--public-url", "http://100.1.1.1:8788")
    orch._update_status(
        state="double-failure",
        qr_url="http://old/qr",
        expires_at="old",
        recovery_pointer="old-recovery",
    )
    orch._update_status(
        state="running",
        clear_fields=("qr_url", "expires_at", "recovery_pointer"),
        timeline_entry="setup run started",
    )
    payload = orch._read_status()
    assert "qr_url" not in payload
    assert "expires_at" not in payload
    assert "recovery_pointer" not in payload
    orch._update_status(state="succeeded", clear_fields=("qr_url", "expires_at", "recovery_pointer"))
    assert "recovery_pointer" not in orch._read_status()


def test_print_status_shows_qr_error_tail_before_resume_output(fake_home, tmp_path, monkeypatch, capsys):
    orch = _make_orch(fake_home, tmp_path, monkeypatch, "--yes", "--public-url", "http://100.1.1.1:8788")
    orch._print_status(
        {
            "state": "failed",
            "phase": "qr",
            "message": "setup resume failed",
            "qr_error_tail": "qr stderr tail",
            "resume_output": "resume stdout/stderr tail",
        }
    )
    lines = capsys.readouterr().out.splitlines()
    assert "QR Error Tail: qr stderr tail" in lines
    assert "Resume Output: resume stdout/stderr tail" in lines
    assert lines.index("QR Error Tail: qr stderr tail") < lines.index(
        "Resume Output: resume stdout/stderr tail"
    )


def test_status_wait_polls_empty_status_only_when_resume_exists(fake_home, tmp_path, monkeypatch, capsys):
    waiting = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--status", "--wait", "--public-url", "http://100.1.1.1:8788",
        extra_env={"CATY_SETUP_STATUS_WAIT_SECONDS": "1"},
    )
    waiting.config = waiting._resolved_config()
    waiting._start_state()
    payloads = iter([{}, {"state": "handoff", "qr_url": "http://100.1.1.1/qr/new"}])
    monkeypatch.setattr(waiting, "_read_status", lambda: next(payloads))
    monkeypatch.setattr(setup_orchestrator.time, "sleep", lambda _seconds: None)
    assert waiting._status(True) == 0
    assert "QR URL" in capsys.readouterr().out

    absent = _make_orch(
        fake_home,
        tmp_path,
        monkeypatch,
        "--status", "--wait", "--public-url", "http://100.1.1.1:8788",
    )
    waiting.state_path.unlink()
    monkeypatch.setattr(absent, "_read_status", lambda: {})
    monkeypatch.setattr(
        setup_orchestrator.time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(AssertionError("empty status without resume must not poll")),
    )
    assert absent._status(True) == 0
