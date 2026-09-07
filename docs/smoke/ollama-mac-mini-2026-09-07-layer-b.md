---
route: ollama
backend: openai-compat
host: mac-mini (family Mac mini, reached over the tailnet from the dev MBP)
os: macOS 26.5.2 (arm64)
date: 2026-09-07 (owner's session, evening JST; host log times below are the host's local time)
layer: B
caty_gateway_version: caty-gateway 0.1.4 (the layer A install of 2026-09-06, UV_TOOL_DIR=~/caty-smoke/uv-tools)
result: PASS
---

Layer B is the owner's real-phone pass: CatyPhone (TestFlight) on the same tailnet, the QR issued on the host with `caty-gateway qr --member <id>` (run over ssh from the dev MBP and scanned from its screen), one spoken turn, a service restart, one more spoken turn in the same conversation. The owner reported the four checks in chat on 2026-09-07; the runner (see `README.md`) logged them here and added the host-side numbers from the gateway log of the same session. The layer A record for this route/host is `ollama-mac-mini-2026-09-06.md`.

Member `smoke-ollama`, port 18766. Restart used: `launchctl kickstart -k gui/$(id -u)/ai.caty.gateway.smoke-ollama`.

## Checks

| # | check | result | evidence |
|---|---|---|---|
| 1 | QR scanned → paired | PASS | owner: paired, no error toast |
| 2 | one spoken turn → reply | PASS | owner: reply arrived; log `status=ok gen_first=0.3s tts_first=2.3s total=2.3s` |
| 3 | restart | PASS | launchd kickstart; service back (`launchctl list` shows a new PID, health answers) |
| 4 | resume turn refers to the earlier turn | PASS | owner: continued the same conversation after the restart |

## Turn latencies (gateway log, same session)

`backend=openai-compat:huihui_ai/granite3.2-vision-abliterated:latest` — three turns in the session: `gen_first` 0.3 s / 0.4 s / 1.3 s, `tts_first` 2.3 s / 2.4 s (`mode=legacy`, audio produced).

## Notes

- The first reply's voice was the host's fallback synthesiser (the owner: "sounds like the PC default"): the smoke member had no `CATY_TTS_VOICE`, so the Fish proxy on the host refused (`Missing 'voice' field`) and the gateway fell back to the OpenClaw TTS capability. `CATY_TTS_VOICE` was set to the built-in preset (`voice_presets.py`, "おまかせ — 落ち着いた日本語") during the session and the service re-bootstrapped; later turns used that voice.
- The model addressed the owner by a made-up name: the smoke member has no user name configured and the 2B model invents one. Not a gateway behaviour.

## Findings

- none for the gateway. Configuration only (voice unset on the smoke member).
