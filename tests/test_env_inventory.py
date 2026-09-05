"""AST inventory and drift checks must work without importing gateway code."""

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "env-inventory.py"
spec = importlib.util.spec_from_file_location("env_inventory", SCRIPT)
inventory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inventory)


def write_source(tmp_path, text):
    source = tmp_path / "src" / "caty_gateway" / "fake_env.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(text, encoding="utf-8")
    return source


def run_inventory(tmp_path, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path), *args],
        text=True, capture_output=True, check=False,
    )


def test_supported_ast_forms_sorted_unique_and_never_imported(tmp_path):
    write_source(tmp_path, '''
raise RuntimeError("inventory must not execute source")
os.environ.get("CATY_NAME", "Caty")
os.getenv("CATY_TOKEN")
os.environ["CATY_GATEWAY_PORT"]
environ.get("CATY_ID", "caty")
getenv("CATY_BACKEND", "openclaw")
env.get("CATY_REQUIRE_AUTH", "1")
self.env.get("CATY_NAME", "Member")
env["CATY_PUBLIC_URL"]
self.env["CATY_LANG"]
environ["CATY_ACCENT_COLOR"]
os.environ.get("CATY_NAME", "Caty")
os.environ["CATY_FAKE_WRITE"] = "not a read"
config.get("CATY_FAKE_CONFIG")
os.environ.get(dynamic_name)
''')
    result = inventory.collect(tmp_path)
    assert set(result) == {
        "CATY_NAME", "CATY_TOKEN", "CATY_GATEWAY_PORT", "CATY_ID", "CATY_BACKEND",
        "CATY_REQUIRE_AUTH", "CATY_PUBLIC_URL", "CATY_LANG", "CATY_ACCENT_COLOR",
    }
    assert result["CATY_NAME"]["defaults"] == {"'Caty'", "'Member'"}
    assert len(result["CATY_NAME"]["where"]) == 3
    assert result["CATY_GATEWAY_PORT"]["defaults"] == {"required"}
    assert result["CATY_TOKEN"]["defaults"] == {"None (unset)"}
    rendered = inventory.render(result)
    names = [line.split("|")[1].strip() for line in rendered.splitlines() if line.startswith("| CATY_")]
    assert names == sorted(set(names))
    assert "src/caty_gateway/fake_env.py:3" in rendered


def test_env_helpers_include_pairing_and_voice_defaults(tmp_path):
    write_source(tmp_path, '''
def positive(name, default):
    raw = os.environ.get(name)
    return default if raw is None else int(raw)
def truthy(name, default=False):
    return os.environ.get(name, default)
def unrelated(name, default):
    return config.get(name, default)
positive("CATY_PAIRING_TTL_SECONDS", 600)
positive(name="CATY_VOICE_PREVIEW_CACHE_ENTRIES", default=32)
truthy("CATY_REQUIRE_AUTH")
unrelated("CATY_FAKE_CONFIG", True)
''')
    result = inventory.collect(tmp_path)
    assert result["CATY_PAIRING_TTL_SECONDS"]["defaults"] == {"600"}
    assert result["CATY_VOICE_PREVIEW_CACHE_ENTRIES"]["defaults"] == {"32"}
    assert result["CATY_REQUIRE_AUTH"]["defaults"] == {"False"}
    assert "CATY_FAKE_CONFIG" not in result
    assert inventory.METADATA["CATY_PAIRING_TTL_SECONDS"][0] == "B2"


def test_generate_check_and_fake_lookup_drift(tmp_path):
    source = write_source(tmp_path, 'os.environ.get("CATY_NAME", "Caty")\n')
    assert run_inventory(tmp_path).returncode == 0
    assert run_inventory(tmp_path, "--check").returncode == 0
    original = source.read_text()
    source.write_text(original + 'os.environ.get("CATY_FAKE_X")\n')
    result = run_inventory(tmp_path, "--check")
    assert result.returncode == 1
    assert "drift" in result.stderr
    assert "CATY_FAKE_X" in result.stderr
    assert "unclassified" in result.stderr
    # Merely regenerating the document cannot approve a new runtime variable.
    assert run_inventory(tmp_path).returncode == 0
    assert run_inventory(tmp_path, "--check").returncode == 1
    source.write_text(original)
    assert run_inventory(tmp_path).returncode == 0
    assert run_inventory(tmp_path, "--check").returncode == 0


def test_defaults_and_manual_document_edits_are_detected(tmp_path):
    source = write_source(tmp_path, 'os.environ.get("CATY_NAME", "Caty")\n')
    assert run_inventory(tmp_path).returncode == 0
    source.write_text('os.environ.get("CATY_NAME", "Member")\n')
    assert run_inventory(tmp_path, "--check").returncode == 1
    assert run_inventory(tmp_path).returncode == 0
    document = tmp_path / "docs" / "env.md"
    document.write_text(document.read_text() + "hand edited\n")
    assert run_inventory(tmp_path, "--check").returncode == 1


def test_output_file_stdout_and_missing_document(tmp_path):
    write_source(tmp_path, 'os.environ.get("CATY_TOKEN", "")\n')
    assert run_inventory(tmp_path, "--check").returncode == 1
    target = tmp_path / "generated" / "inventory.md"
    assert run_inventory(tmp_path, "--output", str(target)).returncode == 0
    assert run_inventory(tmp_path, "--output", str(target), "--check").returncode == 0
    stdout = run_inventory(tmp_path, "--output", "-")
    assert stdout.returncode == 0
    assert stdout.stdout == target.read_text()
    assert run_inventory(tmp_path, "--output", "-", "--check").returncode == 2


@pytest.mark.parametrize("source", ["value = 1\n", "this is invalid Python\n"])
def test_empty_inventory_or_syntax_error_fails_without_traceback(tmp_path, source):
    write_source(tmp_path, source)
    result = run_inventory(tmp_path, "--check")
    assert result.returncode == 1
    assert "Traceback" not in result.stderr


def test_missing_source_fails_without_creating_document(tmp_path):
    result = run_inventory(tmp_path)
    assert result.returncode == 2
    assert "source directory missing" in result.stderr
    assert not (tmp_path / "docs" / "env.md").exists()


def test_expression_defaults_and_markdown_escaping(tmp_path):
    write_source(tmp_path, '''
os.environ.get("CATY_CLAUDE_CWD", os.path.expanduser("~"))
os.environ.get("CATY_NAME", "fake|name`&")
''')
    result = inventory.render(inventory.collect(tmp_path))
    assert "expression: os.path.expanduser('~')" in result
    assert "fake&#124;name&#96;&amp;" in result


def test_metadata_explicit_classification_and_secret_policy():
    names = [name for group in inventory.TIER_NAMES.values() for name in group.split()]
    assert len(names) == len(set(names))
    assert inventory.METADATA["CATY_TOKEN"] == ("A", "secret", "member env (0600)")
    assert inventory.METADATA["CATY_UNSAFE_DEBUG_LOG_CONTENT"][2] == "process-only"
    assert inventory.METADATA["CATY_SETUP_SUPERVISED"][0] == "D"
    assert inventory.METADATA["CATY_OPENAI_BASE_URL"][0] == "B"
    assert "CATY_FAKE_X" not in inventory.METADATA
