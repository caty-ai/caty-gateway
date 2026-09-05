import base64
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock


from caty_gateway import caty_gateway as cg
from caty_gateway import filler_texts
from caty_gateway import voice_activation
from tests.test_config_api import MemoryServer, MemorySocket, multipart


class FakeFillerTextService:
    def __init__(self, data_root, member_id):
        self.data_root = data_root
        self.member_id = member_id
        self.regenerate_calls = []

    def state(self):
        effective = filler_texts.effective(self.member_id, self.data_root)
        return {
            "filler": {
                "desired_text_version": effective.version,
                "active_text_version": None,
                "text_stale": False,
            }
        }

    def regenerate(self, *, force=False):
        self.regenerate_calls.append(force)
        effective = filler_texts.effective(self.member_id, self.data_root)
        if effective.override_status.startswith("invalid:") and not force:
            raise voice_activation.ActivationError("override_invalid", 409)
        return {"ok": True, "action": "regenerated", "state": self.state()}


class WriteAuthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="caty-write-auth-")
        self.config_dir = os.path.join(self.tmp, "config")
        self.asset_dir = os.path.join(self.tmp, "assets")
        self.filler_dir = os.path.join(self.tmp, "fillers")
        os.makedirs(self.config_dir)
        os.makedirs(self.asset_dir)
        os.makedirs(self.filler_dir)
        self.old_env = {k: os.environ.get(k) for k in ("CATY_CONFIG_DIR",)}
        os.environ["CATY_CONFIG_DIR"] = self.config_dir
        self.old_globals = {
            "CATY_TOKEN": cg.CATY_TOKEN,
            "CATY_ADMIN_TOKEN": cg.CATY_ADMIN_TOKEN,
            "ASSET_DIR": cg.ASSET_DIR,
            "FILLER_DIR": cg.FILLER_DIR,
            "FILLER_METADATA": list(cg.FILLER_METADATA),
            "IDENTITY_ID": cg.IDENTITY_ID,
            "_voice_activation_service": cg._voice_activation_service,
        }
        cg.CATY_TOKEN = ""
        cg.CATY_ADMIN_TOKEN = ""
        cg.ASSET_DIR = self.asset_dir
        cg.FILLER_DIR = self.filler_dir
        cg.IDENTITY_ID = "write-auth-member"
        cg._voice_activation_service = FakeFillerTextService(
            self.tmp, cg.IDENTITY_ID
        )
        cg.FILLERS[:] = []
        cg.FILLER_METADATA[:] = []
        cg.SILENCE_1S = None

    def tearDown(self):
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for key, value in self.old_globals.items():
            if key == "FILLER_METADATA":
                cg.FILLER_METADATA[:] = value
            else:
                setattr(cg, key, value)
        cg.FILLERS[:] = []
        cg.SILENCE_1S = None
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

    def json_request(self, method, path, payload, headers=None):
        body = json.dumps(payload).encode("utf-8")
        hdrs = {"Content-Type": "application/json"}
        hdrs.update(headers or {})
        return self.request(method, path, body=body, headers=hdrs)

    def valid_assets_body(self):
        png = b"\x89PNG\r\n\x1a\nx"
        jpg = b"\xff\xd8\xffx"
        return multipart({
            "icon": png,
            "idle": jpg,
            "talk1": png,
            "talk2": jpg,
            "talk3": png,
            "blink": jpg,
            "talk_blink": png,
            "listen": jpg,
        })

    def filler_payload(self):
        return [{"name": "filler1.mp3", "data_base64": base64.b64encode(b"ID3filler").decode("ascii")}]

    def assert_fail_closed(self, method, path, body=b"", headers=None):
        status, _, response = self.request(method, path, body=body, headers=headers)
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(response), {"ok": False, "error": "writes disabled: no token configured"})

    def test_empty_token_disables_all_write_endpoints(self):
        body, ctype = self.valid_assets_body()

        self.assert_fail_closed(
            "PUT", "/config", body=json.dumps({"name": "Blocked"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "If-Match": "1"})
        self.assert_fail_closed("POST", "/assets:batch", body=body, headers={"Content-Type": ctype})
        self.assert_fail_closed(
            "PUT", "/fillers", body=json.dumps(self.filler_payload()).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        self.assert_fail_closed(
            "PUT", "/fillers:texts",
            body=json.dumps({"kinds": {"wait": ["待って"]}}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assert_fail_closed("DELETE", "/fillers:texts")
        self.assert_fail_closed(
            "POST", "/fillers:regenerate", body=b"{}",
            headers={"Content-Type": "application/json"},
        )

    def test_client_token_allows_writes_and_bad_or_missing_token_is_401(self):
        cg.CATY_TOKEN = "client-secret"
        auth = {"X-Caty-Token": "client-secret"}

        status, _, _ = self.json_request("PUT", "/config", {"name": "Token OK"}, headers={**auth, "If-Match": "1"})
        self.assertEqual(status, 200)

        body, ctype = self.valid_assets_body()
        status, _, _ = self.request("POST", "/assets:batch", body=body, headers={**auth, "Content-Type": ctype})
        self.assertEqual(status, 200)

        status, _, _ = self.json_request("PUT", "/fillers", self.filler_payload(), headers=auth)
        self.assertEqual(status, 200)

        status, _, body = self.json_request("PUT", "/config", {"name": "Missing"}, headers={"If-Match": "2"})
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body), {"ok": False, "error": "unauthorized"})

        status, _, body = self.json_request(
            "PUT", "/config", {"name": "Wrong"}, headers={"X-Caty-Token": "wrong", "If-Match": "2"})
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body), {"ok": False, "error": "unauthorized"})

        asset_body, ctype = self.valid_assets_body()
        for headers in ({"Content-Type": ctype}, {"Content-Type": ctype, "X-Caty-Token": "wrong"}):
            status, _, body = self.request("POST", "/assets:batch", body=asset_body, headers=headers)
            self.assertEqual(status, 401)
            self.assertEqual(json.loads(body), {"ok": False, "error": "unauthorized"})

        for headers in ({}, {"X-Caty-Token": "wrong"}):
            status, _, body = self.json_request("PUT", "/fillers", self.filler_payload(), headers=headers)
            self.assertEqual(status, 401)
            self.assertEqual(json.loads(body), {"ok": False, "error": "unauthorized"})

    def test_both_member_and_admin_tokens_allow_writes(self):
        cg.CATY_TOKEN = "client-secret"
        cg.CATY_ADMIN_TOKEN = "admin-secret"

        status, _, _ = self.json_request(
            "PUT", "/config", {"name": "Client Accepted"},
            headers={"X-Caty-Token": "client-secret", "If-Match": "1"})
        self.assertEqual(status, 200)

        status, _, _ = self.json_request(
            "PUT", "/config", {"name": "Admin Accepted"},
            headers={"Authorization": "Bearer admin-secret", "If-Match": "2"})
        self.assertEqual(status, 200)

        status, _, body = self.json_request(
            "PUT", "/config", {"name": "Wrong"},
            headers={"X-Caty-Token": "wrong", "If-Match": "3"})
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body), {"ok": False, "error": "unauthorized"})

    def test_admin_token_only_config_enables_writes(self):
        cg.CATY_TOKEN = ""
        cg.CATY_ADMIN_TOKEN = "admin-secret"

        status, _, body = self.json_request("PUT", "/config", {"name": "No Token"}, headers={"If-Match": "1"})
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body), {"ok": False, "error": "unauthorized"})

        status, _, _ = self.json_request(
            "PUT", "/config", {"name": "Admin Only"},
            headers={"X-Caty-Token": "admin-secret", "If-Match": "1"})
        self.assertEqual(status, 200)

        status, _, body = self.json_request(
            "PUT", "/config", {"name": "Member Rejected"},
            headers={"X-Caty-Token": "client-secret", "If-Match": "2"})
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body), {"ok": False, "error": "unauthorized"})

        # 読み取り系は従来どおり開放のまま（CATY_TOKEN 空）
        status, _, _ = self.request("GET", "/config")
        self.assertEqual(status, 200)

    def test_bearer_and_x_caty_token_headers_are_symmetric(self):
        cg.CATY_TOKEN = "client-secret"

        status, _, _ = self.json_request(
            "PUT", "/config", {"name": "Bearer Client"},
            headers={"Authorization": "Bearer client-secret", "If-Match": "1"})
        self.assertEqual(status, 200)

        cg.CATY_ADMIN_TOKEN = "admin-secret"
        status, _, _ = self.json_request(
            "PUT", "/config", {"name": "X-Caty Admin"},
            headers={"X-Caty-Token": "admin-secret", "If-Match": "2"})
        self.assertEqual(status, 200)

    def test_non_ascii_token_header_is_401_not_500(self):
        cg.CATY_TOKEN = "client-secret"
        for headers in (
            {"X-Caty-Token": "café", "If-Match": "1"},
            {"Authorization": "Bearer café", "If-Match": "1"},
        ):
            status, _, body = self.json_request("PUT", "/config", {"name": "Bad"}, headers=headers)
            self.assertEqual(status, 401)
            self.assertEqual(json.loads(body), {"ok": False, "error": "unauthorized"})

    def test_asset_reads_follow_legacy_optional_auth(self):
        asset_path = os.path.join(self.asset_dir, "icon.png")
        with open(asset_path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\nasset")

        cg.CATY_TOKEN = "client-secret"
        status, _, body = self.request("GET", "/asset/icon.png")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body), {"ok": False, "error": "unauthorized"})

        status, _, body = self.request("GET", "/asset/icon.png", headers={"X-Caty-Token": "client-secret"})
        self.assertEqual(status, 200)
        self.assertEqual(body, b"\x89PNG\r\n\x1a\nasset")

        cg.CATY_TOKEN = ""
        status, _, body = self.request("GET", "/asset/icon.png")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"\x89PNG\r\n\x1a\nasset")

    def test_filler_text_routes_enforce_read_auth_and_optimistic_writes(self):
        cg.CATY_TOKEN = "client-secret"
        auth = {"X-Caty-Token": "client-secret"}

        status, _, body = self.request("GET", "/fillers:texts")
        self.assertEqual(status, 401)
        status, _, body = self.request("GET", "/fillers:texts", headers=auth)
        self.assertEqual(status, 200)
        initial = json.loads(body)
        self.assertEqual(initial["override_status"], "none")
        self.assertEqual(initial["live_pool"], "legacy")
        with mock.patch.dict(
            os.environ, {"CATY_VOICE_FILLER_MAX_TEXTS_PER_KIND": "2"}
        ):
            status, _, body = self.request("GET", "/fillers:texts", headers=auth)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["constraints"]["per_kind_max"], 2)
        with mock.patch.object(
            cg.CONFIG, "get", return_value={"voice_management_state": "managed"}
        ):
            status, _, body = self.request(
                "GET", "/fillers:texts", headers=auth
            )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["live_pool"], "managed")

        status, _, body = self.json_request(
            "PUT", "/fillers:texts", {"kinds": {"wait": [" 待って "]}},
            headers=auth,
        )
        self.assertEqual(status, 200)
        saved = json.loads(body)
        self.assertEqual(saved["override"], {"wait": ["待って"]})
        self.assertNotEqual(saved["version"], initial["version"])

        status, _, body = self.json_request(
            "PUT", "/fillers:texts", {"kinds": {"wait": ["別の文言"]}},
            headers=auth,
        )
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["version"], saved["version"])

        status, _, body = self.json_request(
            "PUT", "/fillers:texts", {"kinds": {"wait": []}},
            headers={**auth, "If-Match": saved["version"]},
        )
        self.assertEqual(status, 400)
        self.assertIn("wait", json.loads(body)["errors"])

        status, _, body = self.request(
            "DELETE", "/fillers:texts", headers=auth
        )
        self.assertEqual(status, 409)
        status, _, body = self.request(
            "DELETE", "/fillers:texts",
            headers={**auth, "If-Match": saved["version"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["override_status"], "none")
        self.assertEqual(cg._voice_activation_service.regenerate_calls, [])

    def test_filler_regenerate_rejects_invalid_override_unless_forced(self):
        cg.CATY_TOKEN = "client-secret"
        auth = {
            "X-Caty-Token": "client-secret",
            "Content-Type": "application/json",
        }
        path = filler_texts.override_path(cg.IDENTITY_ID, self.tmp)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{", encoding="utf-8")

        status, _, body = self.request(
            "POST", "/fillers:regenerate", headers=auth
        )
        self.assertEqual(
            (status, json.loads(body)),
            (409, {"ok": False, "error": "override_invalid"}),
        )
        status, _, body = self.request(
            "POST", "/fillers:regenerate", body=b'{"force":true}', headers=auth
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["action"], "regenerated")

    def test_filler_regenerate_rejects_non_object_json_body(self):
        cg.CATY_TOKEN = "client-secret"

        status, _, body = self.json_request(
            "POST",
            "/fillers:regenerate",
            [],
            headers={"X-Caty-Token": "client-secret"},
        )

        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body), {"ok": False, "error": "invalid_request"})

    def test_successful_filler_text_writes_return_version_when_payload_refresh_fails(self):
        cg.CATY_TOKEN = "client-secret"
        auth = {"X-Caty-Token": "client-secret"}
        with mock.patch.object(
            cg.Handler, "_filler_texts_payload", side_effect=RuntimeError("refresh")
        ):
            status, _, body = self.json_request(
                "PUT", "/fillers:texts", {"kinds": {"wait": ["待って"]}}, headers=auth
            )
            put_response = json.loads(body)
            self.assertEqual(status, 200)
            self.assertTrue(put_response["ok"])

            status, _, body = self.request(
                "DELETE",
                "/fillers:texts",
                headers={**auth, "If-Match": put_response["version"]},
            )

        expected = filler_texts.effective(cg.IDENTITY_ID, self.tmp)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True, "version": expected.version})

    def test_colon_text_route_does_not_collide_with_legacy_mp3_namespace(self):
        legacy = b"ID3legacy-filler"
        with open(os.path.join(self.filler_dir, "texts.mp3"), "wb") as handle:
            handle.write(legacy)
        cg.FILLER_METADATA[:] = [{
            "name": "texts.mp3", "duration_sec": 1.0,
            "size": len(legacy), "text": None,
        }]

        status, headers, body = self.request("GET", "/fillers/texts.mp3")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "audio/mpeg")
        self.assertEqual(body, legacy)
        status, headers, body = self.request("GET", "/fillers:texts")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/json")
        self.assertTrue(json.loads(body)["ok"])


if __name__ == "__main__":
    unittest.main()
