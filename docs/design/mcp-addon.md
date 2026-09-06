# Design note — `caty-gateway mcp` (add-on, not scheduled)

Status: design note for Issue #12, revision 2 (after a 3-seat review of r1). **No implementation until the owner says GO.** Size estimate at the end.

## Purpose

Let an agent that is already running on the PC (Claude Code, Codex CLI) reach the paired CatyPhone from inside its session: put a picture or page on the phone screen, later speak a short status line, and ask whether the gateway is up and reachable. This is an add-on next to the core, in the same spirit as the avatar engine (design Q9): the phone-initiated voice flow stays exactly as it is, and an MCP server is never the way the phone reaches the agent.

Why MCP cannot be the core (recorded in #12): MCP servers are tool boxes the agent calls. They cannot start a conversation when the phone speaks, and Ollama / LM Studio are not MCP clients.

## Shape

- New subcommand `caty-gateway mcp`, registered in `build_parser()` in `src/caty_gateway/cli.py` next to `push` and `doctor`, dispatched with the same lazy-import pattern (`cli.py` special-cases `push` today).
- The subcommand runs a stdio MCP server that is a thin HTTP client of the running gateway, the same way `caty-gateway push` is. It holds no state, opens no listening socket, and imports nothing from the gateway daemon's in-memory state (it cannot: it is a separate process).
- `--member <name>` loads the installed service environment the way `qr --member` does (copy that env-load, do not parse `push` argv), which yields `CATY_TOKEN` and the gateway URL.
- **stdout is JSON-RPC only.** `caty_push.send_event` prints its result JSON to stdout and returns an exit code, so it is not reusable as-is; the HTTP request part is extracted into a small helper (`caty_push._request_event` or similar) that returns the decoded response and raises on error, and `send_event` keeps its CLI behaviour on top of it. Logging goes to stderr.

## Tools

### v1 (this design; two tools)

| Tool | Arguments | Returns | Gateway path it reuses |
|---|---|---|---|
| `caty.push_image` | `url` (http/https, already reachable from the phone), `title` (≤200 chars; if omitted, the last path segment of the URL), `media_type` (`image` / `video` / `youtube`, optional), `session` (optional) | the gateway's own `POST /push` reply `{ok, id, duplicate, session_id, session_id_source}`, or `{ok:false, error}` with the redacted message | `POST /push` kind `media`, handled by `_do_push` in `caty_gateway.py` (allow-list `open_url` / `media`, `url` must be http(s) without userinfo, `title` required, `media_type` optional), auth via `_require_write_auth`; audience = the member |
| `caty.status` | none | `{gateway_up, agent, identity, member, gateway_url, pending_pairings}` | `GET /health` → `{ok, agent}` (auth-gated when `require_auth_enabled()`); `GET /identity` → `identity_payload()` (id, name, accent colour, voice engine); `PairingStore.live_count()` from `pairing_store.py` = number of **unclaimed** QR credentials (a successful claim deletes the record and leaves a `consumed` tombstone, so the store does not say "paired: yes") |

`push_image` in v1 therefore does what `caty-gateway push media` does today, from inside the agent, and nothing more. Local file paths are **not** accepted in v1: `share_store.py` is session-bound, single-use staging for **app → agent** shares (`POST /share`, consumed by the voice turn), and the gateway has no route that serves a PC-local file to the phone. Adding one is new server surface (phase 2).

Idempotency: `PushEventQueue.publish_with_status` treats an `event_key` as a no-op only when kind, payload and audience are identical; a different payload under the same key is a 409 `event_key conflict`. The tool derives `event_key` from `sha256(url + title + media_type)` and sends exactly that payload, so a retried call is a no-op and a changed title is a conflict, which is surfaced as `ok:false`.

### phase 2 (separate Issues; each is new gateway surface, so not "reuse")

- `caty.push_image(path, …)` for PC-local files: needs a new authenticated PC → phone fetch route (e.g. `GET /push-asset/<id>` with a short TTL) plus staging; the phone then receives a `media` push whose URL points at that route. Note `media_type` has no `pdf` value today; PDF depends on the app's URL sniffing and must be confirmed on the phone side first.
- `caty.say(text)`: needs a new push kind in `_do_push` with its own payload branch (the current validation is url/title-shaped and shared by both kinds) and a phone-side handler in wip-caty-talk; TTS stays on the phone by default (`tts_fish.synthesize` / `sanitize_for_tts` in `caty_gateway.py` are a later, PC-side option).
- Richer `caty.status` (`paired`, phone presence, last turn): needs a dedicated gateway status route, because pairedness is not durable in `PairingStore`, `presence_state` is per-job in-process state behind `CATY_PRESENCE_MODE2`, and "last turn" only exists in `history_store` (which this add-on must not read, see non-goals).

## Transport and boundary

- **stdio only** in v1. No HTTP, no SSE, no listening port. The MCP process is spawned by the agent and dies with it.
- **Same token boundary as pairing.** The MCP server authenticates to the gateway with the member's `CATY_TOKEN`, sent as `Authorization: Bearer` exactly like `caty-gateway push`. There is no new secret, no new credential file, no admin token, and no token ever appears in a tool result or stderr line (`caty_push._redact` / `_error_message`).
- Push kinds stay allow-listed in `_do_push`; the MCP layer cannot widen them.
- The MCP server never reads the conversation history (`history_store.py`, `GET /history`), never touches `share_store`, and never calls the backends or `stream_pipeline`; it is one-directional, PC → phone.

## Reuse points (actual symbols, verified against main 8c67770)

- `src/caty_gateway/cli.py` — `build_parser()`: add the `mcp` subparser (`--member`, `--gateway`, `--token-env`).
- `src/caty_gateway/caty_push.py` — `send_event(args, token, kind, event_payload)` (to be split into a request helper + CLI printer), `_redact`, `_error_message`, `parse_audience`.
- `src/caty_gateway/caty_gateway.py` — `_do_push` (kind allow-list, url/title/media_type validation, `_require_write_auth`), `/health`, `/identity` (`identity_payload()`), `require_auth_enabled()`.
- `src/caty_gateway/push_events.py` — `PushEventQueue.publish_with_status` (event_key semantics above).
- `src/caty_gateway/pairing_store.py` — `PairingStore.live_count()`, `default_pairing_member()`, `default_pairing_root()` for `pending_pairings` / `member`.

## Non-goals

- Replacing or bypassing the phone-initiated flow (`/v1/…` voice routes, `stream_pipeline`).
- HTTP or SSE MCP transport, remote agents, multi-member fan-out.
- Ollama / LM Studio integration (they are not MCP clients).
- Any new credential, token scope, admin-token use, or listening port.
- Reading history or shares, injecting text into the phone conversation, or triggering a backend turn.

## Acceptance criteria (proposal for the v1 implementation Issue)

1. `caty-gateway mcp --member <m>` starts, answers `initialize` and `tools/list` with exactly `caty.push_image` and `caty.status`, writes nothing but JSON-RPC to stdout, and exits cleanly on stdin EOF.
2. `caty.push_image` with a public PNG URL reaches the phone (real-device smoke, layer B of #2); a second identical call returns `duplicate:true`; a call with a changed title returns `ok:false` (409).
3. `caty.push_image` with a `file://` URL, a local path, or a URL with userinfo is rejected client-side with `ok:false` before any HTTP call.
4. `caty.status` returns `gateway_up:false` when the gateway is down, and `gateway_up:true` with `agent`, `identity.name`, `member`, `gateway_url` and an integer `pending_pairings` when it is up; no token in the output.
5. Wrong or missing token → `ok:false` with the redacted gateway message; the MCP process's stderr contains no token.
6. `make test` green with unit tests for argument validation, event_key derivation and the HTTP helper (mocked gateway); `make lint gate env-check` green; `docs/env.md` regenerated if any env name is added.
7. README gets one short section under the existing backends table ("Optional: use the phone from Claude Code / Codex via MCP"), stating plainly that v1 pushes URLs, not local files.

## Estimate

- v1 (`push_image` by URL + thin `status` + CLI wiring + tests + README): **S** — `src/caty_gateway/cli.py`, `src/caty_gateway/caty_push.py` (helper split), new `src/caty_gateway/mcp_server.py`, `tests/test_mcp_server.py`, README. Zero new dependencies if JSON-RPC over stdio is hand-rolled (two tools; keeps the one-line install unchanged).
- phase 2, each its own Issue and each new server surface: local-file push route (**M**), `say` with phone handler (**M**, needs wip-caty-talk), richer status route (**S–M**).

Release impact if v1 is implemented: new subcommand and new documented behaviour → **release ①** (minor bump, v0.2.0 candidate as #12 says).
