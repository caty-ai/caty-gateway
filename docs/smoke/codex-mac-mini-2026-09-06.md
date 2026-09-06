---
route: codex
backend: codex
host: mac-mini (family Mac mini, not the dev machine; reached over Tailscale from the dev MBP)
os: macOS 26.5.2 (arm64)
date: 2026-09-06
layer: A
caty_gateway_version: caty-gateway 0.1.4 from PyPI (uv tool install --python 3.12 → Python 3.12.12), isolated prefix UV_TOOL_DIR=~/caty-smoke/uv-tools
result: FAIL (step 5; environment — the host's Codex account had exhausted its usage limit; steps 1-4 PASS)
---

Backend: `codex-cli 0.144.6`, `codex login status` = "Logged in using ChatGPT" (the host user's own account). Member `smoke-codex`, port 18767. Same install prefix and the same tailnet layout as the Ollama record.

## Steps

| # | step | result | terminal value |
|---|---|---|---|
| 1 | clean install | PASS | shared with the Ollama record (same `uv tool install`, 0.1.4) |
| 2 | doctor all PASS | PASS | `doctor --backend codex --port 18767`: all PASS (14), including `codex login` |
| 3 | setup / QR issued | PASS | `setup --member smoke-codex --backend codex --port 18767 --yes` (`CATY_QR_DELIVERY=tty`) → `Setup complete`, label `ai.caty.gateway.smoke-codex`, redacted payload `{"v":1,"url":"http://100.88.190.89:18767","id":"smoke-codex","pair":"5b91a833.[REDACTED]"}` |
| 4 | pair claim (phone-sim) | PASS | HTTP 200, 0.022 s (`pair_id` db94456f) |
| 5 | turns 1-2 | FAIL | turn 1: `/talk2` 200, then `/reply/<id>` 500 after 9.1 s. Gateway log: `stage=stream status=error error_type=RuntimeError`, turn summary `backend=codex status=error gen=8.9s`. Reproduced on the host outside the gateway: `codex exec --skip-git-repo-check --json "Reply with just OK"` → `{"type":"error","message":"You've hit your usage limit. ... try again at 9:31 AM."}` |
| 6 | restart + turn 3 (resume) | not reached | — |
| 7 | token/pair not in logs | not reached by phone-sim (stops at the first fatal stage) | manual check on the host: `grep -cE '[0-9a-f]{8}\.[0-9a-f]{32}' ~/Library/Logs/caty-gateway-smoke-codex.log` = 0 |

## phone-sim summary

```
{"claim":{"http_status":200,"latency_s":0.022},"error":"reply failed (HTTP 500)","finished_at":"2026-09-06T17:07:55Z","gateway_url":"100.88.190.89:18767","label":"codex@mac-mini","layer":"A","log_check":"skipped","log_secret_leak":null,"member_id":"smoke-codex","ok":false,"pair_id":"db94456f","restart":{"downtime_s":null,"observed":"skipped"},"resume_recall":null,"session_id":"smoke-20260906-3debfa","stage":"turn1","stages":["qr","claim","turn1"],"started_at":"2026-09-06T17:07:46Z","turns":[{"degraded":null,"http_status":500,"latency_s":9.091,"n":1,"reply_chars":0,"reply_preview":""}],"warnings":[]}
```

## Findings

- Not a gateway bug: the backend refused the turn because the account's Codex quota was exhausted. `doctor` cannot see this (passive checks only, by design). The gateway surfaced it correctly as a 500 on `/reply/<id>` and did not leak anything into the log.
- Re-run needed once the quota resets (the host said "try again at 9:31 AM" host time): same phone-sim command, no reinstall. The service is left installed on the host.
