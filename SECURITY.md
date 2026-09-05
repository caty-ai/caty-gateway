# Security Policy

## Reporting a vulnerability

Please do not open a public issue for security problems.

Use GitHub's private vulnerability reporting for this repository: https://github.com/caty-ai/caty-gateway/security/advisories/new . Include the version (`caty-gateway --help` shows the subcommands; `pip show caty-gateway` shows the version), the backend, the host OS, and steps to reproduce. `doctor` output is welcome; it contains no secrets.

You will get an acknowledgement in the advisory thread. Fixes ship as a new patch release with a GitHub Release note; credit is given in the release note unless you ask otherwise.

## Scope

In scope:

- The gateway process (`caty-gateway serve`) and its HTTP routes
- Pairing (`/pair/*`), the QR payload, and the pairing store
- The setup and doctor paths, service templates, and generated environment files
- Anything that could expose `CATY_TOKEN`, `CATY_ADMIN_TOKEN`, backend API keys, or conversation content

Out of scope:

- Vulnerabilities in the AI backends themselves (Claude Code, Codex CLI, OpenClaw, Hermes, Ollama, LM Studio) or in Tailscale
- Deployments that set `CATY_PAIRING_ALLOW_NONTAILNET=1`, which is documented as unsupported

## Design notes that matter for reports

- `/pair/claim` accepts connections only from loopback or the Tailscale tailnet; the QR carries a 600-second single-use pairing secret, never the long-lived token.
- Non-loopback `serve` refuses to start without a nonblank `CATY_TOKEN`.
- Bearer tokens are redacted from logs and response headers. Conversation content is not logged by default.

The full pairing contract is in [docs/contracts/pairing-v1.md](docs/contracts/pairing-v1.md).
