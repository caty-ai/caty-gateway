---
route: openclaw
backend: openclaw
host: hetzner-vps (Linux VPS on the tailnet; reached over ssh as the host's admin user from the dev MBP)
os: Ubuntu 24.04.4 LTS (x86_64, 96 vCPU, no GPU)
date: 2026-09-07 (survey 2026-09-07 ~10:20 UTC)
layer: A
caty_gateway_version: caty-gateway 0.1.4 from PyPI (isolated prefix UV_TOOL_DIR=~/caty-smoke/tools, same install as the Ollama and Hermes records on this host)
result: NOT RUN (steps 1-2 measured; stopped before setup — no OpenClaw agent on the host that is not a household member's)
---

Why this host: the "OpenClaw = Mac mini" row on #2 was stale (the mini has no `openclaw` binary any more). A read-only survey of the three candidate hosts on 2026-09-07 found OpenClaw gateways running only on this VPS: four user units, each a household member's own instance with its own state directory (`OPENCLAW_STATE_DIR`) and port, all launched from a per-host install of OpenClaw 2026.8.2. The MBP has the CLI installed but runs no gateway.

## Steps

| # | step | result | terminal value |
|---|---|---|---|
| 1 | clean install | PASS | shared with the Ollama record on this host: `uv tool install caty-gateway` → `caty-gateway==0.1.4` |
| 2 | doctor all PASS | FAIL (expected: no agent) | `doctor --backend openclaw --port 18774` with no `OPENCLAW_BIN` / `CATY_AGENT`: 12 PASS, 2 FAIL — `openclaw agents: set OPENCLAW_BIN to an executable and configure its agents` and `openclaw agent: set CATY_AGENT to an agent shown by openclaw agents list`; `openclaw gateway token` and `openclaw gateway` PASS (a gateway is reachable on the default port — a household member's) |
| 3 | setup / QR issued | not attempted | no smoke agent to name in `CATY_AGENT` (Findings) |
| 4 | pair claim (phone-sim) | not attempted | — |
| 5 | turns 1-2 | not attempted | — |
| 6 | restart + turn 3 (resume) | not attempted | — |
| 7 | token/pair not in logs | not attempted | — |

## phone-sim summary

Not run.

## Findings

- Nothing to report about the gateway; `doctor` names the two missing inputs correctly and finds the running gateway on the default port.
- Why it stops here: the `openclaw` backend runs `openclaw agent --agent <CATY_AGENT>` against the resident gateway, so it needs an agent. Every agent on this host belongs to a household member (their gateway, their state directory, their model credentials); the rule from the Hermes route applies — a smoke must not drive another member's production agent. Two host-side details for whoever sets this up: the `openclaw` found on `PATH` is 2026.4.24 and rejects the shared `~/.openclaw/openclaw.json` (`Config invalid`, several unrecognized keys), while the member gateways run from a separate 2026.8.2 install under the admin home — `OPENCLAW_BIN` must point at that one, together with a dedicated `OPENCLAW_STATE_DIR`.
- The one step that unblocks steps 3-7: a smoke-only OpenClaw instance on the VPS — its own state directory (e.g. `~/.openclaw-smoke`), a gateway on a free port, and one agent (id e.g. `smoke`) whose model can be the host's local Ollama (`ollama` provider; no API key needed, as done for the Hermes route). Then, with `OPENCLAW_BIN=<the 2026.8.2 binary>`, `OPENCLAW_STATE_DIR=~/.openclaw-smoke` and `CATY_AGENT=smoke`, the layer A runner (see `README.md`) runs the 7 steps exactly as the Hermes record (member `smoke-openclaw`, port 18774, phone-sim from the MBP over the tailnet) and replaces this record. Creating that instance is an owner decision (it is a new OpenClaw install on a shared host).
- Left on the host: nothing beyond what the Ollama and Hermes records list.
