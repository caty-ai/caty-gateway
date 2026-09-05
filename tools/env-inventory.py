#!/usr/bin/env python3
"""Generate the environment reference from Python ASTs; never import the app."""

from __future__ import annotations

import argparse
import ast
import difflib
from pathlib import Path
import sys


# Design section 5 tiers. Names are explicit: a new name must be reviewed here,
# even when it shares a prefix with an existing optional feature.
TIER_NAMES = {
    "A": """
        CATY_ID CATY_NAME CATY_ACCENT_COLOR CATY_BACKEND CATY_GATEWAY_PORT
        CATY_GATEWAY_BIND CATY_PUBLIC_URL CATY_TOKEN CATY_ADMIN_TOKEN
        CATY_REQUIRE_AUTH CATY_CONFIG_DIR CATY_PAIRING_DIR CATY_SHARE_DIR
        CATY_HISTORY_DIR CATY_ASSET_DIR CATY_ASSETS_VERSION CATY_FILLER_DIR
        CATY_LANG CATY_VOICE_HINT CATY_TTS_ENGINE CATY_TTS_VOICE
        CATY_PAIRING_ALLOW_NONTAILNET FFMPEG_BIN FFPROBE_BIN CATY_USER_NAME
    """,
    "B": """
        CATY_CLAUDE_BIN CATY_CLAUDE_CWD CATY_CLAUDE_MODEL
        CATY_CLAUDE_PROJECTS_DIR CATY_GCLI_BIN CATY_GCLI_CWD
        CATY_GCLI_EXTRA_ARGS CATY_GCLI_NEW_ARGS CATY_GCLI_RESUME_ARGS
        CATY_GCLI_PARSE_SPEC CATY_GCLI_EXTERNAL CATY_GCLI_SESSION_STORE
        OPENCLAW_BIN CATY_AGENT CATY_GATEWAY_URL CATY_GATEWAY_TOKEN
        OPENCLAW_GATEWAY_TOKEN CATY_SESSION_KEY_PREFIX CATY_HERMES_URL
        CATY_HERMES_API_KEY CATY_OPENAI_BASE_URL CATY_OPENAI_MODEL
        CATY_OPENAI_API_KEY CATY_OPENAI_MAX_HISTORY_CHARS
    """,
    "B2": """
        CATY_PAIRING_TTL_SECONDS CATY_PAIRING_RATE_PER_MIN
        CATY_PAIRING_MAX_FAILURES CATY_PAIRING_LOCKOUT_SECONDS
        CATY_PAIRING_MAX_FAIL_TOTAL
    """,
    "C": """
        FISH_API_KEY FISH_BASE_URL FISH_MODEL FISH_LATENCY
        FISH_RETRY_ATTEMPTS FISH_RETRY_BASE_S FISH_RETRY_CAP_S
        FISH_UNHEALTHY_COOLDOWN_S CATY_OFFLINE
        CATY_VOICE_CATALOG_FETCH_LIMIT CATY_VOICE_CATALOG_TIMEOUT_SECONDS
        CATY_VOICE_CATALOG_TTL_SECONDS CATY_VOICE_FILLER_MAX_TEXTS_PER_KIND
        CATY_VOICE_FILLER_RECOVERY_QUARANTINE_RETENTION_SECONDS
        CATY_VOICE_FILLER_TTS_ATTEMPTS CATY_VOICE_FILLER_TTS_TIMEOUT_SECONDS
        CATY_VOICE_PREVIEW_CACHE_BYTES CATY_VOICE_PREVIEW_CACHE_ENTRIES
        CATY_VOICE_PREVIEW_MAX_BYTES CATY_VOICE_PREVIEW_MAX_DURATION_SECONDS
        CATY_VOICE_PREVIEW_NEGATIVE_TTL_SECONDS CATY_VOICE_PREVIEW_RATE_PER_MINUTE
        CATY_VOICE_PREVIEW_RATE_PRINCIPALS
        CATY_VOICE_PREVIEW_SINGLE_FLIGHT_TIMEOUT_SECONDS
        CATY_VOICE_PREVIEW_TTL_SECONDS CATY_VOICE_PREVIEW_TTS_ATTEMPTS
        CATY_VOICE_PREVIEW_TTS_TIMEOUT_SECONDS CATY_TTS_PROXY
        OPENAI_TTS_BASE_URL CATY_STREAM_TTS CATY_HISTORY_MD
        CATY_HISTORY_MAX_TURNS CATY_TOMBSTONE_TTL_DAYS POYO_API_KEY POYO_BASE
        RENOISE_API_KEY RENOISE_AUTH_TOKEN RENOISE_BASE_URL ANTHROPIC_API_KEY
        CATY_VISION_MODEL CATY_AVATAR_WORKDIR CATY_AVATAR_STYLE_REF
        CATY_AVATAR_VENDOR_HOST_ALLOWLIST CATY_OPENAI_CHAT_TOKEN
        CATY_OPENAI_CHAT_TIMEOUT CATY_OPENAI_CHAT_MAX_CONCURRENCY
        CATY_OPENAI_CHAT_HEARTBEAT_SEC CATY_OPENAI_CHAT_USER_MAX_LEN
        CATY_EXTERNAL_SESSIONS CATY_EXTERNAL_PREVIEW CATY_EXTERNAL_SEED_TURNS
        CATY_PTT_BRAIN_TIMEOUT CATY_PTT_JOB_TTL CATY_PRESENCE_MODE2
        CATY_UNSAFE_DEBUG_LOG_CONTENT CATY_SETUP_DEBUG CATY_QR_DELIVERY
    """,
    "D": """
        CATY_SETUP_ORCHESTRATOR CATY_SETUP_SUPERVISED
        CATY_SETUP_RESUME_TTL_SECONDS CATY_SETUP_HANDOFF_GRACE_SECONDS
        CATY_SETUP_QR_TIMEOUT_SECONDS CATY_SETUP_STATUS_WAIT_SECONDS
        CATY_SETUP_BACKEND_RECOVERY_TIMEOUT_SECONDS CATY_BACKEND_CONFIG_PATHS
        CATY_BACKEND_ENABLE_CMD XDG_STATE_HOME CODEX_HOME HOME PATH PYTHON
        PYTHON3 XDG_RUNTIME_DIR OPENCLAW_HOME HERMES_HOME
    """,
}
SECRET_NAMES = set("""
    CATY_TOKEN CATY_ADMIN_TOKEN CATY_GATEWAY_TOKEN OPENCLAW_GATEWAY_TOKEN
    CATY_HERMES_API_KEY CATY_OPENAI_API_KEY CATY_OPENAI_CHAT_TOKEN
    FISH_API_KEY POYO_API_KEY RENOISE_API_KEY RENOISE_AUTH_TOKEN ANTHROPIC_API_KEY
""".split())
LOCAL_NAMES = set("""
    CATY_CONFIG_DIR CATY_PAIRING_DIR CATY_SHARE_DIR CATY_HISTORY_DIR CATY_ASSET_DIR
    CATY_FILLER_DIR CATY_CLAUDE_CWD CATY_CLAUDE_PROJECTS_DIR CATY_GCLI_CWD
    CATY_GCLI_SESSION_STORE CATY_AVATAR_WORKDIR CATY_AVATAR_STYLE_REF
    CATY_BACKEND_CONFIG_PATHS XDG_STATE_HOME CODEX_HOME HOME PATH PYTHON PYTHON3
    XDG_RUNTIME_DIR OPENCLAW_HOME HERMES_HOME CATY_PUBLIC_URL CATY_GATEWAY_URL
    CATY_HERMES_URL CATY_OPENAI_BASE_URL CATY_TTS_PROXY OPENAI_TTS_BASE_URL
""".split())
PROCESS_ONLY = {"CATY_UNSAFE_DEBUG_LOG_CONTENT", "CATY_SETUP_DEBUG"}
METADATA = {
    name: (
        tier,
        "secret" if name in SECRET_NAMES else "local" if name in LOCAL_NAMES else "public",
        "process-only" if tier == "D" or name in PROCESS_ONLY else "member env (0600)",
    )
    for tier, names in TIER_NAMES.items()
    for name in names.split()
}


