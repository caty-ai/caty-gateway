import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from unittest import mock


from caty_gateway import caty_gateway as cg
from caty_gateway import vision_describer
from caty_gateway.avatar_engine import AvatarCredentialConflict, AvatarEngineBusy, AvatarEngineDisabled, AvatarJobStateError
from tests.test_config_api import MemoryServer, MemorySocket


PNG = b"\x89PNG\r\n\x1a\navatar"


def multipart(fields):
    boundary = "----catyavatar" + uuid.uuid4().hex
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


class FakeJob:
    def __init__(self, snapshot):
        self._snapshot = snapshot

    def snapshot(self):
        return self._snapshot


class FakeEngine:
    def __init__(self, snapshot=None):
        self.current_snapshot = snapshot or {"job_id": "job-1", "stage": "awaiting_base_approval", "paths": {}, "slots": {}, "error": None}
        self.started = None
        self.approved_base = None
        self.approve_set_called = False

    def snapshot(self):
        return self.current_snapshot

    def assert_pass_credentials(self, pass_clients):
        self.checked_pass_clients = pass_clients

    def start_stylize(self, photo, identity_description=None, pass_clients=None):
        self.started = (photo, identity_description)
        self.pass_clients = pass_clients
        return FakeJob(self.current_snapshot)

    def regenerate_base(self, pass_clients=None):
        self.pass_clients = pass_clients
        return FakeJob({"job_id": "job-1", "stage": "stylizing", "paths": {}, "slots": {}, "error": None})

    def regenerate_set(self, pass_clients=None):
        self.pass_clients = pass_clients
        return FakeJob({"job_id": "job-1", "stage": "generating", "paths": {}, "slots": {}, "error": None})

    def approve_base(self, identity_description, pass_clients=None):
        self.approved_base = identity_description
        self.pass_clients = pass_clients
        return FakeJob({"job_id": "job-1", "stage": "generating", "paths": {}, "slots": {}, "error": None})

    def approve_set(self):
        self.approve_set_called = True
        done = {"job_id": "job-1", "stage": "done", "paths": self.current_snapshot.get("paths", {}), "slots": {}, "error": None}
        self.current_snapshot = done
        return FakeJob(done)

    def cancel(self):
        stage = self.current_snapshot.get("stage")
        if stage not in {"awaiting_base_approval", "awaiting_set_approval", "failed", "done"}:
            raise AvatarJobStateError(f"cannot cancel while stage is {stage}")
        self.current_snapshot = {"stage": "idle", "paths": {}, "slots": {}}


class RaisingStartEngine(FakeEngine):
    def __init__(self, exc):
        super().__init__()
        self.exc = exc

    def start_stylize(self, photo, identity_description=None, pass_clients=None):
        raise self.exc


class WrongStageEngine(FakeEngine):
    def regenerate_base(self, pass_clients=None):
        raise AvatarJobStateError("wrong base")

    def regenerate_set(self, pass_clients=None):
        raise AvatarJobStateError("wrong set")

    def approve_base(self, identity_description, pass_clients=None):
        raise AvatarJobStateError("wrong approve base")

    def approve_set(self):
        raise AvatarJobStateError("wrong approve set")


class RaisingRegenerateEngine(FakeEngine):
    def __init__(self, stage, exc):
        super().__init__({"job_id": "job-1", "stage": stage, "paths": {}, "slots": {}, "error": None})
        self.exc = exc

    def regenerate_base(self, pass_clients=None):
        raise self.exc

    def regenerate_set(self, pass_clients=None):
        raise self.exc


class RaisingApproveBaseEngine(FakeEngine):
    def __init__(self, exc):
        super().__init__({"job_id": "job-1", "stage": "awaiting_base_approval", "paths": {}, "slots": {}, "error": None})
        self.exc = exc

    def approve_base(self, identity_description, pass_clients=None):
        raise self.exc


class RaisingApproveSetEngine(FakeEngine):
    def __init__(self, snapshot, exc):
        super().__init__(snapshot)
        self.exc = exc

    def approve_set(self):
        raise self.exc


