"""The public command line, with lazy imports of the gateway runtime."""

import argparse
import os
import pathlib
import platform
import sys


def build_parser():
    from caty_gateway import doctor, setup_orchestrator

    parser = argparse.ArgumentParser(prog="caty-gateway", description="Set up and run a Caty gateway")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("setup", help="Install, start, verify, and pair one gateway member", parents=[setup_orchestrator.SetupOrchestrator._parser()], add_help=False)
    status = commands.add_parser("status", help="Show setup progress")
    status.add_argument("--member", required=True)
    status.add_argument("--wait", action="store_true")
    commands.add_parser("serve", help="Run the gateway in the foreground")
    qr = commands.add_parser("qr", help="Reissue the pairing QR (use --member to load the installed service environment)")
    qr.add_argument("--member", help="Load the installed service environment for this member")
    qr.add_argument("--qr-delivery", choices=("auto", "tty", "url"))
    qr.add_argument("--wait-visible-seconds", type=float)
    commands.add_parser("push", help="Send a gateway event", add_help=False)
    commands.add_parser("doctor", help="Passive preflight for a backend, port, and public URL", parents=[doctor.build_parser()], add_help=False)
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    # Push owns its nested command grammar and help text.
    if argv and argv[0] == "push":
        from caty_gateway.caty_push import main as push_main
        return push_main(argv[1:])
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command in ("setup", "status"):
        from caty_gateway.setup_orchestrator import main as setup_main
        tail = argv[1:]
        if args.command == "status":
            tail = ["--status", *tail]
        return setup_main(tail)
    if args.command == "doctor":
        from caty_gateway.doctor import main as doctor_main
        return doctor_main(argv[1:])
    if args.command == "qr" and args.member is not None:
        from caty_gateway.setup_orchestrator import MEMBER_RE, member_artifact_path, read_member_env

        system = platform.system()
        if system not in ("Linux", "Darwin"):
            print(
                f"ERROR: unsupported platform {system!r}; "
                "caty-gateway qr --member supports Linux and macOS",
                file=sys.stderr,
            )
            return 2
        home = pathlib.Path(os.environ.get("HOME") or str(pathlib.Path.home())).expanduser()
        if not MEMBER_RE.fullmatch(args.member) or args.member in {".", ".."}:
            print(
                f"ERROR: invalid member identifier {args.member!r}; "
                "run caty-gateway setup --member MEMBER first",
                file=sys.stderr,
            )
            return 2
        path = member_artifact_path(args.member, home, system)
        try:
            values = read_member_env(path, system)
            if not values.get("CATY_TOKEN", "").strip():
                raise ValueError("missing token")
            os.environ.update({key: value for key, value in values.items() if key != "PATH"})
        except Exception:  # Parser errors may contain service secrets; never echo them.
            safe_path = str(path).replace("\r", "\\r").replace("\n", "\\n")
            print(
                f"ERROR: invalid installed environment for member {args.member!r} at {safe_path} (unreadable, malformed, or missing CATY_TOKEN); rerun caty-gateway setup --member {args.member}"
                if os.path.exists(path) else f"ERROR: no installed environment for member {args.member!r} at {safe_path}; run caty-gateway setup --member {args.member} first",
                file=sys.stderr,
            )
            return 2
        # Forward the runtime flags verbatim, removing only the public option.
        tail = iter(argv[1:])
        argv = ["qr"]
        for flag in tail:
            option, separator, _ = flag.partition("=")
            if option.startswith("--") and "--member".startswith(option):
                if not separator:
                    next(tail, None)
            else:
                argv.append(flag)
    if args.command == "serve":
        # Reject before runtime imports can initialize a configured backend.
        host = os.environ.get("CATY_GATEWAY_BIND", "").strip() or "0.0.0.0"
        if not os.environ.get("CATY_TOKEN", "").strip() and host not in ("127.0.0.1", "::1", "localhost"):
            print("ERROR: CATY_TOKEN must be non-empty for a non-loopback bind; set CATY_TOKEN or bind to 127.0.0.1", file=sys.stderr)
            return 2
    from caty_gateway.caty_gateway import main as gateway_main
    return gateway_main(argv) or 0


if __name__ == "__main__":
    raise SystemExit(main())
