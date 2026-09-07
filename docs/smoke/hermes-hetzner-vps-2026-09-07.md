---
route: hermes
backend: hermes
host: hetzner-vps (Linux VPS on the tailnet; reached over ssh as the host's admin user from the dev MBP)
os: Ubuntu 24.04.4 LTS (x86_64, 96 vCPU, no GPU)
date: 2026-09-07 (run 2026-09-07 10:13–10:15 UTC; the host journal below is in the host's local time, UTC+2)
layer: A
caty_gateway_version: caty-gateway 0.1.4 from PyPI (isolated prefix UV_TOOL_DIR=~/caty-smoke/tools, same install as the Ollama record on this host)
result: PARTIAL (step 3 FAIL — gateway defect #38, same as the Ollama route on this host; steps 4-7 PASS after the same two-line manual fix of the generated unit)
---

Supersedes the NOT RUN version of this record (earlier on 2026-09-07: no Hermes API key that was not a household member's). With the owner's approval, a smoke-only Hermes profile `smoke` was created on the host: not a household member, API server on loopback `127.0.0.1:18773` with its own freshly generated key, inference from the host's local Ollama (`qwen3-smoke`, a `qwen3:8b` tag with `num_ctx 65536` created for this run because Hermes requires a 64k context and a thinking-capable model). No household member's profile, key or memory was used or changed. The key lives only in two `0600` files on the host (`~/caty-smoke/hermes-smoke.env`, read by `setup`, and the smoke profile's own `.env`).

Backend check before the gateway: `GET /v1/models` on the smoke server → 200 with the key, 401 without; one `POST /v1/responses` ("Reply with exactly: SMOKE-OK") → `completed`, text `SMOKE-OK`, 37 s (cold model load on CPU).

Member `smoke-hermes`, port 18772 (free). phone-sim ran on the dev MBP against the gateway's public URL on the VPS (tailnet 100.98.83.100:18772); env read, restart and journal read went over ssh, i.e. the "Another tailnet host: Linux gateway" block of `README.md`.

## Steps

| # | step | result | terminal value |
|---|---|---|---|
| 1 | clean install | PASS | shared with the Ollama record on this host: `uv tool install caty-gateway` → `caty-gateway==0.1.4` |
| 2 | doctor all PASS | PASS | `doctor --backend hermes --port 18772` with `CATY_HERMES_URL` / `CATY_HERMES_API_KEY` loaded from the env file: 14 PASS, 0 FAIL (`hermes API key`, `hermes models` both PASS; the same command without the env, run earlier in the session before the smoke key existed: 12 PASS / 2 FAIL on exactly those two checks) |
| 3 | setup / QR issued | **FAIL** | `setup --member smoke-hermes --backend hermes --yes --port 18772` with `CATY_QR_DELIVERY=tty`: env file and unit written, then systemd refused the unit (`WorkingDirectory= path is not absolute: "/home/<user>"` — quoted value), `Setup status: failed / Current phase: health` after 30.6 s, no QR issued → #38 (already filed from the Ollama route). **Workaround for the smoke only**: removed the quotes on the `WorkingDirectory=` and `EnvironmentFile=` lines of `caty-gateway-smoke-hermes.service`, `daemon-reload && enable --now` → `active`; health on the tailnet URL answers 401 without a token, as expected |
| 4 | pair claim (phone-sim) | PASS | self-issue via `/pair/new` then one `/pair/claim` from the tailnet: HTTP 200, 0.525 s (`pair_id` 8767a77a) |
| 5 | turns 1-2 | PASS | turn 1: 200 in 34.6 s, reply `OK`; turn 2: 200 in 45.1 s (CPU inference through Hermes → Ollama); `degraded: "tts"` on both (no TTS engine on this headless VPS; text reply intact) |
| 6 | restart + turn 3 (resume) | PASS (restart proven by the journal, not by a health gap) | `ssh … systemctl --user restart caty-gateway-smoke-hermes`: journal `Stopping` 12:14:32.895 → `Started` 12:14:32.916 (host local time; the two systemd lines are 21 ms apart, an upper bound on the stop→start job), `Started` lines 1 → 2, new MainPID 261024. phone-sim reports `restart.observed: false` — same cause as the Ollama route (#39: outage shorter than the ssh round trip), so `--require-restart-observed` was not used. Turn 3: 200 in 36.7 s, `resume_recall: true` (the codeword from turn 1 came back after the restart, i.e. the on-disk history was replayed through Hermes) |
| 7 | token/pair not in logs | PASS | `--log-cmd "ssh … journalctl --user -u caty-gateway-smoke-hermes --no-pager"` → `log_check: pass`; independent check on the host over the 42-line journal: `grep -cE '[0-9a-f]{8}\.[0-9a-f]{32}'` = 0, literal search for the member's `CATY_TOKEN` value = 0, literal search for the Hermes API key = 0 in both the gateway journal and the Hermes smoke server journal |

Wall time of the phone-sim run: 128 s (qr → done), dominated by the three CPU inferences.

## phone-sim summary

```
{"claim":{"http_status":200,"latency_s":0.525},"error":null,"finished_at":"2026-09-07T10:15:13Z","gateway_url":"100.98.83.100:18772","label":"hermes@hetzner-vps","layer":"A","log_check":"pass","log_secret_leak":false,"member_id":"smoke-hermes","ok":true,"pair_id":"8767a77a","restart":{"downtime_s":0.0,"observed":false},"resume_recall":true,"session_id":"smoke-20260907-0d1c50","stage":"done","stages":["qr","claim","turn1","turn2","restart","turn3","logcheck","done"],"started_at":"2026-09-07T10:13:05Z","turns":[{"degraded":"tts","http_status":200,"latency_s":34.604,"n":1,"reply_chars":2,"reply_preview":"OK"},{"degraded":"tts","http_status":200,"latency_s":45.086,"n":2,"reply_chars":77,"reply_preview":"I can help you with tasks by delegating to the right agents—what do you need?"},{"degraded":"tts","http_status":200,"latency_s":36.702,"n":3,"reply_chars":11,"reply_preview":"blue-0e5201"}],"warnings":["restart not observed; health was already 200 after command"]}
```

## Findings

- Gateway: nothing new. #38 (quoted `WorkingDirectory=` / `EnvironmentFile=` in the rendered unit) reproduces identically for the `hermes` backend; #39 (restart not observable over ssh) likewise. The `hermes` backend itself (`/v1/responses`, bearer key, history replay across restart) worked first time.
- Hermes-side notes, not gateway defects: a fresh profile needs a model with a ≥64k context window (Hermes refuses smaller ones at agent init) and one that accepts the `think` request field on Ollama's OpenAI endpoint (gemma3 returns HTTP 400 "does not support thinking"); the `qwen3-smoke` tag satisfies both. The profile's `.env` (cloned from a non-member test profile) carried that profile's API server port and had to be rewritten for the smoke server.
- Observed, not a defect: `degraded: "tts"` on every turn (no TTS engine on the VPS); 35–45 s turns are CPU inference, not gateway latency.
- Left on the host, all stopped: Hermes profile `smoke` and its `hermes-gateway-smoke` user unit (stopped; `systemctl --user start hermes-gateway-smoke` to bring the API server back on 18773), the Ollama tag `qwen3-smoke` (remove with `ollama rm qwen3-smoke`), the `smoke-hermes` member files and its unit (disabled; `systemctl --user start caty-gateway-smoke-hermes` then `~/caty-smoke/bin/caty-gateway qr --member smoke-hermes` for layer B), and `~/caty-smoke/` with the key file. Household units untouched: `systemctl --user is-active` over the 16 household caty-gateway / Hermes / OpenClaw units after the run = 16 × `active`.
