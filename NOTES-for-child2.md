# Notes for child 2

- Implement the `setup`, `status`, `serve`, `qr`, `push`, and `doctor` subcommands behind the single `caty-gateway` entry point.
- Replace the excluded shell service installers with orchestrator-owned launchd and systemd rendering from package resources.
- Remove `setup_orchestrator.py` checkout assumptions, dirty-tree checks, direct source-script paths, and local virtual-environment provisioning assumptions.
- Expand setup backend choices and preflight coverage to `claude`, `codex`, `openclaw`, `hermes`, and `openai-compat`; the child 1 setup acceptance matrix remains aligned to the three values currently accepted by the orchestrator.
- Raise the orchestrator runtime check to Python 3.10 and ensure generated environments set `CATY_REQUIRE_AUTH=1` and `CATY_HISTORY_DIR`, with a no-history option.
- Make non-loopback `serve` fail closed when `CATY_TOKEN` is empty. The retained `tests/test_require_auth.py` authentication tests still pin the legacy open-read behavior until that change lands.
- Generate the complete environment inventory and its drift check.
- Add clean-environment wheel and sdist smoke coverage for setup, service start, QR pairing, and one conversation round trip.
