import base64
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.parse
import uuid
from io import BytesIO
from unittest import mock


from caty_gateway import caty_gateway as cg
from caty_gateway import caty_config


class NonClosingBytesIO(BytesIO):
    def close(self):
        pass


class MemorySocket:
    def __init__(self, request_bytes):
        self.input = BytesIO(request_bytes)
        self.output = NonClosingBytesIO()

    def makefile(self, mode, *args, **kwargs):
        if "r" in mode:
            return self.input
        return self.output

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


def multipart(fields):
    boundary = "----catytest" + uuid.uuid4().hex
    chunks = []
    for name, data in fields.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            f'Content-Disposition: form-data; name="{name}"; filename="{name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
            .encode()
        )
        chunks.append(data)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    body = b"".join(chunks)
    return body, f"multipart/form-data; boundary={boundary}"


class OverlayConfigPathTest(unittest.TestCase):
    def setUp(self):
        self.old_env = {k: os.environ.get(k) for k in ("CATY_CONFIG_DIR", "CATY_ID")}

    def tearDown(self):
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_default_config_dir_uses_member_id_outside_cwd(self):
        os.environ.pop("CATY_CONFIG_DIR", None)
        os.environ["CATY_ID"] = "xyz"

        path = caty_config.OverlayConfig({}).path()
        expected_dir = os.path.expanduser("~/.config/caty-gateway/xyz-config")
        cwd = os.getcwd()

        self.assertEqual(path, os.path.join(expected_dir, "member_config.json"))
        self.assertNotEqual(expected_dir, ".")
        self.assertFalse(path == cwd or path.startswith(cwd + os.sep))

    def test_default_config_dir_uses_caty_when_member_id_unset(self):
        os.environ.pop("CATY_CONFIG_DIR", None)
        os.environ.pop("CATY_ID", None)

        path = caty_config.OverlayConfig({}).path()
        expected_dir = os.path.expanduser("~/.config/caty-gateway/caty-config")

        self.assertEqual(path, os.path.join(expected_dir, "member_config.json"))

    def test_explicit_config_dir_wins_verbatim(self):
        with tempfile.TemporaryDirectory(prefix="caty-config-path-") as tmp:
            os.environ["CATY_CONFIG_DIR"] = tmp
            os.environ["CATY_ID"] = "xyz"

            path = caty_config.OverlayConfig({}).path()

            self.assertEqual(path, os.path.join(tmp, "member_config.json"))

    def test_empty_config_dir_treated_as_unset(self):
        os.environ["CATY_CONFIG_DIR"] = ""
        os.environ["CATY_ID"] = "xyz"

        path = caty_config.OverlayConfig({}).path()
        expected_dir = os.path.expanduser("~/.config/caty-gateway/xyz-config")

        self.assertEqual(path, os.path.join(expected_dir, "member_config.json"))

    def test_default_config_dir_warns_exactly_once(self):
        os.environ.pop("CATY_CONFIG_DIR", None)
        os.environ["CATY_ID"] = "xyz"
        old_flag = caty_config._DEFAULT_CONFIG_DIR_WARNED
        caty_config._DEFAULT_CONFIG_DIR_WARNED = False
        try:
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                overlay = caty_config.OverlayConfig({})
                overlay.path()
                overlay.path()
            warns = [line for line in buf.getvalue().splitlines()
                     if "CATY_CONFIG_DIR is unset" in line]
            self.assertEqual(len(warns), 1)
        finally:
            caty_config._DEFAULT_CONFIG_DIR_WARNED = old_flag


class ConfigApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="caty-config-api-")
        self.config_dir = os.path.join(self.tmp, "config")
        self.asset_dir = os.path.join(self.tmp, "assets")
        self.filler_dir = os.path.join(self.tmp, "fillers")
        os.makedirs(self.config_dir)
        os.makedirs(self.asset_dir)
        os.makedirs(self.filler_dir)
        self.old_env = {
            k: os.environ.get(k)
            for k in ("CATY_CONFIG_DIR", "CATY_VOICE_HINT", "CATY_STREAM_TTS")
        }
        os.environ["CATY_CONFIG_DIR"] = self.config_dir
        os.environ.pop("CATY_VOICE_HINT", None)
        os.environ.pop("CATY_STREAM_TTS", None)
        self.old_globals = {
            "CATY_TOKEN": cg.CATY_TOKEN,
            "CATY_ADMIN_TOKEN": cg.CATY_ADMIN_TOKEN,
            "BACKEND_NAME": cg.BACKEND_NAME,
            "CATY_HERMES_API_KEY": cg.CATY_HERMES_API_KEY,
            "CATY_HERMES_URL": cg.CATY_HERMES_URL,
            "CATY_CLAUDE_BIN": cg.CATY_CLAUDE_BIN,
            "CATY_CLAUDE_CWD": cg.CATY_CLAUDE_CWD,
            "ASSET_DIR": cg.ASSET_DIR,
            "FILLER_DIR": cg.FILLER_DIR,
            "FILLER_DIR_STATUS": cg.FILLER_DIR_STATUS,
            "BACKEND": cg.BACKEND,
        }
        cg.CATY_TOKEN = ""
        cg.CATY_ADMIN_TOKEN = ""
        cg.BACKEND_NAME = "openclaw"
        cg.CATY_HERMES_API_KEY = "api-secret"
        cg.CATY_HERMES_URL = "http://secret-host:8642"
        cg.CATY_CLAUDE_BIN = "/secret/claude"
        cg.CATY_CLAUDE_CWD = "/secret/cwd"
        cg.ASSET_DIR = self.asset_dir
        cg.FILLER_DIR = self.filler_dir
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
            setattr(cg, key, value)
        cg.FILLERS[:] = []
        cg.FILLER_METADATA[:] = []
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

    def write_headers(self, token="write-token", **extra):
        cg.CATY_TOKEN = token
        cg.CATY_ADMIN_TOKEN = ""
        headers = {"X-Caty-Token": token}
        headers.update(extra)
        return headers

    def get_config(self, headers=None):
        status, _, body = self.request("GET", "/config", headers=headers)
        return status, json.loads(body)

    def test_get_config_shape_auth_and_secret_exclusion(self):
        cg.CATY_TOKEN = "secret-token"
        cg.CATY_ADMIN_TOKEN = "admin-hidden-value"
        cg.BACKEND_NAME = "hermes"

        status, _, body = self.request("GET", "/config")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"], "unauthorized")

        status, payload = self.get_config(headers={"X-Caty-Token": "secret-token"})
        self.assertEqual(status, 200)
        self.assertEqual(set(payload), {
            "config_version", "backend", "name", "accent_color", "voice_id",
            "voice_hint", "stream_tts", "stream_tts_effective",
            "stream_tts_supported", "stream_tts_reason", "assets_version",
            "fillers_version", "runtime_kind", "attachment_passthrough",
            "attachment_passthrough_effective",
            "attachment_passthrough_supported",
            "attachment_passthrough_reason",
        })
        self.assertEqual(payload["backend"], "hermes")
        encoded = json.dumps(payload, ensure_ascii=False)
        for secret in ("secret-token", "admin-hidden-value", "api-secret", "secret-host", "/secret/claude", "/secret/cwd", "http://"):
            self.assertNotIn(secret, encoded)

    def test_put_config_updates_whitelist_and_rejects_unknown_without_increment(self):
        status, payload = self.get_config()
        self.assertEqual(status, 200)
        self.assertEqual(payload["config_version"], 1)
        headers = self.write_headers(**{"If-Match": "1"})

        status, _, body = self.json_request(
            "PUT",
            "/config",
            {"name": "Member A", "accent_color": "#00B8D4", "voice_id": "voice-1", "voice_hint": "short\n"},
            headers=headers,
        )
        self.assertEqual(status, 200)
        updated = json.loads(body)
        self.assertEqual(updated["config_version"], 2)
        self.assertEqual(updated["name"], "Member A")
        self.assertEqual(updated["voice_id"], "voice-1")

        for bad_key in ("backend", "agent", "url", "hermes_url", "claude_bin", "CATY_HERMES_API_KEY", "runtime_kind"):
            status, _, body = self.json_request(
                "PUT",
                "/config",
                {"name": "Bad", bad_key: "bad"},
                headers=self.write_headers(**{"If-Match": "2"}),
            )
            self.assertEqual(status, 400, bad_key)
            self.assertIn(bad_key, json.loads(body)["invalid_keys"])

        status, payload = self.get_config(headers={"X-Caty-Token": "write-token"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["config_version"], 2)
        self.assertEqual(payload["name"], "Member A")

    def test_put_config_requires_current_if_match(self):
        status, _, body = self.json_request("PUT", "/config", {"name": "No Match"}, headers=self.write_headers())
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["config_version"], 1)

        status, _, body = self.json_request(
            "PUT", "/config", {"name": "Stale"}, headers=self.write_headers(**{"If-Match": "0"}))
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["config_version"], 1)

    def test_stream_tts_put_validation_and_version_contract(self):
        headers = self.write_headers()

        status, _, body = self.json_request(
            "PUT", "/config", {"stream_tts": "on"}, headers=headers
        )
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["config_version"], 1)

        status, _, body = self.json_request(
            "PUT",
            "/config",
            {"stream_tts": "on"},
            headers=self.write_headers(**{"If-Match": 'W/"1"'}),
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "invalid if-match header")

        status, _, body = self.json_request(
            "PUT",
            "/config",
            {"stream_tts": "on"},
            headers=self.write_headers(**{"If-Match": "0"}),
        )
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["config_version"], 1)

        status, _, body = self.json_request(
            "PUT",
            "/config",
            {"stream_tts": "true"},
            headers=self.write_headers(**{"If-Match": "1"}),
        )
        self.assertEqual(status, 400)
        self.assertEqual(
            json.loads(body),
            {
                "ok": False,
                "error": "invalid config value",
                "invalid_keys": ["stream_tts"],
            },
        )

        status, _, body = self.json_request(
            "PUT",
            "/config",
            {"stream_tts": "on"},
            headers=self.write_headers(**{"If-Match": "1"}),
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["stream_tts"], "on")
        self.assertEqual(payload["stream_tts_effective"], "on")
        self.assertEqual(payload["stream_tts_reason"], "runtime-override")

        status, _, body = self.json_request(
            "PUT",
            "/config",
            {"stream_tts": ""},
            headers=self.write_headers(**{"If-Match": "2"}),
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["stream_tts"], "")
        self.assertEqual(payload["stream_tts_effective"], "off")
        self.assertEqual(payload["stream_tts_reason"], "default-off")

    def test_attachment_passthrough_round_trip_and_capability_fields(self):
        backend = mock.Mock()
        backend.supports_stream.return_value = False
        backend.attachment_transports.return_value = frozenset(
            {"generate", "stream"}
        )
        backend.supported_attachment_mimes.return_value = frozenset(
            {"image/png", "image/jpeg", "application/pdf"}
        )
        with mock.patch.object(cg, "BACKEND", backend):
            status, payload = self.get_config()
            self.assertEqual(status, 200)
            self.assertEqual(payload["attachment_passthrough"], "")
            self.assertEqual(payload["attachment_passthrough_effective"], "on")
            self.assertIs(payload["attachment_passthrough_supported"], True)
            self.assertEqual(payload["attachment_passthrough_reason"], "default-on")

            status, _, body = self.json_request(
                "PUT", "/config", {"attachment_passthrough": "off"},
                headers=self.write_headers(**{"If-Match": "1"}),
            )
            self.assertEqual(status, 200)
            payload = json.loads(body)
            self.assertEqual(payload["attachment_passthrough"], "off")
            self.assertEqual(payload["attachment_passthrough_effective"], "off")
            self.assertEqual(payload["attachment_passthrough_reason"], "runtime-override")
            restarted = caty_config.OverlayConfig(cg._config_defaults)
            self.assertEqual(restarted.get()["attachment_passthrough"], "off")

            status, _, body = self.json_request(
                "PUT", "/config", {"attachment_passthrough": ""},
                headers=self.write_headers(**{"If-Match": "2"}),
            )
            self.assertEqual(status, 200)
            payload = json.loads(body)
            self.assertEqual(payload["attachment_passthrough"], "")
            self.assertEqual(payload["attachment_passthrough_effective"], "on")
            self.assertEqual(payload["attachment_passthrough_reason"], "default-on")

        status, _, body = self.json_request(
            "PUT", "/config", {"attachment_passthrough": "true"},
            headers=self.write_headers(**{"If-Match": "3"}),
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["invalid_keys"], ["attachment_passthrough"])

    def valid_assets(self, fill=b"x"):
        png = b"\x89PNG\r\n\x1a\n" + fill
        jpg = b"\xff\xd8\xff" + fill
        return {
            "icon": png,
            "idle": jpg,
            "talk1": png,
            "talk2": jpg,
            "talk3": png,
            "blink": jpg,
            "talk_blink": png,
            "listen": jpg,
        }

    def post_assets(self, fields, headers=None):
        body, ctype = multipart(fields)
        hdrs = {"Content-Type": ctype}
        hdrs.update(headers or {})
        return self.request("POST", "/assets:batch", body=body, headers=hdrs)

    def test_assets_batch_validates_and_bumps_once(self):
        with open(os.path.join(self.asset_dir, "icon.png"), "wb") as f:
            f.write(b"old-icon")
        headers = self.write_headers()

        status, _, body = self.post_assets(self.valid_assets(), headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["assets_version"], 2)
        with open(os.path.join(self.asset_dir, "icon.png"), "rb") as f:
            self.assertEqual(f.read()[:8], b"\x89PNG\r\n\x1a\n")

        bad = self.valid_assets()
        bad["listen"] = b"not-image"
        status, _, _ = self.post_assets(bad, headers=headers)
        self.assertEqual(status, 400)
        self.assertEqual(cg.resolved_config()["assets_version"], 2)
        with open(os.path.join(self.asset_dir, "icon.png"), "rb") as f:
            self.assertEqual(f.read()[:8], b"\x89PNG\r\n\x1a\n")

    def test_assets_batch_partial_upload_merges_with_existing_slots(self):
        headers = self.write_headers()
        existing = self.valid_assets(fill=b"old")
        for name, data in existing.items():
            with open(os.path.join(self.asset_dir, f"{name}.png"), "wb") as f:
                f.write(data)

        new_icon = b"\x89PNG\r\n\x1a\nnew-icon"
        status, _, body = self.post_assets({"icon": new_icon}, headers=headers)

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["assets_version"], 2)
        with open(os.path.join(self.asset_dir, "icon.png"), "rb") as f:
            self.assertEqual(f.read(), new_icon)
        for name, data in existing.items():
            if name == "icon":
                continue
            with open(os.path.join(self.asset_dir, f"{name}.png"), "rb") as f:
                self.assertEqual(f.read(), data, name)

    def test_assets_batch_rejects_unknown_field(self):
        headers = self.write_headers()

        status, _, body = self.post_assets({"unknown": b"\x89PNG\r\n\x1a\nx"}, headers=headers)

        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "unknown asset field")
        self.assertEqual(cg.resolved_config()["assets_version"], 1)

    def test_assets_batch_rejects_empty_set(self):
        headers = self.write_headers()

        status, _, body = self.post_assets({}, headers=headers)

        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "missing asset field")
        self.assertEqual(cg.resolved_config()["assets_version"], 1)

    def test_assets_batch_full_upload_still_replaces_all_slots(self):
        headers = self.write_headers()
        for name, data in self.valid_assets(fill=b"old").items():
            with open(os.path.join(self.asset_dir, f"{name}.png"), "wb") as f:
                f.write(data)

        replacement = self.valid_assets(fill=b"new")
        status, _, body = self.post_assets(replacement, headers=headers)

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True, "assets_version": 2})
        for name, data in replacement.items():
            with open(os.path.join(self.asset_dir, f"{name}.png"), "rb") as f:
                self.assertEqual(f.read(), data, name)

    def test_assets_batch_rejects_oversized_file_and_total(self):
        headers = self.write_headers()
        too_large = self.valid_assets()
        too_large["icon"] = b"\x89PNG\r\n\x1a\n" + (b"x" * (cg.ASSET_FILE_LIMIT + 1))
        status, _, _ = self.post_assets(too_large, headers=headers)
        self.assertEqual(status, 400)
        self.assertEqual(cg.resolved_config()["assets_version"], 1)

        body, ctype = multipart(self.valid_assets(fill=b"x" * (1600 * 1024)))
        status, _, _ = self.request("POST", "/assets:batch", body=body, headers={"Content-Type": ctype, **headers})
        self.assertEqual(status, 413)
        self.assertEqual(cg.resolved_config()["assets_version"], 1)

    def put_fillers(self, items, headers=None):
        return self.json_request("PUT", "/fillers", items, headers=headers)

    def add_fillers(self, items, headers=None):
        return self.json_request("POST", "/fillers:add", items, headers=headers)

    def generate_filler(self, payload, headers=None):
        return self.json_request("POST", "/fillers:generate", payload, headers=headers)

    def filler_text(self, name, text, headers=None):
        return self.json_request("POST", "/fillers:text", {"name": name, "text": text}, headers=headers)

    def filler_texts_path(self):
        return os.path.abspath(self.filler_dir) + "-texts.json"

    def read_filler_texts(self):
        with open(self.filler_texts_path(), encoding="utf-8") as f:
            return json.load(f)

    def filler_item(self, name, data, text=None):
        item = {"name": name, "data_base64": base64.b64encode(data).decode("ascii")}
        if text is not None:
            item["text"] = text
        return item

    def stub_tts(self, data=b"ID3generated"):
        old_tts = cg.tts
        paths = []

        def fake_tts(text):
            path = os.path.join(self.tmp, f"tts-{uuid.uuid4().hex}.mp3")
            with open(path, "wb") as f:
                f.write(data)
            paths.append(path)
            return path

        cg.tts = fake_tts
        self.addCleanup(lambda: setattr(cg, "tts", old_tts))
        return paths

    def test_fillers_put_replaces_not_appends(self):
        mp3_a = b"\xff\xfbfiller A"  # frame-sync 形式の mp3 magic
        mp3_b = b"ID3filler B"       # ID3 タグ形式
        auth_headers = self.write_headers()
        status, _, body = self.put_fillers([self.filler_item("a.mp3", mp3_a)], headers=auth_headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["fillers_version"], 2)
        status, response_headers, body = self.request("GET", "/filler", headers=auth_headers)
        self.assertEqual(status, 200)
        self.assertEqual(response_headers["content-type"], "audio/mpeg")
        self.assertEqual(body, mp3_a)

        status, _, body = self.put_fillers([self.filler_item("b.mp3", mp3_b)], headers=auth_headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["fillers_version"], 3)
        self.assertEqual(sorted(os.listdir(self.filler_dir)), ["b.mp3"])
        status, _, body = self.request("GET", "/filler", headers=auth_headers)
        self.assertEqual(status, 200)
        self.assertEqual(body, mp3_b)

    def test_fillers_get_lists_metadata_sorted_by_name(self):
        headers = self.write_headers()
        mp3_b = b"ID3filler B"
        mp3_a = b"\xff\xfbfiller A"
        status, _, _ = self.put_fillers([
            self.filler_item("zeta.mp3", mp3_b),
            self.filler_item("member-a.mp3", mp3_a),
        ], headers=headers)
        self.assertEqual(status, 200)

        status, _, body = self.request("GET", "/fillers", headers=headers)

        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["ok"], True)
        self.assertEqual(payload["fillers_version"], 2)
        self.assertEqual([item["name"] for item in payload["fillers"]], ["member-a.mp3", "zeta.mp3"])
        self.assertEqual([item["size"] for item in payload["fillers"]], [len(mp3_a), len(mp3_b)])
        self.assertTrue(all(isinstance(item["duration_sec"], (int, float)) for item in payload["fillers"]))
        self.assertTrue(all("text" in item for item in payload["fillers"]))

    def test_fillers_get_response_shape_is_additive(self):
        # #1077: filler_dir_status は既存フィールドを一切変えずに追加された
        # フィールドであることを固定する（iOS クライアント契約の後方互換性）。
        headers = self.write_headers()
        status, _, body = self.request("GET", "/fillers", headers=headers)
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(
            set(payload),
            {"ok", "fillers", "fillers_version", "filler_dir_status"},
        )
        self.assertEqual(payload["ok"], True)
        self.assertIsInstance(payload["fillers"], list)
        self.assertIsInstance(payload["fillers_version"], int)
        self.assertEqual(payload["filler_dir_status"], "ok")

    def test_fillers_get_recreates_missing_directory_and_reports_status(self):
        # #1077 候補①②: dir が無くても自動作成し、GET のたびに縮退状態を可視化する。
        shutil.rmtree(self.filler_dir)
        self.assertFalse(os.path.isdir(self.filler_dir))
        headers = self.write_headers()

        status, _, body = self.request("GET", "/fillers", headers=headers)

        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["filler_dir_status"], "ok")
        self.assertEqual(payload["fillers"], [])
        self.assertTrue(os.path.isdir(self.filler_dir))

    def test_fillers_get_reports_unavailable_when_directory_uncreatable(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root bypasses directory permission checks")
        shutil.rmtree(self.filler_dir)
        # self.tmp 自体は tearDown の rmtree が必要とするので触らない。
        # 内側に read-only な親を用意し、その配下でのみ mkdir を失敗させる。
        readonly_root = os.path.join(self.tmp, "readonly-fillers-root")
        os.makedirs(readonly_root)
        os.chmod(readonly_root, 0o500)
        cg.FILLER_DIR = os.path.join(readonly_root, "fillers")
        headers = self.write_headers()

        status, _, body = self.request("GET", "/fillers", headers=headers)

        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["filler_dir_status"], "unavailable")
        self.assertEqual(payload["fillers"], [])

    def test_filler_file_get_serves_single_mp3_and_404s_unknown(self):
        headers = self.write_headers()
        mp3 = b"ID3single"
        status, _, _ = self.put_fillers([self.filler_item("single.mp3", mp3)], headers=headers)
        self.assertEqual(status, 200)

        status, response_headers, body = self.request("GET", "/fillers/single.mp3", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(response_headers["content-type"], "audio/mpeg")
        self.assertEqual(body, mp3)

        status, _, body = self.request("GET", "/fillers/missing.mp3", headers=headers)
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"], "not found")

    def test_runtime_fillers_do_not_depend_on_packaged_audio(self):
        self.assertFalse(os.path.isdir(cg.BUNDLED_FILLER_DIR))
        self.assertNotEqual(
            os.path.realpath(cg._resolve_filler_dir()),
            os.path.realpath(cg.BUNDLED_FILLER_DIR),
        )

    def test_fillers_add_merges_overwrites_and_bumps_once(self):
        headers = self.write_headers()
        old_a = b"ID3old A"
        new_a = b"\xff\xfbnew A"
        mp3_b = b"ID3new B"
        status, _, body = self.put_fillers([self.filler_item("a.mp3", old_a)], headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["fillers_version"], 2)

        status, _, body = self.add_fillers([
            self.filler_item("a.mp3", b"ID3middle A"),
            self.filler_item("b.mp3", mp3_b),
            self.filler_item("a.mp3", new_a),
        ], headers=headers)

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["fillers_version"], 3)
        self.assertEqual(cg.resolved_config()["fillers_version"], 3)
        self.assertEqual(sorted(os.listdir(self.filler_dir)), ["a.mp3", "b.mp3"])
        with open(os.path.join(self.filler_dir, "a.mp3"), "rb") as f:
            self.assertEqual(f.read(), new_a)
        with open(os.path.join(self.filler_dir, "b.mp3"), "rb") as f:
            self.assertEqual(f.read(), mp3_b)

    def test_fillers_generate_success_attaches_text_and_bumps_once(self):
        tts_paths = self.stub_tts(b"ID3generated")
        headers = self.write_headers()

        status, _, body = self.generate_filler({"text": "  今、確認するね🙂\n"}, headers=headers)

        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["ok"], True)
        self.assertEqual(payload["fillers_version"], 2)
        self.assertEqual(payload["text"], "今、確認するね🙂")
        self.assertRegex(payload["name"], r"^gen-[0-9a-f]{8}\.mp3$")
        self.assertIsInstance(payload["duration_sec"], (int, float))
        self.assertEqual(cg.resolved_config()["fillers_version"], 2)
        with open(os.path.join(self.filler_dir, payload["name"]), "rb") as f:
            self.assertEqual(f.read(), b"ID3generated")
        self.assertEqual(self.read_filler_texts(), {payload["name"]: "今、確認するね🙂"})
        self.assertTrue(all(not os.path.exists(path) for path in tts_paths))

    def test_disabled_filler_storage_rejects_all_write_routes(self):
        cg.FILLER_DIR = ""
        headers = self.write_headers()
        calls = (
            ("PUT", "/fillers", []),
            ("POST", "/fillers:add", []),
            ("POST", "/fillers:generate", {"text": "待ってね"}),
            ("POST", "/fillers:text", {"name": "a.mp3", "text": "text"}),
        )
        with mock.patch.object(cg, "tts") as mocked_tts:
            for method, path, payload in calls:
                with self.subTest(path=path):
                    status, _, body = self.json_request(
                        method,
                        path,
                        payload,
                        headers=headers,
                    )
                    self.assertEqual(status, 409)
                    self.assertEqual(
                        json.loads(body)["error"],
                        "filler directory disabled",
                    )
            status, _, body = self.request(
                "DELETE",
                "/fillers/a.mp3",
                headers=headers,
            )
            self.assertEqual(status, 409)
            self.assertEqual(
                json.loads(body)["error"],
                "filler directory disabled",
            )
            mocked_tts.assert_not_called()

    def test_fillers_generate_auto_names_are_distinct(self):
        self.stub_tts(b"ID3generated")
        headers = self.write_headers()

        status, _, first_body = self.generate_filler({"text": "first"}, headers=headers)
        status2, _, second_body = self.generate_filler({"text": "second"}, headers=headers)

        self.assertEqual(status, 200)
        self.assertEqual(status2, 200)
        first = json.loads(first_body)["name"]
        second = json.loads(second_body)["name"]
        self.assertNotEqual(first, second)
        self.assertRegex(first, r"^gen-[0-9a-f]{8}\.mp3$")
        self.assertRegex(second, r"^gen-[0-9a-f]{8}\.mp3$")
        self.assertEqual(sorted(os.listdir(self.filler_dir)), sorted([first, second]))

    def test_fillers_generate_explicit_name_overwrites_existing(self):
        self.stub_tts(b"ID3new")
        headers = self.write_headers()
        status, _, _ = self.add_fillers([self.filler_item("named.mp3", b"ID3old", text="old")], headers=headers)
        self.assertEqual(status, 200)

        status, _, body = self.generate_filler({"name": "named.mp3", "text": "new"}, headers=headers)

        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["name"], "named.mp3")
        self.assertEqual(payload["fillers_version"], 3)
        with open(os.path.join(self.filler_dir, "named.mp3"), "rb") as f:
            self.assertEqual(f.read(), b"ID3new")
        self.assertEqual(self.read_filler_texts(), {"named.mp3": "new"})

    def test_fillers_generate_rejects_system_name_without_bump(self):
        self.stub_tts(b"ID3generated")
        headers = self.write_headers()

        status, _, body = self.generate_filler({"name": "silence1s.mp3", "text": "x"}, headers=headers)

        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"], "system filler")
        self.assertEqual(cg.resolved_config()["fillers_version"], 1)
        self.assertEqual(os.listdir(self.filler_dir), [])

    def test_fillers_generate_rejects_empty_and_too_long_text_without_bump(self):
        self.stub_tts(b"ID3generated")
        headers = self.write_headers()

        status, _, body = self.generate_filler({"text": " \n "}, headers=headers)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "empty filler text")
        self.assertEqual(cg.resolved_config()["fillers_version"], 1)
        self.assertEqual(os.listdir(self.filler_dir), [])

        too_long = "あ" * (cg.FILLER_TEXT_MAX + 1)
        status, _, body = self.generate_filler({"name": "too-long.mp3", "text": too_long}, headers=headers)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "text too long")
        self.assertEqual(cg.resolved_config()["fillers_version"], 1)
        self.assertEqual(os.listdir(self.filler_dir), [])

    def test_fillers_generate_tts_failure_does_not_bump_or_leave_filler(self):
        old_tts = cg.tts

        def failing_tts(text):
            raise RuntimeError("boom")

        cg.tts = failing_tts
        self.addCleanup(lambda: setattr(cg, "tts", old_tts))
        headers = self.write_headers()

        status, _, body = self.generate_filler({"name": "bad.mp3", "text": "x"}, headers=headers)

        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body)["error"], "tts failed")
        self.assertEqual(cg.resolved_config()["fillers_version"], 1)
        self.assertEqual(os.listdir(self.filler_dir), [])

    def test_fillers_generate_rejects_non_mp3_tts_output_and_cleans_temp(self):
        tts_paths = self.stub_tts(b"not an mp3")
        headers = self.write_headers()

        status, _, body = self.generate_filler({"name": "bad.mp3", "text": "x"}, headers=headers)

        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body)["error"], "tts failed")
        self.assertEqual(cg.resolved_config()["fillers_version"], 1)
        self.assertEqual(os.listdir(self.filler_dir), [])
        self.assertTrue(all(not os.path.exists(path) for path in tts_paths))

    def test_fillers_generate_rejects_oversized_tts_output_and_cleans_temp(self):
        tts_paths = self.stub_tts(b"ID3" + b"x" * (5 * 1024 * 1024 + 1))
        headers = self.write_headers()

        status, _, body = self.generate_filler({"name": "big.mp3", "text": "x"}, headers=headers)

        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body)["error"], "tts failed")
        self.assertEqual(cg.resolved_config()["fillers_version"], 1)
        self.assertEqual(os.listdir(self.filler_dir), [])
        self.assertTrue(all(not os.path.exists(path) for path in tts_paths))

    def test_fillers_generate_staging_failure_returns_500_without_bump(self):
        self.stub_tts()
        headers = self.write_headers()
        old_swap = cg.Handler._swap_directory

        def failing_swap(handler_self, staging, final_dir):
            raise OSError("disk full")

        cg.Handler._swap_directory = failing_swap
        self.addCleanup(lambda: setattr(cg.Handler, "_swap_directory", old_swap))

        status, _, body = self.generate_filler({"name": "gen.mp3", "text": "x"}, headers=headers)

        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body)["error"], "filler write failed")
        self.assertEqual(cg.resolved_config()["fillers_version"], 1)
        self.assertEqual(os.listdir(self.filler_dir), [])
        # staging の取り残しが無いこと
        parent = os.path.dirname(os.path.abspath(self.filler_dir))
        self.assertEqual([d for d in os.listdir(parent) if d.startswith(".staging-fillers-")], [])

    def test_fillers_generate_requires_write_auth(self):
        cg.CATY_TOKEN = "read-token"
        cg.CATY_ADMIN_TOKEN = "admin-token"
        self.stub_tts(b"ID3generated")

        status, _, _ = self.generate_filler({"text": "x"}, headers={"Authorization": "Bearer wrong-token"})
        self.assertEqual(status, 401)
        status, _, body = self.generate_filler({"text": "x"}, headers={"Authorization": "Bearer read-token"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["fillers_version"], 2)
        status, _, body = self.generate_filler({"text": "x"}, headers={"Authorization": "Bearer admin-token"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["fillers_version"], 3)

    def test_fillers_add_with_text_attaches_trimmed_transcript(self):
        headers = self.write_headers()
        status, _, body = self.add_fillers([
            self.filler_item("a.mp3", b"ID3A", text="  今、確認するね  "),
        ], headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["fillers_version"], 2)

        status, _, body = self.request("GET", "/fillers", headers=headers)
        self.assertEqual(status, 200)
        item = json.loads(body)["fillers"][0]
        self.assertEqual(item["text"], "今、確認するね")
        self.assertEqual(self.read_filler_texts(), {"a.mp3": "今、確認するね"})

    def test_fillers_add_without_text_returns_null(self):
        headers = self.write_headers()
        status, _, _ = self.add_fillers([self.filler_item("a.mp3", b"ID3A")], headers=headers)
        self.assertEqual(status, 200)

        status, _, body = self.request("GET", "/fillers", headers=headers)
        self.assertEqual(status, 200)
        self.assertIsNone(json.loads(body)["fillers"][0]["text"])

    def test_filler_text_set_edit_and_clear(self):
        headers = self.write_headers()
        status, _, _ = self.add_fillers([self.filler_item("a.mp3", b"ID3A")], headers=headers)
        self.assertEqual(status, 200)

        status, _, body = self.filler_text("a.mp3", "  first  ", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["text"], "first")
        self.assertEqual(json.loads(body)["fillers_version"], 3)

        status, _, body = self.filler_text("a.mp3", "second", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["text"], "second")
        self.assertEqual(json.loads(body)["fillers_version"], 4)

        status, _, body = self.filler_text("a.mp3", "   ", headers=headers)
        self.assertEqual(status, 200)
        self.assertIsNone(json.loads(body)["text"])
        self.assertEqual(json.loads(body)["fillers_version"], 5)

        status, _, body = self.request("GET", "/fillers", headers=headers)
        self.assertEqual(status, 200)
        self.assertIsNone(json.loads(body)["fillers"][0]["text"])
        self.assertEqual(self.read_filler_texts(), {})

    def test_filler_text_404s_unknown_name(self):
        headers = self.write_headers()
        status, _, body = self.filler_text("missing.mp3", "text", headers=headers)
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"], "not found")
        self.assertEqual(cg.resolved_config()["fillers_version"], 1)

    def test_filler_text_requires_write_auth(self):
        cg.CATY_TOKEN = "read-token"
        cg.CATY_ADMIN_TOKEN = "admin-token"
        status, _, _ = self.add_fillers([self.filler_item("a.mp3", b"ID3A")], headers={"Authorization": "Bearer admin-token"})
        self.assertEqual(status, 200)

        status, _, _ = self.filler_text("a.mp3", "text", headers={"Authorization": "Bearer wrong-token"})
        self.assertEqual(status, 401)
        status, _, body = self.filler_text("a.mp3", "text", headers={"Authorization": "Bearer read-token"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["text"], "text")
        status, _, body = self.filler_text("a.mp3", "admin text", headers={"Authorization": "Bearer admin-token"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["text"], "admin text")

    def test_filler_delete_removes_text_entry(self):
        headers = self.write_headers()
        status, _, _ = self.add_fillers([self.filler_item("a.mp3", b"ID3A", text="消える")], headers=headers)
        self.assertEqual(status, 200)

        status, _, body = self.request("DELETE", "/fillers/a.mp3", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["fillers_version"], 3)
        self.assertEqual(self.read_filler_texts(), {})

    def test_fillers_put_resets_text_map_to_payload_texts(self):
        headers = self.write_headers()
        status, _, _ = self.add_fillers([
            self.filler_item("a.mp3", b"ID3A", text="A"),
            self.filler_item("b.mp3", b"ID3B", text="old B"),
        ], headers=headers)
        self.assertEqual(status, 200)

        status, _, _ = self.put_fillers([
            self.filler_item("b.mp3", b"ID3new B", text="new B"),
            self.filler_item("c.mp3", b"ID3C"),
        ], headers=headers)
        self.assertEqual(status, 200)

        status, _, body = self.request("GET", "/fillers", headers=headers)
        self.assertEqual(status, 200)
        by_name = {item["name"]: item["text"] for item in json.loads(body)["fillers"]}
        self.assertEqual(by_name, {"b.mp3": "new B", "c.mp3": None})
        self.assertEqual(self.read_filler_texts(), {"b.mp3": "new B"})

        status, _, _ = self.put_fillers([self.filler_item("d.mp3", b"ID3D")], headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(self.read_filler_texts(), {})

    def test_filler_text_sidecar_survives_add_and_delete_of_other_files(self):
        headers = self.write_headers()
        status, _, _ = self.add_fillers([self.filler_item("a.mp3", b"ID3A", text="keep")], headers=headers)
        self.assertEqual(status, 200)

        status, _, _ = self.add_fillers([self.filler_item("b.mp3", b"ID3B")], headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(self.read_filler_texts(), {"a.mp3": "keep"})

        status, _, _ = self.request("DELETE", "/fillers/b.mp3", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(self.read_filler_texts(), {"a.mp3": "keep"})

    def test_filler_text_max_length_enforced_for_add_and_text_route(self):
        headers = self.write_headers()
        too_long = "あ" * (cg.FILLER_TEXT_MAX + 1)
        status, _, body = self.add_fillers([self.filler_item("a.mp3", b"ID3A", text=too_long)], headers=headers)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "text too long")

        status, _, _ = self.add_fillers([self.filler_item("a.mp3", b"ID3A")], headers=headers)
        self.assertEqual(status, 200)
        status, _, body = self.filler_text("a.mp3", too_long, headers=headers)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "text too long")

    def test_filler_unicode_and_emoji_text_round_trips(self):
        headers = self.write_headers()
        text = "了解、すぐ見るね🙂"
        status, _, _ = self.add_fillers([self.filler_item("相槌.mp3", b"ID3A", text=text)], headers=headers)
        self.assertEqual(status, 200)

        status, _, body = self.request("GET", "/fillers", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["fillers"][0]["text"], text)
        self.assertEqual(self.read_filler_texts(), {"相槌.mp3": text})

    def test_corrupt_filler_text_sidecar_is_treated_as_empty(self):
        headers = self.write_headers()
        with open(os.path.join(self.filler_dir, "a.mp3"), "wb") as f:
            f.write(b"ID3A")
        with open(self.filler_texts_path(), "w", encoding="utf-8") as f:
            f.write("{not json")

        cg.load_fillers()

        status, _, body = self.request("GET", "/fillers", headers=headers)
        self.assertEqual(status, 200)
        self.assertIsNone(json.loads(body)["fillers"][0]["text"])
        # 破損 sidecar は空 JSON へ自己修復される（毎ロード警告の永続化防止）
        with open(self.filler_texts_path(), "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f), {})

    def test_fillers_add_empty_list_is_noop_without_version_bump(self):
        headers = self.write_headers()
        status, _, body = self.add_fillers([], headers=headers)

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["fillers_version"], 1)
        self.assertEqual(cg.resolved_config()["fillers_version"], 1)
        self.assertEqual(os.listdir(self.filler_dir), [])

    def test_filler_delete_removes_one_bumps_once_and_allows_empty_set(self):
        headers = self.write_headers()
        status, _, _ = self.put_fillers([
            self.filler_item("a.mp3", b"ID3A"),
            self.filler_item("b.mp3", b"ID3B"),
        ], headers=headers)
        self.assertEqual(status, 200)

        status, _, body = self.request("DELETE", "/fillers/a.mp3", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["fillers_version"], 3)
        self.assertEqual(sorted(os.listdir(self.filler_dir)), ["b.mp3"])

        status, _, body = self.request("DELETE", "/fillers/a.mp3", headers=headers)
        self.assertEqual(status, 404)
        self.assertEqual(cg.resolved_config()["fillers_version"], 3)

        status, _, body = self.request("DELETE", "/fillers/b.mp3", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["fillers_version"], 4)
        self.assertEqual(os.listdir(self.filler_dir), [])
        status, _, body = self.request("GET", "/fillers", headers=headers)
        self.assertEqual(json.loads(body)["fillers"], [])

    def test_fillers_auth_requirements(self):
        cg.CATY_TOKEN = "read-token"
        cg.CATY_ADMIN_TOKEN = "admin-token"

        status, _, _ = self.request("GET", "/fillers")
        self.assertEqual(status, 401)
        status, _, _ = self.request("GET", "/fillers/missing.mp3", headers={"Authorization": "Bearer read-token"})
        self.assertEqual(status, 404)
        status, _, _ = self.add_fillers([self.filler_item("a.mp3", b"ID3A")], headers={"Authorization": "Bearer wrong-token"})
        self.assertEqual(status, 401)
        status, _, _ = self.add_fillers([self.filler_item("a.mp3", b"ID3A")], headers={"Authorization": "Bearer read-token"})
        self.assertEqual(status, 200)
        status, _, _ = self.add_fillers([self.filler_item("b.mp3", b"ID3B")], headers={"Authorization": "Bearer admin-token"})
        self.assertEqual(status, 200)
        status, _, _ = self.request("DELETE", "/fillers/a.mp3", headers={"Authorization": "Bearer wrong-token"})
        self.assertEqual(status, 401)
        status, _, _ = self.request("DELETE", "/fillers/a.mp3", headers={"Authorization": "Bearer read-token"})
        self.assertEqual(status, 200)
        status, _, _ = self.request("DELETE", "/fillers/b.mp3", headers={"Authorization": "Bearer admin-token"})
        self.assertEqual(status, 200)

    def test_fillers_reject_invalid_and_encoded_path_names(self):
        headers = self.write_headers()

        for path in ("/fillers/../etc/passwd", "/fillers/foo/bar.mp3", "/fillers/foo%2Fbar.mp3"):
            status, _, _ = self.request("GET", path, headers=headers)
            self.assertEqual(status, 404, path)
            status, _, _ = self.request("DELETE", path, headers=headers)
            self.assertEqual(status, 404, path)

        for bad_name in ("../etc/passwd", "foo/bar.mp3"):
            status, _, body = self.add_fillers([self.filler_item(bad_name, b"ID3bad")], headers=headers)
            self.assertEqual(status, 400, bad_name)
            self.assertEqual(json.loads(body)["error"], "invalid filler name")

    def test_fillers_url_encoded_unicode_name_round_trips(self):
        headers = self.write_headers()
        name = "相槌.mp3"
        data = b"ID3unicode"
        status, _, _ = self.add_fillers([self.filler_item(name, data)], headers=headers)
        self.assertEqual(status, 200)

        encoded = urllib.parse.quote(name)
        status, response_headers, body = self.request("GET", f"/fillers/{encoded}", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(response_headers["content-type"], "audio/mpeg")
        self.assertEqual(body, data)

    def test_fillers_put_rejects_non_mp3_and_empty_name(self):
        headers = self.write_headers()
        status, _, body = self.put_fillers([self.filler_item("a.mp3", b"not an mp3")], headers=headers)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "invalid mp3")
        status, _, body = self.put_fillers([self.filler_item(" ", b"\xff\xfbx")], headers=headers)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "invalid filler name")

    def test_silence_fillers_are_system_files(self):
        headers = self.write_headers()
        with open(os.path.join(self.filler_dir, "silence1s.mp3"), "wb") as f:
            f.write(b"\xff\xfbSILENCE")
        with open(os.path.join(self.filler_dir, "a.mp3"), "wb") as f:
            f.write(b"ID3A")
        cg.load_fillers()

        # 一覧に silence* は出ない
        status, _, body = self.request("GET", "/fillers", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual([f["name"] for f in json.loads(body)["fillers"]], ["a.mp3"])

        # 削除・テキスト編集・add 上書きは 403
        status, _, body = self.request("DELETE", "/fillers/silence1s.mp3", headers=headers)
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"], "system filler")
        status, _, body = self.filler_text("silence1s.mp3", "x", headers=headers)
        self.assertEqual(status, 403)
        status, _, body = self.add_fillers([self.filler_item("silence1s.mp3", b"\xff\xfbY")], headers=headers)
        self.assertEqual(status, 403)

        # PUT 全置換でも silence* は温存される
        status, _, _ = self.put_fillers([self.filler_item("b.mp3", b"\xff\xfbB")], headers=headers)
        self.assertEqual(status, 200)
        self.assertTrue(os.path.exists(os.path.join(self.filler_dir, "silence1s.mp3")))
        self.assertFalse(os.path.exists(os.path.join(self.filler_dir, "a.mp3")))

    def test_put_config_malformed_if_match_is_400(self):
        status, _, body = self.json_request(
            "PUT", "/config", {"name": "X"}, headers=self.write_headers(**{"If-Match": 'W/"1"'}))
        self.assertEqual(status, 400)
        status, payload = self.get_config(headers={"X-Caty-Token": "write-token"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["config_version"], 1)

    def test_voice_hint_defaults_are_byte_identical_when_env_and_file_absent(self):
        original_backend = cg.BACKEND_NAME
        original_key = cg.CATY_HERMES_API_KEY
        try:
            cg.BACKEND_NAME = "openclaw"
            backend = cg._build_backend()
            # #782: openclaw は編集不可の機能案内（SCREEN_PUSH_HINT）が常時後置される。
            self.assertEqual(
                str(backend.voice_hint),
                cg.DEFAULT_VOICE_HINT + cg._screen_push_hint(),
            )

            cg.BACKEND_NAME = "hermes"
            cg.CATY_HERMES_API_KEY = "x"
            backend = cg._build_backend()
            self.assertEqual(str(backend.voice_hint), cg.THIN_MEMBER_VOICE_HINT)

            cg.BACKEND_NAME = "claude"
            backend = cg._build_backend()
            self.assertEqual(str(backend.voice_hint), cg.THIN_MEMBER_VOICE_HINT)
        finally:
            cg.BACKEND_NAME = original_backend
            cg.CATY_HERMES_API_KEY = original_key

    def test_absent_member_config_returns_prechange_defaults(self):
        status, payload = self.get_config()
        self.assertEqual(status, 200)
        self.assertEqual(payload["config_version"], 1)
        self.assertEqual(payload["backend"], cg.BACKEND_NAME)
        self.assertEqual(payload["name"], cg.IDENTITY_NAME)
        self.assertEqual(payload["accent_color"], cg.IDENTITY_ACCENT_COLOR)
        self.assertEqual(payload["voice_id"], cg.TTS_VOICE)
        self.assertEqual(payload["voice_hint"], cg.DEFAULT_VOICE_HINT)
        self.assertEqual(payload["stream_tts"], "")
        self.assertEqual(payload["stream_tts_effective"], "off")
        self.assertIs(payload["stream_tts_supported"], True)
        self.assertEqual(payload["stream_tts_reason"], "default-off")
        self.assertEqual(payload["assets_version"], cg.IDENTITY_ASSETS_VERSION)
        self.assertEqual(payload["fillers_version"], 1)

    def test_env_only_config_get_preserves_legacy_stream_behavior(self):
        backend = mock.Mock()
        backend.supports_stream.return_value = True
        cg.BACKEND = backend

        for env_value, effective, reason in (
            (None, "off", "default-off"),
            ("1", "on", "legacy-env"),
        ):
            with self.subTest(env_value=env_value):
                if env_value is None:
                    os.environ.pop("CATY_STREAM_TTS", None)
                else:
                    os.environ["CATY_STREAM_TTS"] = env_value

                status, payload = self.get_config()

                self.assertEqual(status, 200)
                self.assertFalse(os.path.exists(cg.CONFIG.path()))
                self.assertEqual(payload["stream_tts"], "")
                self.assertEqual(payload["stream_tts_effective"], effective)
                self.assertIs(payload["stream_tts_supported"], True)
                self.assertEqual(payload["stream_tts_reason"], reason)


if __name__ == "__main__":
    unittest.main()
