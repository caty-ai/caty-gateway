#!/usr/bin/env bash
set -euo pipefail

root="${1:-.}"
python3 -B - "$root" <<'PY'
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
        rel, _, substring = entry.partition("\t")
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

private_path = Path(os.environ.get("SCRUB_PRIVATE_FILE", root / ".scrub-private")).resolve()
private_sections = {key: [] for key in ("names", "literals", "stems", "repos")}
try:
    private_lines = private_path.read_text(encoding="utf-8").splitlines()
except FileNotFoundError:
    print("scrub-audit: private list not loaded (0 entries) — public rules only")
except (OSError, UnicodeError):
    print("scrub-audit: cannot read private list", file=sys.stderr)
    raise SystemExit(2)
else:
    section = None
    for number, entry in enumerate(private_lines, 1):
        if not entry.strip() or entry.lstrip().startswith("#"):
            continue
        if entry in {f"[{key}]" for key in private_sections}:
            section = entry[1:-1]
        elif section is None or (entry.startswith("[") and entry.endswith("]")):
            print(f"scrub-audit: invalid private list at line {number}", file=sys.stderr)
            raise SystemExit(2)
        else:
            private_sections[section].append(entry)
    print(f"scrub-audit: private list loaded ({sum(map(len, private_sections.values()))} entries)")

name_patterns = [re.compile(
    (r"\b" + re.escape(value) + r"\b") if value.isascii() else re.escape(value), re.I,
) for value in private_sections["names"]]
private_substrings = [value.lower() for key in ("literals", "stems", "repos")
                      for value in private_sections[key]]

def private_spans(line):
    spans = [match.span() for pattern in name_patterns for match in pattern.finditer(line)]
    if private_substrings:
        lowered = line.lower()
        # Map offsets back to the original, including Unicode lowercase expansion.
        offsets = [i for i, char in enumerate(line) for _ in char.lower()]
        for value in private_substrings:
            start = lowered.find(value)
            while start != -1:
                spans.append((offsets[start], offsets[start + len(value) - 1] + 1))
                start = lowered.find(value, start + 1)
    return sorted(spans)

def redact_private(line):
    result, end = [], 0
    for start, stop in private_spans(line):
        if stop <= end:
            continue
        if start >= end:
            result.extend((line[end:start], "[redacted]"))
        end = stop
    return "".join(result) + line[end:]

secret_re = re.compile(
    "".join((r"\\b", "sk", r"-[A-Za-z0-9]{8,}")) + "|" +
    "".join(("gh", r"p_[A-Za-z0-9]{10,}")) + "|" +
    "".join(("github", "_pat_")) + "|" + "".join(("xox", r"[bap]-")) + "|" +
    "".join(("AK", r"IA[0-9A-Z]{12,}")) + "|" +
    "".join(("-----BEGIN ", r"[A-Z ]*PRIVATE KEY"))
)
bearer_re = re.compile(r"Bearer [A-Za-z0-9._-]{16,}")
hex_re = re.compile(r"(?i)(?<![0-9a-f])[0-9a-f]{32,}(?![0-9a-f])")
email_re = re.compile(r"(?i)\\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}\\b")
issue_re = re.compile(r"#[0-9]{2,4}\\b")
device_re = re.compile("".join(("xiao", "zhi")) + "|" + "".join(("X", "Z_")) + "|" + "".join(("ESP", "32")), re.I)

def scan_line(rel, number, line, *, from_binary=False):
    scope_rel = logical_rel(rel)
    if allowed(rel, line):
        return
    labels = []
    if secret_re.search(line): labels.append("secret-prefix")
    if bearer_re.search(line): labels.append("bearer")
    if not from_binary and hex_re.search(line): labels.append("hex32")
    if email_re.search(line): labels.append("email")
    if device_re.search(line): labels.append("excluded-device")
    if (scope_rel == "README.md" or scope_rel.startswith("docs/")) and issue_re.search(line): labels.append("issue-reference")
    if private_spans(line): labels.append("private-literal")
    line = redact_private(line)
    rel = redact_private(rel)
    for label in labels:
        findings.append(f"{label}: {rel}:{number}: {line[:240]}")

files = []
for path in sorted(root.rglob("*")):
    if not path.is_file() or any(part in skip_parts for part in path.relative_to(root).parts):
        continue
    # The allowlist authority was parsed and validated above; do not scan it against itself.
    if path == allowlist_path or path.resolve() == private_path:
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
            chat_names = ["".join(("Sl", "ack")), "".join(("Dis", "cord")), "".join(("Tele", "gram"))]
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if any(name.lower() in node.value.lower() for name in chat_names):
                        findings.append(f"prompt-tool: {rel}:{getattr(node, 'lineno', 0)}")

print(f"scrub-audit: root={redact_private(str(root))}")
if findings:
    for finding in findings:
        print(redact_private(finding))
    print(f"scrub-audit: {len(findings)} findings")
    raise SystemExit(1)
print("scrub-audit: 0 findings")
PY
