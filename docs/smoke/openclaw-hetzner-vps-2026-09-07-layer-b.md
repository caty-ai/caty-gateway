---
route: openclaw
backend: openclaw
host: hetzner-vps (Linux VPS on the tailnet; reached over ssh as the host's admin user from the dev MBP)
os: Ubuntu 24.04.4 LTS (x86_64, CPU-only, no GPU)
date: 2026-09-07 (owner's session, evening JST; host log times below are the host's local time)
layer: B
caty_gateway_version: caty-gateway 0.1.6 (clean reinstall for the layer A run of the same day)
result: PASS
---

Layer B is the owner's real-phone pass: CatyPhone (TestFlight) on the same tailnet, the QR issued on the host with `caty-gateway qr --member <id>` (run over ssh from the dev MBP and scanned from its screen), one spoken turn, a service restart, one more spoken turn in the same conversation. The owner reported the four checks in chat on 2026-09-07; the runner (see `README.md`) logged them here and added the host-side numbers from the gateway log of the same session. The layer A record for this route/host is `openclaw-hetzner-vps-2026-09-07.md`.

Member `smoke-openclaw`, port 18774. Restart used: `systemctl --user restart caty-gateway-smoke-openclaw`.

## Checks

| # | check | result | evidence |
|---|---|---|---|
| 1 | QR scanned → paired | PASS | owner: paired |
| 2 | one spoken turn → reply | PASS | owner: reply arrived (voice via the TTS fix); log `status=ok gen_first=359.7s tts_first=361.5s` and `gen_first=262.2s` |
| 3 | restart | PASS | `systemctl --user restart`; unit `active` afterwards |
| 4 | resume turn refers to the earlier turn | PASS | owner: "OK" for the resume turn |

## Turn latencies (gateway log, same session)

`backend=openclaw:main` — `gen_first` 359.7 s and 262.2 s (the 4B tool-capable local model on CPU behind OpenClaw's full agent prompt, with two other models loaded in Ollama at the time); `tts_first` +1.8–1.9 s.

## Notes

- Turns take 4–6 minutes on this host with the smoke model; the owner accepted that for the pairing / voice / restart checks. A responsive OpenClaw route needs a GPU or a cloud model behind the smoke instance (an owner decision, not made).

## Findings

- none for the gateway.