class RaisingCancelEngine(FakeEngine):
    def __init__(self, exc):
        super().__init__()
        self.exc = exc

    def cancel(self):
        raise self.exc


class NoJobEngine(FakeEngine):
    def __init__(self):
        super().__init__({"stage": "idle", "paths": {}, "slots": {}})

    def cancel(self):
        raise AvatarJobStateError("no avatar job has been started")


class FakeDescriber:
    def __init__(self, description="vision hair, glasses"):
        self.description = description
        self.calls = []

    def describe(self, image_bytes):
        self.calls.append(image_bytes)
        return self.description


class RaisingDescriberFactory:
    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def __call__(self):
        self.calls += 1
        raise self.exc


class RecordingRegenerateEngine(FakeEngine):
    def __init__(self, stage):
        super().__init__({"job_id": "job-1", "stage": stage, "paths": {}, "slots": {}, "error": None})
        self.regenerate_base_called = False
        self.regenerate_set_called = False

    def regenerate_base(self, pass_clients=None):
        self.regenerate_base_called = True
        return super().regenerate_base(pass_clients)

    def regenerate_set(self, pass_clients=None):
        self.regenerate_set_called = True
        return super().regenerate_set(pass_clients)


class SecondApproveRejectsEngine(FakeEngine):
    def __init__(self, snapshot):
        super().__init__(snapshot)
        self.approve_set_calls = 0

    def approve_set(self):
        self.approve_set_calls += 1
        if self.approve_set_calls > 1:
            raise AvatarJobStateError("already approved")
        self.approve_set_called = True
        done = {"job_id": "job-1", "stage": "done", "paths": self.current_snapshot.get("paths", {}), "slots": {}, "error": None}
        return FakeJob(done)


class AvatarApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="caty-avatar-api-")
        self.config_dir = os.path.join(self.tmp, "config")
        self.asset_dir = os.path.join(self.tmp, "assets")
        os.makedirs(self.config_dir)
        os.makedirs(self.asset_dir)
        self.old_env = {
            k: os.environ.get(k)
            for k in ("CATY_CONFIG_DIR", "POYO_API_KEY", "RENOISE_API_KEY", "RENOISE_AUTH_TOKEN")
        }
        os.environ["CATY_CONFIG_DIR"] = self.config_dir
        os.environ["POYO_API_KEY"] = "test-poyo-key"
        os.environ["RENOISE_API_KEY"] = "test-renoise-key"
        os.environ.pop("RENOISE_AUTH_TOKEN", None)
        self.old_globals = {
            "CATY_TOKEN": cg.CATY_TOKEN,
            "CATY_ADMIN_TOKEN": cg.CATY_ADMIN_TOKEN,
            "ASSET_DIR": cg.ASSET_DIR,
            "_avatar_engine": cg._avatar_engine,
            "_avatar_engine_factory": cg._avatar_engine_factory,
            "_vision_describer": cg._vision_describer,
            "_vision_describer_factory": cg._vision_describer_factory,
        }
        cg.CATY_TOKEN = ""
        cg.CATY_ADMIN_TOKEN = ""
        cg.ASSET_DIR = self.asset_dir
        cg._avatar_engine = None
        cg._avatar_engine_factory = cg.AvatarEngine
        cg._vision_describer = None
        cg._vision_describer_factory = cg.VisionDescriber

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

    def write_headers(self):
        cg.CATY_TOKEN = "client-secret"
        cg.CATY_ADMIN_TOKEN = ""
        return {"X-Caty-Token": "client-secret"}

    def post_stylize(self, fields, headers=None):
        body, ctype = multipart(fields)
        hdrs = {"Content-Type": ctype}
        hdrs.update(headers or {})
        return self.request("POST", "/avatar:stylize", body=body, headers=hdrs)

    def test_get_avatar_job_requires_read_auth(self):
        cg.CATY_TOKEN = "client-secret"

        status, _, body = self.request("GET", "/avatar/job")

        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body), {"ok": False, "error": "unauthorized"})

    def test_get_avatar_job_files_requires_read_auth(self):
        cg.CATY_TOKEN = "client-secret"

        status, _, body = self.request("GET", "/avatar/job/files/idle")

        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body), {"ok": False, "error": "unauthorized"})

    def test_get_avatar_job_returns_snapshot_or_none(self):
        snapshot = {
            "job_id": "job-1",
            "stage": "awaiting_set_approval",
            "paths": {"contact_sheet": "/tmp/contact-sheet.png"},
            "slots": {"idle": {"status": "done"}},
            "error": None,
        }
        cg._avatar_engine = FakeEngine(snapshot)

        status, _, body = self.request("GET", "/avatar/job", headers=self.write_headers())

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True, "job": snapshot})

        cg._avatar_engine = None
        status, _, body = self.request("GET", "/avatar/job", headers=self.write_headers())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True, "job": None})

    def test_cancel_clears_job_and_get_returns_none_for_allowed_stages(self):
        for stage in ("awaiting_base_approval", "awaiting_set_approval", "failed", "done"):
            with self.subTest(stage=stage):
                cg._avatar_engine = FakeEngine(
                    {"job_id": "job-1", "stage": stage, "paths": {}, "slots": {}, "error": "boom" if stage == "failed" else None}
                )

                status, _, body = self.request("POST", "/avatar/job:cancel", headers=self.write_headers())
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body), {"ok": True, "job": None})

                status, _, body = self.request("GET", "/avatar/job", headers=self.write_headers())
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body), {"ok": True, "job": None})

    def test_cancel_returns_409_for_running_or_missing_job(self):
        cases = {
            "stylizing": FakeEngine({"job_id": "job-1", "stage": "stylizing", "paths": {}, "slots": {}, "error": None}),
            "generating": FakeEngine({"job_id": "job-1", "stage": "generating", "paths": {}, "slots": {}, "error": None}),
            "no job": NoJobEngine(),
        }
        for name, engine in cases.items():
            with self.subTest(name=name):
                cg._avatar_engine = engine

                status, _, body = self.request("POST", "/avatar/job:cancel", headers=self.write_headers())

                self.assertEqual(status, 409)
                payload = json.loads(body)
                self.assertEqual(payload["error"], "wrong_stage")
                self.assertIn("detail", payload)

    def test_mutation_endpoints_require_write_auth(self):
        cg.CATY_TOKEN = "client-secret"
        body, ctype = multipart({"photo": PNG})
        cases = [
            ("POST", "/avatar:stylize", body, {"Content-Type": ctype}),
            ("POST", "/avatar/job:regenerate", b"", {}),
            ("POST", "/avatar/job:cancel", b"", {}),
            ("POST", "/avatar/job:approve-base", b"", {}),
            ("POST", "/avatar/job:approve-set", b"", {}),
        ]

        for method, path, payload, headers in cases:
            status, _, body = self.request(method, path, body=payload, headers=headers)
            self.assertEqual(status, 401, path)
            self.assertEqual(json.loads(body), {"ok": False, "error": "unauthorized"})

    def test_stylize_returns_503_when_engine_disabled(self):
        def disabled_factory():
            raise AvatarEngineDisabled("missing keys")

        cg._avatar_engine_factory = disabled_factory
        status, _, body = self.post_stylize({"photo": PNG}, headers=self.write_headers())

        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body), {"ok": False, "error": "avatar generation disabled", "detail": "missing keys"})

        cg._avatar_engine = RaisingStartEngine(AvatarEngineDisabled("style ref missing"))
        status, _, body = self.post_stylize({"photo": PNG}, headers=self.write_headers())
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)["error"], "avatar generation disabled")

    def test_c9_stylize_without_cloud_or_byok_returns_503_at_pass_start(self):
        cg._avatar_engine = FakeEngine()
        with mock.patch.dict(
            os.environ,
            {"POYO_API_KEY": "", "RENOISE_API_KEY": "", "RENOISE_AUTH_TOKEN": ""},
            clear=False,
        ):
            status, _, body = self.post_stylize({"photo": PNG}, headers=self.write_headers())

        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)["error"], "avatar generation disabled")
        self.assertIsNone(cg._avatar_engine.started)

    def test_stylize_returns_409_when_engine_busy(self):
        cg._avatar_engine = RaisingStartEngine(AvatarEngineBusy("already running"))

        status, _, body = self.post_stylize({"photo": PNG}, headers=self.write_headers())

        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body), {"ok": False, "error": "busy", "detail": "already running"})

    def test_avatar_mutation_unexpected_exceptions_return_500(self):
        final_pngs = {slot: self.write_png(slot) for slot in cg.GATEWAY_SLOTS}
        approve_set_snapshot = {
            "job_id": "job-1",
            "stage": "awaiting_set_approval",
            "paths": {"final_pngs": final_pngs},
            "slots": {},
            "error": None,
        }
        cases = [
            (
                "stylize",
                lambda: setattr(cg, "_avatar_engine", RaisingStartEngine(RuntimeError("disk full"))),
                lambda: self.post_stylize({"photo": PNG}, headers=self.write_headers()),
                "avatar stylize failed",
            ),
            (
                "regenerate",
                lambda: setattr(cg, "_avatar_engine", RaisingRegenerateEngine("awaiting_base_approval", RuntimeError("thread failed"))),
                lambda: self.json_request("POST", "/avatar/job:regenerate", {}, headers=self.write_headers()),
                "avatar regenerate failed",
            ),
            (
                "approve-base",
                lambda: setattr(cg, "_avatar_engine", RaisingApproveBaseEngine(RuntimeError("thread failed"))),
                lambda: self.json_request("POST", "/avatar/job:approve-base", {}, headers=self.write_headers()),
                "avatar approve base failed",
            ),
            (
                "approve-set",
                lambda: setattr(cg, "_avatar_engine", RaisingApproveSetEngine(approve_set_snapshot, RuntimeError("commit failed"))),
                lambda: self.request("POST", "/avatar/job:approve-set", headers=self.write_headers()),
                "avatar approve failed",
            ),
            (
                "cancel",
                lambda: setattr(cg, "_avatar_engine", RaisingCancelEngine(RuntimeError("cancel failed"))),
                lambda: self.request("POST", "/avatar/job:cancel", headers=self.write_headers()),
                "avatar cancel failed",
            ),
        ]

        for name, setup, request, error in cases:
            with self.subTest(name=name):
                setup()
                status, _, body = request()
                self.assertEqual(status, 500)
                self.assertEqual(json.loads(body), {"ok": False, "error": error})

    def test_stylize_rejects_large_photo_and_long_identity_description(self):
        cg._avatar_engine = FakeEngine()
        large_photo = PNG + (b"x" * (cg.ASSET_FILE_LIMIT + 1 - len(PNG)))

        status, _, body = self.post_stylize({"photo": large_photo}, headers=self.write_headers())

        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body), {"ok": False, "error": "photo too large"})
        self.assertIsNone(cg._avatar_engine.started)

        status, _, body = self.post_stylize(
            {"photo": PNG, "identity_description": b"x" * (cg.IDENTITY_DESCRIPTION_LIMIT + 1)},
            headers=self.write_headers(),
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body), {"ok": False, "error": "identity_description too long"})
        self.assertIsNone(cg._avatar_engine.started)

    def test_stylize_happy_path_uses_fake_engine(self):
        snapshot = {"job_id": "job-1", "stage": "stylizing", "paths": {}, "slots": {}, "error": None}
        engine = FakeEngine(snapshot)
        cg._avatar_engine = engine

        status, _, body = self.post_stylize(
            {"photo": PNG, "identity_description": b"blue hair"},
            headers=self.write_headers(),
        )

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True, "job": snapshot})
        self.assertEqual(engine.started, (PNG, "blue hair"))
        self.assertEqual(engine.pass_clients.kind, "stylize")

    def test_c9_cloud_session_multipart_reaches_stylize_with_cloud_clients(self):
        snapshot = {"job_id": "job-1", "stage": "stylizing", "paths": {}, "slots": {}, "error": None}
        engine = FakeEngine(snapshot)
        cg._avatar_engine = engine
        cloud = {"base_url": cg.gateway_config.CATY_CLOUD_ORIGIN, "token": "cloud-token"}

        status, _, body = self.post_stylize(
            {"photo": PNG, "cloud_session": json.dumps(cloud).encode()},
            headers=self.write_headers(),
        )

        self.assertEqual(status, 200, body)
        self.assertEqual(engine.pass_clients.source, "cloud")
        self.assertEqual(engine.pass_clients.kind, "stylize")
        self.assertEqual(engine.pass_clients.poyo.base_url, "https://api.caty.talk/v1/avatar")
        self.assertEqual(engine.pass_clients.renoise.auth_token, "cloud-token")

    def test_c9_mid_pass_cloud_credential_conflict_maps_to_deterministic_409(self):
        cg._avatar_engine = RaisingStartEngine(
            AvatarCredentialConflict("avatar credentials cannot change while a pass is in flight")
        )
        cloud = {"base_url": cg.gateway_config.CATY_CLOUD_ORIGIN, "token": "new-cloud-token"}

        status, _, body = self.post_stylize(
            {"photo": PNG, "cloud_session": json.dumps(cloud).encode()},
            headers=self.write_headers(),
        )

        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["error"], "credential_conflict")

    def test_avatar_job_files_use_safe_allow_list(self):
        paths = {"base_candidate_512": self.write_png("base"), "contact_sheet": self.write_png("sheet"), "final_pngs": {}}
        for slot in cg.GATEWAY_SLOTS:
            paths["final_pngs"][slot] = self.write_png(slot)
        cg._avatar_engine = FakeEngine({"job_id": "job-1", "stage": "awaiting_set_approval", "paths": paths, "slots": {}, "error": None})
        headers = self.write_headers()

        expected = {"base": paths["base_candidate_512"], "contact-sheet": paths["contact_sheet"], **paths["final_pngs"]}
        for name, path in expected.items():
            status, response_headers, body = self.request("GET", f"/avatar/job/files/{name}", headers=headers)
            self.assertEqual(status, 200, name)
            self.assertEqual(response_headers["content-type"], "image/png")
            with open(path, "rb") as f:
                self.assertEqual(body, f.read(), name)

        for name in ("nonexistent", "../../etc/passwd"):
            status, _, body = self.request("GET", f"/avatar/job/files/{name}", headers=headers)
            self.assertEqual(status, 404)
            self.assertEqual(json.loads(body), {"ok": False, "error": "not found"})

    def test_regenerate_dispatches_for_base_and_set_stages(self):
        cases = [
            ("awaiting_base_approval", "regenerate_base_called", "regenerate_set_called"),
            ("awaiting_set_approval", "regenerate_set_called", "regenerate_base_called"),
        ]

        for stage, expected_flag, unexpected_flag in cases:
            with self.subTest(stage=stage):
                engine = RecordingRegenerateEngine(stage)
                cg._avatar_engine = engine

                status, _, body = self.json_request(
                    "POST", "/avatar/job:regenerate", {}, headers=self.write_headers()
                )

                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["ok"], True)
                self.assertTrue(getattr(engine, expected_flag))
                self.assertFalse(getattr(engine, unexpected_flag))
                self.assertEqual(
                    engine.pass_clients.kind,
                    "stylize" if stage == "awaiting_base_approval" else "set",
                )

    def test_c9_regenerate_requires_valid_json_body(self):
        engine = RecordingRegenerateEngine("awaiting_base_approval")
        cg._avatar_engine = engine
        headers = self.write_headers()

        status, _, body = self.request("POST", "/avatar/job:regenerate", headers=headers)
        self.assertEqual(status, 200, body)
        self.assertTrue(engine.regenerate_base_called)

        status, _, body = self.request(
            "POST",
            "/avatar/job:regenerate",
            body=b"{bad-json",
            headers={**headers, "Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "invalid json")

        status, _, body = self.json_request(
            "POST", "/avatar/job:regenerate", [], headers=headers
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "json object required")

    def test_c9_byok_empty_body_regenerate_dispatches(self):
        engine = RecordingRegenerateEngine("awaiting_set_approval")
        cg._avatar_engine = engine

        status, _, body = self.request(
            "POST", "/avatar/job:regenerate", headers=self.write_headers()
        )

        self.assertEqual(status, 200, body)
        self.assertTrue(engine.regenerate_set_called)
        self.assertFalse(engine.regenerate_base_called)
        self.assertEqual(engine.pass_clients.source, "byok")
        self.assertEqual(engine.pass_clients.kind, "set")

    def test_c9_invalid_cloud_session_shapes_do_not_change_engine_state(self):
        cases = (
            ("non-dict", "not-an-object"),
            ("missing-token", {"base_url": cg.gateway_config.CATY_CLOUD_ORIGIN}),
            (
                "extra-key",
                {
                    "base_url": cg.gateway_config.CATY_CLOUD_ORIGIN,
                    "token": "cloud-token",
                    "extra": True,
                },
            ),
        )

        for name, cloud_session in cases:
            with self.subTest(name=name):
                engine = RecordingRegenerateEngine("awaiting_base_approval")
                initial_snapshot = dict(engine.current_snapshot)
                cg._avatar_engine = engine

                status, _, body = self.json_request(
                    "POST",
                    "/avatar/job:regenerate",
                    {"cloud_session": cloud_session},
                    headers=self.write_headers(),
                )

                self.assertEqual(status, 400, body)
                self.assertEqual(json.loads(body)["error"], "invalid_cloud_session")
                self.assertEqual(engine.current_snapshot, initial_snapshot)
                self.assertFalse(engine.regenerate_base_called)
                self.assertFalse(engine.regenerate_set_called)
                self.assertFalse(hasattr(engine, "checked_pass_clients"))

    def test_c9_regenerate_and_approve_base_accept_fresh_cloud_sessions_with_bound_kind(self):
        cloud = {"base_url": cg.gateway_config.CATY_CLOUD_ORIGIN, "token": "fresh-cloud-token"}
        engine = RecordingRegenerateEngine("awaiting_base_approval")
        cg._avatar_engine = engine

        status, _, body = self.json_request(
            "POST", "/avatar/job:regenerate", {"cloud_session": cloud}, headers=self.write_headers()
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(engine.pass_clients.source, "cloud")
        self.assertEqual(engine.pass_clients.kind, "stylize")

        engine = FakeEngine(
            {"job_id": "job-1", "stage": "awaiting_base_approval", "paths": {}, "slots": {}, "error": None}
        )
        cg._avatar_engine = engine
        status, _, body = self.json_request(
            "POST",
            "/avatar/job:approve-base",
            {"identity_description": "same identity", "cloud_session": cloud},
            headers=self.write_headers(),
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(engine.pass_clients.source, "cloud")
        self.assertEqual(engine.pass_clients.kind, "set")

    def test_approve_base_forwards_identity_description(self):
        engine = FakeEngine({"job_id": "job-1", "stage": "awaiting_base_approval", "paths": {}, "slots": {}, "error": None})
        cg._avatar_engine = engine
        cg._vision_describer_factory = RaisingDescriberFactory(AssertionError("describer should not be constructed"))

        status, _, body = self.json_request(
            "POST",
            "/avatar/job:approve-base",
            {"identity_description": "  blue hair, glasses  "},
            headers=self.write_headers(),
        )

        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["ok"], True)
        self.assertEqual(payload["description_source"], "client")
        self.assertEqual(engine.approved_base, "blue hair, glasses")
        self.assertEqual(engine.pass_clients.kind, "set")

    def test_approve_base_uses_vision_description_when_client_omits_it(self):
        base_path = self.write_png("base")
        engine = FakeEngine(
            {
                "job_id": "job-1",
                "stage": "awaiting_base_approval",
                "paths": {"base_candidate_512": base_path},
                "slots": {},
                "error": None,
            }
        )
        describer = FakeDescriber("vision black bob, red jacket")
        cg._avatar_engine = engine
        cg._vision_describer_factory = lambda: describer

        status, _, body = self.json_request("POST", "/avatar/job:approve-base", {}, headers=self.write_headers())

        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["ok"], True)
        self.assertEqual(payload["description_source"], "vision")
        self.assertEqual(engine.approved_base, "vision black bob, red jacket")
        self.assertEqual(describer.calls, [PNG])

    def test_approve_base_falls_back_to_generic_when_vision_disabled(self):
        base_path = self.write_png("base")
        engine = FakeEngine(
            {
                "job_id": "job-1",
                "stage": "awaiting_base_approval",
                "paths": {"base_candidate_512": base_path},
                "slots": {},
                "error": None,
            }
        )
        factory = RaisingDescriberFactory(vision_describer.VisionDescriberDisabled("missing key"))
        cg._avatar_engine = engine
        cg._vision_describer_factory = factory

        status, _, body = self.json_request("POST", "/avatar/job:approve-base", {}, headers=self.write_headers())

        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["ok"], True)
        self.assertEqual(payload["description_source"], "generic")
        self.assertEqual(engine.approved_base, cg.GENERIC_IDENTITY_DESCRIPTION)
        self.assertEqual(factory.calls, 1)

    def test_approve_base_falls_back_to_generic_when_describe_raises(self):
        # Invariant: no describer failure may fail approve-base — including
        # exceptions outside the VisionDescriber* hierarchy (leaked bugs).
        base_path = self.write_png("base")
        for exc in (vision_describer.VisionDescriberError("api down"), RuntimeError("leaked bug")):
            with self.subTest(exc=type(exc).__name__):
                engine = FakeEngine(
                    {
                        "job_id": "job-1",
                        "stage": "awaiting_base_approval",
                        "paths": {"base_candidate_512": base_path},
                        "slots": {},
                        "error": None,
                    }
                )

                class _RaisingDescriber:
                    def describe(self, image_bytes):
                        raise exc

                cg._avatar_engine = engine
                cg._vision_describer_factory = lambda: _RaisingDescriber()

                status, _, body = self.json_request(
                    "POST", "/avatar/job:approve-base", {}, headers=self.write_headers()
                )

                self.assertEqual(status, 200)
                payload = json.loads(body)
                self.assertEqual(payload["ok"], True)
                self.assertEqual(payload["description_source"], "generic")
                self.assertEqual(engine.approved_base, cg.GENERIC_IDENTITY_DESCRIPTION)

    def test_approve_base_rejects_long_identity_description(self):
        engine = FakeEngine({"job_id": "job-1", "stage": "awaiting_base_approval", "paths": {}, "slots": {}, "error": None})
        cg._avatar_engine = engine

        status, _, body = self.json_request(
            "POST",
            "/avatar/job:approve-base",
            {"identity_description": "x" * (cg.IDENTITY_DESCRIPTION_LIMIT + 1)},
            headers=self.write_headers(),
        )

        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body), {"ok": False, "error": "identity_description too long"})
        self.assertIsNone(engine.approved_base)

    def test_wrong_stage_returns_409(self):
        headers = self.write_headers()
        cg._avatar_engine = WrongStageEngine({"job_id": "job-1", "stage": "awaiting_base_approval", "paths": {}, "slots": {}, "error": None})

        status, _, body = self.json_request("POST", "/avatar/job:regenerate", {}, headers=headers)
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["error"], "wrong_stage")

        cg._avatar_engine.current_snapshot["stage"] = "awaiting_set_approval"
        status, _, body = self.json_request("POST", "/avatar/job:regenerate", {}, headers=headers)
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["error"], "wrong_stage")

        status, _, body = self.json_request("POST", "/avatar/job:approve-base", {}, headers=headers)
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["error"], "wrong_stage")

        cg._avatar_engine.current_snapshot["paths"] = {"final_pngs": {slot: self.write_png(slot) for slot in cg.GATEWAY_SLOTS}}
        status, _, body = self.request("POST", "/avatar/job:approve-set", headers=headers)
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["error"], "wrong_stage")

    def test_approve_set_applies_assets_and_bumps_version(self):
        final_pngs = {}
        for slot in cg.GATEWAY_SLOTS:
            final_pngs[slot] = self.write_png(f"final-{slot}", data=b"\x89PNG\r\n\x1a\n" + slot.encode("ascii"))
        snapshot = {"job_id": "job-1", "stage": "awaiting_set_approval", "paths": {"final_pngs": final_pngs}, "slots": {}, "error": None}
        cg._avatar_engine = FakeEngine(snapshot)
        before = cg.resolved_config()["assets_version"]

        status, _, body = self.request("POST", "/avatar/job:approve-set", headers=self.write_headers())

        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["assets_version"], before + 1)
        self.assertEqual(payload["job"]["stage"], "done")
        for slot, path in final_pngs.items():
            with open(path, "rb") as f:
                expected = f.read()
            with open(os.path.join(self.asset_dir, f"{slot}.png"), "rb") as f:
                self.assertEqual(f.read(), expected, slot)

    def test_approve_set_second_commit_failure_does_not_reapply_assets(self):
        final_pngs = {}
        for slot in cg.GATEWAY_SLOTS:
            final_pngs[slot] = self.write_png(f"second-race-{slot}", data=b"\x89PNG\r\n\x1a\nrace-" + slot.encode("ascii"))
        snapshot = {"job_id": "job-1", "stage": "awaiting_set_approval", "paths": {"final_pngs": final_pngs}, "slots": {}, "error": None}
        engine = SecondApproveRejectsEngine(snapshot)
        cg._avatar_engine = engine
        before = cg.resolved_config()["assets_version"]
        apply_calls = []
        original_apply = cg.Handler._apply_asset_slots

        def spy_apply(handler, slot_bytes):
            apply_calls.append(sorted(slot_bytes))
            return original_apply(handler, slot_bytes)

        cg.Handler._apply_asset_slots = spy_apply
        try:
            status, _, body = self.request("POST", "/avatar/job:approve-set", headers=self.write_headers())
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["assets_version"], before + 1)

            status, _, body = self.request("POST", "/avatar/job:approve-set", headers=self.write_headers())
            self.assertEqual(status, 409)
            self.assertEqual(json.loads(body)["error"], "wrong_stage")
        finally:
            cg.Handler._apply_asset_slots = original_apply

        self.assertEqual(engine.approve_set_calls, 2)
        self.assertEqual(len(apply_calls), 1)
        self.assertEqual(cg.resolved_config()["assets_version"], before + 1)

    def test_approve_set_apply_failure_after_approve_returns_distinct_500(self):
        final_pngs = {slot: self.write_png(f"publish-fail-{slot}") for slot in cg.GATEWAY_SLOTS}
        snapshot = {"job_id": "job-1", "stage": "awaiting_set_approval", "paths": {"final_pngs": final_pngs}, "slots": {}, "error": None}
        engine = FakeEngine(snapshot)
        cg._avatar_engine = engine
        original_apply = cg.Handler._apply_asset_slots

        def fail_apply(handler, slot_bytes):
            raise RuntimeError("disk full")

        cg.Handler._apply_asset_slots = fail_apply
        try:
            status, _, body = self.request("POST", "/avatar/job:approve-set", headers=self.write_headers())
        finally:
            cg.Handler._apply_asset_slots = original_apply

        self.assertEqual(status, 500)
        self.assertEqual(
            json.loads(body),
            {"ok": False, "error": "avatar assets publish failed after approve", "job_id": "job-1"},
        )
        self.assertTrue(engine.approve_set_called)
        self.assertEqual(engine.current_snapshot["stage"], "done")

    def write_png(self, name, data=PNG):
        path = os.path.join(self.tmp, f"{name}.png")
        with open(path, "wb") as f:
            f.write(data)
        return path


if __name__ == "__main__":
    unittest.main()
