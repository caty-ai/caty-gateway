---
route: ollama
backend: openai-compat
host: hetzner-vps (Linux VPS on the tailnet; reached over ssh as the host's admin user from the dev MBP)
os: Ubuntu 24.04.4 LTS (x86_64, 96 vCPU, no GPU)
date: 2026-09-07 (runs 2026-09-07 16:45–16:54 UTC; the host journal below is in the host's local time, UTC+2)
layer: A
caty_gateway_version: caty-gateway 0.1.6 from PyPI (installed on 2026-09-07 with `uv tool install --reinstall caty-gateway` into the isolated prefix UV_TOOL_DIR=~/caty-smoke/tools, uv-managed Python 3.11.15; the same install as the OpenClaw 0.1.6 record on this host)
result: PASS (all seven steps; step 3 re-done from scratch on 0.1.6 — #38 fixed; step 6 restart proven by phone-sim itself — #39 fixed)
---

Supersedes the 0.1.4 PARTIAL version of this record (2026-09-06 run, recorded 2026-09-07): step 3 was FAIL there (gateway defect #38, the quoted `WorkingDirectory=` / `EnvironmentFile=` lines in the rendered unit) and step 6 could only be proven from the host journal (#39, phone-sim could not observe a restart faster than the ssh round trip). Both are fixed in what is on main now — #38 in the 0.1.6 package, #39 in `tools/smoke/phone-sim.py` (PR #50) — so this run repeats steps 2–7 with a fresh setup on 0.1.6 and the current tool. The 0.1.4 findings stay in the git history of this file.

Backend: Ollama 0.20.2 already running on the host on `127.0.0.1:11434` (CPU only), model `gemma3:1b`, unchanged from the 0.1.4 run. `CATY_OPENAI_BASE_URL=http://127.0.0.1:11434/v1`. Member `smoke-ollama`, port 18771 (the host also runs other gateways for household members on their own ports — none was touched).

phone-sim ran on the dev MBP against the gateway's public URL on the VPS (tailnet 100.98.83.100:18771); the env read, the restart and the journal read went over ssh, i.e. the "Another tailnet host: Linux gateway" block of `README.md`, this time with `--require-restart-observed` in the command, as that block shows.

The member's env file carries two voice settings added for the layer B session of 2026-09-07 (`CATY_TTS_PROXY`, `CATY_TTS_VOICE`, values not recorded). Because `setup` regenerates the env file, they were backed up before step 3 and appended again afterwards, then the service was restarted once before step 4. They are why the turns below no longer report `degraded: "tts"`.

## Steps

| # | step | result | terminal value |
|---|---|---|---|
| 1 | clean install | PASS | `uv tool list` in the isolated prefix → `caty-gateway v0.1.6` (reinstalled from PyPI on 2026-09-07 for the OpenClaw record; not reinstalled again for this run); `caty-gateway --help` OK; service Python 3.11.15 |
| 2 | doctor all PASS | PASS | `doctor --backend openai-compat --port 18771` with the two `CATY_OPENAI_*` vars: 14 PASS, 0 FAIL |
| 3 | setup / QR issued | PASS | First attempt was refused by preflight — `target configuration changed outside this setup job; restore it or use --reset with a new member` — because the 0.1.4 attempt had left resume metadata (`~/.local/state/caty-gateway/setup/smoke-ollama.json`, its health phase had failed) and the env/unit had been hand-edited since (the #38 workaround, the voice lines). That is the guard working as designed. Then, as the message says: the hand-edited env and unit were moved aside (copies kept under `~/caty-smoke/backup-20260907/`, 0600), `systemctl --user disable --now` on the old unit, and `setup --reset --member smoke-ollama --backend openai-compat --yes --port 18771` with `CATY_QR_DELIVERY=tty`: `Setup complete` in 1.19 s, `Setup status: succeeded / Current phase: complete`, resume metadata deleted on success, unit `active`, health on the tailnet URL answers 401 without a token. The rendered unit has unquoted `WorkingDirectory=/home/<user>` and `EnvironmentFile=/home/<user>/.config/caty-gateway/smoke-ollama.env` — **#38 confirmed fixed in 0.1.6 on the host where it was found** (the 0.1.4 copy with the quotes is kept next to it as `smoke-ollama.service.as-generated`). ASCII QR shown |
| 4 | pair claim (phone-sim) | PASS | self-issue via `/pair/new` then one `/pair/claim` from the tailnet: HTTP 200, 0.527 s (`pair_id` 03535f98) |
| 5 | turns 1-2 | PASS | turn 1: 200 in 2.21 s, reply `OK`; turn 2: 200 in 3.04 s; `degraded: null` on both (TTS through the proxy configured for layer B) |
| 6 | restart + turn 3 (resume) | PASS | `ssh … systemctl --user restart caty-gateway-smoke-ollama` with `--require-restart-observed`: phone-sim probed `/health` concurrently with the command and saw the outage itself — `restart.observed: true`, `downtime_s: 1.18`, `proven_by: "health-gap"`, and the idle connection it held across the restart was closed too (`sentinel_dropped: true`). Host side: journal `Stopping` 18:53:26 → `Started` 18:53:26, MainPID 854972 → 856529. Turn 3: 200 in 3.17 s, `resume_recall: true` (the codeword from turn 1 came back after the restart — a recall probe, read as `README.md` says: evidence that the conversation continued across the restart, not proof of all history content). This was the second run of the tool against this setup: the first, one minute earlier, passed every check except the recall probe — the 1B model answered `blue-7373` for a `blue-` + 6-hex codeword (a fragment of the codeword came back, the model mangled the rest; the same model and member recalled correctly in the 0.1.4 run of this record and in the two #39 live runs of PR #50 earlier the same day) — and phone-sim proved that restart by `connection-drop` (`observed: false`, one transient probe failure ignored by the tool's two-strike rule). The recall probe is a model-quality probe (see `README.md`), so the run was repeated once and the passing run is the one recorded; nothing on the host was changed between the two |
| 7 | token/pair not in logs | PASS | `--log-cmd "ssh … journalctl --user -u caty-gateway-smoke-ollama --no-pager"` → `log_check: pass`; independent check on the host over the 338-line journal (both runs of the day plus the setup): `grep -cE '[0-9a-f]{8}\.[0-9a-f]{32}'` = 0 and a literal search for the member's current `CATY_TOKEN` value = 0 |

Wall time of the recorded phone-sim run: 22 s (qr → done).

## phone-sim summary

```
{"claim":{"http_status":200,"latency_s":0.527},"error":null,"finished_at":"2026-09-07T16:53:34Z","gateway_url":"100.98.83.100:18771","label":"ollama@hetzner-vps","layer":"A","log_check":"pass","log_secret_leak":false,"member_id":"smoke-ollama","ok":true,"pair_id":"03535f98","restart":{"downtime_s":1.18,"grace_s":5,"marker_changed":null,"observed":true,"proven_by":"health-gap","sentinel_dropped":true},"resume_recall":true,"session_id":"smoke-20260907-7a38d9","stage":"done","stages":["qr","claim","turn1","turn2","restart","turn3","logcheck","done"],"started_at":"2026-09-07T16:53:12Z","turns":[{"degraded":null,"http_status":200,"latency_s":2.214,"n":1,"reply_chars":3,"reply_preview":"OK\n"},{"degraded":null,"http_status":200,"latency_s":3.039,"n":2,"reply_chars":28,"reply_preview":"I can answer your questions."},{"degraded":null,"http_status":200,"latency_s":3.171,"n":3,"reply_chars":11,"reply_preview":"blue-708eb6"}],"warnings":[]}
```

(Timestamps are UTC; the host journal above is in the host's local time.)

## Findings

- Gateway: nothing new. #38 is confirmed fixed on this host (step 3); the `openai-compat` backend, pairing, history replay across restart and log hygiene behaved as on 0.1.4.
- Tool: #39 is confirmed fixed from this host pair — `--require-restart-observed` passes over ssh, once by an observed 1.18 s gap and once (first run) by the held-connection proof when the gap was shorter than one probe.
- Observed, not a defect: `setup` refuses to overwrite a member whose env/unit were edited outside setup and points at `--reset`; a re-install of an existing smoke member therefore needs the old env/unit moved aside first (backups kept). The regenerated env drops any hand-added lines, so the layer B voice settings had to be re-appended.
- Observed, not a defect: the recall probe is probabilistic with a 1B model (one miss in the two runs of this record; the 0.1.4 run and the two #39 live runs in PR #50 recalled correctly); a miss shows as `resume_recall: false` with the codeword fragment in `reply_preview`, not as a gateway error.
- Left on the host: the isolated prefix `~/caty-smoke/` (tools, bin, unit copies, `backup-20260907/`) and the member's env/state files stay for a re-run or layer B; the smoke service was stopped (`systemctl --user stop caty-gateway-smoke-ollama`), port 18771 released. The member's `CATY_TOKEN` and pairing credential were regenerated by this setup, so a phone paired during the earlier layer B session must scan a new QR (`~/caty-smoke/bin/caty-gateway qr --member smoke-ollama` after `systemctl --user start caty-gateway-smoke-ollama`).
