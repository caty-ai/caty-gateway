import json
import os
import shutil
import sys
import tempfile
import unittest


from caty_gateway import caty_gateway as cg
from tests.test_config_api import MemoryServer, MemorySocket


class RequireAuthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="caty-require-auth-")
        self.old_env = {
            k: os.environ.get(k)
            for k in (
                "CATY_REQUIRE_AUTH",
                "CATY_GATEWAY_BIND",
                "CATY_HISTORY_DIR",
                "CATY_TOKEN",
                "CATY_ADMIN_TOKEN",
            )
        }
        os.environ["CATY_HISTORY_DIR"] = self.tmp
        for key in ("CATY_REQUIRE_AUTH", "CATY_GATEWAY_BIND", "CATY_TOKEN", "CATY_ADMIN_TOKEN"):
            os.environ.pop(key, None)
        self.old_globals = {
            "CATY_TOKEN": cg.CATY_TOKEN,
            "CATY_ADMIN_TOKEN": cg.CATY_ADMIN_TOKEN,
        }
        cg.CATY_TOKEN = ""
        cg.CATY_ADMIN_TOKEN = ""

    def tearDown(self):
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for key, value in self.old_globals.items():
            setattr(cg, key, value)
        shutil.rmtree(self.tmp)

    def request(self, method, path, body=b"", headers=None):
        headers = dict(headers or {})
        headers.setdefault("Host", "127.0.0.1")
        headers.setdefault("Connection", "close")
        headers.setdefault("Content-Length", str(len(body)))
        raw = [f"{method} {path} HTTP/1.1"]
        raw.extend(f"{key}: {value}" for key, value in headers.items())
        request_bytes = ("\r\n".join(raw) + "\r\n\r\n").encode("latin-1") + body
        sock = MemorySocket(request_bytes)
        cg.Handler(sock, ("127.0.0.1", 0), MemoryServer())
        data = sock.output.getvalue()
        head, _, rest = data.partition(b"\r\n\r\n")
        lines = head.decode("iso-8859-1").split("\r\n")
        status = int(lines[0].split()[1])
        response_headers = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                response_headers[key.lower()] = value.strip()
        length = int(response_headers.get("content-length", "0"))
        return status, response_headers, rest[:length]

    def auth_headers(self, token="member-token"):
        return {"X-Caty-Token": token}

    def test_flag_off_empty_token_keeps_allow_all_reads(self):
        status, _, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["ok"], True)

        status, _, body = self.request("GET", "/history")
        self.assertEqual(status, 200)
        self.assertIn("sessions", json.loads(body))

    def test_flag_off_configured_token_still_requires_header(self):
        cg.CATY_TOKEN = "member-token"

        status, _, body = self.request("GET", "/history")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body), {"ok": False, "error": "unauthorized"})

    def test_flag_on_empty_token_fails_closed_for_protected_routes(self):
        os.environ["CATY_REQUIRE_AUTH"] = "1"

        for method, path in (("GET", "/health"), ("GET", "/history"), ("POST", "/talk2")):
            status, _, body = self.request(method, path)
            self.assertEqual(status, 401)
            self.assertEqual(json.loads(body), {"ok": False, "error": "unauthorized"})

    def test_flag_on_configured_token_authorizes_valid_header(self):
        os.environ["CATY_REQUIRE_AUTH"] = "true"
        cg.CATY_TOKEN = "member-token"

        status, _, body = self.request("GET", "/health")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body), {"ok": False, "error": "unauthorized"})

        status, _, body = self.request("GET", "/health", headers=self.auth_headers())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["ok"], True)

        status, _, body = self.request("GET", "/history", headers=self.auth_headers())
        self.assertEqual(status, 200)
        self.assertIn("sessions", json.loads(body))

    def test_flag_on_does_not_relax_write_auth_without_tokens(self):
        os.environ["CATY_REQUIRE_AUTH"] = "1"

        status, _, body = self.request("PUT", "/config", body=b"{}")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body), {"ok": False, "error": "writes disabled: no token configured"})

    def test_bind_host_defaults_when_unset(self):
        os.environ.pop("CATY_GATEWAY_BIND", None)

        self.assertEqual(cg._bind_host(), "0.0.0.0")

    def test_bind_host_empty_string_uses_default(self):
        os.environ["CATY_GATEWAY_BIND"] = "   "

        self.assertEqual(cg._bind_host(), "0.0.0.0")

    def test_bind_host_custom_value_passes_through(self):
        os.environ["CATY_GATEWAY_BIND"] = "127.0.0.1"

        self.assertEqual(cg._bind_host(), "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
