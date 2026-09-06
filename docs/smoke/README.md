# Smoke tests: layer A without a phone

This is the phone-less smoke procedure for #31, a child of #2. Run from a
checkout containing `tools/smoke/phone-sim.py`; the gateway itself should be a
clean installation on the route's test host. Alpha records the actual run;
this directory's README is a procedure, not evidence that a route has passed.

The seven steps from design §4-3 are:

1. Clean install and record the installed version.
2. Run `doctor --backend <b>` until every check is PASS.
3. Run `setup --member smoke-<route> --backend <b>` and issue the QR.
4. Claim the pairing credential.
5. Complete two conversation turns.
6. Restart the service and complete a third turn in the same conversation.
7. Check that neither token nor pairing secret appears in logs.

| Layer | Owner and scope |
|---|---|
| A: this tool | Alpha, without a phone: clean install → doctor all PASS → setup → scripted pair claim → two text turns → service restart → third text turn (resume) → token/pair absent from logs |
| B: phone | Owner: scan `caty-gateway qr --member <id>`, complete one spoken turn, then resume after a restart |

Layer A does **not** prove QR rendering/scanning, audio/STT/TTS quality, or iOS
client behaviour. A completed backend reply with `degraded: "tts"` passes A.

## Prepare each route

Follow [Engineering: Quick Start](../engineering.md#quick-start) for prerequisites:
Python 3.10+, ffmpeg/ffprobe, logged-in Tailscale, and the backend's authenticated
CLI or running HTTP server. Use a fresh test installation/profile; do not remove
an existing user's service or history to make it clean. Install with
`uv tool install caty-gateway` (or `pipx install caty-gateway`) and record
`uv tool list` (or `pip show caty-gateway` in the installation's environment).
The installer URL in Engineering is marked as awaiting the PyPI release; if the
package is unavailable, record that blocker instead of claiming a clean install.

Run one route at a time, or supply distinct `--port N` values to setup. Replace
angle-bracket placeholders before executing. Keep backend secrets in a protected
0600 environment file and load them into the shell before doctor/setup; do not
put secret values in command arguments or copy them into a smoke record.

### Claude

Authenticate the `claude` CLI first.

```sh
caty-gateway doctor --backend claude
caty-gateway setup --member smoke-claude --backend claude
```

### Codex

Authenticate the `codex` CLI first.

```sh
caty-gateway doctor --backend codex
caty-gateway setup --member smoke-codex --backend codex
```

### OpenClaw

Configure the `openclaw` CLI and its agent first.

```sh
caty-gateway doctor --backend openclaw
caty-gateway setup --member smoke-openclaw --backend openclaw
```

### Hermes

Start Hermes' `/v1/responses` service and load `CATY_HERMES_API_KEY` from your
protected environment file. Set `CATY_HERMES_URL` there if it is not the default
`http://127.0.0.1:8642`.

```sh
caty-gateway doctor --backend hermes
caty-gateway setup --member smoke-hermes --backend hermes
```

### Ollama via openai-compat

Start Ollama and select an installed model returned by its `/v1/models` endpoint.

```sh
export CATY_OPENAI_BASE_URL=http://127.0.0.1:11434/v1
export CATY_OPENAI_MODEL='<installed-model-id>'
caty-gateway doctor --backend openai-compat
caty-gateway setup --member smoke-ollama --backend openai-compat
```

### LM Studio via openai-compat

Start LM Studio's local server and select a model returned by `/v1/models`.
Use its configured port if it differs from 1234.

```sh
export CATY_OPENAI_BASE_URL=http://127.0.0.1:1234/v1
export CATY_OPENAI_MODEL='<loaded-model-id>'
caty-gateway doctor --backend openai-compat
caty-gateway setup --member smoke-lm-studio --backend openai-compat
```

## Run phone-sim

Use the route's member id below. Keep history enabled (do not use setup's
`--no-history`). The tool self-issues with authenticated `/pair/new`, then makes
exactly one `/pair/claim` attempt. Issuance replaces any existing live credential;
do not reissue a QR concurrently. Claim must originate from loopback or the
Tailscale tailnet, including the IPv6 ULA allowed by the
[pairing contract](../contracts/pairing-v1.md).

### Same host: Linux

```sh
python3 -B tools/smoke/phone-sim.py \
  --env-file "$HOME/.config/caty-gateway/<id>.env" \
  --restart-cmd 'systemctl --user restart caty-gateway-<id>' \
  --log-cmd 'journalctl --user -u caty-gateway-<id> --no-pager' \
  --require-recall --require-restart-observed --require-log-check \
  --label '<route>@<host>'
```

### Same host: macOS

macOS stores the member environment in the 0600 launchd plist, **not** a Linux
`.env` file. This captured command converts only the two needed keys to env text.
Do not run the inner extraction command separately: its output is confidential.

```sh
python3 -B tools/smoke/phone-sim.py \
  --env-cmd "python3 -c 'import pathlib,plistlib,shlex; p=pathlib.Path.home()/\"Library/LaunchAgents/ai.caty.gateway.<id>.plist\"; e=plistlib.loads(p.read_bytes())[\"EnvironmentVariables\"]; print(\"\\n\".join(k+\"=\"+shlex.quote(e[k]) for k in (\"CATY_TOKEN\",\"CATY_PUBLIC_URL\")))'" \
  --restart-cmd 'launchctl kickstart -k gui/$(id -u)/ai.caty.gateway.<id>' \
  --log-file "$HOME/Library/Logs/caty-gateway-<id>.log" \
  --require-recall --require-restart-observed --require-log-check \
  --label '<route>@<host>'
```

### Another tailnet host: Linux gateway

SSH must already work without an interactive prompt. The gateway's configured
public URL must be reachable from the host running phone-sim. The inner quotes
keep `~` from expanding on the local host before SSH sends the command.

```sh
python3 -B tools/smoke/phone-sim.py \
  --env-cmd "ssh <host> cat '~/.config/caty-gateway/<id>.env'" \
  --restart-cmd "ssh <host> systemctl --user restart caty-gateway-<id>" \
  --log-cmd "ssh <host> journalctl --user -u caty-gateway-<id> --no-pager" \
  --require-recall --require-restart-observed --require-log-check \
  --label '<route>@<host>'
```

The restart commands and log locations above match
[Engineering: Service operation](../engineering.md#service). History normally
lives at `~/.local/state/caty-gateway/history/<id>/`; pairing state lives at
`~/.local/state/caty-gateway/pairing/<member>/`. See
[Storage locations](../engineering.md#storage-locations) for the other paths.
For layer B, reissue with `caty-gateway qr --member <id>`; the QR CLI loads the
installed environment and does not print the plaintext payload. If you already
have a securely stored payload, `--qr-json <path>` (or `--qr-json -` for stdin)
is an alternative to either env source.

Exactly three turns run, with one session id. The last asks for the first turn's
codeword. Read `resume_recall` as a recall probe, not proof of all history content.
`restart.observed: false` means no health outage was seen; it is a warning unless
`--require-restart-observed` is set. `--no-restart` reports a skipped restart and
cannot establish restart/resume. No log source means `log_check: "skipped"`;
`--require-log-check` makes that fatal. Log files and commands are repeatable.

The final stdout is one JSON line; progress goes to stderr. Exit 0 means the
selected checks passed. Save only that redacted summary in the record, never
credentials, env output, or matching log lines. `make smoke-a SMOKE_ARGS='…'`
also invokes the tool. Alpha writes a per-route record after the real run.

## Record template

```markdown
---
route: <claude|codex|openclaw|hermes|ollama|lm-studio>
backend: <--backend value>
host: <hostname or role>
os: <macOS 15.x | Ubuntu 24.04 ...>
date: <YYYY-MM-DD>
layer: A
caty_gateway_version: <pip show / uv tool list>
result: PASS | FAIL | PARTIAL
---
## Steps
| # | step | result | terminal value |
|---|---|---|---|
| 1 | clean install | | <command + version> |
| 2 | doctor all PASS | | <PASS count> |
| 3 | setup / QR issued | | <member id, port> |
| 4 | pair claim (phone-sim) | | <http 200, latency> |
| 5 | turns 1-2 | | <latency, degraded?> |
| 6 | restart + turn 3 (resume) | | <downtime_s, resume_recall> |
| 7 | token/pair not in logs | | <log source, pass> |
## phone-sim summary
<the one JSON line, verbatim (it contains no secrets)>
## Findings
- <bugs → Issue link, or "none">
```

Layer B checklist: the owner's per-route one-screen checklist is posted on #2.
