#!/usr/bin/env python3
"""Play the CatyPhone role for layer A: pair, text turns, restart, and log audit.

Credentials are read as data, never sourced into a shell. HTTP redirects are not
followed, and a claim is never retried. Only the final sanitized summary goes to
stdout; progress contains fixed stage names, never remote bodies or commands.
"""

import argparse
import http.client
import json
import re
import secrets
import shlex
import subprocess
import sys
import time
import urllib.parse


class SmokeFailure(ValueError):
    """A fixed, safe diagnostic owned by this client."""


PAIR_RE = re.compile(r"\b[0-9a-f]{8}\.[0-9a-f]{32}\b")


def redact(text, secrets):
    """Replace known values simultaneously, plus any contract-shaped pair."""
    values = secrets.values() if isinstance(secrets, dict) else secrets
    values = {value for value in values if isinstance(value, str) and value}
    patterns = [re.escape(value) for value in sorted(values, key=len, reverse=True)]
    pattern = "|".join(patterns + [PAIR_RE.pattern])

    def replacement(match):
        value = match.group()
        if value in values:
            return "[REDACTED]"
        return value.split(".", 1)[0] + ".[REDACTED]"

    return re.sub(pattern, replacement, str(text))


def find_leaks(log_text, secrets):
    """Return only names of known credentials and generic pair patterns."""
    names = {name for name, value in secrets.items() if value and value in log_text}
    if PAIR_RE.search(log_text):
        names.add("pair_pattern")
    return sorted(names)


def parse_env_text(text):
    """Parse single-line shell-style assignments without evaluation/expansion."""
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^export\s+", "", line)
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", line)
        if not match:
            raise SmokeFailure("invalid env assignment")
        try:
            words = shlex.split(match[2], comments=True, posix=True)
        except ValueError:
            raise SmokeFailure("invalid env quoting") from None
        if len(words) > 1:
            raise SmokeFailure("env values containing spaces must be quoted")
        result[match[1]] = words[0] if words else ""
    return result


def validate_url(value):
    if not isinstance(value, str) or not value or re.search(r"[\s\x00-\x1f\x7f]", value):
        raise SmokeFailure("invalid gateway URL")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
        valid = (
            parsed.scheme in ("http", "https") and parsed.hostname
            and parsed.username is None and parsed.password is None
            and "?" not in value and "#" not in value
            and "\\" not in value and (port is None or port > 0)
        )
    except ValueError:
        valid = False
    if not valid:
        raise SmokeFailure("invalid gateway URL")
    return value


def validate_qr_payload(obj):
    """Validate contract v1; errors contain no input values."""
    if not isinstance(obj, dict) or type(obj.get("v")) is not int or obj["v"] != 1:
        raise SmokeFailure("QR version must be integer 1")
    validate_url(obj.get("url"))
    if not isinstance(obj.get("pair"), str) or not PAIR_RE.fullmatch(obj["pair"]):
        raise SmokeFailure("invalid QR pairing credential")
    if not isinstance(obj.get("id"), str) or not obj["id"].strip():
        raise SmokeFailure("QR member id must be non-empty")
    return {key: obj[key] for key in ("v", "url", "pair", "id")}


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def build_summary(*, label="", session_id="", started_at=None):
    return {
        "ok": False, "stage": "qr", "stages": [], "label": label, "layer": "A",
        "session_id": session_id, "pair_id": None, "member_id": None,
        "gateway_url": None, "claim": {"http_status": None, "latency_s": None},
        "turns": [], "restart": {"observed": "skipped", "downtime_s": None},
        "resume_recall": None, "log_check": "skipped", "log_secret_leak": None,
        "warnings": [], "error": None, "started_at": started_at or utc_now(),
        "finished_at": None,
    }


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default error includes unknown arguments and their values.
        raise SmokeFailure("invalid arguments; use --help for usage")


def positive_seconds(value):
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("timeout must be positive and finite") from None
    if not 0 < number < float("inf"):
        raise argparse.ArgumentTypeError("timeout must be positive and finite")
    return number


