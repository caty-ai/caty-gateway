#!/usr/bin/env bash
set -euo pipefail

root="${1:-.}"
python3 - "$root" <<'PY'
from __future__ import annotations
import ast
import os
from pathlib import Path
import re
import struct
import subprocess
import sys

root = Path(sys.argv[1]).resolve()
findings = []
binary_suffixes = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".whl", ".gz"}
skip_parts = {".git", ".venv", ".venv-check", ".pytest_cache", "__pycache__", "dist", "build-check"}

def split(*parts):
    return "".join(parts)

def logical_rel(rel):
    parts = Path(rel).parts
    if parts and parts[0].startswith("caty_gateway-"):
        return Path(*parts[1:]).as_posix()
    return rel

allowlist_path = root / ".scrub-allow"
try:
    allowlist_lines = allowlist_path.read_text(encoding="utf-8").splitlines()
except OSError as error:
    print(f"scrub-audit: cannot read {allowlist_path}: {error}", file=sys.stderr)
    raise SystemExit(2)

global_allow_patterns = []
scoped_allow_substrings = []
for number, entry in enumerate(allowlist_lines, 1):
    if not entry or entry.startswith("#"):
        continue
    if "\t" in entry:
        rel, substring = entry.split("\t", 1)
        if not rel or not substring:
            print(
                f"scrub-audit: invalid scoped allowlist entry at {allowlist_path}:{number}",
                file=sys.stderr,
            )
            raise SystemExit(2)
        scoped_allow_substrings.append((rel, substring))
        continue
    try:
        global_allow_patterns.append(re.compile(entry))
    except re.error as error:
        print(
            f"scrub-audit: invalid allowlist pattern at {allowlist_path}:{number}: {error}",
            file=sys.stderr,
        )
        raise SystemExit(2)

def allowed(rel, line):
    rel = logical_rel(rel)
    return any(pattern.search(line) for pattern in global_allow_patterns) or any(
        rel == allowed_rel and substring in line
        for allowed_rel, substring in scoped_allow_substrings
    )

name_parts = [
    split("sho", "jikumaru"), split("sho", "ji"), split("ku", "maru"),
    "翔", "アルファ", "セバス", split("se", "bas"), "クレア",
    split("cl", "aire"), "セロ", split("ce", "ro"), "マイン",
    split("mi", "ne"), split("so", "ra"), split("lu", "ca"), "ルカ",
    split("al", "pha"),
]
name_re = re.compile("|".join(
    (r"\\b" + re.escape(value) + r"\\b") if value.isascii() else re.escape(value)
    for value in name_parts
), re.I)
forbidden_literals = [
    split("100.98.", "83.100"), split("/home/", "admin"), split("/home/", "cero"),
    split("/Users/", "shoji", "kumaru"), split("jiku", "maru-sho"),
    split("claude", "-workspace"), split("wip-caty", "-watch"),
    split("Hetz", "ner"), split("5161d41404314212", "af1254556477c17d"),
    split("~/", ".open", "claw/env"), split("ai.open", "claw.caty-"), split("caty", "ptt-"),
    split("Mac ", "mini"),
]
private_stems = [
    split("firmware", "-assets"), split("tools/make-face", "-frames"),
    split("members/", "mine.json"), split("members/", "cero.json"),
    split("ESP", "32/"), split("caty", "-talk/"),
]
private_repos = [
    split("shoji", "kumaru/", "wip-caty", "-talk"), split("wip-caty", "-talk"),
    split("caty-talk", "-LP"), split("family", "-vault"),
    split("alpha", "-wiki"), split("Shared", "Hub"),
]
secret_re = re.compile(
    split(r"\\b", "sk", r"-[A-Za-z0-9]{8,}") + "|" +
    split("gh", r"p_[A-Za-z0-9]{10,}") + "|" +
    split("github", "_pat_") + "|" + split("xox", r"[bap]-") + "|" +
    split("AK", r"IA[0-9A-Z]{12,}") + "|" +
    split("-----BEGIN ", r"[A-Z ]*PRIVATE KEY")
)
bearer_re = re.compile(r"Bearer [A-Za-z0-9._-]{16,}")
hex_re = re.compile(r"(?i)(?<![0-9a-f])[0-9a-f]{32,}(?![0-9a-f])")
email_re = re.compile(r"(?i)\\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}\\b")
issue_re = re.compile(r"#[0-9]{2,4}\\b")
device_re = re.compile(split("xiao", "zhi") + "|" + split("X", "Z_") + "|" + split("ESP", "32"), re.I)

