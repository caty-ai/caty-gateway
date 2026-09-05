# Self-hosting

The gateway supports macOS launchd and Linux systemd user services. Install the package into a dedicated environment, keep credentials in a mode-0600 environment file, and render the packaged service template for the selected member. Service labels use `ai.caty.gateway.<member>`. On Linux, enable lingering when the user service must survive logout. On macOS, load the generated plist in the user's GUI domain.

## Environment

Tier A variables affect every installation.

| Variable | Purpose |
| --- | --- |
| `CATY_ID`, `CATY_NAME`, `CATY_ACCENT_COLOR` | Public member identity |
| `CATY_BACKEND` | Backend selector |
| `CATY_GATEWAY_BIND`, `CATY_GATEWAY_PORT`, `CATY_PUBLIC_URL` | Listener and pairing URL |
| `CATY_TOKEN`, `CATY_ADMIN_TOKEN`, `CATY_REQUIRE_AUTH` | Client and administrative authentication |
| `CATY_ASSET_DIR`, `CATY_CONFIG_DIR`, `CATY_FILLER_DIR`, `CATY_HISTORY_DIR` | Runtime storage |
| `CATY_USER_NAME` | User-facing prompt label; defaults to `ユーザー` |

Tier B variables configure a backend: `CATY_CLAUDE_BIN`, `CATY_CLAUDE_CWD`, `CATY_CLAUDE_MODEL`, `CATY_CLAUDE_PROJECTS_DIR`, `CATY_GCLI_BIN`, `CATY_GCLI_CWD`, `CATY_GCLI_EXTRA_ARGS`, `OPENCLAW_BIN`, `CATY_AGENT`, `CATY_GATEWAY_URL`, `CATY_GATEWAY_TOKEN`, `OPENCLAW_GATEWAY_TOKEN`, `CATY_SESSION_KEY_PREFIX`, `CATY_HERMES_URL`, `CATY_HERMES_API_KEY`, `CATY_OPENAI_BASE_URL`, `CATY_OPENAI_MODEL`, `CATY_OPENAI_API_KEY`, and `CATY_OPENAI_MAX_HISTORY_CHARS`.

Tier B2 variables tune pairing safeguards: `CATY_PAIRING_TTL_SECONDS`, `CATY_PAIRING_RATE_PER_MIN`, `CATY_PAIRING_MAX_FAILURES`, `CATY_PAIRING_LOCKOUT_SECONDS`, and `CATY_PAIRING_MAX_FAIL_TOTAL`.

Tier C variables enable optional voice, push, attachment, history, and avatar features. Keep provider keys out of command lines and logs. See `privacy.md` before enabling outbound avatar or vision processing.