def make_parser():
    parser = SafeParser(prog="phone-sim", description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--qr-json", metavar="PATH", help="QR JSON file; - reads stdin")
    source.add_argument("--env-file", metavar="PATH", help="member env file (read as data)")
    source.add_argument("--env-cmd", metavar="CMD", help="command producing member env text")
    restart = parser.add_mutually_exclusive_group()
    restart.add_argument("--restart-cmd", metavar="CMD", help="required unless --no-restart")
    restart.add_argument("--no-restart", action="store_true")
    parser.add_argument("--restart-timeout", type=positive_seconds, default=90)
    parser.add_argument("--turn-timeout", type=positive_seconds, default=180)
    parser.add_argument("--claim-timeout", type=positive_seconds, default=15)
    parser.add_argument("--log-timeout", type=positive_seconds, default=60)
    parser.add_argument("--log-file", action="append", default=[], metavar="PATH")
    parser.add_argument("--log-cmd", action="append", default=[], metavar="CMD")
    parser.add_argument("--session-id", default="smoke-" + time.strftime("%Y%m%d", time.gmtime()) + "-" + secrets.token_hex(3))
    parser.add_argument("--label", default="")
    parser.add_argument("--require-recall", action="store_true")
    parser.add_argument("--require-restart-observed", action="store_true")
    parser.add_argument("--require-log-check", action="store_true")
    return parser


def read_text(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def run_command(command, timeout):
    # Never include a command, its stderr, or TimeoutExpired.output in an error:
    # these can contain credentials that have not yet been learned.
    result = subprocess.run(command, shell=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise SmokeFailure("command failed (output suppressed)")
    return result.stdout.decode("utf-8", errors="replace")


def request(url, method, path, *, token=None, headers=None, body=None, timeout=15):
    parsed = urllib.parse.urlsplit(url)
    connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(parsed.hostname, parsed.port, timeout=timeout)
    request_headers = {"Connection": "close", **(headers or {})}
    if token:
        request_headers["Authorization"] = "Bearer " + token
    if isinstance(body, dict):
        body = json.dumps(body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    try:
        connection.request(method, parsed.path.rstrip("/") + path, body=body, headers=request_headers)
        response = connection.getresponse()
        return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()
    finally:
        connection.close()


def json_object(raw):
    try:
        obj = json.loads(raw)
    except (ValueError, UnicodeError):
        raise SmokeFailure("invalid JSON response or input") from None
    if not isinstance(obj, dict):
        raise SmokeFailure("expected a JSON object")
    return obj


def remember_pair(obj, known):
    value = obj.get("pair")
    if isinstance(value, str) and value:
        known["pair"] = value
        if "." in value:
            known["pair_secret"] = value.split(".", 1)[1]


def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise SmokeFailure("stage timed out")
    return value


def pause(deadline):
    time.sleep(min(0.5, remaining(deadline)))


def do_turn(args, payload, token, text, result, known):
    started = time.monotonic()
    deadline = started + args.turn_timeout
    try:
        status, _, raw = request(
            payload["url"], "POST", "/talk2", token=token, body=b"",
            headers={"X-Session-Id": args.session_id,
                     "X-Caty-Text": urllib.parse.quote(text, safe=""), "Content-Length": "0"},
            timeout=remaining(deadline),
        )
        result["http_status"] = status
        if status != 200:
            raise SmokeFailure("text submission failed (HTTP %d)" % status)
        job = json_object(raw).get("id")
        if not isinstance(job, str) or not job:
            raise SmokeFailure("text submission returned no job id")
        while True:
            status, headers, _ = request(
                payload["url"], "GET", "/reply/" + urllib.parse.quote(job, safe=""),
                token=token, timeout=remaining(deadline),
            )
            result["http_status"] = status
            if status == 200:
                reply = (urllib.parse.unquote(headers["x-reply-enc"])
                         if "x-reply-enc" in headers else headers.get("x-reply", ""))
                result.update(reply_chars=len(reply), reply_preview=redact(reply, known)[:80],
                              degraded=headers.get("x-degraded") or None)
                return reply
            if status != 202:
                raise SmokeFailure("reply failed (HTTP %d)" % status)
            pause(deadline)
    finally:
        result["latency_s"] = round(time.monotonic() - started, 3)


def do_restart(args, payload, token, summary):
    if args.no_restart:
        summary["warnings"].append("restart skipped; turn 3 does not prove persistence across restart")
        if args.require_restart_observed:
            raise SmokeFailure("restart observation required but restart skipped")
        return
    result = summary["restart"] = {"observed": False, "downtime_s": None}
    deadline = time.monotonic() + args.restart_timeout
    run_command(args.restart_cmd, remaining(deadline))
    down_since = None
    while True:
        probe_started = time.monotonic()
        try:
            status, _, _ = request(payload["url"], "GET", "/health", token=token,
                                   timeout=min(0.5, remaining(deadline)))
        except (OSError, http.client.HTTPException):
            status = None
        if status == 200:
            result["downtime_s"] = round(time.monotonic() - down_since, 3) if down_since is not None else 0.0
            if not result["observed"]:
                summary["warnings"].append("restart not observed; health was already 200 after command")
                if args.require_restart_observed:
                    raise SmokeFailure("restart was not observed")
            return
        result["observed"] = True
        if down_since is None:
            down_since = probe_started
        result["downtime_s"] = round(time.monotonic() - down_since, 3)
        pause(deadline)


def sanitize_summary(value, known):
    if isinstance(value, str):
        return redact(value, known)
    if isinstance(value, list):
        return [sanitize_summary(item, known) for item in value]
    if isinstance(value, dict):
        return {key: sanitize_summary(item, known) for key, item in value.items()}
    return value


def main(argv):
    summary = build_summary()
    known = {}

    def stage(name):
        summary["stage"] = name
        summary["stages"].append(name)
        print("[phone-sim] " + name, file=sys.stderr)

    try:
        args = make_parser().parse_args(argv)
        if not args.restart_cmd and not args.no_restart:
            raise SmokeFailure("--restart-cmd or --no-restart is required")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", args.session_id):
            raise SmokeFailure("session id must use only letters, digits, dot, underscore or hyphen")
        stage("qr")
        if args.qr_json:
            raw = sys.stdin.read() if args.qr_json == "-" else read_text(args.qr_json)
            obj = json_object(raw)
            remember_pair(obj, known)
            payload = validate_qr_payload(obj)
        else:
            text = read_text(args.env_file) if args.env_file else run_command(args.env_cmd, args.claim_timeout)
            env = parse_env_text(text)
            admin_token = env.get("CATY_TOKEN", "")
            if admin_token:
                known["CATY_TOKEN"] = admin_token
            if not admin_token:
                raise SmokeFailure("env file requires member token")
            url = validate_url(env.get("CATY_PUBLIC_URL"))
            status, _, raw = request(url, "POST", "/pair/new", token=admin_token,
                                     body=b"", timeout=args.claim_timeout)
            if status != 200:
                raise SmokeFailure("pair issuance failed (HTTP %d)" % status)
            obj = json_object(raw)
            remember_pair(obj, known)
            if obj.get("ok") is not True:
                raise SmokeFailure("pair issuance was not successful")
            payload = validate_qr_payload(obj)
        # Do not echo free-form fields until the credential source was parsed.
        # A malformed source may contain a secret we have not learned yet.
        summary.update(label=args.label, session_id=args.session_id,
                       pair_id=payload["pair"].split(".")[0], member_id=payload["id"],
                       gateway_url=urllib.parse.urlsplit(payload["url"]).netloc)
        stage("claim")
        started = time.monotonic()
        try:
            status, _, raw = request(payload["url"], "POST", "/pair/claim",
                                     body={"pair": payload["pair"],
                                           "device": {"name": "phone-sim", "platform": "layer A"}},
                                     timeout=args.claim_timeout)
            summary["claim"]["http_status"] = status
        finally:
            summary["claim"]["latency_s"] = round(time.monotonic() - started, 3)
        if status != 200:
            raise SmokeFailure("pair claim failed (HTTP %d); not retried" % status)
        claimed = json_object(raw)
        token = claimed.get("token")
        if isinstance(token, str) and token:
            known["token"] = token
        if claimed.get("ok") is not True or not isinstance(token, str) or not token.strip():
            raise SmokeFailure("invalid pair claim response")
        for key in ("url", "id"):
            if claimed.get(key) != payload[key]:
                summary["warnings"].append("claim response " + key + " differs from QR")
        codeword = "blue-" + secrets.token_hex(3)
        texts = [
            'Remember this codeword and answer only "OK": ' + codeword,
            "Reply with one short sentence: what can you help me with?",
            "What was the codeword I gave you earlier in this conversation? Reply with only the codeword.",
        ]
        for n, text in enumerate(texts, 1):
            if n == 3:
                stage("restart")
                do_restart(args, payload, token, summary)
            stage("turn%d" % n)
            turn = {"n": n, "http_status": None, "latency_s": None,
                    "reply_chars": 0, "reply_preview": "", "degraded": None}
            summary["turns"].append(turn)
            reply = do_turn(args, payload, token, text, turn, known)
            if n == 3:
                summary["resume_recall"] = codeword.lower() in reply.lower()
                if not summary["resume_recall"]:
                    summary["warnings"].append("turn 3 did not recall the codeword")
                    if args.require_recall:
                        raise SmokeFailure("resume recall required but codeword was not recalled")
        stage("logcheck")
        if args.log_file or args.log_cmd:
            summary.update(log_check="pass", log_secret_leak=False)
            # A failed source fails the stage; never call a partial audit a pass.
            try:
                for path in args.log_file:
                    if find_leaks(read_text(path), known):
                        summary.update(log_check="leak", log_secret_leak=True)
                        raise SmokeFailure("secret detected in logs (content suppressed)")
                for command in args.log_cmd:
                    if find_leaks(run_command(command, args.log_timeout), known):
                        summary.update(log_check="leak", log_secret_leak=True)
                        raise SmokeFailure("secret detected in logs (content suppressed)")
            except Exception:
                if not summary["log_secret_leak"]:
                    summary.update(log_check="skipped", log_secret_leak=None)
                raise
        else:
            summary["warnings"].append("no log source provided; secret leakage was not checked")
            if args.require_log_check:
                raise SmokeFailure("log check required but no log source provided")
        stage("done")
        summary["ok"] = True
    except (Exception, KeyboardInterrupt) as error:
        # Library exceptions may include request headers, file contents, or
        # subprocess output, including credentials not learned yet.
        summary["error"] = (redact(str(error), known) if isinstance(error, SmokeFailure)
                            else "operation failed (%s; details suppressed)" % type(error).__name__)
    summary["finished_at"] = utc_now()
    print(json.dumps(sanitize_summary(summary, known), ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
