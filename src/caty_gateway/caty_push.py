#!/usr/bin/env python3
"""Send push events to a Caty gateway."""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def parse_audience(value):
    if value == "all":
        return "all"
    prefix = "member:"
    if value.startswith(prefix) and value[len(prefix):]:
        return {"member": value[len(prefix):]}
    raise argparse.ArgumentTypeError("audience must be 'all' or 'member:<id>'")


def add_common_arguments(subparser):
    subparser.add_argument("url")
    subparser.add_argument("--title", required=True)
    subparser.add_argument("--audience", type=parse_audience, default="all")
    subparser.add_argument("--session")
    subparser.add_argument("--key")
    subparser.add_argument("--gateway", default="http://127.0.0.1:8788")
    subparser.add_argument("--token-env", default="CATY_TOKEN")


def build_parser():
    parser = argparse.ArgumentParser(description="Send push events to a Caty gateway")
    subparsers = parser.add_subparsers(dest="command", required=True)

    open_url = subparsers.add_parser("open-url", help="send an open_url event")
    add_common_arguments(open_url)

    media = subparsers.add_parser("media", help="send a media event (image/video/YouTube URL)")
    add_common_arguments(media)
    media.add_argument(
        "--media-type",
        choices=["image", "video", "youtube"],
        help="optional hint for the app-side classifier (falls back to URL sniffing)",
    )
    return parser


def _redact(message, token):
    return message.replace(token, "[REDACTED]") if token else message


def _error_message(error, token):
    try:
        body = error.read().decode("utf-8", "replace")
        payload = json.loads(body)
        detail = payload.get("error") if isinstance(payload, dict) else None
    except (OSError, ValueError):
        detail = None
    message = f"gateway returned HTTP {error.code}"
    if detail:
        message += f": {detail}"
    return _redact(message, token)


def send_event(args, token, kind, event_payload):
    payload = {
        "kind": kind,
        "payload": event_payload,
        "audience": args.audience,
    }
    if args.session is not None:
        payload["session_id"] = args.session
    if args.key is not None:
        payload["event_key"] = args.key

    endpoint = urllib.parse.urljoin(args.gateway.rstrip("/") + "/", "push")
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        print(f"error: {_error_message(error, token)}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError, ValueError) as error:
        message = _redact(str(getattr(error, "reason", error)), token)
        print(f"error: gateway request failed: {message}", file=sys.stderr)
        return 1

    print(_redact(json.dumps(result, ensure_ascii=False), token))
    return 0


def main(argv=None):
    args = build_parser().parse_args(argv)
    token = os.environ.get(args.token_env)
    if not token:
        print(f"error: token environment variable {args.token_env} is not set", file=sys.stderr)
        return 2
    if args.command == "open-url":
        return send_event(args, token, "open_url", {"url": args.url, "title": args.title})
    if args.command == "media":
        event_payload = {"url": args.url, "title": args.title}
        if args.media_type:
            event_payload["media_type"] = args.media_type
        return send_event(args, token, "media", event_payload)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
