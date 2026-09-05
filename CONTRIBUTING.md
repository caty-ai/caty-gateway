# Contributing

Thanks for helping people connect their own AI to CatyPhone. This page covers the two things most contributors want to do: add a backend, and send a focused fix. Read [docs/engineering.md](docs/engineering.md) first if you have not run the gateway yet.

## Ground rules

- Open an issue before a large change so the scope and the files you will touch are visible. Small fixes can go straight to a pull request.
- Keep credentials, private deployment paths, host names, and personal test fixtures out of the repository. `make test` runs a scrub audit and a publication gate that fail on them.
- Do not change the pairing wire contract (`docs/contracts/pairing-v1.md`) or the public environment variable names in a backend PR. Those are versioned separately.
- Run `make test` and `make lint` locally before you push. CI runs the same targets on Ubuntu.

## Adding a backend

There are two shapes. Pick the one that matches how the AI is driven.

### Per-turn CLI backends: one preset entry

For tools that take one prompt per process and can resume a session by id (the same shape as Codex CLI), you add a single preset and no new module.

1. Add one entry to `PRESETS` in `src/caty_gateway/backends/generic_cli.py`: `bin`, `new_args`, `resume_args`, `parse_spec`, and `external`. Copy the `codex` entry and adjust.
2. Add four cases to `tests/test_generic_cli_backend.py`, duplicating the existing `codex` cases: new session, reply extraction, resume, and re-creation after a failed resume.
3. Add one passive preflight row in `src/caty_gateway/doctor.py`: `<bin> --version` and a login-state check that does not send a prompt. Add the name to the `--backend` choices in `setup_orchestrator.py` and to the backend selector in `caty_gateway.py`.
4. Add a row to the backend table in `README.md` (and the translations, or note in the PR that translations need a follow-up) with the tier **connection available**.
5. To promote the row to **bundled with a real-conversation record**, attach `docs/smoke/<backend>-<host>-<date>.md` written so a maintainer can reproduce it: clean install, `doctor` all PASS, `setup` to QR, pairing from CatyPhone, two turns that show resume, a service restart followed by a third turn, and a check that no token appears in plain-text logs.

A backend PR without a smoke record is fine to merge. The README row simply stays at "connection available" until the record lands.

### HTTP backends: one module

For a resident server with an HTTP API, add `src/caty_gateway/backends/<name>.py`.

- Subclass `Backend` from `backends/base.py` and implement the abstract methods `generate()` and `stream()`. Set `supports_stream()` to `True` only if you implement streaming. There is no `health()` abstract; preflight belongs in `doctor.py`.
- Name environment variables `CATY_<NAME>_URL` and `CATY_<NAME>_API_KEY`. Register them in `tools/env-inventory.py` so `make env-check` classifies them, then regenerate `docs/env.md`.
- Contract test: `caty-gateway doctor --backend <name>` must instantiate the backend and reach its model-list endpoint passively.
- Follow steps 4 and 5 above for the README row and the optional smoke record.

## Sending a fix

- One concern per pull request. Describe what changed, why, and how you verified it.
- Include a test when the change is observable. `pytest` errors, failed tests, and zero collected tests all fail the build.
- If you touch environment variables, run `python tools/env-inventory.py` and commit the regenerated `docs/env.md`.
- Documentation lives in three layers: `README*.md` for people deciding whether to install, `docs/engineering.*.md` for people running it, `docs/reference.*.md` for exact values. Put new material in the layer that matches its reader.

## Labels you will see

- `component:*` maps to the module table in `docs/engineering.md`.
- `platform:*` maps to CI lanes.
- `severity:*` describes impact as reported; priority is decided on the board, not with a label.
- `backend:*` marks which AI the report is about.
- `needs-repro` means we need the backend, host OS, and `doctor` output to continue.

## Licensing

By contributing you agree that your contribution is licensed under the [MIT License](LICENSE) of this repository.
