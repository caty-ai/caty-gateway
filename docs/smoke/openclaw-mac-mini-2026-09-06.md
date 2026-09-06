---
route: openclaw
backend: openclaw
host: mac-mini (family Mac mini)
os: macOS 26.5.2 (arm64)
date: 2026-09-06
layer: A
caty_gateway_version: caty-gateway 0.1.4 from PyPI (same isolated prefix as the other mac-mini records)
result: NOT RUN (stopped at step 2; the host no longer runs an OpenClaw gateway)
---

## Steps

| # | step | result | terminal value |
|---|---|---|---|
| 1 | clean install | PASS | shared with the Ollama record |
| 2 | doctor all PASS | FAIL | `doctor --backend openclaw`: `openclaw agents` PASS, `openclaw agent` PASS, `openclaw gateway token` PASS, **`openclaw gateway` FAIL** (`start the gateway and set CATY_GATEWAY_URL to its reachable HTTP URL`). `~/.openclaw/openclaw.json` still says port 18789, but nothing listens there and `launchctl list` has no `ai.openclaw.gateway` job: the household's OpenClaw agent on this host was migrated to Hermes (`ai.hermes.gateway-caty` is what runs now). |
| 3-7 | — | not reached | starting someone else's production OpenClaw gateway is outside this smoke |

## phone-sim summary

Not run.

## Findings

- No gateway defect. `doctor` identified the missing OpenClaw gateway precisely.
- This route needs a host that actually runs OpenClaw (the #2 table assumed the Mac mini; that assumption is now stale). Candidates: a fresh OpenClaw install on the dev MBP second user, or another family host that still runs OpenClaw. Listed on #2.
