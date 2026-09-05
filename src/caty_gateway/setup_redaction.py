"""Shared secret redaction for the setup orchestrator and supervisor."""

from __future__ import annotations

import re


PAIR_RE = re.compile(r"\b[0-9a-f]{8}\.[0-9a-f]{32}\b")
BEARER_RE = re.compile(r"(?i)\bBearer\s+\S+")
ORCHESTRATOR_TOKEN_RE = re.compile(r"\b[0-9a-f]{48}\b")
CLOUD_SESSION_TOKEN_RE = re.compile(
    r"(?i)([\"']?cloud_session[\"']?\s*[=:]\s*\{[^{}]*?[\"']?token[\"']?\s*[=:]\s*)"
    r"(?:'[^']*'|\"[^\"]*\"|[^\s,;}]+)"
)
SECRET_ASSIGN_RE = re.compile(
    r"(?im)(\b[A-Z0-9_]*(?:TOKEN|API_KEY|PASSWORD)[A-Z0-9_]*\b['\"]?\s*[=:]\s*)"
    r"(?:'[^']*'|\"[^\"]*\"|\S+)"
)
PLIST_SECRET_RE = re.compile(
    r"(<key>[^<]*(?:token|api_key|password)[^<]*</key>\s*<string>)[^<]*(</string>)",
    re.IGNORECASE | re.DOTALL,
)


def redact(text: str) -> str:
    """Return setup output with every supported credential shape redacted."""
    text = PLIST_SECRET_RE.sub(r"\1[REDACTED]\2", str(text))
    text = ORCHESTRATOR_TOKEN_RE.sub("[REDACTED]", text)
    text = PAIR_RE.sub("[REDACTED]", text)
    text = BEARER_RE.sub("Bearer [REDACTED]", text)
    text = CLOUD_SESSION_TOKEN_RE.sub(lambda match: match.group(1) + "[REDACTED]", text)
    return SECRET_ASSIGN_RE.sub(lambda match: match.group(1) + "[REDACTED]", text)