def dotted_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return dotted_name(node.value) + "." + node.attr
    return ""


def env_mapping(node):
    return dotted_name(node) in {"os.environ", "environ", "env", "self.env"}


def lookup(node):
    """Return the key and default expressions of supported environment reads."""
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load) and env_mapping(node.value):
        return node.slice, "required"
    if not isinstance(node, ast.Call) or not node.args:
        return None
    function = node.func
    if dotted_name(function) not in {"os.getenv", "getenv"} and not (
        isinstance(function, ast.Attribute) and function.attr == "get" and env_mapping(function.value)
    ):
        return None
    default = node.args[1] if len(node.args) > 1 else next(
        (keyword.value for keyword in node.keywords if keyword.arg == "default"), None
    )
    return node.args[0], default


def default_text(node):
    if node is None:
        return "None (unset)"
    if isinstance(node, str):
        return node
    if isinstance(node, ast.Constant):
        return repr(node.value)
    return "expression: " + ast.unparse(node)


def wrappers(tree):
    """Find module-local helpers that forward a key parameter to an env read.

    This covers numeric/boolean validators without executing source or mistaking
    arbitrary string constants, writes, or configuration dictionary reads for env.
    """
    found = {}
    for function in tree.body:
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        parameters = [argument.arg for argument in function.args.posonlyargs + function.args.args]
        for node in ast.walk(function):
            read = lookup(node)
            if read and isinstance(read[0], ast.Name) and read[0].id in parameters:
                found[function.name] = (function, parameters.index(read[0].id))
                break
    return found


