---
route: hermes
backend: hermes
host: hetzner-vps (Linux VPS on the tailnet; reached over ssh as the host's admin user from the dev MBP)
os: Ubuntu 24.04.4 LTS (x86_64, CPU-only, no GPU)
date: 2026-09-07 (owner's session, evening JST; host log times below are the host's local time)
layer: B
caty_gateway_version: caty-gateway 0.1.6 (see the Ollama VPS layer B record for the prefix note)
result: PASS
---

Layer B is the owner's real-phone pass: CatyPhone (TestFlight) on the same tailnet, the QR issued on the host with `caty-gateway qr --member <id>` (run over ssh from the dev MBP and scanned from its screen), one spoken turn, a service restart, one more spoken turn in the same conversation. The owner reported the four checks in chat on 2026-09-07; the runner (see `README.md`) logged them here and added the host-side numbers from the gateway log of the same session. The layer A record for this route/host is `hermes-hetzner-vps-2026-09-07.md`.

Member `smoke-hermes`, port 18772. Restart used: `systemctl --user restart caty-gateway-smoke-hermes`.

## Checks

| # | check | result | evidence |
|---|---|---|---|
| 1 | QR scanned → paired | PASS | owner: paired |
| 2 | one spoken turn → reply | PASS | owner: reply arrived, voice after the TTS fix; log `status=ok gen_first=51.6s tts_first=53.6s total=53.6s` |
| 3 | restart | PASS | `systemctl --user restart`; unit `active` afterwards |
| 4 | resume turn refers to the earlier turn | PASS | owner: conversation continued after the restart |

## Turn latencies (gateway log, same session)

`backend=hermes:http://127.0.0.1:18773` — `gen_first` 36.2 s (text-only turn before the TTS fix) and 51.6 s (with voice, `tts_first` +2.0 s). The whole latency is the smoke Hermes profile's 8B local model on CPU; during this session Ollama held three models in memory (load average ≈46), which added ~10 s over the layer A turns.

## Notes

- The agent introduces itself as "Beta": the smoke Hermes profile's SOUL.md was cloned from the owner's test profile when the profile was created (no keys or memory were cloned; API key and port were replaced). The owner noted this persona had the best Japanese of the four routes.
- Slow turns and occasional odd answers are the local 8B model, not the gateway; the layer A record explains the model choice.

## Findings

- none for the gateway.
