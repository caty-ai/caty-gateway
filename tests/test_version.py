"""Keep the public package version aligned with distribution metadata."""

import importlib
import importlib.metadata
from pathlib import Path
import re
import sys

import caty_gateway


ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    if sys.version_info >= (3, 11):
        import tomllib

        return tomllib.loads(text)["project"]["version"]

    in_project = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_project = stripped == "[project]"
        elif in_project:
            match = re.match(r'^version\s*=\s*"([^"]+)"', line)
            if match:
                return match.group(1)
    raise AssertionError("No version found in pyproject.toml [project]")


def test_version_matches_pyproject():
    assert caty_gateway.__version__ == _pyproject_version()


def test_version_fallback_when_dist_missing(monkeypatch):
    def missing_version(distribution_name):
        raise importlib.metadata.PackageNotFoundError(distribution_name)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(importlib.metadata, "version", missing_version)
            importlib.reload(caty_gateway)
            assert caty_gateway.__version__ == "0+unknown"
    finally:
        importlib.reload(caty_gateway)
    assert caty_gateway.__version__ == _pyproject_version()


def test_no_hardcoded_version_outside_pyproject():
    matches = []
    for directory in (ROOT / "src", ROOT / "tools"):
        for path in sorted(directory.rglob("*")):
            if not path.is_file():
                continue
            data = path.read_bytes()
            if b"\x00" in data:
                continue  # Binary files are outside this text-only check.
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            for line_number, line in enumerate(text.splitlines(), 1):
                if re.search(r"0\.1\.", line):
                    matches.append(f"{path.relative_to(ROOT)}:{line_number}:{line}")
    assert not matches, "Hardcoded version strings found:\n" + "\n".join(matches)
