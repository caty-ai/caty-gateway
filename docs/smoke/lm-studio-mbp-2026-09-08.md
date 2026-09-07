---
route: lm-studio
backend: openai-compat
host: dev MBP (the owner's development MacBook Pro on the tailnet; phone-sim ran on the same host)
os: macOS 26.6.2 (arm64)
date: 2026-09-08 (run 2026-09-07 18:21–18:24 UTC = 2026-09-08 01:21 host local, UTC+7; the gateway log lines below are in host local time)
layer: A
caty_gateway_version: caty-gateway 0.1.6 from PyPI (isolated prefix UV_TOOL_DIR=~/caty-smoke/uv-tools, Python 3.12.11; the same install as the Claude and Codex records on this host)
result: PASS (all seven steps)
---

Owner's decision of 2026-09-08: LM Studio is installed on the dev MBP for this route (`brew install --cask lm-studio` → LM Studio 0.4.23; the app was opened once by the owner for the Gatekeeper prompt, which also enabled the `lms` CLI). Model `google/gemma-3-1b` (Gemma 3 1B Instruct QAT 4-bit, MLX, 772 MB) fetched with `lms get google/gemma-3-1b --yes`; server started with `lms server start --port 1234` and the model loaded with `lms load google/gemma-3-1b` (19.9 s, 736 MiB). Backend check before the gateway: one `POST /v1/chat/completions` ("Reply with exactly: SMOKE-OK") → `SMOKE-OK`, 1.4 s. `CATY_OPENAI_BASE_URL=http://127.0.0.1:1234/v1`, `CATY_OPENAI_MODEL=google/gemma-3-1b`.

Member `smoke-lm-studio`, port 18783, isolated from the owner's own gateway on this host by its own member id, port, launchd label, log file and history directory. phone-sim ran on the same host per the "Same host: macOS" block of `README.md` with `--require-recall --require-restart-observed --require-log-check` and `--turn-timeout 300`.

No TTS engine is configured for the smoke member, so every turn reports `degraded: "tts"` (text-only fallback; the text reply is intact and layer A passes on it, as `README.md` states). On this route the fallback dominates the turn time — see Findings and #55.

## Steps

| # | step | result | terminal value |
|---|---|---|---|
| 1 | clean install | PASS | shared with the Claude record on this host: `uv tool install --python 3.12 caty-gateway` → `caty-gateway==0.1.6` |
| 2 | doctor all PASS | PASS | `doctor --backend openai-compat --port 18783` with the two `CATY_OPENAI_*` vars: 14 PASS, 0 FAIL (`openai-compat models`, `openai-compat model` both PASS against LM Studio's `/v1/models`) |
| 3 | setup / QR issued | PASS | `setup --member smoke-lm-studio --backend openai-compat --yes --port 18783` with `CATY_QR_DELIVERY=tty`: `Setup complete` in 0.9 s, `Setup status: succeeded / Current phase: complete`; launchd label `ai.caty.gateway.smoke-lm-studio` `state = running`; the plist carries the two `CATY_OPENAI_*` values; health on loopback answers 401 without a token; ASCII QR shown |
| 4 | pair claim (phone-sim) | PASS | self-issue via `/pair/new` then one `/pair/claim` (same host, tailnet URL): HTTP 200, 0.002 s (`pair_id` 0ceaeb9e) |
| 5 | turns 1-2 | PASS | turn 1: 200 in 56.8 s, reply `はい、わかりました。\n\nOK` (the 1B model prefixed the requested `OK`; the probe only needs a 200 with a reply); turn 2: 200 in 46.2 s, reply `I'm here to translate text and answer questions.`; gateway log: `gen=0.3s` / `gen=0.2s` — the rest of each turn is the TTS fallback (Findings); `degraded: "tts"` on both |
| 6 | restart + turn 3 (resume) | PASS | `launchctl kickstart -k gui/<uid>/ai.caty.gateway.smoke-lm-studio` with `--require-restart-observed`: phone-sim observed the outage itself — `restart.observed: true`, `downtime_s: 2.536`, `proven_by: "health-gap"`, and the held connection dropped too (`sentinel_dropped: true`); launchd pid 37557 → 44118. Turn 3: 200 in 28.8 s, `resume_recall: true` (the codeword from turn 1 came back after the restart — a recall probe, read as `README.md` says: evidence that the conversation continued across the restart, not proof of all history content) |
| 7 | token/pair not in logs | PASS | `--log-file ~/Library/Logs/caty-gateway-smoke-lm-studio.log` → `log_check: pass`; independent check over the 36-line log: `grep -cE '[0-9a-f]{8}\.[0-9a-f]{32}'` = 0, literal search for the member's `CATY_TOKEN` value = 0 |

Wall time of the phone-sim run: 135 s (qr → done), of which model generation was under 1 s in total.

## phone-sim summary

```
{"claim":{"http_status":200,"latency_s":0.002},"error":null,"finished_at":"2026-09-07T18:23:44Z","gateway_url":"100.104.116.34:18783","label":"lm-studio@mbp","layer":"A","log_check":"pass","log_secret_leak":false,"member_id":"smoke-lm-studio","ok":true,"pair_id":"0ceaeb9e","restart":{"downtime_s":2.536,"grace_s":5,"marker_changed":null,"observed":true,"proven_by":"health-gap","sentinel_dropped":true},"resume_recall":true,"session_id":"smoke-20260907-91a287","stage":"done","stages":["qr","claim","turn1","turn2","restart","turn3","logcheck","done"],"started_at":"2026-09-07T18:21:29Z","turns":[{"degraded":"tts","http_status":200,"latency_s":56.801,"n":1,"reply_chars":15,"reply_preview":"はい、わかりました。\n\nOK\n"},{"degraded":"tts","http_status":200,"latency_s":46.245,"n":2,"reply_chars":48,"reply_preview":"I’m here to translate text and answer questions."},{"degraded":"tts","http_status":200,"latency_s":28.832,"n":3,"reply_chars":12,"reply_preview":"blue-0d97d7\n"}],"warnings":[]}
```

(Timestamps are UTC; the gateway log is in host local time.)

## Findings

- Gateway: one observation filed as #55, not a smoke failure. With no TTS engine configured, each turn's text was ready after `gen` (0.2–0.3 s) but the turn returned only after the batch TTS fallback — which shells out to `openclaw capability tts convert` with a 120 s timeout — had failed: `stage=stream_tts status=batch_fallback error_type=ConnectionRefusedError` → 28–56 s later `stage=batch_tts status=text_only error_type=RuntimeError`. The Claude and Codex runs on this host paid the same fallback (≈10–12 s per turn). Routes unrelated to OpenClaw should not depend on the OpenClaw CLI failing quickly.
- Observed, not a defect: the `openai-compat` backend, pairing, the recall probe across a `launchctl kickstart -k` restart and log hygiene all PASS on the packaged 0.1.6 with LM Studio as the model server; this restart was slow enough (2.5 s) for phone-sim to observe the gap directly.
- Observed, not a defect: the 1B model decorates short answers (`はい、わかりました。` before `OK`); the recall probe still matched.
- Left on the host: LM Studio 0.4.23 (`/Applications/LM Studio.app`, `~/.lmstudio/`), the `google/gemma-3-1b` model (772 MB) and the LM Studio server on port 1234 (stopped after the run with `lms server stop`; restart with `lms server start --port 1234` and `lms load google/gemma-3-1b` before a layer B session); `~/caty-smoke/` and the member's plist/log/state files; the gateway service is left running for the owner's layer B session (`caty-gateway qr --member smoke-lm-studio` reissues the QR); to stop: `launchctl bootout gui/<uid>/ai.caty.gateway.smoke-lm-studio`. The app's first-run onboarding also downloaded `google/gemma-4-e4b` (6.9 GB), which this smoke did not use; whether to keep it is the owner's call.
