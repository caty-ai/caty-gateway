---
route: ollama
backend: openai-compat
host: mac-mini (family Mac mini, not the dev machine; reached over Tailscale from the dev MBP)
os: macOS 26.5.2 (arm64)
date: 2026-09-06
layer: A
caty_gateway_version: caty-gateway 0.1.4 from PyPI (uv tool install --python 3.12 → Python 3.12.12), isolated prefix UV_TOOL_DIR=~/caty-smoke/uv-tools
result: PASS
---

Backend: Ollama 0.18.2 already running on the host (`brew services`), model `huihui_ai/granite3.2-vision-abliterated:latest` (the only model pulled there). `CATY_OPENAI_BASE_URL=http://127.0.0.1:11434/v1`. Member `smoke-ollama`, port 18766 (the default port was taken by the host's existing gateway; `doctor` reported it and `--port` fixed it).

phone-sim ran on the dev MBP (tailnet 100.104.116.34) against the gateway's public URL on the mini (tailnet 100.88.190.89:18766); the env read, the restart and the log read went over `ssh mac-mini`. Claim therefore came from a tailnet peer, as a phone would.

## Steps

| # | step | result | terminal value |
|---|---|---|---|
| 1 | clean install | PASS | `uv tool install --python 3.12 caty-gateway` → `caty-gateway==0.1.4`, `qrcode==8.2`, `pillow==12.3.0`; `caty-gateway --help` OK |
| 2 | doctor all PASS | PASS | `doctor --backend openai-compat --port 18766`: 14 PASS, 0 FAIL (without `--port`: 1 FAIL `port`, default port already listening on this host) |
| 3 | setup / QR issued | PASS | `setup --member smoke-ollama --backend openai-compat --port 18766 --yes` with `CATY_QR_DELIVERY=tty` (ssh session) → `Setup complete`, launchd label `ai.caty.gateway.smoke-ollama`, ASCII QR shown, redacted payload `{"v":1,"url":"http://100.88.190.89:18766","id":"smoke-ollama","pair":"9c772d2d.[REDACTED]"}` |
| 4 | pair claim (phone-sim) | PASS | self-issue via `/pair/new` then one `/pair/claim` from the tailnet: HTTP 200, 0.018 s (`pair_id` e8beb29f; the setup-time credential 9c772d2d was replaced by the new issue, as the contract says) |
| 5 | turns 1-2 | PASS | turn 1: 200 in 19.9 s (model cold load), reply `OK`; turn 2: 200 in 2.9 s; `degraded: null` (TTS produced audio) |
| 6 | restart + turn 3 (resume) | PASS | `launchctl kickstart -k gui/501/ai.caty.gateway.smoke-ollama`: outage observed, `downtime_s` 0.545; turn 3: 200 in 2.9 s, `resume_recall: true` (the codeword from turn 1 came back after the restart, i.e. the on-disk history was replayed) |
| 7 | token/pair not in logs | PASS | `--log-cmd "ssh mac-mini cat ~/Library/Logs/caty-gateway-smoke-ollama.log"` → `log_check: pass`; independent check on the host: `grep -cE '[0-9a-f]{8}\.[0-9a-f]{32}'` over the 36-line log = 0 |

## phone-sim summary

```
{"claim":{"http_status":200,"latency_s":0.018},"error":null,"finished_at":"2026-09-06T17:06:36Z","gateway_url":"100.88.190.89:18766","label":"ollama@mac-mini","layer":"A","log_check":"pass","log_secret_leak":false,"member_id":"smoke-ollama","ok":true,"pair_id":"e8beb29f","restart":{"downtime_s":0.545,"observed":true},"resume_recall":true,"session_id":"smoke-20260906-9f107b","stage":"done","stages":["qr","claim","turn1","turn2","restart","turn3","logcheck","done"],"started_at":"2026-09-06T17:06:09Z","turns":[{"degraded":null,"http_status":200,"latency_s":19.935,"n":1,"reply_chars":3,"reply_preview":"\nOK"},{"degraded":null,"http_status":200,"latency_s":2.858,"n":2,"reply_chars":13,"reply_preview":"\n了解我可以帮助你做什么？"},{"degraded":null,"http_status":200,"latency_s":2.854,"n":3,"reply_chars":17,"reply_preview":"\n blue-d1326c\n\n<|"}],"warnings":[]}
```

## Findings

- Gateway: none blocking. Two observations reported on #2 for separate S Issues: (a) `setup` is interactive by default and needs `--yes` when stdin is not a TTY, which the Quick Start does not say; (b) `status --member` keeps the `Detail:` line of an earlier failed attempt after a later successful run.
- Backend quirks, not gateway: turn 2 came back in Chinese (model behaviour); turn 3 reply carried a `<|` token fragment from the model.
- Service left installed on the host for layer B (`caty-gateway qr --member smoke-ollama` on the mini; `PATH` must include `~/caty-smoke/bin`).
