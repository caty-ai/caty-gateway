---
route: ollama
backend: openai-compat
host: hetzner-vps (Linux VPS on the tailnet; reached over ssh as the host's admin user from the dev MBP)
os: Ubuntu 24.04.4 LTS (x86_64, CPU-only, no GPU)
date: 2026-09-07 (owner's session, evening JST; host log times below are the host's local time)
layer: B
caty_gateway_version: caty-gateway 0.1.6 (the prefix ~/caty-smoke was reinstalled to 0.1.6 for the OpenClaw layer A run; the smoke-ollama member files are from the 0.1.4 setup with the #38 unit fix)
result: PASS
---

Layer B is the owner's real-phone pass: CatyPhone (TestFlight) on the same tailnet, the QR issued on the host with `caty-gateway qr --member <id>` (run over ssh from the dev MBP and scanned from its screen), one spoken turn, a service restart, one more spoken turn in the same conversation. The owner reported the four checks in chat on 2026-09-07; the runner (see `README.md`) logged them here and added the host-side numbers from the gateway log of the same session. The layer A record for this route/host is `ollama-hetzner-vps-2026-09-07.md`.

Member `smoke-ollama`, port 18771. Restart used: `systemctl --user restart caty-gateway-smoke-ollama`.

## Checks

| # | check | result | evidence |
|---|---|---|---|
| 1 | QR scanned → paired | PASS | owner: paired |
| 2 | one spoken turn → reply | PASS | owner: text reply arrived; first turns `status=degraded mode=text_only` (no voice), after the TTS fix `status=ok tts_first=1.7s` |
| 3 | restart | PASS | `systemctl --user restart`; unit `active` afterwards |
| 4 | resume turn refers to the earlier turn | PASS | owner: conversation continued after the restart |

## Turn latencies (gateway log, same session)

`backend=openai-compat:gemma3:1b` — `gen_first` 0.3–0.4 s; before the TTS fix `total=2.1s mode=text_only`, after it `tts_first=1.7s total=1.7s mode=legacy` (audio produced).

## Notes

- No voice at first, exactly as the layer A record predicted (`degraded: tts`): the VPS has no TTS engine and nothing listens on the default proxy port. Fix applied to the three VPS smoke members during the session: `CATY_TTS_PROXY` → the Fish proxy on the family Mac mini over the tailnet (it binds all interfaces) and `CATY_TTS_VOICE` → the built-in preset. Measured from the VPS before enabling: HTTP 200, 45 KB mp3 in 1.5 s. This is a smoke-member setting, not a gateway change.

## Findings

- none for the gateway. The text-only fallback when no TTS engine is reachable behaved as designed.
