# Design note — `caty-gateway mcp` (add-on, not scheduled)

Status: design note for Issue #12. **No implementation until the owner says GO.** Size estimate at the end.

## Purpose

Let an agent that is already running on the PC (Claude Code, Codex CLI) reach the paired CatyPhone from inside its session: send an image or PDF to the phone, speak a short status line, or ask whether the phone is paired and present. This is an add-on next to the core, in the same spirit as the avatar engine (design Q9): the phone-initiated voice flow stays exactly as it is, and an MCP server is never the way the phone reaches the agent.

Why MCP cannot be the core (recorded in #12): MCP servers are tool boxes the agent calls. They cannot start a conversation when the phone speaks, and Ollama / LM Studio are not MCP clients.

## Shape

- New subcommand `caty-gateway mcp`, registered in `build_parser()` in `src/caty_gateway/cli.py` next to `push` and `doctor`.
- The subcommand runs a stdio MCP server. It is a thin client of the running gateway: every tool call becomes an HTTP request to the local gateway, the same way `caty-gateway push` does today. The MCP process holds no state and opens no listening socket.
- The agent's MCP config points at the command (`caty-gateway mcp --member <name>`), so the token and the gateway URL are resolved the way `qr --member` already resolves the installed service environment.

## Tools (v1: exactly three)

| Tool | Arguments | Returns | Gateway path it reuses |
|---|---|---|---|
| `caty.push_image` | `path` (local file, image or PDF), `caption` (string, optional), `session` (optional) | `{ok, share_id, event_key}` or `{ok:false, error}` | file → share store (`share_store.py`), then `caty_push.send_event(args, token, "media", payload)` → `POST /push` handled by `_do_push` in `caty_gateway.py` (kind `media`, audience = the member) |
| `caty.say` | `text` (≤ 280 chars), `session` (optional) | `{ok, event_key}` or `{ok:false, error}` | **needs a new push kind `say`** in `_do_push` (today only `open_url` and `media` are accepted) and a matching handler on the phone; TTS itself stays on the phone side or reuses `tts_fish.synthesize` on the PC, to be decided in the implementation Issue |
| `caty.status` | none | `{paired, member, presence, last_turn_at, gateway_url}` | `GET /health` (auth-gated when `require_auth_enabled()`), pairing state from `pairing_store.load_config`, presence from `presence_state` |

Every tool returns a JSON object; failures are returned as `ok:false` with the same redacted message `caty_push._error_message` produces today (token never echoed).

## Transport and boundary

- **stdio only** in v1. No HTTP, no SSE, no listening port. The MCP process is spawned by the agent and dies with it.
- **Same token boundary as pairing.** The MCP server authenticates to the gateway with the member's `CATY_TOKEN`, sent as `Authorization: Bearer` exactly like `caty-gateway push`. There is no new secret, no new credential file, and no token ever appears in a tool result or log line (`caty_push._redact`).
- Push kinds stay allow-listed in `_do_push`; the MCP layer cannot widen them.
- The MCP server never reads the conversation history (`history_store.py`) and never calls the backends; it is one-directional, PC → phone.

## Reuse points (actual symbols)

- `src/caty_gateway/cli.py` — `build_parser()`: add the `mcp` subparser (`--member`, `--gateway`, `--token-env`), dispatched with the same lazy-import pattern as `push`.
- `src/caty_gateway/caty_push.py` — `send_event(args, token, kind, event_payload)`, `_redact`, `_error_message`, `parse_audience`: the HTTP client for `push_image` and `say`.
- `src/caty_gateway/caty_gateway.py` — `_do_push` (kind allow-list, audience validation, `_require_write_auth`), `/health` handler, `_require_auth`.
- `src/caty_gateway/push_events.py` — `PushEventQueue.publish_with_status` (event_key idempotency; `push_image` should pass an `event_key` derived from the file hash so a retried tool call does not duplicate the push).
- `src/caty_gateway/share_store.py` — staging a local file so the phone can fetch it by URL (`ClaimedFile`, `sniff_attachment_mime`, `ShareQuotaExceeded`).
- `src/caty_gateway/pairing_store.py` — `load_config` for `status.paired`; `src/caty_gateway/presence_state.py` — current phase for `status.presence`.
- `src/caty_gateway/tts_fish.py` — `synthesize` / `sanitize_for_tts` (in `caty_gateway.py`) if `say` renders audio on the PC.

## Non-goals

- Replacing or bypassing the phone-initiated flow (`/v1/…` voice routes, `stream_pipeline`).
- HTTP or SSE MCP transport, remote agents, multi-member fan-out.
- Ollama / LM Studio integration (they are not MCP clients).
- Any new credential, token scope, or admin-token use.
- Reading history, injecting text into the phone conversation, or triggering a backend turn.

## Acceptance criteria (proposal for the implementation Issue)

1. `caty-gateway mcp --member <m>` starts, answers `initialize` and `tools/list` with exactly the three tools, and exits cleanly on stdin EOF.
2. `caty.push_image` with a PNG and a PDF reaches the phone (real-device smoke, layer B of #2); a second call with the same file is a no-op via `event_key`.
3. `caty.say` reaches the phone; text longer than 280 chars is rejected client-side with `ok:false`.
4. `caty.status` reports `paired:false` before pairing and `paired:true` after, without a token in the output.
5. Wrong or missing token → `ok:false` with the redacted gateway message; `grep` of the MCP process log shows no token.
6. `make test` green with unit tests for the argument validation and the HTTP client (mocked gateway), `make lint gate env-check` green, `docs/env.md` regenerated if any env name is added.
7. README gets one short section under the existing backends table ("Optional: use the phone from Claude Code / Codex via MCP").

## Estimate

- `push_image` + `status` + CLI wiring + tests + README: **S–M** (one lane, `src/caty_gateway/cli.py`, new `src/caty_gateway/mcp_server.py`, `tests/test_mcp_server.py`, README).
- `say`: **M** on its own, because it needs a new push kind in `_do_push` **and** a phone-side handler (wip-caty-talk), so it should be a second Issue after the first two tools ship.
- Dependency: choose an MCP library or hand-roll JSON-RPC over stdio (the protocol surface for three tools is small; hand-rolling keeps the dependency list at zero, which matters for the one-line install).

Release impact if implemented: new subcommand and new documented behaviour → **release ①** (minor bump, v0.2.0 candidate as #12 says).
