import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from unittest import mock



from caty_gateway import caty_gateway as cg
from caty_gateway import pairing_store as ps


class NonClosingBytesIO(io.BytesIO):
    def close(self):
        pass


class MemorySocket:
    def __init__(self, request_bytes):
        self.input = io.BytesIO(request_bytes)
        self.output = NonClosingBytesIO()

    def makefile(self, mode, *args, **kwargs):
        return self.input if "r" in mode else self.output

    def sendall(self, data):
        self.output.write(data)

    def settimeout(self, timeout):
        pass

    def shutdown(self, how):
        pass

    def close(self):
        pass


class MemoryServer:
    server_name = "127.0.0.1"
    server_port = 0


class MutableClock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value


class PairingRoutesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="caty-pairing-routes-")
        self.clock = MutableClock()
        self.config = ps.PairingConfig()
        self.store = ps.PairingStore(
            os.path.join(self.tmp.name, "pairing"),
            "caty",
            config=self.config,
            clock=self.clock,
        )
        self.saved = {
            "token": cg.CATY_TOKEN,
            "admin": cg.CATY_ADMIN_TOKEN,
            "store": cg._pairing_store,
            "config": cg._pairing_config,
            "limiter": cg._pair_claim_rate_limiter,
        }
        cg.CATY_TOKEN = "cred-wt-1084"
        cg.CATY_ADMIN_TOKEN = ""
        cg._pairing_store = self.store
        cg._pairing_config = self.config
        cg._pair_claim_rate_limiter = cg._PairClaimRateLimiter(clock=self.clock)
        self.env = mock.patch.dict(
            os.environ,
            {"CATY_PUBLIC_URL": "http://100.64.0.10:8788"},
            clear=False,
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        cg.CATY_TOKEN = self.saved["token"]
        cg.CATY_ADMIN_TOKEN = self.saved["admin"]
        cg._pairing_store = self.saved["store"]
        cg._pairing_config = self.saved["config"]
        cg._pair_claim_rate_limiter = self.saved["limiter"]
        self.store.close()
        self.tmp.cleanup()

    def request(self, method, path, body=b"", headers=None, peer="127.0.0.1"):
        headers = dict(headers or {})
        headers.setdefault("Host", "127.0.0.1")
        headers.setdefault("Connection", "close")
        headers.setdefault("Content-Length", str(len(body)))
        lines = [f"{method} {path} HTTP/1.1"]
        lines.extend(f"{key}: {value}" for key, value in headers.items())
        raw = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body
        sock = MemorySocket(raw)
        cg.Handler(sock, (peer, 12345), MemoryServer())
        response = sock.output.getvalue()
        head, _, rest = response.partition(b"\r\n\r\n")
        response_lines = head.decode("iso-8859-1").split("\r\n")
        status = int(response_lines[0].split()[1])
        response_headers = {}
        for line in response_lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                response_headers[key.lower()] = value.strip()
        length = int(response_headers.get("content-length", "0"))
        payload = json.loads(rest[:length]) if length else None
        return status, payload

    def claim(self, pair, peer="127.0.0.1", device=None):
        payload = {"pair": pair}
        if device is not None:
            payload["device"] = device
        return self.request(
            "POST",
            "/pair/claim",
            json.dumps(payload).encode(),
            {"Content-Type": "application/json"},
            peer,
        )

    def auth_headers(self, token=None):
        token = cg.CATY_TOKEN if token is None else token
        return {"Authorization": f"Bearer {token}"}

    def test_connection_payload_has_no_token_and_gateway_start_is_issue_free(self):
        issued = self.store.issue()
        before = self.store.live_count()
        for _ in range(5):
            payload = cg._connection_payload()
            self.assertNotIn("token", payload)
            self.assertNotIn("pair", payload)
        fake_server = mock.Mock()
        fake_server.serve_forever.return_value = None
        with mock.patch.object(cg.sys, "argv", ["caty_gateway.py"]), \
                mock.patch.object(cg, "_GatewayHTTPServer", return_value=fake_server), \
                mock.patch.object(cg, "report_content_logging_mode"), \
                mock.patch.object(cg, "load_fillers"), \
                redirect_stdout(io.StringIO()):
            for _ in range(3):
                cg.main()
        self.assertEqual(self.store.live_count(), before)
        self.assertTrue(ps.PAIR_RE.fullmatch(issued["pair"]))

    def test_startup_warns_for_disabled_pairing_and_nontailnet_bypass(self):
        cg.CATY_TOKEN = ""
        cg.CATY_ADMIN_TOKEN = ""
        cg._pairing_config = replace(self.config, allow_nontailnet=True)
        fake_server = mock.Mock()
        fake_server.serve_forever.return_value = None
        with mock.patch.object(cg.sys, "argv", ["caty_gateway.py"]), \
                mock.patch.object(cg, "_GatewayHTTPServer", return_value=fake_server), \
                mock.patch.object(cg, "report_content_logging_mode"), \
                mock.patch.object(cg, "load_fillers"), \
                mock.patch.dict(os.environ, {"CATY_REQUIRE_AUTH": "0", "CATY_GATEWAY_BIND": "127.0.0.1"}), \
                redirect_stdout(io.StringIO()) as stdout:
            cg.main()
        output = stdout.getvalue()
        self.assertIn("source-address gate disabled", output)
        self.assertIn("gateway is unauthenticated", output)
        self.assertIn("CATY_TOKEN is required", output)

    def test_whitespace_only_token_disables_pairing_everywhere_and_warns(self):
        cg.CATY_TOKEN = "   "
        cg.CATY_ADMIN_TOKEN = ""
        with redirect_stdout(io.StringIO()) as stdout, \
                redirect_stderr(io.StringIO()) as stderr:
            self.assertFalse(cg._print_qr_tty())
            self.assertFalse(cg.print_qr_url())
        self.assertEqual(stdout.getvalue(), "")
        self.assertGreaterEqual(stderr.getvalue().count("non-empty CATY_TOKEN"), 2)

        status, payload = self.request(
            "POST", "/pair/new", headers=self.auth_headers("   ")
        )
        self.assertEqual((status, payload["error"]), (503, "pairing_disabled"))
        with mock.patch.object(cg, "log") as logged:
            status, payload = self.claim("a1b2c3d4." + "a" * 32, peer="100.64.0.8")
        self.assertEqual((status, payload["error"]), (503, "pairing_disabled"))
        self.assertNotIn(
            "rejected_source_outside_allowlist",
            "\n".join(str(call) for call in logged.call_args_list),
        )

        fake_server = mock.Mock()
        fake_server.serve_forever.return_value = None
        with mock.patch.object(cg.sys, "argv", ["caty_gateway.py"]), \
                mock.patch.object(cg, "_GatewayHTTPServer", return_value=fake_server), \
                mock.patch.object(cg, "report_content_logging_mode"), \
                mock.patch.object(cg, "load_fillers"), \
                mock.patch.dict(os.environ, {"CATY_GATEWAY_BIND": "127.0.0.1"}), \
                redirect_stdout(io.StringIO()) as startup_stdout:
            cg.main()
        self.assertIn("CATY_TOKEN is whitespace only", startup_stdout.getvalue())

    def test_claim_once_returns_token_then_consumed(self):
        issued = self.store.issue()
        status, payload = self.claim(issued["pair"], peer="100.64.0.8")
        self.assertEqual(status, 200)
        self.assertEqual(payload["token"], cg.CATY_TOKEN)
        self.assertEqual(payload["url"], "http://100.64.0.10:8788")
        status, payload = self.claim(issued["pair"], peer="100.64.0.8")
        self.assertEqual((status, payload["error"]), (409, "pairing_consumed"))

    def test_claim_statuses_expired_unknown_wrong_and_revoked(self):
        expired = self.store.issue()
        self.clock.value += self.config.ttl_seconds
        status, payload = self.claim(expired["pair"])
        self.assertEqual((status, payload["error"]), (410, "pairing_expired"))

        status, payload = self.claim("01234567." + "a" * 32)
        self.assertEqual((status, payload["error"]), (401, "pairing_invalid"))

        wrong_target = self.store.issue()
        wrong = wrong_target["pid"] + "." + "0" * 32
        status, payload = self.claim(wrong)
        self.assertEqual((status, payload["error"]), (401, "pairing_invalid"))

        self.store.revoke()
        status, payload = self.claim(wrong_target["pair"])
        self.assertEqual((status, payload["error"]), (401, "pairing_invalid"))

    def test_pair_new_auth_token_patterns_and_revoke_admin_only(self):
        status, payload = self.request("POST", "/pair/new")
        self.assertEqual(status, 401)
        self.assertEqual(self.store.live_count(), 0)

        status, payload = self.request(
            "POST", "/pair/new", headers=self.auth_headers()
        )
        self.assertEqual(status, 200)
        self.assertTrue(ps.PAIR_RE.fullmatch(payload["pair"]))
        self.assertNotIn("token", payload)

        cg.CATY_ADMIN_TOKEN = "admin-token"
        status, payload = self.request(
            "POST", "/pair/new", headers=self.auth_headers("admin-token")
        )
        self.assertEqual(status, 200)

        cg.CATY_TOKEN = ""
        status, payload = self.request(
            "POST", "/pair/new", headers=self.auth_headers("admin-token")
        )
        self.assertEqual((status, payload["error"]), (503, "pairing_disabled"))
        status, payload = self.request(
            "POST", "/pair/revoke", headers=self.auth_headers("admin-token")
        )
        self.assertEqual((status, payload), (200, {"ok": True}))

        cg.CATY_ADMIN_TOKEN = ""
        status, payload = self.request("POST", "/pair/new")
        self.assertEqual(status, 403)
        status, payload = self.request("POST", "/pair/revoke")
        self.assertEqual(status, 403)

    def test_pair_new_never_logs_secret_response_body(self):
        with mock.patch.object(cg, "log") as logged:
            status, payload = self.request(
                "POST", "/pair/new", headers=self.auth_headers()
            )
        self.assertEqual(status, 200)
        secret = payload["pair"].split(".")[1]
        output = "\n".join(str(call) for call in logged.call_args_list)
        self.assertNotIn(secret, output)
        self.assertIn(payload["pair"].split(".")[0], output)

    def test_claim_token_gate_precedes_body_read_and_storage(self):
        issued = self.store.issue()
        cg.CATY_TOKEN = ""
        with mock.patch.object(self.store, "claim", wraps=self.store.claim) as claim:
            status, payload = self.request(
                "POST", "/pair/claim", b"not-json", peer="100.64.0.9"
            )
        self.assertEqual((status, payload["error"]), (503, "pairing_disabled"))
        claim.assert_not_called()
        cg.CATY_TOKEN = "cred-wt-1084"
        status, payload = self.claim(issued["pair"], peer="100.64.0.9")
        self.assertEqual(status, 200)

    def test_claim_postcondition_rechecks_token_after_store_consume(self):
        issued = self.store.issue()
        with mock.patch.object(
            cg,
            "_pairing_token_configured",
            side_effect=[True, False],
        ) as configured, mock.patch.object(
            self.store,
            "claim",
            wraps=self.store.claim,
        ) as claim:
            status, payload = self.claim(issued["pair"], peer="100.64.0.8")
        self.assertEqual((status, payload["error"]), (503, "pairing_disabled"))
        self.assertEqual(configured.call_count, 2)
        claim.assert_called_once()
        self.assertEqual(self.store.live_count(), 0)

    def test_source_gate_uses_raw_peer_not_x_forwarded_for(self):
        issued = self.store.issue()
        body = json.dumps({"pair": issued["pair"]}).encode()
        status, payload = self.request(
            "POST",
            "/pair/claim",
            body,
            {"X-Forwarded-For": "100.64.0.8"},
            peer="192.168.1.50",
        )
        self.assertEqual((status, payload["error"]), (503, "pairing_disabled"))
        with mock.patch.object(cg, "log") as logged:
            status, payload = self.claim(issued["pair"], peer="not-an-ip")
        self.assertEqual((status, payload["error"]), (503, "pairing_disabled"))
        output = "\n".join(str(call) for call in logged.call_args_list)
        self.assertIn("peer=not-an-ip", output)
        self.assertIn("peer_kind=unparseable", output)
        self.assertFalse(cg._tailnet_or_loopback_peer("fd7a:115c:a1e0::1234"))
        status, payload = self.claim(issued["pair"], peer="100.64.0.12")
        self.assertEqual(status, 200)

    def test_malformed_is_rate_counted_and_never_touches_store(self):
        with mock.patch.object(self.store, "claim") as claim:
            for _ in range(self.config.rate_per_minute):
                status, payload = self.request(
                    "POST", "/pair/claim", b"{}", peer="100.64.0.22"
                )
                self.assertEqual((status, payload["error"]), (400, "pairing_malformed"))
            status, payload = self.request(
                "POST", "/pair/claim", b"{}", peer="100.64.0.22"
            )
        self.assertEqual((status, payload["error"]), (429, "rate_limited"))
        claim.assert_not_called()

    def test_limiter_internal_error_fails_closed_before_body(self):
        with mock.patch.object(
            cg._pair_claim_rate_limiter, "allow", side_effect=RuntimeError("boom")
        ), mock.patch.object(self.store, "claim") as claim:
            status, payload = self.request("POST", "/pair/claim", b"{}")
        self.assertEqual((status, payload["error"]), (429, "rate_limited"))
        claim.assert_not_called()

    def test_five_failures_lock_out_and_loopback_disables_only_cumulative_revoke(self):
        issued = self.store.issue()
        wrong = issued["pid"] + "." + "0" * 32
        with mock.patch.object(
            cg._pair_claim_rate_limiter, "allow", return_value=True
        ):
            for _ in range(self.config.max_failures):
                status, payload = self.claim(wrong, peer="127.0.0.1")
                self.assertEqual((status, payload["error"]), (401, "pairing_invalid"))
            status, payload = self.claim(wrong, peer="127.0.0.1")
        self.assertEqual((status, payload["error"]), (429, "rate_limited"))
        self.assertEqual(self.store.live_count(), 1)

    def test_only_declared_pair_routes_exist_and_claim_never_creates(self):
        issued = self.store.issue()
        before = self.store.live_count()
        status, payload = self.request("POST", "/pair/anything")
        self.assertEqual(status, 404)
        self.assertEqual(self.store.live_count(), before)
        status, payload = self.claim(issued["pair"])
        self.assertEqual(status, 200)
        self.assertEqual(self.store.live_count(), 0)

    def test_corrupt_record_is_route_401_deleted_and_never_500(self):
        issued = self.store.issue()
        path = os.path.join(self.store.root_dir, issued["pid"] + ".json")
        with open(path, encoding="utf-8") as stream:
            record = json.load(stream)
        record["secret_sha256"] = "corrupt"
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(record, stream)
        status, payload = self.claim(issued["pair"])
        self.assertEqual((status, payload["error"]), (401, "pairing_invalid"))
        self.assertFalse(os.path.exists(path))

    def test_store_postcondition_failure_maps_503_without_consuming(self):
        issued = self.store.issue()
        with mock.patch.object(
            self.store,
            "claim",
            side_effect=ps.PairingUnavailable("client credential unavailable"),
        ):
            status, payload = self.claim(issued["pair"])
        self.assertEqual((status, payload["error"]), (503, "pairing_disabled"))
        self.assertEqual(self.store.live_count(), 1)

    def test_device_is_sanitized_and_invalid_fields_are_ignored(self):
        issued = self.store.issue()
        with mock.patch.object(cg, "log") as logged:
            status, _ = self.claim(
                issued["pair"],
                device={"name": "Phone\r\nINJECT", "platform": 42},
            )
        self.assertEqual(status, 200)
        output = "\n".join(str(call) for call in logged.call_args_list)
        self.assertNotIn("\r", output)
        self.assertNotIn("\nINJECT", output)
        self.assertIn("invalid_device_platform", output)

    def test_redaction_canary_preserves_pairing_error_codes_and_pid(self):
        issued = self.store.issue()
        secret = issued["pair"].split(".")[1]
        pid = issued["pid"]
        self.assertEqual(cg._redact_log_text(issued["pair"]), "[REDACTED]")
        self.assertEqual(cg._redact_log_text("pairing_invalid"), "pairing_invalid")
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.claim(pid + "." + "0" * 32)
            self.request("POST", "/pair/claim", b"{}")
        captured = stdout.getvalue()
        self.assertNotIn(secret, captured)
        self.assertIn("pairing_invalid", captured)
        self.assertIn("pairing_malformed", captured)
        self.assertIn(pid, captured)

    def test_print_qr_missing_qrcode_fails_before_issue_and_only_stderr(self):
        existing = self.store.issue()
        before_names = sorted(os.listdir(self.store.root_dir))
        before_live = self.store.live_count()
        with mock.patch.object(cg, "_issue_pairing_for_qr") as issue, \
                mock.patch.dict(sys.modules, {"qrcode": None}), \
                redirect_stdout(io.StringIO()) as stdout, \
                redirect_stderr(io.StringIO()) as stderr:
            self.assertFalse(cg.print_qr(delivery="tty"))
        issue.assert_not_called()
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("pip install", stderr.getvalue())
        self.assertEqual(self.store.live_count(), before_live)
        self.assertEqual(sorted(os.listdir(self.store.root_dir)), before_names)
        self.assertTrue(
            os.path.exists(os.path.join(self.store.root_dir, existing["pid"] + ".json"))
        )

    def test_print_qr_stdout_redacts_pair_when_qrcode_present(self):
        pair = "01234567." + "a" * 32
        secret = pair.split(".")[1]
        class FakeQR:
            full_payload = None

            def __init__(self, border):
                pass

            def add_data(self, payload):
                FakeQR.full_payload = payload

            def make(self, fit):
                pass

            def print_ascii(self, invert):
                print("<qr-ascii>")

        fake_module = mock.Mock(QRCode=FakeQR)
        with mock.patch.object(cg, "_issue_pairing_for_qr", return_value={"pair": pair}), \
                mock.patch.dict(sys.modules, {"qrcode": fake_module}), \
                redirect_stdout(io.StringIO()) as stdout:
            self.assertTrue(cg.print_qr(delivery="tty"))
        self.assertIn(pair, FakeQR.full_payload)
        self.assertNotIn(pair, stdout.getvalue())
        self.assertNotIn(secret, stdout.getvalue())

    def test_print_qr_server_unreachable_falls_back_without_stdout_secret(self):
        class FakeQR:
            def __init__(self, border):
                pass

            def add_data(self, payload):
                self.payload = payload

            def make(self, fit):
                pass

            def print_ascii(self, invert):
                print("<qr-ascii>")

        fake_module = mock.Mock(QRCode=FakeQR)
        with mock.patch.object(
            cg,
            "_issue_pairing_from_resident_server",
            side_effect=cg.urllib.error.URLError("unreachable"),
        ), mock.patch.dict(sys.modules, {"qrcode": fake_module}), \
                redirect_stdout(io.StringIO()) as stdout:
            self.assertTrue(cg.print_qr(delivery="tty"))
        records = [
            name for name in os.listdir(self.store.root_dir) if name.endswith(".json")
        ]
        self.assertEqual(len(records), 1)
        with open(
            os.path.join(self.store.root_dir, records[0]), encoding="utf-8"
        ) as stream:
            record = json.load(stream)
        pid = record["pid"]
        output = stdout.getvalue()
        self.assertIn(f'"pair":"{pid}.[REDACTED]"', output)
        self.assertNotRegex(output, r"[0-9a-f]{8}\.[0-9a-f]{32}")

    def test_unusable_pairing_store_disables_pairing_without_killing_gateway(self):
        # 原則②: pairing is additive, so an unusable store must not take voice /
        # TTS / talk down with it.  §6-2 routes store unavailability to 503.
        fake_server = mock.Mock()
        fake_server.serve_forever.return_value = None
        broken = ps.PairingUnavailable("cannot initialize pairing store root")
        with mock.patch.object(cg.sys, "argv", ["caty_gateway.py"]), \
                mock.patch.object(cg, "_get_pairing_store", side_effect=broken), \
                mock.patch.object(cg, "_GatewayHTTPServer", return_value=fake_server), \
                mock.patch.object(cg, "report_content_logging_mode"), \
                mock.patch.object(cg, "load_fillers"), \
                redirect_stdout(io.StringIO()) as stdout:
            cg.main()
        fake_server.serve_forever.assert_called_once()
        self.assertIn("pairing store unavailable", stdout.getvalue())

        with mock.patch.object(cg, "_get_pairing_store", side_effect=broken):
            status, payload = self.claim("a1b2c3d4." + "a" * 32, peer="100.64.0.8")
            self.assertEqual((status, payload["error"]), (503, "pairing_disabled"))
            status, payload = self.request(
                "POST", "/pair/new", b"", self.auth_headers()
            )
            self.assertEqual((status, payload["error"]), (503, "pairing_disabled"))

    def test_blocked_source_hits_are_counted_and_logged_once_per_window(self):
        # §8-2 counts every hit including rejected ones, and an unauthenticated
        # flood must not write one journal line per request.
        peer = "192.168.1.50"
        limit = self.config.rate_per_minute
        with mock.patch.object(cg, "log") as logged, \
                mock.patch.object(self.store, "claim") as claim:
            for _ in range(limit + 5):
                status, payload = self.claim(peer=peer, pair="a1b2c3d4." + "a" * 32)
                self.assertEqual((status, payload["error"]), (503, "pairing_disabled"))
        claim.assert_not_called()
        pairing_lines = [call for call in logged.call_args_list if "stage=pairing" in str(call)]
        self.assertEqual(len(pairing_lines), 1)
        self.assertIn("rejected_source_outside_allowlist", str(pairing_lines[0]))
        self.assertIn("peer_kind=v4", str(pairing_lines[0]))
        self.assertEqual(cg._pair_claim_rate_limiter.entries[peer][1], limit + 5)

    def test_ipv4_mapped_tailnet_peer_is_accepted(self):
        issued = self.store.issue()
        status, payload = self.claim(issued["pair"], peer="::ffff:100.64.0.8")
        self.assertEqual(status, 200)
        self.assertEqual(payload["token"], cg.CATY_TOKEN)
        other = self.store.issue()
        status, payload = self.claim(other["pair"], peer="::ffff:192.168.1.50")
        self.assertEqual((status, payload["error"]), (503, "pairing_disabled"))
        rejected = self.store.issue()
        status, payload = self.claim(rejected["pair"], peer="::ffff:8.8.8.8")
        self.assertEqual((status, payload["error"]), (503, "pairing_disabled"))

    def test_ipv6_claim_allow_and_reject_matrix(self):
        ula = self.store.issue()
        status, payload = self.claim(ula["pair"], peer="fd7a:115c:a1e0:b1a::1234")
        self.assertEqual(status, 200)
        self.assertEqual(payload["token"], cg.CATY_TOKEN)

        for peer in ("2001:db8::1", "fd00::1"):
            issued = self.store.issue()
            with self.subTest(peer=peer), mock.patch.object(cg, "log") as logged:
                status, payload = self.claim(issued["pair"], peer=peer)
            self.assertEqual((status, payload["error"]), (503, "pairing_disabled"))
            output = "\n".join(str(call) for call in logged.call_args_list)
            self.assertIn(f"peer={peer}", output)
            self.assertIn("peer_kind=v6", output)

    def test_pairing_token_preserves_raw_value_after_trim_only_enable_check(self):
        cg.CATY_TOKEN = " abc "
        issued = self.store.issue()
        status, payload = self.claim(issued["pair"], peer="100.64.0.8")
        self.assertEqual(status, 200)
        self.assertEqual(payload["token"], " abc ")

    def test_allow_nontailnet_bypass_does_not_emit_source_range_warn(self):
        cg._pairing_config = replace(self.config, allow_nontailnet=True)
        issued = self.store.issue()
        with mock.patch.object(cg, "log") as logged:
            status, payload = self.claim(issued["pid"] + "." + "0" * 32, peer="2001:db8::1")
        self.assertEqual((status, payload["error"]), (401, "pairing_invalid"))
        self.assertNotIn(
            "rejected_source_outside_allowlist",
            "\n".join(str(call) for call in logged.call_args_list),
        )

    def test_resident_issue_url_follows_bind_host(self):
        for bind, expected in (
            ("0.0.0.0", f"http://127.0.0.1:{cg.PORT}/pair/new"),
            ("", f"http://127.0.0.1:{cg.PORT}/pair/new"),
            ("127.0.0.1", f"http://127.0.0.1:{cg.PORT}/pair/new"),
            ("100.64.0.10", f"http://100.64.0.10:{cg.PORT}/pair/new"),
            ("::1", f"http://[::1]:{cg.PORT}/pair/new"),
        ):
            with self.subTest(bind=bind):
                with mock.patch.object(cg, "BIND_HOST", bind):
                    self.assertEqual(cg._resident_pair_new_url(), expected)

    def test_unexpected_store_error_still_returns_a_coded_response(self):
        # §6-1: every path answers with {"ok": false, "error": ...}; an escaping
        # exception would close the socket with no status line at all.
        issued = self.store.issue()
        with mock.patch.object(
            self.store, "claim", side_effect=RuntimeError("boom")
        ), mock.patch.object(cg, "log"):
            status, payload = self.claim(issued["pair"], peer="100.64.0.8")
        self.assertEqual((status, payload["error"]), (503, "pairing_disabled"))
        with mock.patch.object(
            self.store, "issue", side_effect=RuntimeError("boom")
        ), mock.patch.object(cg, "log"):
            status, payload = self.request(
                "POST", "/pair/new", b"", self.auth_headers()
            )
        self.assertEqual((status, payload["error"]), (503, "pairing_disabled"))

    def test_response_build_failure_after_consume_is_still_coded(self):
        # §6-1: the credential is already consumed on this path, so a failure
        # while building the body must not drop the connection uncoded.
        issued = self.store.issue()
        with mock.patch.object(
            cg, "_connection_payload", side_effect=RuntimeError("boom")
        ), mock.patch.object(cg, "log"):
            status, payload = self.claim(issued["pair"], peer="100.64.0.8")
        self.assertEqual((status, payload["error"]), (503, "pairing_disabled"))
        with mock.patch.object(
            cg, "_connection_payload", side_effect=RuntimeError("boom")
        ), mock.patch.object(cg, "log"):
            status, payload = self.request(
                "POST", "/pair/new", b"", self.auth_headers()
            )
        self.assertEqual((status, payload["error"]), (503, "pairing_disabled"))

    def test_qr_subcommand_exit_code_reflects_print_qr_result(self):
        # S3 無人実行が失敗を成功と誤認しないための fail-loud 契約（exit code）。
        for result, expected_code in ((False, 1), (True, 0)):
            with mock.patch.object(
                    cg.sys,
                    "argv",
                    ["caty_gateway.py", "qr", "--qr-delivery", "tty"],
                ), \
                    mock.patch.object(cg, "print_qr", return_value=result), \
                    redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    cg.main()
            self.assertEqual(ctx.exception.code, expected_code)


if __name__ == "__main__":
    unittest.main()
