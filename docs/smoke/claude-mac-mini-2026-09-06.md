---
route: claude
backend: claude
host: mac-mini (family Mac mini, not the dev machine; reached over Tailscale from the dev MBP)
os: macOS 26.5.2 (arm64)
date: 2026-09-06
layer: A
caty_gateway_version: caty-gateway 0.1.4 from PyPI (uv tool install --python 3.12 → Python 3.12.12), isolated prefix UV_TOOL_DIR=~/caty-smoke/uv-tools
result: FAIL (step 5; environment — Claude Code CLI on the host is not logged in; steps 1-4 PASS)
---

Backend: Claude Code `2.1.220` at `~/.local/bin/claude` on the host. Member `smoke-claude`, port 18768. Same install prefix and tailnet layout as the Ollama record.

## Steps

| # | step | result | terminal value |
|---|---|---|---|
| 1 | clean install | PASS | shared with the Ollama record (same `uv tool install`, 0.1.4) |
| 2 | doctor all PASS | PASS with 1 WARN | `doctor --backend claude --port 18768`: 13 PASS, 1 WARN `claude credentials: sign in with Claude CLI; credentials stored in the OS keychain cannot be checked passively`, 0 FAIL |
| 3 | setup / QR issued | PASS | `setup --member smoke-claude --backend claude --port 18768 --yes` (`CATY_QR_DELIVERY=tty`) → `Setup complete`, label `ai.caty.gateway.smoke-claude`, redacted payload `{"v":1,"url":"http://100.88.190.89:18768","id":"smoke-claude","pair":"874e3bb5.[REDACTED]"}` |
| 4 | pair claim (phone-sim) | PASS | HTTP 200, 0.020 s (`pair_id` ea366d13) |
| 5 | turns 1-2 | FAIL | turn 1: `/talk2` 200, then `/reply/<id>` 500 after 1.1 s. Gateway log: `stage=stream status=error error_type=RuntimeError`, `backend=claude status=error gen=0.9s`. Reproduced on the host: `claude -p "Reply with just OK" --output-format json` → `is_error: true`, result `Not logged in · Please run /login` |
| 6 | restart + turn 3 (resume) | not reached | — |
| 7 | token/pair not in logs | not reached by phone-sim | manual check on the host: token-shaped grep over `~/Library/Logs/caty-gateway-smoke-claude.log` = 0 |

## phone-sim summary

```
{"claim":{"http_status":200,"latency_s":0.02},"error":"reply failed (HTTP 500)","finished_at":"2026-09-06T17:09:01Z","gateway_url":"100.88.190.89:18768","label":"claude@mac-mini","layer":"A","log_check":"skipped","log_secret_leak":null,"member_id":"smoke-claude","ok":false,"pair_id":"ea366d13","restart":{"downtime_s":null,"observed":"skipped"},"resume_recall":null,"session_id":"smoke-20260906-e0f22d","stage":"turn1","stages":["qr","claim","turn1"],"started_at":"2026-09-06T17:08:59Z","turns":[{"degraded":null,"http_status":500,"latency_s":1.083,"n":1,"reply_chars":0,"reply_preview":""}],"warnings":[]}
```

## Findings

- Not a gateway bug: the doctor WARN was the right signal (keychain credentials cannot be verified passively) and the first turn is where a missing login shows up. Owner action: `claude` → `/login` on the mini (or run this route on a host where Claude Code is signed in), then re-run the same phone-sim command; no reinstall needed. The service is left installed on the host.
- The 1 WARN means step 2 was not literally "all PASS"; recorded as such rather than rounded up.
