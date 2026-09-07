---
route: claude
backend: claude
host: dev MBP (the owner's development MacBook Pro on the tailnet; phone-sim ran on the same host)
os: macOS 26.6.2 (arm64)
date: 2026-09-08 (run 2026-09-07 17:31–17:33 UTC = 2026-09-08 00:31 host local, UTC+7; the gateway log lines below are in host local time)
layer: A
caty_gateway_version: caty-gateway 0.1.6 from PyPI (`uv tool install --python 3.12 caty-gateway` → Python 3.12.11, 1.1 s, wheels cached), isolated prefix UV_TOOL_DIR=~/caty-smoke/uv-tools, UV_TOOL_BIN_DIR=~/caty-smoke/bin
result: PASS (all seven steps)
---

Owner's decision of 2026-09-08: routes 1, 2 and 6 run on the dev MBP instead of the Mac mini, whose Claude CLI is not logged in (`claude-mac-mini-2026-09-06.md`, FAIL at step 5 for that reason — that record stays as history and is not superseded, it describes a different host). The MBP's Claude CLI (2.1.263) is logged in; the smoke member is isolated from the owner's own gateway on this host by its own member id, port, launchd label, log file and history directory. Nothing of the owner's gateway was touched.

Member `smoke-claude`, port 18781. phone-sim ran on the same host per the "Same host: macOS" block of `README.md` (env from the 0600 launchd plist via the captured extraction command, restart with `launchctl kickstart -k`, log file `~/Library/Logs/caty-gateway-smoke-claude.log`), with `--require-recall --require-restart-observed --require-log-check` and `--turn-timeout 300` (each turn spawns the Claude CLI).

No TTS engine is configured for the smoke member, so every turn reports `degraded: "tts"` (`stage=stream_tts status=batch_fallback error_type=ConnectionRefusedError` → `stage=batch_tts status=text_only`): the text reply is intact and layer A passes on it, as `README.md` states. For layer B the member needs a voice, as on the VPS smoke members.

## Steps

| # | step | result | terminal value |
|---|---|---|---|
| 1 | clean install | PASS | `uv tool install --python 3.12 caty-gateway` into the isolated prefix (1.1 s) → `caty-gateway==0.1.6`, `qrcode==8.2`, `pillow==12.3.0`; `caty-gateway --help` OK; uv 0.11.21 was already on the host |
| 2 | doctor all PASS | PASS with 1 WARN | `doctor --backend claude --port 18781`: 14 PASS, 1 WARN `claude credentials: sign in with Claude CLI; credentials stored in the OS keychain cannot be checked passively`, 0 FAIL — the same WARN as on the Mac mini; the login itself was verified with `claude -p "Reply with just OK" --output-format json` → `is_error: false`, result `OK` |
| 3 | setup / QR issued | PASS | `setup --member smoke-claude --backend claude --yes --port 18781` with `CATY_QR_DELIVERY=tty`: `Setup complete` in 3.0 s, `Setup status: succeeded / Current phase: complete`; launchd label `ai.caty.gateway.smoke-claude` `state = running`; health on loopback answers 401 without a token; ASCII QR shown |
| 4 | pair claim (phone-sim) | PASS | self-issue via `/pair/new` then one `/pair/claim` (same host, tailnet URL): HTTP 200, 0.003 s (`pair_id` 7688395b) |
| 5 | turns 1-2 | PASS | turn 1: 200 in 22.2 s, reply `OK` (`gen=10.2s` for the CLI); turn 2: 200 in 21.7 s, a 52-character Japanese sentence (the preview is redacted in the summary below because the CLI answered in the owner's assistant persona and addressed the owner by name — the smoke member runs the same logged-in CLI, with the host's user-level instructions); `degraded: "tts"` on both, see above |
| 6 | restart + turn 3 (resume) | PASS | `launchctl kickstart -k gui/<uid>/ai.caty.gateway.smoke-claude` with `--require-restart-observed`: the new process was listening before any probe failed twice, so `restart.observed: false`, `downtime_s: 0.0`; phone-sim proved the restart by the idle connection it held across it (`proven_by: "connection-drop"`, `sentinel_dropped: true`); launchd pid 73441 → 78886. Turn 3: 200 in 25.8 s, `resume_recall: true` (the codeword from turn 1 came back after the restart — a recall probe, read as `README.md` says: evidence that the conversation continued across the restart, not proof of all history content) |
| 7 | token/pair not in logs | PASS | `--log-file ~/Library/Logs/caty-gateway-smoke-claude.log` → `log_check: pass`; independent check over the 36-line log: `grep -cE '[0-9a-f]{8}\.[0-9a-f]{32}'` = 0, literal search for the member's `CATY_TOKEN` value = 0 |

Wall time of the phone-sim run: 74 s (qr → done), three CLI generations of 10–13 s each.

## phone-sim summary

The `reply_preview` of turn 2 is replaced by `[redacted: 52-character reply in the owner's assistant persona]`; everything else is the tool's output verbatim.

```
{"claim":{"http_status":200,"latency_s":0.003},"error":null,"finished_at":"2026-09-07T17:32:47Z","gateway_url":"100.104.116.34:18781","label":"claude@mbp","layer":"A","log_check":"pass","log_secret_leak":false,"member_id":"smoke-claude","ok":true,"pair_id":"7688395b","restart":{"downtime_s":0.0,"grace_s":5,"marker_changed":null,"observed":false,"proven_by":"connection-drop","sentinel_dropped":true},"resume_recall":true,"session_id":"smoke-20260907-a39484","stage":"done","stages":["qr","claim","turn1","turn2","restart","turn3","logcheck","done"],"started_at":"2026-09-07T17:31:33Z","turns":[{"degraded":"tts","http_status":200,"latency_s":22.208,"n":1,"reply_chars":2,"reply_preview":"OK"},{"degraded":"tts","http_status":200,"latency_s":21.725,"n":2,"reply_chars":52,"reply_preview":"[redacted: 52-character reply in the owner's assistant persona]"},{"degraded":"tts","http_status":200,"latency_s":25.774,"n":3,"reply_chars":11,"reply_preview":"blue-aeba39"}],"warnings":["restart not observed as a health gap; proven by connection-drop"]}
```

(Timestamps are UTC; the gateway log is in host local time.)

## Findings

- Gateway: nothing new. The `claude` backend (CLI spawn per turn), pairing, the recall probe across a `launchctl kickstart -k` restart and log hygiene all PASS on the packaged 0.1.6.
- Observed, not a defect: macOS `kickstart -k` on this host restarted the gateway faster than one probe (the Mac mini run of 2026-09-06 saw 0.5 s of downtime); the tool's held-connection proof covered it (#39).
- Observed, not a defect: the smoke member answers in the owner's assistant persona because the Claude CLI applies the host user's instructions; a smoke on a host with a neutral CLI profile would not need the redaction above.
- Observed, not a defect: `degraded: "tts"` on every turn — no TTS engine for the smoke member (text-only fallback, as designed).
- Left on the host: the isolated prefix `~/caty-smoke/` and the member's plist/log/state files stay for layer B; the service is left running for the owner's layer B session (`caty-gateway qr --member smoke-claude` from `~/caty-smoke/bin` reissues the QR); to stop: `launchctl bootout gui/<uid>/ai.caty.gateway.smoke-claude`.
