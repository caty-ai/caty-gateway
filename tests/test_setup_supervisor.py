import hashlib
import json
import os
import pathlib
import re
import shlex
import signal
import stat
import subprocess
import sys
import textwrap
import threading
import time

import pytest



from caty_gateway import setup_supervisor


def _write_exec(path: pathlib.Path, body: str) -> None:
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _manifest(tmp_path: pathlib.Path, content: bytes = b"before\n"):
    config = tmp_path / "brain.json"
    config.write_bytes(content)
    state_dir = tmp_path / "state"
    manifest = setup_supervisor.create_backup([config], state_dir, "alice")
    return config, manifest


def _declared_absent_manifest(tmp_path: pathlib.Path):
    config = tmp_path / "brain.json"
    state_dir = tmp_path / "state"
    manifest = setup_supervisor.create_backup([config], state_dir, "alice")
    return config, manifest


def _supervisor(tmp_path: pathlib.Path, manifest: pathlib.Path, orchestrator: pathlib.Path, extra_env=None):
    status = tmp_path / "state" / "alice.status.json"
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    env["PYTHONPATH"] = str(orchestrator.parent) + os.pathsep + env.get("PYTHONPATH", "")
    supervisor = setup_supervisor.Supervisor(
        [
            "--member", "alice",
            "--target", "openclaw.service",
            "--platform", "Linux",
            "--probe-url", "http://127.0.0.1:18789",
            "--backup-manifest", str(manifest),
            "--status-file", str(status),
            "--orchestrator", orchestrator.stem,
            "--python", sys.executable,
            "--timeout", "1",
            "--resume-timeout", "5",
            "--grace-seconds", "0",
        ],
        env=env,
    )
    return supervisor, status


def test_backup_restore_is_byte_exact_and_records_absent(tmp_path):
    config = tmp_path / "openclaw.json"
    config.write_bytes(b"original\x00bytes\n")
    config.chmod(0o640)
    absent = tmp_path / "not-created-yet"
    manifest = setup_supervisor.create_backup([config, absent], tmp_path / "state", "alice")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["roots"][1] == {"exists": False, "path": str(absent)}

    config.write_bytes(b"enabled config")
    absent.write_text("new", encoding="utf-8")
    setup_supervisor.restore_backup(manifest)
    assert config.read_bytes() == b"original\x00bytes\n"
    assert stat.S_IMODE(config.stat().st_mode) == 0o640
    assert hashlib.sha256(config.read_bytes()).hexdigest() == payload["roots"][0]["sha256"]
    assert not absent.exists()
    backup_copy = manifest.parent / payload["roots"][0]["backup"]
    assert stat.S_IMODE(backup_copy.stat().st_mode) == 0o600


def test_backup_manifest_preserves_hostile_hex_filename_and_restores(tmp_path):
    filename = "0123456789abcdef" * 3
    config = tmp_path / filename
    config.write_bytes(b"original")
    manifest = setup_supervisor.create_backup([config], tmp_path / "state", "alice")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["roots"][0]["path"] == str(config)
    assert filename in manifest.read_text(encoding="utf-8")
    config.write_bytes(b"changed")
    setup_supervisor.restore_backup(manifest)
    assert config.read_bytes() == b"original"


def test_backup_rejects_symlinked_entries(tmp_path):
    root = tmp_path / "config"
    root.mkdir()
    target = tmp_path / "secret"
    target.write_text("secret", encoding="utf-8")
    (root / "linked").symlink_to(target)
    with pytest.raises(OSError, match="symlink"):
        setup_supervisor.create_backup([root], tmp_path / "state", "alice")

    broken = tmp_path / "broken-root"
    broken.symlink_to(tmp_path / "missing-target")
    with pytest.raises(OSError, match="symlink"):
        setup_supervisor.create_backup([broken], tmp_path / "state-2", "alice")


def test_absent_backup_restore_removes_new_symlink(tmp_path):
    target = tmp_path / "was-absent"
    manifest = setup_supervisor.create_backup([target], tmp_path / "state", "alice")
    target.symlink_to(tmp_path / "still-missing")
    assert os.path.lexists(target)
    setup_supervisor.restore_backup(manifest)
    assert not os.path.lexists(target)


def _seed_backup_generation(backup_root: pathlib.Path, name: str, created_at: float) -> pathlib.Path:
    generation = backup_root / name
    generation.mkdir(parents=True)
    (generation / "manifest.json").write_text(
        json.dumps({"version": 1, "created_at": created_at, "roots": []}),
        encoding="utf-8",
    )
    return generation