def scan_line(rel, number, line, *, from_binary=False):
    scope_rel = logical_rel(rel)
    if allowed(rel, line):
        return
    labels = []
    if secret_re.search(line): labels.append("secret-prefix")
    if bearer_re.search(line): labels.append("bearer")
    if not from_binary and hex_re.search(line): labels.append("hex32")
    if email_re.search(line): labels.append("email")
    if name_re.search(line): labels.append("personal-name")
    if any(value.lower() in line.lower() for value in forbidden_literals): labels.append("forbidden-literal")
    if any(value.lower() in line.lower() for value in private_stems): labels.append("private-path")
    if any(value.lower() in line.lower() for value in private_repos): labels.append("private-repo")
    if device_re.search(line): labels.append("excluded-device")
    if (scope_rel == "README.md" or scope_rel.startswith("docs/")) and issue_re.search(line): labels.append("issue-reference")
    for label in labels:
        findings.append(f"{label}: {rel}:{number}: {line[:240]}")

files = []
for path in sorted(root.rglob("*")):
    if not path.is_file() or any(part in skip_parts for part in path.relative_to(root).parts):
        continue
    # The allowlist authority was parsed and validated above; do not scan it against itself.
    if path == allowlist_path:
        continue
    files.append(path)

for path in files:
    rel = path.relative_to(root).as_posix()
    data = path.read_bytes()
    is_binary = b"\\0" in data[:8192] or path.suffix.lower() in binary_suffixes
    if path.suffix.lower() == ".png":
        chunks = []
        offset = 8
        try:
            while offset < len(data):
                length = struct.unpack(">I", data[offset:offset + 4])[0]
                kind = data[offset + 4:offset + 8].decode("ascii")
                chunks.append(kind)
                offset += 12 + length
                if kind == "IEND": break
        except Exception as error:
            findings.append(f"png-invalid: {rel}: {type(error).__name__}")
        allowed_chunks = {"IHDR", "IDAT", "IEND", "PLTE", "tRNS"}
        extra = [kind for kind in chunks if kind not in allowed_chunks]
        if extra:
            findings.append(f"png-ancillary: {rel}: {','.join(extra)}")
    if is_binary:
        try:
            output = subprocess.run(
                ["strings", "-n", "8", str(path)], check=False,
                text=True, capture_output=True,
            ).stdout
        except OSError as error:
            findings.append(f"strings-error: {rel}: {error}")
            continue
        for number, line in enumerate(output.splitlines(), 1):
            scan_line(rel, number, line, from_binary=True)
        continue
    text = data.decode("utf-8", errors="replace")
    for number, line in enumerate(text.splitlines(), 1):
        scan_line(rel, number, line)
    if logical_rel(rel).startswith("src/") and path.suffix == ".py":
        try:
            tree = ast.parse(text)
        except SyntaxError as error:
            findings.append(f"python-syntax: {rel}:{error.lineno}: {error.msg}")
        else:
            chat_names = [split("Sl", "ack"), split("Dis", "cord"), split("Tele", "gram")]
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if any(name.lower() in node.value.lower() for name in chat_names):
                        findings.append(f"prompt-tool: {rel}:{getattr(node, 'lineno', 0)}")

print(f"scrub-audit: root={root}")
if findings:
    for finding in findings:
        print(finding)
    print(f"scrub-audit: {len(findings)} findings")
    raise SystemExit(1)
print("scrub-audit: 0 findings")
PY