def wrapper_lookup(node, helpers):
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
        return None
    helper = helpers.get(node.func.id)
    if helper is None:
        return None
    function, key_index = helper
    parameters = function.args.posonlyargs + function.args.args
    defaults = dict(zip(
        [argument.arg for argument in parameters[len(parameters) - len(function.args.defaults):]],
        function.args.defaults,
    ))
    values = dict(defaults)
    values.update((argument.arg, value) for argument, value in zip(parameters, node.args))
    values.update((keyword.arg, keyword.value) for keyword in node.keywords if keyword.arg)
    return values.get(parameters[key_index].arg), values.get("default")


def collect(root):
    inventory = {}
    for path in sorted((root / "src" / "caty_gateway").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        helpers = wrappers(tree)
        for node in ast.walk(tree):
            read = lookup(node) or wrapper_lookup(node, helpers)
            if read is None or not isinstance(read[0], ast.Constant) or not isinstance(read[0].value, str):
                continue
            name = read[0].value
            entry = inventory.setdefault(name, {"defaults": set(), "where": set()})
            entry["defaults"].add(default_text(read[1]))
            entry["where"].add(f"{path.relative_to(root).as_posix()}:{node.lineno}")
    return inventory


def cell(value):
    return value.replace("&", "&amp;").replace("|", "&#124;").replace("`", "&#96;").replace("\n", " ")


def render(inventory):
    lines = [
        "# Environment inventory", "",
        "Generated by `python tools/env-inventory.py`. Do not edit by hand.", "",
        "Defaults are the source lookup expressions, including each distinct call-site default;",
        "later fallback logic is not evaluated. Local numeric/boolean env helpers are included.",
        "No application module is imported and no live environment values are read.", "",
        "Tiers: A = common; B = backend; B2 = pairing safeguards; C = optional features;",
        "D = internal or host environment (listed for audit, not recommended configuration).",
        "Sensitivity: secret = credential; local = local path or service address; public = other settings.",
        "Persistence describes permitted value storage, not a promise that setup writes every option:",
        "member env (0600) = may be kept in the member environment file; process-only = do not persist.",
        "Unknown names are unclassified and make `--check` fail until reviewed in the script mapping.", "",
        "| name | default | tier | sensitivity | persistence | where |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name, entry in sorted(inventory.items()):
        tier, sensitivity, persistence = METADATA.get(name, ("unclassified",) * 3)
        defaults = "; ".join(sorted(entry["defaults"]))
        where = "; ".join(sorted(entry["where"]))
        lines.append("| " + " | ".join(map(cell, (name, defaults, tier, sensitivity, persistence, where))) + " |")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1], help="project root")
    parser.add_argument("--output", "-o", type=Path, help="output file (default: <root>/docs/env.md; - for stdout)")
    parser.add_argument("--check", action="store_true", help="fail on drift, missing output, or unclassified names")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    output = args.output if args.output is not None else root / "docs" / "env.md"
    if not (root / "src" / "caty_gateway").is_dir():
        parser.error(f"source directory missing: {root / 'src' / 'caty_gateway'}")
    if args.check and output == Path("-"):
        parser.error("--check needs a file output")
    try:
        inventory = collect(root)
        if not inventory:
            print("env-inventory: no environment reads found", file=sys.stderr)
            return 1
        generated = render(inventory)
        if args.check:
            existing = output.read_text(encoding="utf-8") if output.exists() else ""
            unknown = sorted(set(inventory) - METADATA.keys())
            if existing != generated:
                sys.stderr.writelines(difflib.unified_diff(
                    existing.splitlines(keepends=True), generated.splitlines(keepends=True),
                    fromfile=str(output), tofile="generated env inventory",
                ))
                print("env-inventory: drift; run python tools/env-inventory.py", file=sys.stderr)
            if unknown:
                print("env-inventory: unclassified names: " + ", ".join(unknown), file=sys.stderr)
            if existing != generated or unknown:
                return 1
            print(f"env-inventory: {len(inventory)} names, no drift")
        elif output == Path("-"):
            sys.stdout.write(generated)
        else:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(generated, encoding="utf-8")
        return 0
    except (OSError, SyntaxError) as error:
        print(f"env-inventory: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
