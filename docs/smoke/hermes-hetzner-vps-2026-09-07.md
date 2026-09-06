---
route: hermes
backend: hermes
host: hetzner-vps (Linux VPS on the tailnet; reached with `ssh hetzner-vps-admin` from the dev MBP)
os: Ubuntu 24.04.4 LTS (x86_64, 96 vCPU, no GPU)
date: 2026-09-07
layer: A
caty_gateway_version: caty-gateway 0.1.4 from PyPI (same isolated install as `ollama-hetzner-vps-2026-09-07.md`)
result: NOT RUN (steps 1-2 measured; stopped before setup — no Hermes API key that is not a household member's)
---

Supersedes `hermes-hetzner-vps-2026-09-06.md` (host not reachable then). This time the host was reachable and the clean install and `doctor` ran; the route stops at the backend key.

## Steps

| # | step | result | terminal value |
|---|---|---|---|
| 1 | clean install | PASS | shared with the Ollama record on this host: `uv tool install caty-gateway` → `caty-gateway==0.1.4` in `UV_TOOL_DIR=~/caty-smoke/tools` |
| 2 | doctor all PASS | FAIL (expected: no key) | `doctor --backend hermes --port 18772` with no `CATY_HERMES_*` set: 12 PASS, 2 FAIL — `hermes API key: set CATY_HERMES_API_KEY` and `hermes models: start Hermes and verify CATY_HERMES_URL and CATY_HERMES_API_KEY` |
| 3-7 | — | not attempted | no admin-owned Hermes endpoint + key to point the smoke member at (below) |

## phone-sim summary

Not run.

## Findings

- Nothing to report about the gateway; `doctor` names the missing pieces correctly. Note that once a key exists, `setup --backend hermes` on this host will hit the same systemd unit rendering defect as the Ollama route (#38) and need the same two-line unit fix until #38 is merged.
- What is on the host (read-only survey, no configuration was changed): Hermes is installed and several Hermes API servers are running, each belonging to a household member's profile (their `API_SERVER_KEY`, their memory). `GET /v1/models` on the tailnet endpoint that the caty-gateway default `CATY_HERMES_URL` port maps to answers `401 {"error":{"type":"gateway_auth_error","code":"gateway_auth_failed"}}` — a key is required, and the key of another household member's production instance must not be used for a smoke (same rule as the 2026-09-06 record). There is no smoke-only Hermes instance, and creating one means enabling `API_SERVER_*` on a profile and minting a key — a Hermes configuration change, which is outside this run (owner's ball on #2, row "4 Hermes | key").
- The one step that unblocks steps 3-7: on the VPS, an admin-owned smoke Hermes API server on a free loopback or tailnet port (a dedicated profile, not a household member's; `API_SERVER_ENABLED: true`, a fresh `API_SERVER_KEY`) and a `0600` env file `~/caty-smoke/hermes-smoke.env` containing `CATY_HERMES_URL=http://127.0.0.1:<port>` and `CATY_HERMES_API_KEY=<that key>`. Alpha then runs the 7 steps exactly as the Ollama record (member `smoke-hermes`, port 18772, phone-sim from the MBP over the tailnet) and replaces this record.
- Left on the host: nothing beyond the shared `~/caty-smoke/` prefix described in the Ollama record.
