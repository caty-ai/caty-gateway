---
route: ollama
backend: openai-compat
host: hetzner-vps (Linux VPS on the tailnet; reached with `ssh hetzner-vps-admin` from the dev MBP)
os: Ubuntu 24.04.4 LTS (x86_64, 96 vCPU, no GPU)
date: 2026-09-07
layer: A
caty_gateway_version: caty-gateway 0.1.4 from PyPI (uv 0.11.2, already on the host; `uv tool install caty-gateway` → uv-managed Python 3.11.15), isolated prefix UV_TOOL_DIR=~/caty-smoke/tools, UV_TOOL_BIN_DIR=~/caty-smoke/bin
result: PARTIAL (step 3 FAIL — gateway defect #38; steps 4-7 PASS after a two-line manual fix of the generated unit)
---

Backend: Ollama 0.20.2 already running on the host on `127.0.0.1:11434` (CPU only), model `gemma3:1b` (the smallest of the models pulled there). `CATY_OPENAI_BASE_URL=http://127.0.0.1:11434/v1`. Member `smoke-ollama`, port 18771 (free; the host also runs other gateways for household members on their own ports — none was touched).

phone-sim ran on the dev MBP against the gateway's public URL on the VPS (tailnet 100.98.83.100:18771); the env read, the restart and the journal read went over `ssh hetzner-vps-admin`, i.e. the "Another tailnet host: Linux gateway" block of `README.md`. Claim therefore came from a tailnet peer, as a phone would. The round trip MBP ↔ VPS is about 0.4 s (see step 6).

## Steps

| # | step | result | terminal value |
|---|---|---|---|
| 1 | clean install | PASS | `uv tool install caty-gateway` (0.7 s, wheels cached) → `caty-gateway==0.1.4`, `qrcode==8.2`, `pillow==12.3.0`; `caty-gateway --help` OK. uv was not installed by this run (it was already at `~/.local/bin/uv`, 0.11.2) |
| 2 | doctor all PASS | PASS | `doctor --backend openai-compat --port 18771` with the two `CATY_OPENAI_*` vars: 14 PASS, 0 FAIL |
| 3 | setup / QR issued | **FAIL** | `setup --member smoke-ollama --backend openai-compat --yes --port 18771` with `CATY_QR_DELIVERY=tty` (ssh session) wrote the env file and the user unit, but systemd refused the unit: `…service:8: WorkingDirectory= path is not absolute: "/home/<user>"` (the value is rendered with literal double quotes; `EnvironmentFile=` too). Setup ended after 30.6 s with `Setup status: failed / Current phase: health`; no QR was issued. → #38. **Workaround for the smoke only**: removed the quotes from those two lines of the generated `caty-gateway-smoke-ollama.service`, `systemctl --user daemon-reload && enable --now` → `active`, health on the tailnet URL answers (401 without a token, as expected) |
| 4 | pair claim (phone-sim) | PASS | self-issue via `/pair/new` then one `/pair/claim` from the tailnet: HTTP 200, 0.443 s (`pair_id` 02f069c2) |
| 5 | turns 1-2 | PASS | turn 1: 200 in 2.78 s, reply `OK`; turn 2: 200 in 2.79 s; `degraded: "tts"` on both (no TTS engine on this headless VPS: journal shows `stage=stream_tts status=batch_fallback error_type=ConnectionRefusedError` then `stage=batch_tts status=text_only`; the text reply is intact) |
| 6 | restart + turn 3 (resume) | PASS (restart proven by the journal, not by a health gap) | `ssh … systemctl --user restart caty-gateway-smoke-ollama`: journal `Stopping` 21:52:54.070 → `Started` 21:52:54.094 (24 ms), MainPID 3141071 → 3142623. phone-sim reports `restart.observed: false` because the gateway was back before the ssh command returned (outage shorter than the ~0.4 s round trip); three earlier attempts with `--require-restart-observed` (`restart`, `restart --no-block`, `stop && start --no-block`) all stopped there → #39, so the final run dropped that flag. Turn 3: 200 in 2.86 s, `resume_recall: true` (the codeword from turn 1 came back after the restart, i.e. the on-disk history was replayed) |
| 7 | token/pair not in logs | PASS | `--log-cmd "ssh … journalctl --user -u caty-gateway-smoke-ollama --no-pager"` → `log_check: pass`; independent check on the host over the 117-line journal: `grep -cE '[0-9a-f]{8}\.[0-9a-f]{32}'` = 0 and a literal search for the member's `CATY_TOKEN` value = 0 |

Wall time of the final phone-sim run: 18 s (qr → done).

## phone-sim summary

```
{"claim":{"http_status":200,"latency_s":0.443},"error":null,"finished_at":"2026-09-06T19:53:00Z","gateway_url":"100.98.83.100:18771","label":"ollama@hetzner-vps","layer":"A","log_check":"pass","log_secret_leak":false,"member_id":"smoke-ollama","ok":true,"pair_id":"02f069c2","restart":{"downtime_s":0.0,"observed":false},"resume_recall":true,"session_id":"smoke-20260906-3ff4e2","stage":"done","stages":["qr","claim","turn1","turn2","restart","turn3","logcheck","done"],"started_at":"2026-09-06T19:52:42Z","turns":[{"degraded":"tts","http_status":200,"latency_s":2.777,"n":1,"reply_chars":3,"reply_preview":"OK\n"},{"degraded":"tts","http_status":200,"latency_s":2.787,"n":2,"reply_chars":55,"reply_preview":"I can help you with a simple task or answer a question."},{"degraded":"tts","http_status":200,"latency_s":2.859,"n":3,"reply_chars":11,"reply_preview":"blue-cf5dc0"}],"warnings":["restart not observed; health was already 200 after command"]}
```

(Timestamps are UTC; the host journal above is in the host's local time.)

## Findings

- Gateway defect, blocking on Linux: `setup` renders `WorkingDirectory=` and `EnvironmentFile=` with double quotes, which systemd rejects, so a fresh 0.1.4 install never reaches a running service without hand-editing the unit → #38 (S–M, `src/caty_gateway/setup_orchestrator.py` + template). The household gateways already on this host were installed another way and are unaffected.
- Tool gap, not a gateway bug: on Linux the restart is faster than the ssh round trip, so `phone-sim` cannot observe a health outage and `--require-restart-observed` can never pass from a remote host → #39 (record the journal `Stopping`/`Started` pair and the MainPID change instead, as done here).
- Observed, not a defect: `degraded: "tts"` on every turn because the VPS has no TTS engine; the gateway falls back to text and still returns 200.
- Left on the host: the isolated prefix `~/caty-smoke/` (tools, bin, the as-generated unit copy) and the member's env/state files stay for layer B or a re-run; the smoke service was stopped and disabled (`systemctl --user disable --now caty-gateway-smoke-ollama`), port 18771 released. To re-run: `systemctl --user start caty-gateway-smoke-ollama`, then `~/caty-smoke/bin/caty-gateway qr --member smoke-ollama`.