@pytest.mark.parametrize("existing_count", [0, 1, 3, 4])
def test_backup_gc_boundaries_keep_new_generation_and_latest_three(tmp_path, existing_count):
    state_dir = tmp_path / "state"
    backup_root = state_dir / "alice.brain-backup"
    backup_root.mkdir(parents=True)
    seeded = [
        _seed_backup_generation(
            backup_root, "20260804T00000%dZ-100-%d" % (index, index), float(index)
        )
        for index in range(existing_count)
    ]
    source = tmp_path / "brain.json"
    source.write_text("current", encoding="utf-8")

    manifest = setup_supervisor.create_backup([source], state_dir, "alice")

    managed = {
        entry
        for entry in backup_root.iterdir()
        if setup_supervisor._backup_generation_created_at(entry) is not None
    }
    assert manifest.parent in managed
    assert len(managed) == min(existing_count + 1, setup_supervisor.BACKUP_GENERATIONS_TO_KEEP)
    expected_older = set(seeded[-(setup_supervisor.BACKUP_GENERATIONS_TO_KEEP - 1) :])
    if existing_count < setup_supervisor.BACKUP_GENERATIONS_TO_KEEP:
        expected_older = set(seeded)
    assert managed == expected_older | {manifest.parent}


def test_backup_gc_ignores_symlink_foreign_file_and_corrupt_generation(tmp_path):
    state_dir = tmp_path / "state"
    backup_root = state_dir / "alice.brain-backup"
    backup_root.mkdir(parents=True)
    for index in range(3):
        _seed_backup_generation(
            backup_root, "20260804T00000%dZ-100-%d" % (index, index), float(index)
        )
    foreign = backup_root / "operator-note.txt"
    foreign.write_text("keep", encoding="utf-8")
    foreign_generation = _seed_backup_generation(backup_root, "operator-snapshot", 99.0)
    corrupt = backup_root / "20260804T000009Z-100-9"
    corrupt.mkdir()
    (corrupt / "manifest.json").write_text("not json", encoding="utf-8")
    link_target = tmp_path / "external"
    link_target.mkdir()
    (link_target / "keep").write_text("outside", encoding="utf-8")
    link = backup_root / "20260804T000010Z-100-10"
    link.symlink_to(link_target, target_is_directory=True)
    source = tmp_path / "brain.json"
    source.write_text("current", encoding="utf-8")

    manifest = setup_supervisor.create_backup([source], state_dir, "alice")

    assert manifest.is_file()
    assert foreign.read_text(encoding="utf-8") == "keep"
    assert foreign_generation.is_dir()
    assert corrupt.is_dir()
    assert link.is_symlink()
    assert (link_target / "keep").read_text(encoding="utf-8") == "outside"


