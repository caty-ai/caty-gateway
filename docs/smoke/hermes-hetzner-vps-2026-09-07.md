---
route: hermes
backend: hermes
host: hetzner-vps (Linux VPS on the tailnet; reached over ssh as the host's admin user from the dev MBP)
os: Ubuntu 24.04.4 LTS (x86_64, 96 vCPU, no GPU)
date: 2026-09-07 (writer's local date, UTC+7; the commands ran 2026-09-06 19:4x–19:5x UTC, in the same session as the Ollama record on this host)
layer: A
caty_gateway_version: caty-gateway 0.1.4 from PyPI (same isolated install as `ollama-hetzner-vps-2026-09-07.md`)
result: NOT RUN (steps 1-2 measured; stopped before setup — no Hermes API key that is not a household member's)
---

Supersedes `hermes-hetzner-vps-2026-09-06.md`, which was written by an earlier automated session (2026-09-06, ~17:00 UTC) that had no shell access to the host. This later session had the admin ssh route, so the clean install and `doctor` ran; the route stops at the backend key.

## Steps

| # | step | result | terminal value |
|---|---|---|---|
| 1 | clean install | PASS | shared with the Ollama record on this host: `uv tool install caty-gateway` → `caty-gateway==0.1.4` in `UV_TOOL_DIR=~/caty-smoke/tools` |
| 2 | doctor all PASS | FAIL (expected: no key) | `doctor --backend hermes --port 18772` with no `CATY_HERMES_*` set: 12 PASS, 2 FAIL — `hermes API key: set CATY_HERMES_API_KEY` and `hermes models: start Hermes and verify CATY_HERMES_URL and CATY_HERMES_API_KEY` |
| 3 | setup / QR issued | not attempted | no admin-owned Hermes endpoint + key to point the smoke member at (Findings) |
| 4 | pair claim (phone-sim) | not attempted | — |
| 5 | turns 1-2 | not attempted | — |
| 6 | restart + turn 3 (resume) | not attempted | — |
| 7 | token/pair not in logs | not attempted | — |

## phone-sim summary

Not run.

## Findings

- Nothing to report about the gateway; `doctor` names the missing pieces correctly. Note that once a key exists, `setup --backend hermes` on this host will hit the same systemd unit rendering defect as the Ollama route (#38) and need the same two-line unit fix until #38 is merged.
- What is on the host (read-only survey, no configuration was changed): Hermes is installed and several Hermes API servers are running, each belonging to a household member's profile (their `API_SERVER_KEY`, their memory). `GET /v1/models` on the tailnet endpoint that the caty-gateway default `CATY_HERMES_URL` port maps to answers `401 {"error":{"type":"gateway_auth_error","code":"gateway_auth_failed"}}` — a key is required, and the key of another household member's production instance must not be used for a smoke (same rule as the 2026-09-06 record). There is no smoke-only Hermes instance, and creating one means enabling `API_SERVER_*` on a profile and minting a key — a Hermes configuration change, which is outside this run (owner's ball on #2, row "4 Hermes | key").
- The one step that unblocks steps 3-7: on the VPS, an admin-owned smoke Hermes API server on a free loopback or tailnet port (a dedicated profile, not a household member's; `API_SERVER_ENABLED: true`, a fresh `API_SERVER_KEY`) and a `0600` env file `~/caty-smoke/hermes-smoke.env` containing `CATY_HERMES_URL=http://127.0.0.1:<port>` and `CATY_HERMES_API_KEY=<that key>`. The layer A runner (see `README.md`) then runs the 7 steps exactly as the Ollama record (member `smoke-hermes`, port 18772, phone-sim from the MBP over the tailnet) and replaces this record.
- Left on the host: nothing beyond the shared `~/caty-smoke/` prefix described in the Ollama record.
