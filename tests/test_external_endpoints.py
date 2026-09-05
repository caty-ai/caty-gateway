import json
import os
import shutil
import sys
import tempfile
import unittest


from caty_gateway import caty_gateway as cg
from caty_gateway import history_store
from caty_gateway import session_links
from tests.test_config_api import MemoryServer, MemorySocket


class FakeBackend:
    def __init__(self):
        self.sessions = [
            {
                "native_id": "native-a",
                "label": "External A",
                "updated_at": "2026-07-05T01:00:02Z",
                "preview": "preview A",
            }
        ]
        self.turns = {
            "native-a": [
                {"role": "user", "text": "oldest", "ts": "2026-07-05T01:00:00Z"},
                {"role": "assistant", "text": "newest", "ts": "2026-07-05T01:00:01Z"},
            ]
        }
        self.list_limits = []
        self.read_calls = []

    def list_external(self, limit=30):
        self.list_limits.append(limit)
        return [dict(item) for item in self.sessions[:limit]]

    def read_external(self, native_id, limit=50):
        self.read_calls.append((native_id, limit))
        return [dict(item) for item in self.turns.get(native_id, [])[:limit]]


class ExternalEndpointsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="caty-external-api-")
        self.old_env = {
            k: os.environ.get(k)
            for k in (
                "CATY_EXTERNAL_SESSIONS",
                "CATY_EXTERNAL_PREVIEW",
                "CATY_EXTERNAL_SEED_TURNS",
                "CATY_HISTORY_DIR",
            )
        }
        os.environ["CATY_HISTORY_DIR"] = self.tmp
        for key in ("CATY_EXTERNAL_SESSIONS", "CATY_EXTERNAL_PREVIEW", "CATY_EXTERNAL_SEED_TURNS"):
            os.environ.pop(key, None)
        self.fake_backend = FakeBackend()
        self.old_globals = {
            "CATY_TOKEN": cg.CATY_TOKEN,
            "CATY_ADMIN_TOKEN": cg.CATY_ADMIN_TOKEN,
            "BACKEND_NAME": cg.BACKEND_NAME,
            "BACKEND": cg.BACKEND,
        }
        cg.CATY_TOKEN = ""
        cg.CATY_ADMIN_TOKEN = ""
        cg.BACKEND_NAME = "claude"
        cg.BACKEND = self.fake_backend

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

    def json_request(self, method, path, payload, headers=None):
        body = json.dumps(payload).encode("utf-8")
        hdrs = {"Content-Type": "application/json"}
        hdrs.update(headers or {})
        return self.request(method, path, body=body, headers=hdrs)

    def auth_headers(self, token="member-token"):
        return {"X-Caty-Token": token}

    def enable(self):
        os.environ["CATY_EXTERNAL_SESSIONS"] = "1"
        cg.CATY_TOKEN = "member-token"

    def assert_same_unknown_response(self, actual, expected):
        actual_status, actual_headers, actual_body = actual
        expected_status, expected_headers, expected_body = expected
        self.assertEqual(actual_status, expected_status)
        self.assertEqual(actual_body, expected_body)
        self.assertEqual(actual_headers.get("content-type"), expected_headers.get("content-type"))
        self.assertEqual(actual_headers.get("content-length"), expected_headers.get("content-length"))

    def test_ungated_routes_are_unknown_route_identical(self):
        get_unknown = self.request("GET", "/totally/bogus/path")
        get_external = self.request("GET", "/external/sessions?limit=30")
        self.assert_same_unknown_response(get_external, get_unknown)

        body = json.dumps({"native_id": "native-a"}).encode("utf-8")
        post_unknown = self.request("POST", "/totally/bogus/path", body=body)
        post_external = self.request("POST", "/external/takeover", body=body)
        self.assert_same_unknown_response(post_external, post_unknown)

    def test_gated_empty_token_is_403_for_both_routes(self):
        os.environ["CATY_EXTERNAL_SESSIONS"] = "1"

        status, _, body = self.request("GET", "/external/sessions")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body), {"ok": False, "error": "writes disabled: no token configured"})

        status, _, body = self.json_request("POST", "/external/takeover", {"native_id": "native-a"})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body), {"ok": False, "error": "writes disabled: no token configured"})

    def test_both_tokens_accepted_for_both_routes(self):
        self.enable()
        cg.CATY_ADMIN_TOKEN = "admin-token"

        status, _, body = self.request("GET", "/external/sessions", headers=self.auth_headers())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["backend"], "claude")

        status, _, body = self.json_request(
            "POST", "/external/takeover", {"native_id": "native-a"}, headers=self.auth_headers())
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

        admin = {"Authorization": "Bearer admin-token"}
        status, _, body = self.request("GET", "/external/sessions", headers=admin)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["backend"], "claude")

        status, _, body = self.json_request("POST", "/external/takeover", {"native_id": "native-a"}, headers=admin)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_get_lists_backend_sessions_preview_and_limit_clamping(self):
        self.enable()
        os.environ["CATY_EXTERNAL_PREVIEW"] = "0"

        status, _, body = self.request("GET", "/external/sessions", headers=self.auth_headers())
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["backend"], "claude")
        self.assertEqual(payload["sessions"][0]["preview"], "")

        self.request("GET", "/external/sessions?limit=101", headers=self.auth_headers())
        self.request("GET", "/external/sessions?limit=0", headers=self.auth_headers())
        self.request("GET", "/external/sessions?limit=bogus", headers=self.auth_headers())
        self.assertEqual(self.fake_backend.list_limits, [30, 100, 1, 30])

    def test_takeover_seeds_history_links_and_title(self):
        self.enable()

        status, _, body = self.json_request(
            "POST", "/external/takeover", {"native_id": "native-a"}, headers=self.auth_headers())
        self.assertEqual(status, 200)
        payload = json.loads(body)
        sid = payload["session_id"]
        self.assertEqual(payload["title"], "External A")
        self.assertEqual(payload["seeded"], 2)
        self.assertFalse(payload["already_linked"])
        self.assertEqual(session_links.find_by_native("native-a"), sid)
        self.assertEqual(session_links.get(sid), {"backend": "claude", "native": "native-a"})

        turns = history_store.read_session(sid)
        self.assertEqual([turn["seq"] for turn in turns], [1, 2])
        self.assertEqual([turn["text"] for turn in turns], ["oldest", "newest"])
        self.assertEqual([turn["ts"] for turn in turns], ["2026-07-05T01:00:00Z", "2026-07-05T01:00:01Z"])
        sessions = history_store.list_sessions()["sessions"]
        self.assertEqual(sessions[0]["title"], "External A")

    def test_takeover_is_idempotent_and_does_not_duplicate_history(self):
        self.enable()

        first_status, _, first_body = self.json_request(
            "POST", "/external/takeover", {"native_id": "native-a"}, headers=self.auth_headers())
        self.assertEqual(first_status, 200)
        first = json.loads(first_body)
        sid = first["session_id"]
        self.assertEqual(len(history_store.read_session(sid)), 2)

        second_status, _, second_body = self.json_request(
            "POST", "/external/takeover", {"native_id": "native-a"}, headers=self.auth_headers())
        self.assertEqual(second_status, 200)
        second = json.loads(second_body)
        self.assertTrue(second["already_linked"])
        self.assertEqual(second["seeded"], 0)
        self.assertEqual(second["session_id"], sid)
        self.assertEqual(len(history_store.read_session(sid)), 2)
        self.assertEqual(self.fake_backend.read_calls, [("native-a", 50)])

    def test_unknown_native_id_is_rejected_without_link(self):
        self.enable()

        status, _, body = self.json_request(
            "POST", "/external/takeover", {"native_id": "missing"}, headers=self.auth_headers())
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body), {"ok": False, "error": "not found"})
        self.assertIsNone(session_links.find_by_native("missing"))
        self.assertEqual(self.fake_backend.read_calls, [])

    def test_request_seed_turns_cannot_raise_env_ceiling(self):
        self.enable()
        os.environ["CATY_EXTERNAL_SEED_TURNS"] = "1"

        status, _, body = self.json_request(
            "POST", "/external/takeover", {"native_id": "native-a", "seed_turns": 99}, headers=self.auth_headers())
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["seeded"], 1)
        self.assertEqual(self.fake_backend.read_calls, [("native-a", 1)])
        self.assertEqual(len(history_store.read_session(payload["session_id"])), 1)

    def test_takeover_listing_validation_uses_max_listing_window(self):
        self.enable()

        status, _, body = self.json_request(
            "POST", "/external/takeover", {"native_id": "native-a"}, headers=self.auth_headers())
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(self.fake_backend.list_limits, [100])

    def test_preview_disabled_masks_labels_and_takeover_title(self):
        self.enable()
        os.environ["CATY_EXTERNAL_PREVIEW"] = "0"

        status, _, body = self.request("GET", "/external/sessions", headers=self.auth_headers())
        self.assertEqual(status, 200)
        sessions = json.loads(body)["sessions"]
        self.assertEqual(sessions[0]["native_id"], "native-a")
        self.assertEqual(sessions[0]["label"], "native-a")
        self.assertEqual(sessions[0]["preview"], "")

        status, _, body = self.json_request(
            "POST", "/external/takeover", {"native_id": "native-a"}, headers=self.auth_headers())
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["title"], "native-a")
        sessions = history_store.list_sessions()["sessions"]
        self.assertEqual(sessions[0]["title"], "native-a")

    def test_infinity_seed_turns_returns_clean_400(self):
        self.enable()
        body = b'{"native_id": "native-a", "seed_turns": Infinity}'
        headers = self.auth_headers()
        headers["Content-Type"] = "application/json"

        status, _, response_body = self.request("POST", "/external/takeover", body=body, headers=headers)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(response_body), {"ok": False, "error": "invalid seed_turns"})
        self.assertIsNone(session_links.find_by_native("native-a"))
        self.assertEqual(self.fake_backend.read_calls, [])

    def test_backend_mismatch_existing_link_returns_409_without_modifying_link(self):
        self.enable()
        session_links.put("preexisting-sid", "openclaw", "native-a")

        status, _, body = self.json_request(
            "POST", "/external/takeover", {"native_id": "native-a"}, headers=self.auth_headers())
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body), {"ok": False, "error": "linked by another backend"})
        self.assertEqual(session_links.find_by_native("native-a"), "preexisting-sid")
        self.assertEqual(session_links.get("preexisting-sid"), {"backend": "openclaw", "native": "native-a"})
        self.assertEqual(history_store.list_sessions()["sessions"], [])
        self.assertEqual(self.fake_backend.read_calls, [])


if __name__ == "__main__":
    unittest.main()