def test_backup_gc_rejects_symlinked_member_root_without_touching_target(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    external = tmp_path / "external-backups"
    external.mkdir()
    marker = external / "keep"
    marker.write_text("outside", encoding="utf-8")
    (state_dir / "alice.brain-backup").symlink_to(external, target_is_directory=True)
    source = tmp_path / "brain.json"
    source.write_text("current", encoding="utf-8")

    with pytest.raises(OSError, match="must not be a symlink"):
        setup_supervisor.create_backup([source], state_dir, "alice")

    assert marker.read_text(encoding="utf-8") == "outside"
    assert list(external.iterdir()) == [marker]


def test_concurrent_backup_creation_is_serialized_and_retains_three(tmp_path):
    state_dir = tmp_path / "state"
    source = tmp_path / "brain.json"
    source.write_text("current", encoding="utf-8")
    manifests = []
    failures = []

    def create() -> None:
        try:
            manifests.append(setup_supervisor.create_backup([source], state_dir, "alice"))
        except Exception as error:  # surfaced below with all worker failures
            failures.append(error)

    threads = [threading.Thread(target=create) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not failures
    assert all(not thread.is_alive() for thread in threads)
    backup_root = state_dir / "alice.brain-backup"
    managed = [
        entry
        for entry in backup_root.iterdir()
        if setup_supervisor._backup_generation_created_at(entry) is not None
    ]
    assert len(manifests) == 8
    assert len(managed) == setup_supervisor.BACKUP_GENERATIONS_TO_KEEP
    assert set(managed).issubset({manifest.parent for manifest in manifests})


def test_happy_path_restarts_resumes_and_self_collect_scope(tmp_path, monkeypatch):
    config, manifest = _manifest(tmp_path)
    record = tmp_path / "systemctl.argv"
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    _write_exec(
        fakebin / "systemctl",
        """
        #!/usr/bin/env sh
        printf '%s\n' "$*" >> "$RECORD"
        exit 0
        """,
    )
    resume = tmp_path / "resume.py"
    resume.write_text("raise SystemExit(0)\n", encoding="utf-8")
    supervisor, status = _supervisor(
        tmp_path,
        manifest,
        resume,
        {"PATH": str(fakebin) + os.pathsep + os.environ.get("PATH", ""), "RECORD": str(record)},
    )
    monkeypatch.setattr(supervisor, "_assert_detached", lambda: None)
    monkeypatch.setattr(supervisor, "_wait_healthy", lambda: True)

    assert supervisor.run() == 0
    payload = json.loads(status.read_text(encoding="utf-8"))
    assert payload["state"] == "succeeded"
    assert payload["terminal"] is True
    invocations = record.read_text(encoding="utf-8")
    assert "--user restart openclaw.service" in invocations
    assert " enable " not in (" " + invocations + " ")
    assert not any(tmp_path.rglob("*.service"))
    assert config.read_bytes() == b"before\n"


def test_resume_argv_is_frozen_minimal_surface(tmp_path, monkeypatch):
    _config, manifest = _manifest(tmp_path)
    argv_record = tmp_path / "resume.argv.json"
    resume = tmp_path / "resume.py"
    resume.write_text(
        "import json, os, sys\n"
        "open(os.environ['ARGV_RECORD'], 'w').write(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    supervisor, _status = _supervisor(
        tmp_path, manifest, resume, {"ARGV_RECORD": str(argv_record)}
    )
    monkeypatch.setattr(supervisor, "_assert_detached", lambda: None)
    monkeypatch.setattr(
        supervisor,
        "_restart",
        lambda: type("R", (), {"returncode": 0, "stderr": ""})(),
    )
    monkeypatch.setattr(supervisor, "_wait_healthy", lambda: True)
    assert supervisor.run() == 0
    assert json.loads(argv_record.read_text(encoding="utf-8")) == [
        "--member", "alice", "--yes"
    ]
    timeout_action = next(
        action for action in setup_supervisor.Supervisor._parser()._actions
        if action.dest == "resume_timeout"
    )
    assert timeout_action.default > 3700


def test_resume_capture_is_continuously_drained_and_bounded(tmp_path):
    _config, manifest = _manifest(tmp_path)
    resume = tmp_path / "resume.py"
    resume.write_text(
        "import sys\n"
        "sys.stdout.write('x' * 1000000 + '\\nOUT-TAIL')\n"
        "sys.stderr.write('y' * 1000000 + '\\nERR-TAIL')\n",
        encoding="utf-8",
    )
    supervisor, _status = _supervisor(tmp_path, manifest, resume)
    result = supervisor._resume()
    assert result.returncode == 0
    assert len(result.stdout.encode("utf-8")) <= 16384
    assert len(result.stderr.encode("utf-8")) <= 16384
    assert result.stdout.endswith("OUT-TAIL")
    assert result.stderr.endswith("ERR-TAIL")
    assert not list((tmp_path / "state").glob("alice.resume-*.tmp"))


def test_resume_capture_keeps_truncated_partial_line_without_newline(tmp_path):
    _config, manifest = _manifest(tmp_path)
    resume = tmp_path / "resume.py"
    resume.write_text(
        "import sys\n"
        "sys.stdout.write('x' * 20000 + 'partial diagnostic tail')\n",
        encoding="utf-8",
    )
    supervisor, _status = _supervisor(tmp_path, manifest, resume)

    result = supervisor._resume()

    assert result.stdout.endswith("partial diagnostic tail")
    assert len(result.stdout.encode("utf-8")) == 16384


def test_resume_failure_restores_bytes_restarts_again_and_redacts_capture(tmp_path, monkeypatch):
    secret = "a" * 48
    config, manifest = _manifest(tmp_path, b"original\n")
    config.write_bytes(b"changed by enable\n")
    record = tmp_path / "systemctl.argv"
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    _write_exec(
        fakebin / "systemctl",
        """
        #!/usr/bin/env sh
        printf '%s\n' "$*" >> "$RECORD"
        exit 0
        """,
    )
    resume = tmp_path / "resume.py"
    resume.write_text("print(%r)\nraise SystemExit(7)\n" % secret, encoding="utf-8")
    supervisor, status = _supervisor(
        tmp_path,
        manifest,
        resume,
        {"PATH": str(fakebin) + os.pathsep + os.environ.get("PATH", ""), "RECORD": str(record)},
    )
    status.with_name("alice.json").write_text(
        json.dumps(
            {
                "schema_version": setup_supervisor.RESUME_SCHEMA_VERSION,
                "member": "alice",
                "backend_enable_done": True,
                "backup_manifest_path": str(manifest),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(supervisor, "_assert_detached", lambda: None)
    monkeypatch.setattr(supervisor, "_wait_healthy", lambda: True)

    assert supervisor.run() == 1
    assert config.read_bytes() == b"original\n"
    assert record.read_text(encoding="utf-8").count("restart openclaw.service") == 2
    status_text = status.read_text(encoding="utf-8")
    assert json.loads(status_text)["state"] == "rolled-back"
    assert secret not in status_text
    assert "[REDACTED]" in status_text
    message = json.loads(status_text)["message"]
    assert "brain configuration was restored" in message
    assert "backend was restarted back onto the old configuration" in message
    assert "restart the flow from the backend phase" in message


def test_resume_failure_after_backend_phase_keeps_enable_change_and_does_not_restart_again(
    tmp_path, monkeypatch
):
    config, manifest = _manifest(tmp_path, b"original\n")
    config.write_bytes(b"enabled\n")
    record = tmp_path / "systemctl.argv"
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    _write_exec(
        fakebin / "systemctl",
        """
        #!/usr/bin/env sh
        printf '%s\n' "$*" >> "$RECORD"
        exit 0
        """,
    )
    status = tmp_path / "state" / "alice.status.json"
    state = status.with_name("alice.json")
    resume = tmp_path / "resume.py"
    resume.write_text(
        "import json, pathlib\n"
        "pathlib.Path(%r).write_text(json.dumps({"
        "'schema_version': %d, 'member': 'alice', 'backend_enable_done': False, "
        "'backup_manifest_path': ''}))\n"
        "raise SystemExit(7)\n" % (str(state), setup_supervisor.RESUME_SCHEMA_VERSION),
        encoding="utf-8",
    )
    supervisor, status = _supervisor(
        tmp_path,
        manifest,
        resume,
        {"PATH": str(fakebin) + os.pathsep + os.environ.get("PATH", ""), "RECORD": str(record)},
    )
    monkeypatch.setattr(supervisor, "_assert_detached", lambda: None)
    monkeypatch.setattr(supervisor, "_wait_healthy", lambda: True)

    assert supervisor.run() == 1
    assert config.read_bytes() == b"enabled\n"
    assert record.read_text(encoding="utf-8").count("restart openclaw.service") == 1
    payload = json.loads(status.read_text(encoding="utf-8"))
    assert payload["state"] == "failed"
    assert "backend is healthy" in payload["message"]
    assert "enable change is kept" in payload["message"]
    assert "resumes from the failed phase" in payload["message"]


def test_empty_manifest_failure_never_claims_configuration_restoration(tmp_path, monkeypatch):
    manifest = setup_supervisor.create_backup([], tmp_path / "state", "alice")
    resume = tmp_path / "resume.py"
    resume.write_text("raise SystemExit(9)\n", encoding="utf-8")
    supervisor, status = _supervisor(tmp_path, manifest, resume)
    monkeypatch.setattr(supervisor, "_assert_detached", lambda: None)
    restarts = []
    monkeypatch.setattr(
        supervisor,
        "_restart",
        lambda: restarts.append(True) or type("R", (), {"returncode": 0, "stderr": ""})(),
    )
    monkeypatch.setattr(supervisor, "_wait_healthy", lambda: True)

    assert supervisor.run() == 1
    assert len(restarts) == 2
    payload = json.loads(status.read_text(encoding="utf-8"))
    assert payload["state"] == "failed"
    assert "no brain configuration was declared for backup, so nothing was restored" in payload["message"]
    assert "brain configuration was restored" not in payload["message"]


def test_declared_absent_manifest_resume_failure_removes_created_file_and_reports_rolled_back(
    tmp_path, monkeypatch
):
    config, manifest = _declared_absent_manifest(tmp_path)
    config.write_text("created by enable\n", encoding="utf-8")
    resume = tmp_path / "resume.py"
    resume.write_text("raise SystemExit(9)\n", encoding="utf-8")
    supervisor, status = _supervisor(tmp_path, manifest, resume)
    monkeypatch.setattr(supervisor, "_assert_detached", lambda: None)
    restarts = []
    monkeypatch.setattr(
        supervisor,
        "_restart",
        lambda: restarts.append(True) or type("R", (), {"returncode": 0, "stderr": ""})(),
    )
    monkeypatch.setattr(supervisor, "_wait_healthy", lambda: True)

    assert supervisor.run() == 1
    assert len(restarts) == 2
    assert not config.exists()
    payload = json.loads(status.read_text(encoding="utf-8"))
    assert payload["state"] == "rolled-back"
    assert "declared brain configuration paths had no pre-enable content" in payload["message"]
    assert "enable-created files were removed" in payload["message"]
    assert "nothing was restored" not in payload["message"]
    assert payload["recovery_pointer"].startswith("Rerun the same setup command")


def test_resume_capture_drops_secret_fragment_from_evicted_partial_line(tmp_path, monkeypatch):
    hex_canary = "0123456789abcdef" * 3
    config, manifest = _manifest(tmp_path)
    config.write_bytes(b"enabled")
    resume = tmp_path / "resume.py"
    suffix = "\nsafe diagnostic\n" + ("q" * 16343)
    resume.write_text(
        "import sys\n"
        "sys.stdout.write(%r + %r + %r)\n"
        "raise SystemExit(8)\n" % ("p" * 20, hex_canary, suffix),
        encoding="utf-8",
    )
    supervisor, status = _supervisor(tmp_path, manifest, resume)
    monkeypatch.setattr(supervisor, "_assert_detached", lambda: None)
    monkeypatch.setattr(
        supervisor,
        "_restart",
        lambda: type("R", (), {"returncode": 0, "stderr": ""})(),
    )
    monkeypatch.setattr(supervisor, "_wait_healthy", lambda: True)

    assert supervisor.run() == 1
    captured = json.loads(status.read_text(encoding="utf-8"))["resume_output"]
    assert hex_canary not in captured
    assert not re.search(r"[0-9a-f]{9,}", captured)
    assert "safe diagnostic" in captured


def test_double_failure_writes_private_actionable_recovery(tmp_path, monkeypatch):
    secret = "b" * 48
    _config, manifest = _manifest(tmp_path)
    resume = tmp_path / "resume.py"
    resume.write_text("raise SystemExit(9)\n", encoding="utf-8")
    supervisor, status = _supervisor(tmp_path, manifest, resume)
    monkeypatch.setattr(supervisor, "_assert_detached", lambda: None)
    monkeypatch.setattr(supervisor, "_restart", lambda: type("R", (), {"returncode": 0, "stderr": ""})())
    monkeypatch.setattr(supervisor, "_wait_healthy", lambda: True)
    monkeypatch.setattr(
        setup_supervisor,
        "restore_backup",
        lambda _path: (_ for _ in ()).throw(PermissionError("blocked " + secret)),
    )

    assert supervisor.run() == 2
    payload = json.loads(status.read_text(encoding="utf-8"))
    assert payload["state"] == "double-failure"
    recovery = pathlib.Path(payload["recovery_pointer"])
    recovery_text = recovery.read_text(encoding="utf-8")
    assert str(manifest.parent) in recovery_text
    original_mode = json.loads(manifest.read_text(encoding="utf-8"))["roots"][0]["mode"]
    assert "install -m %04o" % original_mode in recovery_text
    assert "openclaw.service" in recovery_text
    assert "journalctl --user -u openclaw.service" in recovery_text
    assert secret not in recovery_text
    assert stat.S_IMODE(recovery.stat().st_mode) == 0o600


def test_recovery_commands_stage_restrictive_directories_then_restore_modes(tmp_path):
    config = tmp_path / "brain config"
    config.mkdir()
    nested_dir = config / "nested config"
    nested_dir.mkdir()
    nested = nested_dir / "member file.json"
    nested.write_bytes(b"original bytes")
    nested.chmod(0o640)
    nested_dir.chmod(0o500)
    config.chmod(0o500)
    manifest = setup_supervisor.create_backup([config], tmp_path / "state", "alice")
    resume = tmp_path / "resume.py"
    resume.write_text("raise SystemExit(1)\n", encoding="utf-8")
    supervisor, _status = _supervisor(tmp_path, manifest, resume)

    config.chmod(0o700)
    nested_dir.chmod(0o700)
    nested.write_bytes(b"changed")
    nested.chmod(0o600)
    recovery = supervisor._recovery_text("manual test")
    text = recovery.read_text(encoding="utf-8")
    assert "# Recovery commands for " in text
    assert "install -d -m 0700" in text
    assert "install -m 0640" in text
    assert "chmod 0500" in text
    assert shlex.quote(str(nested)) in text
    assert "openclaw.service" in text
    assert "journalctl --user -u openclaw.service" in text

    lines = text.splitlines()
    section_start = next(index for index, line in enumerate(lines) if line.startswith("# Recovery commands for "))
    section_end = lines.index("Restart target: openclaw.service")
    commands = lines[section_start:section_end]
    assert commands[0].startswith("# ")
    file_install = next(index for index, line in enumerate(commands) if line.startswith("install -m 0640"))
    nested_chmod = commands.index("chmod 0500 %s" % shlex.quote(str(nested_dir)))
    root_chmod = commands.index("chmod 0500 %s" % shlex.quote(str(config)))
    assert file_install < nested_chmod < root_chmod

    result = subprocess.run(
        ["sh", "-eu", "-c", "\n".join(commands)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert nested.read_bytes() == b"original bytes"
    assert stat.S_IMODE(nested.stat().st_mode) == 0o640
    assert stat.S_IMODE(nested_dir.stat().st_mode) == 0o500
    assert stat.S_IMODE(config.stat().st_mode) == 0o500

    # Leave pytest's temporary tree removable.
    config.chmod(0o700)
    nested_dir.chmod(0o700)


def test_detachment_assertion_aborts_before_restart(tmp_path, monkeypatch):
    _config, manifest = _manifest(tmp_path)
    resume = tmp_path / "resume.py"
    resume.write_text("raise SystemExit(0)\n", encoding="utf-8")
    supervisor, status = _supervisor(tmp_path, manifest, resume)
    monkeypatch.setattr(supervisor, "_own_unit", lambda: "openclaw.service")
    restarted = []
    monkeypatch.setattr(supervisor, "_restart", lambda: restarted.append(True))
    assert supervisor.run() == 1
    assert restarted == []
    payload = json.loads(status.read_text(encoding="utf-8"))
    assert payload["state"] == "rolled-back"
    assert "before restart" in payload["message"]
    assert "rolled back" in payload["message"]
    assert payload["recovery_pointer"].startswith("Rerun the same setup command")


def test_detachment_failure_with_empty_manifest_says_nothing_was_restored(tmp_path, monkeypatch):
    manifest = setup_supervisor.create_backup([], tmp_path / "state", "alice")
    resume = tmp_path / "resume.py"
    resume.write_text("raise SystemExit(0)\n", encoding="utf-8")
    supervisor, status = _supervisor(tmp_path, manifest, resume)
    monkeypatch.setattr(supervisor, "_own_unit", lambda: "openclaw.service")
    assert supervisor.run() == 1
    payload = json.loads(status.read_text(encoding="utf-8"))
    assert payload["state"] == "failed"
    assert "no brain configuration was declared for backup, so nothing was restored" in payload["message"]
    assert "rolled back" not in payload["message"]


def test_detachment_failure_with_declared_absent_manifest_reports_rolled_back(tmp_path, monkeypatch):
    config, manifest = _declared_absent_manifest(tmp_path)
    config.write_text("created by enable\n", encoding="utf-8")
    resume = tmp_path / "resume.py"
    resume.write_text("raise SystemExit(0)\n", encoding="utf-8")
    supervisor, status = _supervisor(tmp_path, manifest, resume)
    monkeypatch.setattr(supervisor, "_own_unit", lambda: "openclaw.service")

    assert supervisor.run() == 1
    assert not config.exists()
    payload = json.loads(status.read_text(encoding="utf-8"))
    assert payload["state"] == "rolled-back"
    assert "declared brain configuration paths had no pre-enable content" in payload["message"]
    assert "enable-created files were removed" in payload["message"]
    assert "nothing was restored" not in payload["message"]
    assert payload["recovery_pointer"].startswith("Rerun the same setup command")


def test_parent_identity_mismatch_rolls_back_before_restart(tmp_path, monkeypatch):
    config, manifest = _manifest(tmp_path)
    config.write_bytes(b"enabled")
    resume = tmp_path / "resume.py"
    resume.write_text("raise SystemExit(0)\n", encoding="utf-8")
    supervisor, status = _supervisor(tmp_path, manifest, resume)
    supervisor.args.parent_pid = os.getpid()
    supervisor.args.parent_start_time = "definitely-wrong"
    monkeypatch.setattr(supervisor, "_assert_detached", lambda: None)
    restarted = []
    monkeypatch.setattr(supervisor, "_restart", lambda: restarted.append(True))
    assert supervisor.run() == 1
    assert restarted == []
    assert config.read_bytes() == b"before\n"
    payload = json.loads(status.read_text(encoding="utf-8"))
    assert payload["state"] == "rolled-back"
    assert "parent identity changed" in payload["message"]
    assert payload["terminal"] is True
    assert payload["recovery_pointer"].startswith("Rerun the same setup command")


def test_corrupt_backup_aborts_before_first_restart_with_recovery(tmp_path, monkeypatch):
    _config, manifest = _manifest(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    backup = manifest.parent / payload["roots"][0]["backup"]
    backup.write_bytes(b"corrupted")
    resume = tmp_path / "resume.py"
    resume.write_text("raise SystemExit(0)\n", encoding="utf-8")
    supervisor, status = _supervisor(tmp_path, manifest, resume)
    monkeypatch.setattr(supervisor, "_assert_detached", lambda: None)
    monkeypatch.setattr(supervisor, "_inherit_parent_environment", lambda: None)
    restarted = []
    monkeypatch.setattr(supervisor, "_restart", lambda: restarted.append(True))
    assert supervisor.run() == 2
    assert restarted == []
    current = json.loads(status.read_text(encoding="utf-8"))
    assert current["state"] == "double-failure"
    assert pathlib.Path(current["recovery_pointer"]).is_file()


def test_supervisor_terminal_status_clears_stale_fields(tmp_path):
    _config, manifest = _manifest(tmp_path)
    resume = tmp_path / "resume.py"
    resume.write_text("raise SystemExit(0)\n", encoding="utf-8")
    supervisor, status = _supervisor(tmp_path, manifest, resume)
    setup_supervisor.update_status(
        status,
        qr_url="http://old/qr",
        expires_at="old",
        recovery_pointer="old",
        supervisor_pid=4100,
        supervisor_start_time="supervisor-start",
        orchestrator_pid=4200,
        orchestrator_start_time="orchestrator-start",
    )
    supervisor._status("succeeded", message="done")
    payload = json.loads(status.read_text(encoding="utf-8"))
    assert "qr_url" not in payload
    assert "expires_at" not in payload
    assert "recovery_pointer" not in payload
    assert "supervisor_pid" not in payload
    assert "supervisor_start_time" not in payload
    assert "orchestrator_pid" not in payload
    assert "orchestrator_start_time" not in payload


def test_concurrent_status_updates_preserve_both_owner_pairs(tmp_path, monkeypatch):
    status = tmp_path / "state/alice.status.json"
    first_writer_inside_atomic = threading.Event()
    release_first_writer = threading.Event()
    original_atomic_write = setup_supervisor.atomic_write_json
    errors = []

    def block_first_atomic_write(path, payload, **kwargs):
        if payload.get("supervisor_pid") == 4100 and "orchestrator_pid" not in payload:
            first_writer_inside_atomic.set()
            if not release_first_writer.wait(timeout=5):
                raise AssertionError("second status writer did not exercise the locked overlap")
        return original_atomic_write(path, payload, **kwargs)

    def write(**changes):
        try:
            setup_supervisor.update_status(status, **changes)
        except Exception as error:  # pragma: no cover - asserted below
            errors.append(error)

    monkeypatch.setattr(setup_supervisor, "atomic_write_json", block_first_atomic_write)
    supervisor_writer = threading.Thread(
        target=write,
        kwargs={"supervisor_pid": 4100, "supervisor_start_time": "supervisor-start"},
    )
    orchestrator_writer = threading.Thread(
        target=write,
        kwargs={"orchestrator_pid": 4200, "orchestrator_start_time": "orchestrator-start"},
    )
    supervisor_writer.start()
    assert first_writer_inside_atomic.wait(timeout=5)
    orchestrator_writer.start()
    assert orchestrator_writer.is_alive()
    release_first_writer.set()
    supervisor_writer.join(timeout=5)
    orchestrator_writer.join(timeout=5)

    assert not errors
    assert not supervisor_writer.is_alive()
    assert not orchestrator_writer.is_alive()
    payload = json.loads(status.read_text(encoding="utf-8"))
    assert payload["supervisor_pid"] == 4100
    assert payload["supervisor_start_time"] == "supervisor-start"
    assert payload["orchestrator_pid"] == 4200
    assert payload["orchestrator_start_time"] == "orchestrator-start"


def test_schema_version_alias_matches_orchestrator():
    from caty_gateway import setup_orchestrator

    assert setup_supervisor.RESUME_SCHEMA_VERSION == setup_supervisor.SCHEMA_VERSION
    assert setup_supervisor.SCHEMA_VERSION == setup_orchestrator.SCHEMA_VERSION


def test_valid_args_unexpected_failure_attempts_terminal_status(tmp_path, monkeypatch):
    _config, manifest = _manifest(tmp_path)
    resume = tmp_path / "resume.py"
    resume.write_text("raise SystemExit(0)\n", encoding="utf-8")
    supervisor, status = _supervisor(tmp_path, manifest, resume)
    monkeypatch.setattr(
        setup_supervisor,
        "Supervisor",
        lambda _argv=None: supervisor,
    )
    monkeypatch.setattr(
        supervisor,
        "run",
        lambda: (_ for _ in ()).throw(RuntimeError("unexpected")),
    )
    assert setup_supervisor.main([]) == 2
    payload = json.loads(status.read_text(encoding="utf-8"))
    assert payload["state"] == "failed"
    assert payload["terminal"] is True


def test_sigterm_records_terminal_status(tmp_path):
    _config, manifest = _manifest(tmp_path)
    resume = tmp_path / "resume.py"
    resume.write_text("raise SystemExit(0)\n", encoding="utf-8")
    supervisor, status = _supervisor(tmp_path, manifest, resume)
    with pytest.raises(SystemExit) as raised:
        supervisor._sigterm(None, None)
    assert raised.value.code == 143
    assert json.loads(status.read_text(encoding="utf-8"))["state"] == "interrupted"


def test_sigterm_during_resume_kills_child_before_terminal_status(tmp_path, monkeypatch):
    _config, manifest = _manifest(tmp_path)
    pid_file = tmp_path / "resume.pid"
    resume = tmp_path / "resume.py"
    resume.write_text(
        "import os, pathlib, time\n"
        "pathlib.Path(%r).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n" % str(pid_file),
        encoding="utf-8",
    )
    supervisor, status = _supervisor(tmp_path, manifest, resume)
    monkeypatch.setattr(supervisor, "_assert_detached", lambda: None)
    monkeypatch.setattr(
        supervisor,
        "_restart",
        lambda: type("R", (), {"returncode": 0, "stderr": ""})(),
    )
    monkeypatch.setattr(supervisor, "_wait_healthy", lambda: True)

    def terminate_when_started():
        deadline = time.time() + 5
        while time.time() < deadline and not pid_file.exists():
            time.sleep(0.01)
        if pid_file.exists():
            os.kill(os.getpid(), signal.SIGTERM)

    previous = signal.getsignal(signal.SIGTERM)
    sender = threading.Thread(target=terminate_when_started, daemon=True)
    sender.start()
    try:
        with pytest.raises(SystemExit) as raised:
            supervisor.run()
    finally:
        signal.signal(signal.SIGTERM, previous)
    sender.join(timeout=1)
    assert raised.value.code == 143
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    with pytest.raises(OSError):
        os.kill(child_pid, 0)
    payload = json.loads(status.read_text(encoding="utf-8"))
    assert payload["state"] == "interrupted"
    assert payload["terminal"] is True


def test_resume_unblocks_sigterm_before_orchestrator_and_supervised_qr(tmp_path):
    _config, manifest = _manifest(tmp_path)
    orchestrator_mask = tmp_path / "orchestrator-mask.json"
    qr_mask = tmp_path / "qr-mask.json"
    qr = tmp_path / "qr-mask-probe.py"
    qr.write_text(
        "import json, os, pathlib, signal\n"
        "blocked = signal.pthread_sigmask(signal.SIG_BLOCK, set())\n"
        "pathlib.Path(os.environ['QR_MASK_PATH']).write_text(\n"
        "    json.dumps(signal.SIGTERM in blocked), encoding='utf-8'\n"
        ")\n",
        encoding="utf-8",
    )
    resume = tmp_path / "resume-mask-probe.py"
    resume.write_text(
        "import json, os, pathlib, signal, sys\n"
        "from caty_gateway import setup_orchestrator\n"
        "def run(self):\n"
        "    blocked = signal.pthread_sigmask(signal.SIG_BLOCK, set())\n"
        "    pathlib.Path(os.environ['ORCHESTRATOR_MASK_PATH']).write_text(\n"
        "        json.dumps(signal.SIGTERM in blocked), encoding='utf-8'\n"
        "    )\n"
        "    return self._run_supervised_qr(\n"
        "        [sys.executable, os.environ['QR_SCRIPT']], self.env\n"
        "    )\n"
        "setup_orchestrator.SetupOrchestrator.run = run\n"
        "raise SystemExit(setup_orchestrator.main())\n",
        encoding="utf-8",
    )
    supervisor, _status = _supervisor(
        tmp_path,
        manifest,
        resume,
        extra_env={
            "GATEWAY_DIR": str(pathlib.Path(setup_supervisor.__file__).resolve().parent),
            "ORCHESTRATOR_MASK_PATH": str(orchestrator_mask),
            "QR_MASK_PATH": str(qr_mask),
            "QR_SCRIPT": str(qr),
        },
    )

    result = supervisor._resume()

    assert result.returncode == 0, result.stderr
    assert {
        "orchestrator": json.loads(orchestrator_mask.read_text(encoding="utf-8")),
        "qr": json.loads(qr_mask.read_text(encoding="utf-8")),
    } == {"orchestrator": False, "qr": False}


def test_sigterm_between_resume_spawn_and_assignment_kills_child(tmp_path, monkeypatch):
    _config, manifest = _manifest(tmp_path)
    resume = tmp_path / "resume.py"
    resume.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    supervisor, status = _supervisor(tmp_path, manifest, resume)
    monkeypatch.setattr(supervisor, "_assert_detached", lambda: None)
    monkeypatch.setattr(
        supervisor,
        "_restart",
        lambda: type("R", (), {"returncode": 0, "stderr": ""})(),
    )
    monkeypatch.setattr(supervisor, "_wait_healthy", lambda: True)
    original_popen = setup_supervisor.subprocess.Popen
    spawned = []

    def terminate_before_return(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        spawned.append(process)
        os.kill(os.getpid(), signal.SIGTERM)
        return process

    monkeypatch.setattr(setup_supervisor.subprocess, "Popen", terminate_before_return)
    previous = signal.getsignal(signal.SIGTERM)
    try:
        with pytest.raises(SystemExit) as raised:
            supervisor.run()
    finally:
        signal.signal(signal.SIGTERM, previous)
        for process in spawned:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    assert raised.value.code == 143
    assert len(spawned) == 1
    assert spawned[0].poll() is not None
    payload = json.loads(status.read_text(encoding="utf-8"))
    assert payload["state"] == "interrupted"
