---
route: openclaw
backend: openclaw
host: hetzner-vps (Linux VPS on the tailnet; reached over ssh as the host's admin user from the dev MBP)
os: Ubuntu 24.04.4 LTS (x86_64, CPU-only, no GPU)
date: 2026-09-07 (runs 2026-09-07 12:00–12:22 UTC; the host journal below is in the host's local time, UTC+2)
layer: A
caty_gateway_version: caty-gateway 0.1.6 from PyPI (`uv tool install --reinstall caty-gateway` into the isolated prefix UV_TOOL_DIR=~/caty-smoke/tools; the Ollama and Hermes records on this host were made on 0.1.4)
result: PARTIAL (steps 1-5 and 7 PASS; step 6 = restart PASS, resume probe FAIL — the 4B local model does not recall the codeword even without the gateway in the loop, while the OpenClaw session itself continued across the restart)
---

Supersedes the NOT RUN version of this record (earlier on 2026-09-07: no OpenClaw agent on the host that was not a household member's). With the owner's approval, a smoke-only OpenClaw instance was created on the host: its own state directory, a gateway on loopback with its own freshly generated token, one agent (`main`, display name `smoke`), model from the host's local Ollama (`PetrosStav/gemma3-tools:4b`, the smallest tool-capable model pulled there; `qwen3:8b` timed out at 600 s per turn on this CPU-only host, `gemma3:1b` is rejected by OpenClaw because it cannot take tool payloads). No household member's instance, token or workspace was used or changed. The token lives only in two `0600` files on the host (the instance's `openclaw.json` and `~/caty-smoke/openclaw-smoke.env`, read by `setup`). `OPENCLAW_BIN` points at a two-line wrapper in `~/caty-smoke/bin/` that pins the Node runtime and the state directory before exec'ing the real binary, because the gateway's systemd unit carries a fixed `PATH` without the Node version manager.

Backend check before the gateway: `openclaw agent --agent main -m "Reply with exactly: SMOKE-OK" --json` → `status: ok`, text `SMOKE-OK`, 155 s (CPU inference; a second turn took 148 s).

Member `smoke-openclaw`, port 18774 (free). phone-sim ran on the dev MBP against the gateway's public URL on the VPS (tailnet 100.98.83.100:18774) with `--turn-timeout 600`; env read, restart and journal read went over ssh, i.e. the "Another tailnet host: Linux gateway" block of `README.md`. Two runs, 12:00 and 12:12 UTC, with the same pass/fail outcome at every step.

## Steps

| # | step | result | terminal value |
|---|---|---|---|
| 1 | clean install | PASS | `uv tool install --reinstall caty-gateway` → `caty-gateway==0.1.6` (replacing 0.1.4 in the same prefix); `caty-gateway --help` OK |
| 2 | doctor all PASS | PASS | `doctor --backend openclaw --port 18774` with `OPENCLAW_BIN`, `CATY_AGENT=main`, `CATY_GATEWAY_TOKEN` set: 16 PASS, 0 FAIL (`openclaw agents`, `openclaw agent`, `openclaw gateway token`, `openclaw gateway` all PASS) |
| 3 | setup / QR issued | PASS | `setup --member smoke-openclaw --backend openclaw --yes --port 18774` with `CATY_QR_DELIVERY=tty`: `Setup complete` in 2.4 s, `Setup status: succeeded / Current phase: complete`, unit `active`; the rendered unit has unquoted `WorkingDirectory=` / `EnvironmentFile=` — **#38 confirmed fixed in 0.1.6** on the host where it was found; ASCII QR shown; health on the tailnet URL answers 401 without a token, as expected |
| 4 | pair claim (phone-sim) | PASS | self-issue via `/pair/new` then one `/pair/claim` from the tailnet: HTTP 200, 0.525 s (`pair_id` 38a223fa on the second run; 199b9021 on the first) |
| 5 | turns 1-2 | PASS | run 2: turn 1: 200 in 181.4 s, reply `OK.`; turn 2: 200 in 193.7 s; `degraded: null` on both (run 1: 182.5 s / 192.4 s, turn 1 `degraded: "tts"`). Gateway journal: `backend=openclaw:main status=ok gen_first≈180-190s` — the latency is the 4B model on CPU behind OpenClaw's full agent prompt, not the gateway |
| 6 | restart + turn 3 (resume) | PARTIAL (restart PASS by the journal; resume probe FAIL, attributed to the model) | `ssh … systemctl --user restart caty-gateway-smoke-openclaw`: journal `Stopping` 14:18:53.462 → `Started` 14:18:53.478 (host local time; the two systemd lines are 16 ms apart — they bound the unit's stop→start job, not HTTP availability), `Started` lines in the unit journal 1 after setup → 2 after run 1's restart → 3 after run 2's restart. `restart.observed: false` for the same reason as the other routes on this host (#39). Turn 3: 200 in 191.1 s but `resume_recall: false` — the model answered "Phoenix" in both runs. Attribution: (a) the OpenClaw agent database on the host holds run 1's turn 1 (codeword prompt) and turn 3 ("Phoenix" reply) under **one** session node whose key carries the phone-sim session id, i.e. the gateway resumed the same OpenClaw session after its restart; (b) a two-turn probe run directly against the OpenClaw agent with one `--session-key` and **no caty-gateway and no restart** ("The codeword is teal-…" → "What was the codeword?") also failed to recall (replied `OK`). The recall miss reproduces without the gateway, so it is the model, as in `ollama-mac-mini-2026-09-06.md` (a small model missed the same probe in two of four runs there) |
| 7 | token/pair not in logs | PASS | phone-sim stopped before its own log check (`log_check: skipped`, a consequence of the step 6 failure), so the check was done independently on the host over the gateway journal covering both runs (71 lines): `grep -cE '[0-9a-f]{8}\.[0-9a-f]{32}'` = 0, literal search for the member's `CATY_TOKEN` = 0, literal search for the OpenClaw gateway token = 0 in both the gateway journal and the smoke OpenClaw journal |

Wall time per phone-sim run: 575–579 s (three CPU inferences of ~3 min each).

## phone-sim summary

```
{"claim":{"http_status":200,"latency_s":0.525},"error":"resume recall required but codeword was not recalled","finished_at":"2026-09-07T12:22:05Z","gateway_url":"100.98.83.100:18774","label":"openclaw@hetzner-vps","layer":"A","log_check":"skipped","log_secret_leak":null,"member_id":"smoke-openclaw","ok":false,"pair_id":"38a223fa","restart":{"downtime_s":0.0,"observed":false},"resume_recall":false,"session_id":"smoke-20260907-eda4ae","stage":"turn3","stages":["qr","claim","turn1","turn2","restart","turn3"],"started_at":"2026-09-07T12:12:30Z","turns":[{"degraded":null,"http_status":200,"latency_s":181.41,"n":1,"reply_chars":3,"reply_preview":"OK."},{"degraded":null,"http_status":200,"latency_s":193.742,"n":2,"reply_chars":49,"reply_preview":"OK. What URL and title would you like me to send?"},{"degraded":null,"http_status":200,"latency_s":191.073,"n":3,"reply_chars":39,"reply_preview":"The codeword was “Phoenix”. [chuckling]"}],"warnings":["restart not observed; health was already 200 after command","turn 3 did not recall the codeword"]}
```

(The line above is run 2, 12:12 UTC, the final run. Run 1, 12:00 UTC, differed only in ids and wording: `pair_id` 199b9021, session `smoke-20260907-ab9bb7`, turns 182.5 / 192.4 / 196.6 s, turn 3 preview `[whispering] Oh, that’s a tricky one… Let me see… It was “Phoenix.”`.)

## Findings

- Gateway: nothing new. The `openclaw` backend (agent turn over the resident gateway, session key per phone session, resume after the gateway restart) worked; #38 is confirmed fixed by 0.1.6 on the host where it was found; #39 (restart not observable over ssh) reproduces.
- Tool note for `phone-sim`: when the recall probe fails, the run stops before `logcheck`, so step 7 has to be done by hand — worth a follow-up so a failed probe still audits the log (same family as #39).
- Backend notes, not gateway defects: a tool-capable local model is required (OpenClaw always sends the tool schema); ~3 min per turn on this CPU-only host for a 4B model, so `--turn-timeout` must be raised; the 4B model cannot pass the recall probe. A layer B or a PASS-grade layer A on this host needs either a GPU or a stronger tool-capable model.
- Left on the host, all stopped: the smoke OpenClaw instance (state directory, gateway unit `openclaw-smoke`, stopped; `systemctl --user start openclaw-smoke` brings it back on loopback 19609), the wrapper `~/caty-smoke/bin/openclaw-smoke`, the `smoke-openclaw` member files and unit (disabled; `systemctl --user start caty-gateway-smoke-openclaw` then `~/caty-smoke/bin/caty-gateway qr --member smoke-openclaw` for layer B), `~/caty-smoke/` (now caty-gateway 0.1.6). Household services untouched: `systemctl --user is-active` over every household unit after the runs reported only `active`.
