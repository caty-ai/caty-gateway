---
route: hermes
backend: hermes
host: hetzner-vps (Linux VPS on the tailnet; reached over ssh as the host's admin user from the dev MBP)
os: Ubuntu 24.04.4 LTS (x86_64, 96 vCPU, no GPU)
date: 2026-09-07 (run 2026-09-07 16:54–16:57 UTC; the host journal below is in the host's local time, UTC+2)
layer: A
caty_gateway_version: caty-gateway 0.1.6 from PyPI (isolated prefix UV_TOOL_DIR=~/caty-smoke/tools, uv-managed Python 3.11.15; the same install as the Ollama and OpenClaw 0.1.6 records on this host)
result: PASS (all seven steps; step 3 re-done from scratch on 0.1.6 — #38 fixed; step 6 restart proven by phone-sim itself — #39 fixed)
---

Supersedes the 0.1.4 PARTIAL version of this record (run 2026-09-07 10:13–10:15 UTC): step 3 was FAIL there (gateway defect #38, the quoted `WorkingDirectory=` / `EnvironmentFile=` lines in the rendered unit) and step 6 could only be proven from the host journal (#39). Both are fixed in what is on main now — #38 in the 0.1.6 package, #39 in `tools/smoke/phone-sim.py` (PR #50) — so this run repeats steps 2–7 with a fresh setup on 0.1.6 and the current tool. The 0.1.4 findings stay in the git history of this file.

Backend, unchanged from the 0.1.4 run: the smoke-only Hermes profile `smoke` on the host (not a household member; API server on loopback `127.0.0.1:18773` with its own key; inference from the host's local Ollama, `qwen3-smoke`, a `qwen3:8b` tag with a 64k context). The key lives only in two `0600` files on the host (`~/caty-smoke/hermes-smoke.env`, read by `setup`, and the smoke profile's own `.env`). Its API server unit had been stopped after the layer B session and was started for this run: `GET /v1/models` → 200 with the key, 401 without.

Member `smoke-hermes`, port 18772. phone-sim ran on the dev MBP against the gateway's public URL on the VPS (tailnet 100.98.83.100:18772); env read, restart and journal read went over ssh, i.e. the "Another tailnet host: Linux gateway" block of `README.md`, with `--require-restart-observed` and `--turn-timeout 300` (CPU inference).

The member's env file carries two voice settings added for the layer B session of 2026-09-07 (`CATY_TTS_PROXY`, `CATY_TTS_VOICE`, values not recorded). Because `setup` regenerates the env file, they were backed up before step 3 and appended again afterwards, then the service was restarted once before step 4. They are why the turns below no longer report `degraded: "tts"`.

## Steps

| # | step | result | terminal value |
|---|---|---|---|
| 1 | clean install | PASS | shared with the Ollama 0.1.6 record on this host: `uv tool list` → `caty-gateway v0.1.6`; service Python 3.11.15 |
| 2 | doctor all PASS | PASS | `doctor --backend hermes --port 18772` with `CATY_HERMES_URL` / `CATY_HERMES_API_KEY` loaded from the env file: 14 PASS, 0 FAIL (`hermes API key`, `hermes models` both PASS) |
| 3 | setup / QR issued | PASS | The 0.1.4 attempt had left resume metadata (`~/.local/state/caty-gateway/setup/smoke-hermes.json`, its health phase had failed) and the env/unit had been hand-edited since (the #38 workaround, the voice lines), so — as on the Ollama route — the old env and unit were moved aside (copies under `~/caty-smoke/backup-20260907/`, 0600), the old unit disabled, and `setup --reset --member smoke-hermes --backend hermes --yes --port 18772` run with `CATY_QR_DELIVERY=tty`: `Setup complete` in 1.20 s, `Setup status: succeeded / Current phase: complete`, resume metadata deleted on success, unit `active`, health on the tailnet URL answers 401 without a token. The rendered unit has unquoted `WorkingDirectory=/home/<user>` and `EnvironmentFile=/home/<user>/.config/caty-gateway/smoke-hermes.env` — **#38 confirmed fixed in 0.1.6 for the `hermes` backend too** (the 0.1.4 copy with the quotes is kept next to it as `smoke-hermes.service.as-generated`). ASCII QR shown |
| 4 | pair claim (phone-sim) | PASS | self-issue via `/pair/new` then one `/pair/claim` from the tailnet: HTTP 200, 0.524 s (`pair_id` 6169adab) |
| 5 | turns 1-2 | PASS | turn 1: 200 in 36.7 s, reply `OK`; turn 2: 200 in 49.0 s, a 47-character Japanese sentence saying it can help with tasks using sub-agents and tools (the preview is redacted in the summary below because the smoke profile's cloned persona addresses the owner by name); `degraded: null` on both (TTS through the proxy configured for layer B) |
| 6 | restart + turn 3 (resume) | PASS | `ssh … systemctl --user restart caty-gateway-smoke-hermes` with `--require-restart-observed`: the gateway was back before any probe failed twice, so `restart.observed: false`, `downtime_s: 0.0` — and phone-sim proved the restart anyway: the idle connection it held across the restart was closed by the old process (`proven_by: "connection-drop"`, `sentinel_dropped: true`). Host side: journal `Stopping` 18:56:22 → `Started` 18:56:22, MainPID 857664 → 892735. Turn 3: 200 in 33.6 s, `resume_recall: true` (the codeword from turn 1 came back after the restart — a recall probe, read as `README.md` says: evidence that the conversation continued across the restart through Hermes, not proof of all history content) |
| 7 | token/pair not in logs | PASS | `--log-cmd "ssh … journalctl --user -u caty-gateway-smoke-hermes --no-pager"` → `log_check: pass`; independent check on the host over the 172-line gateway journal: `grep -cE '[0-9a-f]{8}\.[0-9a-f]{32}'` = 0, literal search for the member's current `CATY_TOKEN` value = 0, literal search for the Hermes API key = 0 in both the gateway journal and the Hermes smoke server journal |

Wall time of the phone-sim run: 137 s (qr → done), dominated by the three CPU inferences.

## phone-sim summary

The `reply_preview` of turn 2 is replaced by `[redacted: 47-character greeting naming the owner]`; everything else is the tool's output verbatim.

```
{"claim":{"http_status":200,"latency_s":0.524},"error":null,"finished_at":"2026-09-07T16:57:05Z","gateway_url":"100.98.83.100:18772","label":"hermes@hetzner-vps","layer":"A","log_check":"pass","log_secret_leak":false,"member_id":"smoke-hermes","ok":true,"pair_id":"6169adab","restart":{"downtime_s":0.0,"grace_s":5,"marker_changed":null,"observed":false,"proven_by":"connection-drop","sentinel_dropped":true},"resume_recall":true,"session_id":"smoke-20260907-8d9cc5","stage":"done","stages":["qr","claim","turn1","turn2","restart","turn3","logcheck","done"],"started_at":"2026-09-07T16:54:48Z","turns":[{"degraded":null,"http_status":200,"latency_s":36.719,"n":1,"reply_chars":2,"reply_preview":"OK"},{"degraded":null,"http_status":200,"latency_s":49.002,"n":2,"reply_chars":47,"reply_preview":"[redacted: 47-character greeting naming the owner]"},{"degraded":null,"http_status":200,"latency_s":33.558,"n":3,"reply_chars":11,"reply_preview":"blue-55dabb"}],"warnings":["restart not observed as a health gap; proven by connection-drop"]}
```

(Timestamps are UTC; the host journal above is in the host's local time.)

## Findings

- Gateway: nothing new. #38 is confirmed fixed for the `hermes` backend on this host (step 3); `/v1/responses` with a bearer key, pairing, the recall probe across restart and log hygiene behaved as on 0.1.4.
- Tool: #39 is confirmed fixed on the slow route as well — the Hermes gateway restarts faster than one probe, and `--require-restart-observed` passed by the held-connection proof.
- Observed, not a defect: the same `setup` guard and env-regeneration behaviour as on the Ollama route (old env/unit moved aside, voice lines re-appended).
- Observed, not a defect: the smoke Hermes profile was cloned from a household profile for the 0.1.4 run and its persona greets the owner by name; that shows up only in the free-text reply and is redacted here. A future smoke profile should be created with a neutral persona so records need no redaction.
- Left on the host: as on the Ollama route — `~/caty-smoke/` (tools, bin, unit copies, `backup-20260907/`), the member's env/state files and the smoke Hermes profile stay; `caty-gateway-smoke-hermes` and the Hermes smoke API server (`hermes-gateway-smoke`) were stopped, ports 18772/18773 released. The member's `CATY_TOKEN` and pairing credential were regenerated by this setup, so a phone paired during the earlier layer B session must scan a new QR.
