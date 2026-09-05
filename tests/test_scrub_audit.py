"""Exercise the shell entry point with isolated scrub inputs."""

import os
from pathlib import Path
import subprocess


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "scrub-audit.sh"
ABSENT = "scrub-audit: private list not loaded (0 entries) — public rules only"


def audit(root, private_file=None):
    (root / ".scrub-allow").write_text("", encoding="utf-8")
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env.pop("SCRUB_PRIVATE_FILE", None)
    if private_file is not None:
        env["SCRUB_PRIVATE_FILE"] = str(private_file)
    return subprocess.run(
        ["bash", str(SCRIPT), str(root)], env=env,
        capture_output=True, text=True, check=False,
    )


def test_without_private_file(tmp_path):
    (tmp_path / "scratch.txt").write_text("ordinary text\n", encoding="utf-8")
    result = audit(tmp_path)
    assert result.returncode == 0
    assert result.stdout.count(ABSENT) == 1
    assert "scrub-audit: 0 findings" in result.stdout
    assert result.stderr == ""


def test_private_redaction(tmp_path):
    name, literal = "ExamplePerson", "/home/example-private"
    private_file = tmp_path / ".scrub-private"
    private_file.write_text(
        f"# local detectors\n\n[names]\n{name}\n[literals]\n{literal}\n[stems]\n[repos]\n",
        encoding="utf-8",
    )
    scratch = tmp_path / "scratch.txt"
    scratch.write_text(f"before {name} and {literal} then {name.upper()} after\n", encoding="utf-8")
    for override in (None, tmp_path / "local-detectors.txt"):
        if override is not None:
            private_file.rename(override)
        result = audit(tmp_path, override)
        assert result.returncode == 1
        assert "scrub-audit: private list loaded (2 entries)" in result.stdout
        assert "private-literal: scratch.txt:1: before [redacted] and [redacted] then [redacted] after" in result.stdout
        assert "scrub-audit: 1 findings" in result.stdout
        for entry in (name, literal):
            assert entry.lower() not in (result.stdout + result.stderr).lower()
        if override is not None:
            override.rename(private_file)
    scratch.write_text(f"prefix{name}suffix\n", encoding="utf-8")
    assert audit(tmp_path).returncode == 0
    png = tmp_path / f"{name}.png"
    png.write_bytes(b"invalid png")
    result = audit(tmp_path)
    assert result.returncode == 1
    assert "png-invalid: [redacted].png:" in result.stdout
    assert name not in result.stdout + result.stderr
    png.unlink()
    entries = ("検査用名", " abc", "bcd ", "example-org/private-repo")
    private_file.write_text(
        "\n".join(f"[{section}]\n{entry}" for section, entry in
                  zip(("names", "literals", "stems", "repos"), entries)), encoding="utf-8",
    )
    token = "gh" + "p_" + "B" * 20
    scratch.write_text(f"前{entries[0]}後 abcd {entries[3]} {token}\n", encoding="utf-8")
    result = audit(tmp_path)
    assert result.returncode == 1
    assert "private list loaded (4 entries)" in result.stdout
    assert "private-literal:" in result.stdout and "secret-prefix:" in result.stdout
    assert "前[redacted]後[redacted][redacted]" in result.stdout
    assert all(entry not in result.stdout + result.stderr for entry in entries)


def test_public_secret_without_private_file(tmp_path):
    token = "gh" + "p_" + "A" * 20
    (tmp_path / "scratch.txt").write_text(token + "\n", encoding="utf-8")
    result = audit(tmp_path)
    assert result.returncode == 1
    assert result.stdout.count(ABSENT) == 1
    assert f"secret-prefix: scratch.txt:1: {token}" in result.stdout


def test_malformed_private_file_fails_closed_without_echo(tmp_path):
    (tmp_path / "scratch.txt").write_text("ordinary text\n", encoding="utf-8")
    private = tmp_path.parent / "private-malformed.txt"
    private.write_text("secret-entry-before-header\n[names]\nzzz-private-name\n", encoding="utf-8")
    result = audit(tmp_path, private_file=private)
    assert result.returncode == 2
    assert "invalid private list" in result.stderr
    assert "secret-entry-before-header" not in result.stdout + result.stderr
    assert "zzz-private-name" not in result.stdout + result.stderr
    assert ABSENT not in result.stdout
