import io
import os
import pathlib
import signal
import socket
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock



from caty_gateway import caty_gateway as cg
from caty_gateway import pairing_store as ps


PAIR = "deadbeef." + ("0123456789abcdef") * 2  # deliberate test canary, not a credential (concatenation keeps the family secret-guard quiet)


class _FakeImage:
    def save(self, handle, format):
        assert format == "PNG"
        handle.write(b"\x89PNG\r\n\x1a\nfixture")


class _FakeQR:
    payload = None

    def __init__(self, border):
        assert border == 1

    def add_data(self, payload):
        type(self).payload = payload

    def make(self, fit):
        assert fit is True

    def make_image(self):
        return _FakeImage()


class _FakeQRCodeModule:
    QRCode = _FakeQR


class _NonClosingBytesIO(io.BytesIO):
    def close(self):
        pass


class _MemorySocket:
    def __init__(self, request):
        self.input = io.BytesIO(request)
        self.output = _NonClosingBytesIO()

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


class _MemoryServer:
    server_name = "127.0.0.1"
    server_port = 0


def _request_handler(handler, path, peer="127.0.0.1", headers=None):
    headers = dict(headers or {})
    headers.setdefault("Host", "127.0.0.1")
    headers.setdefault("Connection", "close")
    lines = [f"GET {path} HTTP/1.1"]
    lines.extend(f"{key}: {value}" for key, value in headers.items())
    sock = _MemorySocket(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
    handler(sock, (peer, 12345), _MemoryServer())
    response = sock.output.getvalue()
    head, _, body = response.partition(b"\r\n\r\n")
    status = int(head.splitlines()[0].split()[1])
    return status, head, body


class _FakeDeliveryServer:
    def __init__(self, *, fetch=False):
        self.server_address = ("127.0.0.1", 49152)
        self.RequestHandlerClass = None
        self.timeout = None
        self.fetch = fetch
        self.closed = False
        self.requests = 0

    def handle_request(self):
        if self.fetch and self.requests == 0:
            self.requests += 1
            _request_handler(
                self.RequestHandlerClass,
                self.RequestHandlerClass.delivery_path,
            )
        else:
            time.sleep(0.01)

    def server_close(self):
        self.closed = True


class QRDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.saved = {
            "token": cg.CATY_TOKEN,
            "store": cg._pairing_store,
            "config": cg._pairing_config,
        }
        self.tmp = tempfile.TemporaryDirectory(prefix="caty-qr-delivery-test-")
        self.store = ps.PairingStore(
            os.path.join(self.tmp.name, "pairing"),
            "caty",
            config=ps.PairingConfig(ttl_seconds=60),
        )
        cg.CATY_TOKEN = "tok-" + "delivery-test"  # concatenation keeps the family secret-guard quiet
        cg._pairing_store = self.store
        cg._pairing_config = self.store.config
        self.env = mock.patch.dict(
            os.environ,
            {"CATY_PUBLIC_URL": "http://127.0.0.1:8788"},
            clear=False,
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        cg.CATY_TOKEN = self.saved["token"]
        cg._pairing_store = self.saved["store"]
        cg._pairing_config = self.saved["config"]
        self.store.close()
        self.tmp.cleanup()

    def _issue(self, seconds=60):
        issued = self.store.issue()
        issued["expires_at"] = time.time() + seconds
        return issued

    def test_mode_resolution_cli_wins_and_auto_follows_stdout(self):
        class Stream:
            def __init__(self, tty):
                self.tty = tty

            def isatty(self):
                return self.tty

        with mock.patch.dict(os.environ, {"CATY_QR_DELIVERY": "url"}):
            self.assertEqual(cg._qr_delivery_mode("tty", Stream(False)), "tty")
            self.assertEqual(cg._qr_delivery_mode(None, Stream(True)), "url")
        with mock.patch.dict(os.environ, {"CATY_QR_DELIVERY": "auto"}):
            self.assertEqual(cg._qr_delivery_mode(None, Stream(True)), "tty")
            self.assertEqual(cg._qr_delivery_mode(None, Stream(False)), "url")

    def test_invalid_delivery_mode_fails_loud_for_env_and_cli(self):
        message = "CATY_QR_DELIVERY/--qr-delivery must be one of: auto, tty, url"
        with mock.patch.dict(os.environ, {"CATY_QR_DELIVERY": "bogus"}), \
                redirect_stderr(io.StringIO()) as stderr:
            self.assertFalse(cg.print_qr())
        self.assertIn(message, stderr.getvalue())

        with mock.patch.dict(os.environ, {"CATY_QR_DELIVERY": "bogus"}), \
                mock.patch.object(cg.sys, "argv", ["caty_gateway.py", "qr"]), \
                redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit) as exit_status:
                cg.main()
        self.assertEqual(exit_status.exception.code, 1)
        self.assertIn(message, stderr.getvalue())

        with mock.patch.object(
            cg.sys,
            "argv",
            ["caty_gateway.py", "qr", "--qr-delivery", "bogus"],
        ), redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit) as exit_status:
                cg.main()
        self.assertEqual(exit_status.exception.code, 2)
        self.assertIn("invalid choice", stderr.getvalue())

    def test_print_qr_auto_dispatches_redirected_stdout_to_url(self):
        with mock.patch.dict(os.environ, {"CATY_QR_DELIVERY": "auto"}), \
                mock.patch.object(cg, "print_qr_url", return_value=True) as url, \
                mock.patch.object(cg, "_print_qr_tty", return_value=True) as tty, \
                redirect_stdout(io.StringIO()):
            self.assertTrue(cg.print_qr())
        url.assert_called_once_with(None)
        tty.assert_not_called()

    def test_main_url_mode_skips_startup_stdout_and_calls_dispatch_once(self):
        with mock.patch.object(
            cg.sys,
            "argv",
            ["caty_gateway.py", "qr", "--qr-delivery", "url"],
        ), mock.patch.object(cg, "print_qr", return_value=True) as dispatch, \
                mock.patch.object(cg, "_get_pairing_config") as config, \
                redirect_stdout(io.StringIO()) as stdout:
            with self.assertRaises(SystemExit) as exit_status:
                cg.main()
        self.assertEqual(exit_status.exception.code, 0)
        dispatch.assert_called_once_with(delivery="url", wait_visible_seconds=None)
        config.assert_not_called()
        self.assertEqual(stdout.getvalue(), "")

    def test_png_dependencies_fail_before_pairing_issuance(self):
        existing = self.store.issue()
        existing_pid = existing["pair"].partition(".")[0]
        issued = mock.Mock()
        with mock.patch.object(
            cg, "_load_qr_png_dependencies", side_effect=ImportError("PIL")
        ), mock.patch.object(cg, "_issue_pairing_for_qr", issued), \
                redirect_stderr(io.StringIO()) as stderr:
            self.assertFalse(cg.print_qr_url(1))
        issued.assert_not_called()
        self.assertEqual(self.store.live_count(), 1)
        self.assertTrue(
            os.path.isfile(os.path.join(self.store.root_dir, existing_pid + ".json"))
        )
        self.assertIn("qrcode[pil]", stderr.getvalue())

    def test_broken_pillow_renderer_fails_before_pairing_issuance(self):
        class BrokenQR:
            def __init__(self, border):
                pass

            def add_data(self, payload):
                pass

            def make(self, fit):
                pass

            def make_image(self):
                raise RuntimeError("renderer unavailable")

        broken_module = mock.Mock(QRCode=BrokenQR)
        issued = mock.Mock()
        with mock.patch.dict(sys.modules, {"qrcode": broken_module}), \
                mock.patch.object(cg, "_issue_pairing_for_qr", issued), \
                redirect_stderr(io.StringIO()) as stderr:
            self.assertFalse(cg.print_qr_url(1))
        issued.assert_not_called()
        self.assertIn("qrcode[pil]", stderr.getvalue())

    def test_bind_failure_fails_before_issuance_and_removes_private_directory(self):
        temporary_dir = tempfile.mkdtemp(dir=self.tmp.name)
        issued = mock.Mock()
        stderr = io.StringIO()
        with mock.patch.object(
            cg, "_load_qr_png_dependencies", return_value=_FakeQRCodeModule
        ), mock.patch.object(cg, "_issue_pairing_for_qr", issued), mock.patch.object(
            cg,
            "_bind_qr_delivery_server",
            side_effect=OSError("address is not local"),
        ), mock.patch.object(
            cg.tempfile, "mkdtemp", return_value=temporary_dir
        ), redirect_stderr(stderr):
            self.assertFalse(cg.print_qr_url(1))

        issued.assert_not_called()
        self.assertFalse(os.path.exists(temporary_dir))
        self.assertIn("URL QR delivery failed", stderr.getvalue())

    def test_peer_gate_accepts_loopback_tailnet_and_mapped_v4_only(self):
        for peer in ("127.0.0.1", "::1", "100.64.0.1", "100.127.255.254", "::ffff:100.64.0.8"):
            self.assertTrue(cg._tailnet_or_loopback_peer(peer), peer)
        for peer in ("192.168.1.2", "8.8.8.8", "100.128.0.1", "::ffff:192.168.1.2", "bad"):
            self.assertFalse(cg._tailnet_or_loopback_peer(peer), peer)

    def test_one_shot_server_returns_png_then_404_and_ignores_xff(self):
        png_path = pathlib.Path(self.tmp.name) / "qr.png"
        png_path.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        delivered = threading.Event()
        handler = cg._qr_delivery_handler(
            "/qr/random", str(png_path), delivered, time.time() + 60
        )
        status, _, _ = _request_handler(handler, "/qr/random-wrong")
        self.assertEqual(status, 404)
        status, _, _ = _request_handler(
            handler,
            "/qr/random",
            peer="192.168.1.2",
            headers={"X-Forwarded-For": "100.64.0.8"},
        )
        self.assertEqual(status, 404)
        status, head, body = _request_handler(
            handler,
            "/qr/random",
            headers={"X-Forwarded-For": "8.8.8.8"},
        )
        self.assertEqual(status, 200)
        self.assertIn(b"Content-Type: image/png", head)
        self.assertTrue(body.startswith(b"\x89PNG"))
        status, _, _ = _request_handler(handler, "/qr/random")
        self.assertEqual(status, 404)

    def test_url_delivery_fetch_succeeds_with_clean_stdout_and_cleanup(self):
        issued = self._issue()
        output = io.StringIO()
        result = {}
        error_output = io.StringIO()
        fake_server = _FakeDeliveryServer(fetch=True)
        log_output = []

        def run():
            with redirect_stdout(output), redirect_stderr(error_output):
                result["ok"] = cg.print_qr_url(3)

        with mock.patch.object(cg, "_load_qr_png_dependencies", return_value=_FakeQRCodeModule), \
                mock.patch.object(cg, "_issue_pairing_for_qr", return_value=issued) as issue, \
                mock.patch.object(cg, "_bind_qr_delivery_server", return_value=fake_server), \
                mock.patch.object(
                    cg,
                    "log",
                    side_effect=lambda *values: log_output.append(
                        cg._redact_log_text(" ".join(str(value) for value in values))
                    ),
                ):
            thread = threading.Thread(target=run)
            thread.start()
            deadline = time.time() + 2
            while "QR URL: " not in output.getvalue() and time.time() < deadline:
                time.sleep(0.01)
            lines = output.getvalue().splitlines()
            url = next(line.removeprefix("QR URL: ") for line in lines if line.startswith("QR URL: "))
            png = next(line.removeprefix("PNG: ") for line in lines if line.startswith("PNG: "))
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            issue.assert_called_once_with()
            cg.log(issued["pair"], "pairing_invalid")

        self.assertTrue(result["ok"])
        text = output.getvalue()
        actual_secret = issued["pair"].partition(".")[2]
        combined = text + error_output.getvalue() + "\n".join(log_output)
        self.assertNotIn(issued["pair"], combined)
        self.assertNotIn(actual_secret, combined)
        self.assertNotRegex(text, r"[█▀▄]")
        self.assertIn("[REDACTED] pairing_invalid", "\n".join(log_output))
        self.assertRegex(text, r"Expires: \d{4}-\d{2}-\d{2}T.*Z \(\d+ minutes remaining\)")
        self.assertFalse(os.path.exists(png))
        self.assertFalse(os.path.exists(os.path.dirname(png)))
        self.assertTrue(fake_server.closed)
        instructions = text.splitlines()[3:]
        self.assertEqual(len(instructions), 4)
        self.assertIn("SAME private conversation", instructions[0])
        self.assertIn("never paste pair strings or raw command output", instructions[0])
        self.assertIn("Do not open the QR URL yourself", instructions[1])
        self.assertIn("the first fetch consumes it", instructions[1])
        self.assertIn("only the person pairing should open it", instructions[1])
        self.assertIn("upload the PNG", instructions[2])
        self.assertIn("delete the local PNG immediately after sending", instructions[2])
        self.assertIn("delete the uploaded copy after pairing", instructions[2])
        self.assertIn("open the URL", instructions[3])
        self.assertIn("screenshot the QR", instructions[3])
        self.assertIn("import it from Photos in CatyPhone", instructions[3])

    def test_url_uses_the_exact_address_that_accepted_the_bind(self):
        issued = self._issue()
        output = io.StringIO()
        fake_server = _FakeDeliveryServer(fetch=True)
        fake_server.server_address = ("100.64.0.11", 49152)

        with mock.patch.object(
            cg, "_load_qr_png_dependencies", return_value=_FakeQRCodeModule
        ), mock.patch.object(
            cg,
            "_qr_delivery_bind_target",
            return_value=("member.tailnet", ["100.64.0.10", "100.64.0.11"]),
        ), mock.patch.object(
            cg, "_issue_pairing_for_qr", return_value=issued
        ), mock.patch.object(
            cg, "_bind_qr_delivery_server", return_value=fake_server
        ), redirect_stdout(output), redirect_stderr(io.StringIO()):
            self.assertTrue(cg.print_qr_url(3))

        url_line = next(
            line for line in output.getvalue().splitlines() if line.startswith("QR URL: ")
        )
        self.assertTrue(url_line.startswith("QR URL: http://100.64.0.11:49152/qr/"))
        self.assertNotIn("member.tailnet", url_line)

    def test_url_delivery_claim_is_success_and_deadline_is_fail_loud(self):
        issued = self._issue()
        claimed = {}
        stderr = io.StringIO()

        def claim_after_issue():
            deadline = time.time() + 2
            while self.store.live_count() == 0 and time.time() < deadline:
                time.sleep(0.01)
            claimed["token"] = self.store.claim(
                issued["pair"], credential_getter=lambda: cg.CATY_TOKEN
            )

        worker = threading.Thread(target=claim_after_issue)
        worker.start()
        output = io.StringIO()
        claim_server = _FakeDeliveryServer(fetch=False)
        with mock.patch.object(cg, "_load_qr_png_dependencies", return_value=_FakeQRCodeModule), \
                mock.patch.object(cg, "_issue_pairing_for_qr", return_value=issued), \
                mock.patch.object(cg, "_bind_qr_delivery_server", return_value=claim_server), \
                redirect_stdout(output), redirect_stderr(stderr):
            self.assertTrue(cg.print_qr_url(3))
        worker.join(timeout=2)
        self.assertEqual(claimed["token"], cg.CATY_TOKEN)
        self.assertNotIn("timed out", stderr.getvalue())
        claim_png = next(
            line.removeprefix("PNG: ")
            for line in output.getvalue().splitlines()
            if line.startswith("PNG: ")
        )
        self.assertFalse(os.path.exists(claim_png))
        self.assertFalse(os.path.exists(os.path.dirname(claim_png)))

        timeout_issue = self._issue(seconds=60)
        output = io.StringIO()
        stderr = io.StringIO()
        timeout_server = _FakeDeliveryServer(fetch=False)
        with mock.patch.object(cg, "_load_qr_png_dependencies", return_value=_FakeQRCodeModule), \
                mock.patch.object(cg, "_issue_pairing_for_qr", return_value=timeout_issue), \
                mock.patch.object(cg, "_bind_qr_delivery_server", return_value=timeout_server), \
                redirect_stdout(output), redirect_stderr(stderr):
            self.assertFalse(cg.print_qr_url(0))
        png = next(
            line.removeprefix("PNG: ")
            for line in output.getvalue().splitlines()
            if line.startswith("PNG: ")
        )
        self.assertFalse(os.path.exists(png))
        self.assertFalse(os.path.exists(os.path.dirname(png)))
        self.assertIn("timed out", stderr.getvalue())
        self.assertIn("rerun caty_gateway.py qr", stderr.getvalue())

    def test_post_issue_exception_is_redacted_and_removes_private_directory(self):
        issued = self._issue()
        temporary_dir = tempfile.mkdtemp(dir=self.tmp.name)
        fake_server = _FakeDeliveryServer(fetch=False)
        stderr = io.StringIO()
        with mock.patch.object(cg, "_load_qr_png_dependencies", return_value=_FakeQRCodeModule), \
                mock.patch.object(cg, "_issue_pairing_for_qr", return_value=issued), \
                mock.patch.object(cg, "_bind_qr_delivery_server", return_value=fake_server), \
                mock.patch.object(cg.tempfile, "mkdtemp", return_value=temporary_dir), \
                mock.patch.object(
                    cg,
                    "_render_qr_png",
                    side_effect=RuntimeError("render failed for " + issued["pair"]),
                ), redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            self.assertFalse(cg.print_qr_url(1))

        self.assertFalse(os.path.exists(temporary_dir))
        self.assertTrue(fake_server.closed)
        self.assertNotIn(issued["pair"], stderr.getvalue())
        self.assertNotIn(issued["pair"].partition(".")[2], stderr.getvalue())
        self.assertIn("[REDACTED]", stderr.getvalue())

    def test_server_close_failure_still_removes_png_and_cancels_success(self):
        issued = self._issue()
        output = io.StringIO()
        stderr = io.StringIO()

        class FailingCloseServer(_FakeDeliveryServer):
            def server_close(self):
                self.closed = True
                raise OSError("close failed")

        fake_server = FailingCloseServer(fetch=True)
        with mock.patch.object(cg, "_load_qr_png_dependencies", return_value=_FakeQRCodeModule), \
                mock.patch.object(cg, "_issue_pairing_for_qr", return_value=issued), \
                mock.patch.object(cg, "_bind_qr_delivery_server", return_value=fake_server), \
                redirect_stdout(output), redirect_stderr(stderr):
            self.assertFalse(cg.print_qr_url(3))

        png = next(
            line.removeprefix("PNG: ")
            for line in output.getvalue().splitlines()
            if line.startswith("PNG: ")
        )
        self.assertFalse(os.path.exists(os.path.dirname(png)))
        self.assertIn("URL QR cleanup failed", stderr.getvalue())

    def test_sigint_closes_server_and_removes_private_directory(self):
        issued = self._issue()
        output = io.StringIO()

        class InterruptingServer(_FakeDeliveryServer):
            def handle_request(self):
                raise KeyboardInterrupt

        fake_server = InterruptingServer(fetch=False)
        with mock.patch.object(
            cg, "_load_qr_png_dependencies", return_value=_FakeQRCodeModule
        ), mock.patch.object(
            cg, "_issue_pairing_for_qr", return_value=issued
        ), mock.patch.object(
            cg, "_bind_qr_delivery_server", return_value=fake_server
        ), redirect_stdout(output), redirect_stderr(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                cg.print_qr_url(3)

        png = next(
            line.removeprefix("PNG: ")
            for line in output.getvalue().splitlines()
            if line.startswith("PNG: ")
        )
        self.assertTrue(fake_server.closed)
        self.assertFalse(os.path.exists(os.path.dirname(png)))

    def test_sigterm_in_wait_loop_cleans_up_and_restores_handler(self):
        issued = self._issue()
        temporary_dir = tempfile.mkdtemp(dir=self.tmp.name)
        wait_loop_entered = threading.Event()

        class WaitingServer(_FakeDeliveryServer):
            def handle_request(self):
                wait_loop_entered.set()
                time.sleep(0.05)

        fake_server = WaitingServer(fetch=False)
        previous_handler = signal.getsignal(signal.SIGTERM)

        def terminate_in_wait_loop():
            if wait_loop_entered.wait(timeout=2):
                os.kill(os.getpid(), signal.SIGTERM)

        timer = threading.Timer(0.01, terminate_in_wait_loop)
        timer.start()
        try:
            with mock.patch.object(
                cg, "_load_qr_png_dependencies", return_value=_FakeQRCodeModule
            ), mock.patch.object(
                cg, "_issue_pairing_for_qr", return_value=issued
            ), mock.patch.object(
                cg, "_bind_qr_delivery_server", return_value=fake_server
            ), mock.patch.object(
                cg.tempfile, "mkdtemp", return_value=temporary_dir
            ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as exit_status:
                    cg.print_qr_url(3)
        finally:
            timer.cancel()
            timer.join(timeout=2)

        self.assertEqual(exit_status.exception.code, 143)
        self.assertTrue(fake_server.closed)
        self.assertFalse(os.path.exists(temporary_dir))
        self.assertIs(signal.getsignal(signal.SIGTERM), previous_handler)

    def test_second_sigterm_during_cleanup_is_ignored_and_restores_handler(self):
        temporary_dir = tempfile.mkdtemp(dir=self.tmp.name)

        class TerminatingCloseServer(_FakeDeliveryServer):
            def server_close(self):
                self.closed = True
                os.kill(os.getpid(), signal.SIGTERM)

        fake_server = TerminatingCloseServer(fetch=False)
        previous_handler = signal.getsignal(signal.SIGTERM)
        try:
            with cg._sigterm_raises_system_exit():
                cleanup_handler = signal.getsignal(signal.SIGTERM)

                self.assertTrue(cg._cleanup_qr_delivery(fake_server, temporary_dir))

                self.assertTrue(fake_server.closed)
                self.assertFalse(os.path.exists(temporary_dir))
                self.assertIs(signal.getsignal(signal.SIGTERM), cleanup_handler)
        finally:
            self.assertIs(signal.getsignal(signal.SIGTERM), previous_handler)

    def test_cleanup_in_non_main_thread_still_closes_and_removes_directory(self):
        temporary_dir = tempfile.mkdtemp(dir=self.tmp.name)
        fake_server = _FakeDeliveryServer(fetch=False)
        previous_handler = signal.getsignal(signal.SIGTERM)
        results = []
        errors = []

        def run():
            try:
                results.append(cg._cleanup_qr_delivery(fake_server, temporary_dir))
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=run)
        thread.start()
        thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [True])
        self.assertTrue(fake_server.closed)
        self.assertFalse(os.path.exists(temporary_dir))
        self.assertIs(signal.getsignal(signal.SIGTERM), previous_handler)

    def test_normal_success_restores_sigterm_handler(self):
        issued = self._issue()
        fake_server = _FakeDeliveryServer(fetch=True)
        original_handler = signal.getsignal(signal.SIGTERM)

        def previous_handler(_signum, _frame):
            pass

        signal.signal(signal.SIGTERM, previous_handler)
        try:
            with mock.patch.object(
                cg, "_load_qr_png_dependencies", return_value=_FakeQRCodeModule
            ), mock.patch.object(
                cg, "_issue_pairing_for_qr", return_value=issued
            ), mock.patch.object(
                cg, "_bind_qr_delivery_server", return_value=fake_server
            ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertTrue(cg.print_qr_url(3))

            self.assertIs(signal.getsignal(signal.SIGTERM), previous_handler)
        finally:
            signal.signal(signal.SIGTERM, original_handler)

    def test_sigterm_context_manager_in_non_main_thread_still_runs_body(self):
        body_executed = threading.Event()
        errors = []

        def run():
            try:
                with cg._sigterm_raises_system_exit():
                    body_executed.set()
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=run)
        thread.start()
        thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertTrue(body_executed.is_set())
        self.assertEqual(errors, [])

    def test_expiry_caps_wait_even_when_visible_wait_is_longer(self):
        self.assertEqual(cg._url_qr_wait_seconds(999, time.time() + 0.01), 1)
        self.assertEqual(cg._url_qr_wait_seconds(0, time.time() + 60), 1)

    def test_handler_rejects_expired_image_and_reserves_concurrent_fetch(self):
        png_path = pathlib.Path(self.tmp.name) / "qr-expiry.png"
        png_path.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        expired = cg._qr_delivery_handler(
            "/qr/expired", str(png_path), threading.Event(), time.time() - 1
        )
        status, _, _ = _request_handler(expired, "/qr/expired")
        self.assertEqual(status, 404)

        entered = threading.Event()
        release = threading.Event()

        class SlowFile(io.BytesIO):
            def __enter__(self):
                entered.set()
                release.wait(timeout=2)
                return self

            def __exit__(self, *args):
                return False

        delivered = threading.Event()
        concurrent = cg._qr_delivery_handler(
            "/qr/concurrent", str(png_path), delivered, time.time() + 60
        )
        first = {}

        def fetch_first():
            first["status"] = _request_handler(concurrent, "/qr/concurrent")[0]

        try:
            real_open = open

            def slow_open(path, *args, **kwargs):
                if str(path) == str(png_path):
                    return SlowFile(b"\x89PNG\r\n\x1a\nfixture")
                return real_open(path, *args, **kwargs)

            with mock.patch("builtins.open", side_effect=slow_open):
                first_thread = threading.Thread(target=fetch_first)
                first_thread.start()
                self.assertTrue(entered.wait(timeout=2))
                self.assertEqual(
                    _request_handler(concurrent, "/qr/concurrent")[0], 404
                )
                release.set()
                first_thread.join(timeout=3)
            self.assertEqual(first["status"], 200)
        finally:
            release.set()

    def test_silent_client_cannot_park_single_threaded_delivery_server(self):
        png_path = pathlib.Path(self.tmp.name) / "qr-silent-client.png"
        png_path.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        delivered = threading.Event()
        handler = cg._qr_delivery_handler(
            "/qr/silent-client", str(png_path), delivered, time.time() + 60
        )
        self.assertEqual(handler.timeout, 5)
        handler.timeout = 0.05
        server_side, silent = socket.socketpair()
        try:
            stalled_request = threading.Thread(
                target=handler,
                args=(server_side, ("127.0.0.1", 12345), _MemoryServer()),
            )
            stalled_request.start()
            stalled_request.join(timeout=1)
            self.assertFalse(stalled_request.is_alive())
            self.assertFalse(delivered.is_set())

            self.assertEqual(
                _request_handler(handler, "/qr/silent-client")[0], 200
            )
            self.assertTrue(delivered.is_set())
        finally:
            silent.close()
            server_side.close()

    def test_disconnected_reader_releases_reservation_for_next_fetch(self):
        png_path = pathlib.Path(self.tmp.name) / "qr-disconnected-reader.png"
        png_path.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        delivered = threading.Event()
        handler = cg._qr_delivery_handler(
            "/qr/disconnected", str(png_path), delivered, time.time() + 60
        )
        request = (
            b"GET /qr/disconnected HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\nConnection: close\r\n\r\n"
        )
        disconnected = _MemorySocket(request)
        with mock.patch.object(
            disconnected, "sendall", side_effect=BrokenPipeError("reader closed")
        ):
            handler(disconnected, ("127.0.0.1", 12345), _MemoryServer())

        self.assertFalse(delivered.is_set())
        self.assertEqual(_request_handler(handler, "/qr/disconnected")[0], 200)
        self.assertEqual(_request_handler(handler, "/qr/disconnected")[0], 404)

    def test_bind_target_rejects_wildcard_public_and_lan_addresses(self):
        for value in (
            "http://0.0.0.0:8788",
            "http://[::]:8788",
            "http://8.8.8.8:8788",
            "http://192.168.1.10:8788",
        ):
            with self.subTest(value=value), mock.patch.dict(
                os.environ, {"CATY_PUBLIC_URL": value}
            ):
                with self.assertRaises(ValueError):
                    cg._qr_delivery_bind_target()

    def test_bind_target_rejects_mixed_safe_and_unsafe_addresses(self):
        resolved = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.64.0.9", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", 0)),
        ]
        with mock.patch.dict(
            os.environ, {"CATY_PUBLIC_URL": "http://member.example:8788"}
        ), mock.patch.object(cg.socket, "getaddrinfo", return_value=resolved):
            with self.assertRaisesRegex(ValueError, "resolve only"):
                cg._qr_delivery_bind_target()


if __name__ == "__main__":
    unittest.main()
