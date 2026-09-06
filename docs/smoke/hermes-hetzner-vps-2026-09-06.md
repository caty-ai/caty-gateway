---
route: hermes
backend: hermes
host: hetzner-vps (Linux VPS, Tailscale)
os: unknown (not reached)
date: 2026-09-06
layer: A
caty_gateway_version: not installed
result: NOT RUN (host not reachable from the automated session; nothing was measured)
---

## Steps

| # | step | result | terminal value |
|---|---|---|---|
| 1 | clean install | not attempted | The only SSH route available to the automation this session is a forced-command read key (`pgl-luca-dispatch: request rejected` for any shell); the operator route was not permitted from the automated session. No command ran on the host. |
| 2-7 | — | not reached | — |

## phone-sim summary

Not run.

## Findings

- Nothing to report about the gateway. This route is blocked on host access, not on software.
- What layer A needs on that host, once reachable: `uv tool install caty-gateway` into a private `UV_TOOL_DIR`, a Hermes `/v1/responses` endpoint plus `CATY_HERMES_API_KEY` that is *not* another household member's production instance, `loginctl enable-linger`, and then the "Another tailnet host: Linux gateway" command block in `README.md`.
- Ollama was not checked on that host either (same reason).
