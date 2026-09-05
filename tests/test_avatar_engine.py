import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from PIL import Image


from caty_gateway import avatar_engine
from caty_gateway.face_core import Thresholds


class FakeResponse:
    def __init__(self, payload=b""):
        self.payload = payload
        self.offset = 0

    def read(self, size=-1):
        if size is None or size < 0:
            size = len(self.payload) - self.offset
        chunk = self.payload[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def write_png(path: Path, clear_rate=75.0, padding=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (4, 4), "white")
    if clear_rate == 75.0:
        for x in (1, 2):
            for y in (1, 2):
                image.putpixel((x, y), (200, 20, 20))
    image.save(path)
    if padding:
        with path.open("ab") as handle:
            handle.write(b"x" * padding)


class FakeRenoise:
    def __init__(self):
        self.uploads = []

    def upload_bytes(self, data, filename, material_type="image"):
        self.uploads.append(("bytes", filename))
        return f"https://upload.test/{filename}"

    def upload(self, file_path, material_type="image"):
        name = Path(file_path).name
        self.uploads.append(("file", name))
        return f"https://upload.test/{name}"


class FakePoyo:
    def __init__(self, mode="accepted", block_event=None):
        self.calls = []
        self.mode = mode
        self.block_event = block_event

    def generate(self, prompt, image_urls, output_path):
        if self.block_event is not None:
            self.block_event.wait(5)
        output_path = Path(output_path)
        self.calls.append({"prompt": prompt, "image_urls": list(image_urls), "output_path": output_path})
        if output_path.name.startswith("base-candidate"):
            write_png(output_path, clear_rate=75.0, padding=500)
            return "https://result.test/base.png"
        padding = 300 if "candidate2" in output_path.name else 10
        write_png(output_path, clear_rate=75.0, padding=padding)
        return f"https://result.test/{output_path.name}"


class MedianDriftPoyo(FakePoyo):
    def generate(self, prompt, image_urls, output_path):
        output_path = Path(output_path)
        self.calls.append({"prompt": prompt, "image_urls": list(image_urls), "output_path": output_path})
        if output_path.name.startswith("talk1."):
            write_png(output_path, clear_rate=100.0, padding=500)
        else:
            write_png(output_path, clear_rate=75.0, padding=500)
        return f"https://result.test/{output_path.name}"


def pass_clients(kind, poyo, renoise, credential_id="test-credentials", source="test"):
    return avatar_engine.AvatarPassClients(
        kind=kind,
        poyo=poyo,
        renoise=renoise,
        credential_id=credential_id,
        source=source,
    )


class PoyoClientTests(unittest.TestCase):
    def test_documented_default_base_url(self):
        # Documented default base URL: https://api.poyo.ai
        domain = "poyo"
        self.assertEqual(avatar_engine.DEFAULT_POYO_BASE, f"https://api.{domain}.ai")

    def test_engine_defaults_to_pixel_art_thresholds(self):
        # #254: the watercolor 4MB floor falsely rejected nearly every
        # pixel-art candidate; the avatar flow must default to 400KB.
        engine = avatar_engine.AvatarEngine(
            work_dir=tempfile.mkdtemp(),
            poyo_client=FakePoyo(),
            renoise_client=FakeRenoise(),
            style_ref_path=__file__,
        )
        self.assertEqual(engine.thresholds.size_floor_bytes, 400_000)

    def test_all_requests_send_custom_user_agent(self):
        # The renoise WAF 403s urllib's default "Python-urllib" UA (VPS, 2026-07-04).
        seen = []

        def fake_urlopen(request, timeout=60):
            seen.append({k.lower(): v for k, v in request.header_items()})
            return FakeResponse(json.dumps({"data": {"task_id": "t1", "status": "running"}}).encode("utf-8"))

        poyo = avatar_engine.PoyoClient(api_key="secret", base_url="https://poyo.mock", urlopen=fake_urlopen)
        poyo.submit("p", ["https://img.test/a.png"])
        poyo.status("t1")

        def fake_upload_urlopen(request, timeout=120):
            seen.append({k.lower(): v for k, v in request.header_items()})
            return FakeResponse(json.dumps({"downloadUrl": "https://asset.mock/x"}).encode("utf-8"))

        renoise = avatar_engine.RenoiseClient(api_key="rk", urlopen=fake_upload_urlopen)
        renoise.upload_bytes(b"\x89PNGdata", "x.png")

        self.assertEqual(len(seen), 3)
        for headers in seen:
            self.assertEqual(headers.get("user-agent"), avatar_engine.USER_AGENT)

    def test_submit_keeps_multiple_image_urls_order_and_retries_429_once(self):
        requests = []
        sleeps = []

        def fake_urlopen(request, timeout=60):
            requests.append(request)
            if len(requests) == 1:
                raise urllib.error.HTTPError(
                    request.full_url,
                    429,
                    "rate limited",
                    hdrs={},
                    fp=io.BytesIO(b"rate limited"),
                )
            return FakeResponse(json.dumps({"data": {"task_id": "task-123"}}).encode("utf-8"))

        client = avatar_engine.PoyoClient(
            api_key="secret",
            base_url="https://poyo.mock",
            urlopen=fake_urlopen,
            sleep=lambda seconds: sleeps.append(seconds),
        )

        task_id = client.submit("prompt text", ["https://img.test/identity.png", "https://img.test/style.png"])

        self.assertEqual(task_id, "task-123")
        self.assertEqual(sleeps, [15])
        body = json.loads(requests[1].data.decode("utf-8"))
        self.assertEqual(body["model"], "nano-banana-pro-edit")
        self.assertEqual(body["input"]["image_urls"], ["https://img.test/identity.png", "https://img.test/style.png"])

    def test_submit_does_not_retry_url_errors(self):
        # A lost submit response may mean the paid task was already queued;
        # blind URLError retry could double-bill, so it must surface at once.
        calls = []
        sleeps = []

        def fake_urlopen(request, timeout=60):
            calls.append(request)
            raise urllib.error.URLError("temporary network failure")

        client = avatar_engine.PoyoClient(
            api_key="secret",
            base_url="https://poyo.mock",
            urlopen=fake_urlopen,
            sleep=lambda seconds: sleeps.append(seconds),
            retry_backoff=0,
        )

        with self.assertRaises(urllib.error.URLError):
            client.submit("prompt text", ["https://img.test/identity.png"])

        self.assertEqual(len(calls), 1)
        self.assertEqual(sleeps, [])

    def test_status_retries_url_errors_and_http_5xx(self):
        calls = []
        sleeps = []

        def fake_urlopen(request, timeout=60):
            calls.append(request)
            if len(calls) == 1:
                raise urllib.error.URLError("temporary network failure")
            if len(calls) == 2:
                raise urllib.error.HTTPError(request.full_url, 502, "bad gateway", hdrs={}, fp=io.BytesIO(b""))
            return FakeResponse(json.dumps({"data": {"status": "running"}}).encode("utf-8"))

        client = avatar_engine.PoyoClient(
            api_key="secret",
            base_url="https://poyo.mock",
            urlopen=fake_urlopen,
            sleep=lambda seconds: sleeps.append(seconds),
            retry_backoff=0,
        )

        payload = client.status("task-123")

        self.assertEqual(payload["data"]["status"], "running")
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [0, 0])

    def test_poll_fails_after_bounded_unknown_statuses_and_accepts_canceled_spelling(self):
        unknown_client = avatar_engine.PoyoClient(
            api_key="secret",
            base_url="https://poyo.mock",
            urlopen=lambda request, timeout=60: FakeResponse(
                json.dumps({"data": {"status": "mystery"}}).encode("utf-8")
            ),
            sleep=lambda seconds: None,
            poll_interval=0,
            timeout=10,
            unknown_status_limit=2,
        )
        with self.assertRaisesRegex(RuntimeError, "unknown status"):
            unknown_client.poll("task-unknown")

        canceled_client = avatar_engine.PoyoClient(
            api_key="secret",
            base_url="https://poyo.mock",
            urlopen=lambda request, timeout=60: FakeResponse(
                json.dumps({"data": {"status": "canceled", "message": "stopped"}}).encode("utf-8")
            ),
        )
        with self.assertRaisesRegex(RuntimeError, "stopped"):
            canceled_client.poll("task-canceled")

    def test_download_streams_with_size_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "result.png"
            client = avatar_engine.PoyoClient(
                api_key="secret",
                base_url="https://poyo.mock",
                urlopen=lambda request, timeout=120: FakeResponse(b"1234"),
                max_download_bytes=3,
            )

            with self.assertRaisesRegex(RuntimeError, "exceeded maximum size"):
                client.download("https://files.poyo.ai/result.png", out)

            self.assertFalse(out.exists())


class RenoiseClientTests(unittest.TestCase):
    def test_documented_default_base_url(self):
        # Documented default base URL: https://www.renoise.ai/api/public/v1
        domain = "renoise"
        self.assertEqual(avatar_engine.DEFAULT_RENOISE_BASE_URL, f"https://www.{domain}.ai/api/public/v1")

    def test_upload_uses_manual_multipart_and_download_url(self):
        captured = {}

        def fake_urlopen(request, timeout=120):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["body"] = request.data
            return FakeResponse(json.dumps({"downloadUrl": "https://upload.test/file.png"}).encode("utf-8"))

        client = avatar_engine.RenoiseClient(
            api_key="renoise-key",
            base_url="https://renoise.mock/api",
            urlopen=fake_urlopen,
        )
        url = client.upload_bytes(b"PNGDATA", "avatar.png")

        self.assertEqual(url, "https://upload.test/file.png")
        self.assertEqual(captured["url"], "https://renoise.mock/api/materials/upload")
        self.assertEqual(captured["headers"]["X-api-key"], "renoise-key")
        self.assertIn(b'name="file"; filename="avatar.png"', captured["body"])
        self.assertIn(b"Content-Type: application/octet-stream", captured["body"])
        self.assertIn(b'name="type"', captured["body"])
        self.assertIn(b"PNGDATA", captured["body"])

    def test_upload_auth_token_uses_bearer_without_api_key_header(self):
        captured = {}

        def fake_urlopen(request, timeout=120):
            captured["headers"] = dict(request.header_items())
            return FakeResponse(json.dumps({"downloadUrl": "https://upload.test/file.png"}).encode("utf-8"))

        client = avatar_engine.RenoiseClient(
            api_key=None,
            auth_token="bearer-token",
            base_url="https://renoise.mock/api",
            urlopen=fake_urlopen,
        )

        client.upload_bytes(b"\x89PNG data", "avatar.png")

        self.assertEqual(captured["headers"]["Authorization"], "Bearer bearer-token")
        self.assertNotIn("X-api-key", captured["headers"])

    def test_upload_sanitizes_filename_allows_material_types_and_sniffs_content_type(self):
        captured = {}

        def fake_urlopen(request, timeout=120):
            captured["body"] = request.data
            return FakeResponse(json.dumps({"downloadUrl": "https://upload.test/file.jpg"}).encode("utf-8"))

        client = avatar_engine.RenoiseClient(
            api_key="renoise-key",
            base_url="https://renoise.mock/api",
            urlopen=fake_urlopen,
        )

        client.upload_bytes(b"\xff\xd8jpeg", 'bad"\r\nname.jpg', material_type="video")

        self.assertIn(b'filename="bad___name.jpg"', captured["body"])
        self.assertIn(b"Content-Type: image/jpeg", captured["body"])
        self.assertIn(b"\r\nvideo\r\n", captured["body"])
        with self.assertRaisesRegex(ValueError, "material_type"):
            client.upload_bytes(b"data", "avatar.bin", material_type="document")


class AvatarEngineTests(unittest.TestCase):
    def tearDown(self):
        avatar_engine._active_job = None

    def make_engine(self, tmp, poyo=None, renoise=None, thresholds=None):
        style_ref = Path(tmp) / "style-ref.png"
        write_png(style_ref)
        poyo = poyo or FakePoyo()
        renoise = renoise or FakeRenoise()
        return avatar_engine.AvatarEngine(
            work_dir=tmp,
            poyo_client=poyo,
            renoise_client=renoise,
            thresholds=thresholds or Thresholds(size_floor_bytes=1, deviation_pct=5.0, retry_count=0),
            member_name="Test Member",
            style_ref_path=style_ref,
        )

    def make_poyo_client(self, status_payload: bytes, timeout=5.0):
        def fake_urlopen(request, timeout=60):
            if request.full_url.endswith(avatar_engine.POYO_SUBMIT_PATH):
                return FakeResponse(json.dumps({"data": {"task_id": "task-123"}}).encode("utf-8"))
            if "/api/generate/status/" in request.full_url:
                return FakeResponse(status_payload)
            raise AssertionError(f"unexpected poyo URL: {request.full_url}")

        return avatar_engine.PoyoClient(
            api_key="secret",
            base_url="https://poyo.mock",
            urlopen=fake_urlopen,
            sleep=lambda seconds: None,
            poll_interval=0,
            timeout=timeout,
            retry_attempts=1,
        )

    def test_engine_snapshot_and_chained_refs(self):
        with tempfile.TemporaryDirectory() as tmp:
            poyo = FakePoyo()
            renoise = FakeRenoise()
            engine = self.make_engine(tmp, poyo=poyo, renoise=renoise)

            job = engine.start_stylize(b"source-image", identity_description="black hair, glasses")
            self.assertTrue(job.wait(5), job.snapshot())
            self.assertEqual(job.snapshot()["stage"], "awaiting_base_approval")

            engine.approve_base("black hair, glasses")
            self.assertTrue(job.wait(5), job.snapshot())
            snapshot = engine.snapshot()

        self.assertEqual(snapshot["stage"], "awaiting_set_approval")
        self.assertIn("base_candidate_512", snapshot["paths"])
        self.assertIn("contact_sheet", snapshot["paths"])
        self.assertEqual(set(snapshot["paths"]["final_pngs"]), set(avatar_engine.GATEWAY_SLOTS))
        self.assertEqual(poyo.calls[0]["image_urls"], ["https://upload.test/identity.png", "https://upload.test/style-ref.png"])

        refs_by_output = {call["output_path"].name: call["image_urls"] for call in poyo.calls[1:]}
        self.assertEqual(refs_by_output["idle.attempt1.png"], ["https://upload.test/base-candidate.2k.png"])
        self.assertEqual(refs_by_output["talk1.attempt1.png"], ["https://upload.test/base-candidate.2k.png"])
        self.assertEqual(refs_by_output["blink.attempt1.png"], ["https://upload.test/base-candidate.2k.png"])
        self.assertEqual(refs_by_output["listen.attempt1.png"], ["https://upload.test/base-candidate.2k.png"])
        self.assertEqual(refs_by_output["talk2.attempt1.png"], ["https://upload.test/talk1.2k.png"])
        self.assertEqual(refs_by_output["talk3.attempt1.png"], ["https://upload.test/talk1.2k.png"])
        self.assertEqual(refs_by_output["talk_blink.attempt1.png"], ["https://upload.test/blink.2k.png"])

    def test_engine_drift_retry_uses_largest_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            poyo = FakePoyo()
            engine = self.make_engine(
                tmp,
                poyo=poyo,
                thresholds=Thresholds(size_floor_bytes=1_000_000, deviation_pct=5.0, retry_count=1),
            )

            job = engine.start_stylize(b"source-image")
            self.assertTrue(job.wait(5), job.snapshot())
            engine.approve_base("same identity")
            self.assertTrue(job.wait(5), job.snapshot())

            talk1 = job.report["frames"]["talk1"]

        self.assertTrue(talk1["fallback"])
        self.assertEqual(talk1["attempt"], 2)
        self.assertEqual(talk1["candidate"], 2)
        self.assertIn("talk1", job.report["fallback_slots"])

    def test_engine_rejects_candidate_for_clear_rate_deviation_from_median(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = self.make_engine(
                tmp,
                poyo=MedianDriftPoyo(),
                thresholds=Thresholds(size_floor_bytes=1, deviation_pct=5.0, retry_count=0),
            )

            job = engine.start_stylize(b"source-image")
            self.assertTrue(job.wait(5), job.snapshot())
            engine.approve_base("same identity")
            self.assertTrue(job.wait(5), job.snapshot())

            talk1 = job.report["frames"]["talk1"]

        self.assertTrue(talk1["fallback"])
        self.assertIn("deviates", talk1["fallback_reason"])
        self.assertIn("median", talk1["fallback_reason"])

    def test_single_job_lock_rejects_second_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            release = threading.Event()
            first = self.make_engine(Path(tmp) / "one", poyo=FakePoyo(block_event=release))
            second = self.make_engine(Path(tmp) / "two")

            job = first.start_stylize(b"source-image")
            with self.assertRaises(avatar_engine.AvatarEngineBusy):
                second.start_stylize(b"other-image")
            release.set()
            self.assertTrue(job.wait(5), job.snapshot())

    def test_start_stylize_checks_style_ref_before_claiming_active_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = avatar_engine.AvatarEngine(
                work_dir=tmp,
                poyo_client=FakePoyo(),
                renoise_client=FakeRenoise(),
                style_ref_path=Path(tmp) / "missing-style.png",
            )

            with self.assertRaisesRegex(avatar_engine.AvatarEngineDisabled, "style reference"):
                engine.start_stylize(b"source-image")

            self.assertIsNone(avatar_engine._active_job)

    def test_poyo_failures_mark_failed_and_release_active_slot(self):
        cases = {
            "failed status": json.dumps(
                {"data": {"status": "failed", "error_message": "generation failed"}}
            ).encode("utf-8"),
            "finished without urls": json.dumps({"data": {"status": "finished", "files": []}}).encode("utf-8"),
            "timeout": json.dumps({"data": {"status": "running"}}).encode("utf-8"),
            "malformed json": b"{bad json",
        }
        timeouts = {"timeout": 0.0}

        for name, payload in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                engine = self.make_engine(
                    tmp,
                    poyo=self.make_poyo_client(payload, timeout=timeouts.get(name, 5.0)),
                    renoise=FakeRenoise(),
                )

                job = engine.start_stylize(b"source-image")
                self.assertTrue(job.wait(5), job.snapshot())
                snapshot = job.snapshot()

                self.assertEqual(snapshot["stage"], "failed")
                self.assertTrue(snapshot["error"])
                followup = self.make_engine(Path(tmp) / "followup", poyo=FakePoyo(), renoise=FakeRenoise())
                followup_job = followup.start_stylize(b"next-image")
                self.assertTrue(followup_job.wait(5), followup_job.snapshot())
                avatar_engine._active_job = None

    def test_stage_guard_negative_cases(self):
        with tempfile.TemporaryDirectory() as tmp:
            release = threading.Event()
            engine = self.make_engine(Path(tmp) / "stylizing", poyo=FakePoyo(block_event=release))
            job = engine.start_stylize(b"source-image")
            for method in (engine.regenerate_base, lambda: engine.approve_base("id"), engine.regenerate_set, engine.approve_set):
                with self.assertRaises(avatar_engine.AvatarJobStateError):
                    method()
            release.set()
            self.assertTrue(job.wait(5), job.snapshot())

        avatar_engine._active_job = None
        with tempfile.TemporaryDirectory() as tmp:
            engine = self.make_engine(tmp)
            job = engine.start_stylize(b"source-image")
            self.assertTrue(job.wait(5), job.snapshot())
            for method in (engine.regenerate_set, engine.approve_set):
                with self.assertRaises(avatar_engine.AvatarJobStateError):
                    method()
            block = threading.Event()
            blocking_poyo = FakePoyo(block_event=block)
            engine._pass_client_factory = lambda kind: pass_clients(kind, blocking_poyo, FakeRenoise())
            engine.approve_base("same identity")
            for method in (engine.regenerate_base, lambda: engine.approve_base("id"), engine.regenerate_set, engine.approve_set):
                with self.assertRaises(avatar_engine.AvatarJobStateError):
                    method()
            block.set()
            self.assertTrue(job.wait(5), job.snapshot())
            for method in (engine.regenerate_base, lambda: engine.approve_base("id")):
                with self.assertRaises(avatar_engine.AvatarJobStateError):
                    method()

    def test_active_slot_can_be_reclaimed_after_done_and_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = self.make_engine(Path(tmp) / "done-one")
            job = first.start_stylize(b"source-image")
            self.assertTrue(job.wait(5), job.snapshot())
            first.approve_base("same identity")
            self.assertTrue(job.wait(5), job.snapshot())
            first.approve_set()

            second = self.make_engine(Path(tmp) / "done-two")
            second_job = second.start_stylize(b"next-image")
            self.assertTrue(second_job.wait(5), second_job.snapshot())

        avatar_engine._active_job = None
        with tempfile.TemporaryDirectory() as tmp:
            failing = self.make_engine(
                Path(tmp) / "failed-one",
                poyo=self.make_poyo_client(
                    json.dumps({"data": {"status": "failed", "message": "bad"}}).encode("utf-8")
                ),
            )
            failed_job = failing.start_stylize(b"source-image")
            self.assertTrue(failed_job.wait(5), failed_job.snapshot())
            self.assertEqual(failed_job.snapshot()["stage"], "failed")

            followup = self.make_engine(Path(tmp) / "failed-two")
            followup_job = followup.start_stylize(b"next-image")
            self.assertTrue(followup_job.wait(5), followup_job.snapshot())

    def test_cancel_clears_job_and_releases_active_slot_for_allowed_stages(self):
        for stage in ("awaiting_base_approval", "awaiting_set_approval", "failed", "done"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                if stage == "failed":
                    engine = self.make_engine(
                        Path(tmp) / "engine",
                        poyo=self.make_poyo_client(
                            json.dumps({"data": {"status": "failed", "message": "bad"}}).encode("utf-8")
                        ),
                    )
                else:
                    engine = self.make_engine(Path(tmp) / "engine")
                job = engine.start_stylize(b"source-image")
                self.assertTrue(job.wait(5), job.snapshot())
                if stage in {"awaiting_set_approval", "done"}:
                    engine.approve_base("same identity")
                    self.assertTrue(job.wait(5), job.snapshot())
                if stage == "done":
                    engine.approve_set()
                self.assertEqual(job.snapshot()["stage"], stage)

                engine.cancel()

                self.assertIsNone(engine._job)
                self.assertEqual(engine.snapshot()["stage"], "idle")
                self.assertIsNone(avatar_engine._active_job)
                followup = self.make_engine(Path(tmp) / "followup")
                followup_job = followup.start_stylize(b"next-image")
                self.assertTrue(followup_job.wait(5), followup_job.snapshot())
                avatar_engine._active_job = None

    def test_cancel_rejects_running_or_missing_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            release = threading.Event()
            engine = self.make_engine(Path(tmp) / "stylizing", poyo=FakePoyo(block_event=release))
            job = engine.start_stylize(b"source-image")
            with self.assertRaisesRegex(avatar_engine.AvatarJobStateError, "stylizing"):
                engine.cancel()
            release.set()
            self.assertTrue(job.wait(5), job.snapshot())

        avatar_engine._active_job = None
        with tempfile.TemporaryDirectory() as tmp:
            engine = self.make_engine(Path(tmp) / "generating")
            job = engine.start_stylize(b"source-image")
            self.assertTrue(job.wait(5), job.snapshot())
            release = threading.Event()
            blocking_poyo = FakePoyo(block_event=release)
            engine._pass_client_factory = lambda kind: pass_clients(kind, blocking_poyo, FakeRenoise())
            engine.approve_base("same identity")
            with self.assertRaisesRegex(avatar_engine.AvatarJobStateError, "generating"):
                engine.cancel()
            release.set()
            self.assertTrue(job.wait(5), job.snapshot())

        avatar_engine._active_job = None
        with tempfile.TemporaryDirectory() as tmp:
            engine = self.make_engine(Path(tmp) / "empty")
            with self.assertRaisesRegex(avatar_engine.AvatarJobStateError, "no avatar job"):
                engine.cancel()

    def test_cancel_racing_start_stylize_never_clobbers_new_job(self):
        for _ in range(10):
            with tempfile.TemporaryDirectory() as tmp:
                engine = self.make_engine(Path(tmp) / "engine")
                job = engine.start_stylize(b"source-image")
                self.assertTrue(job.wait(5), job.snapshot())
                engine.approve_base("same identity")
                self.assertTrue(job.wait(5), job.snapshot())
                engine.approve_set()
                self.assertEqual(job.snapshot()["stage"], "done")

                barrier = threading.Barrier(2)
                new_jobs = []
                cancel_errors = []

                def call_cancel():
                    barrier.wait()
                    try:
                        engine.cancel()
                    except avatar_engine.AvatarJobStateError as exc:
                        cancel_errors.append(exc)

                def call_stylize():
                    barrier.wait()
                    new_jobs.append(engine.start_stylize(b"next-image"))

                threads = [threading.Thread(target=call_cancel), threading.Thread(target=call_stylize)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(5)

                self.assertEqual(len(new_jobs), 1)
                new_job = new_jobs[0]
                self.assertTrue(new_job.wait(5), new_job.snapshot())
                # single-job invariant: a racing cancel of the OLD job must never
                # detach the freshly claimed job (engine wedge, GLM review finding 1)
                self.assertIs(engine._job, new_job)
                self.assertIs(avatar_engine._active_job, new_job)
                self.assertEqual(engine.snapshot()["stage"], "awaiting_base_approval")
                engine.cancel()
                self.assertIsNone(engine._job)
                self.assertIsNone(avatar_engine._active_job)

    def test_concurrent_regenerate_base_claims_stage_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = self.make_engine(tmp)
            job = engine.start_stylize(b"source-image")
            self.assertTrue(job.wait(5), job.snapshot())
            poyo = FakePoyo()
            engine._pass_client_factory = lambda kind: pass_clients(kind, poyo, FakeRenoise())
            starts = []
            job.start_thread = lambda target: starts.append(target)
            barrier = threading.Barrier(3)
            results = []
            errors = []

            def call_regenerate():
                barrier.wait()
                try:
                    results.append(engine.regenerate_base())
                except Exception as exc:  # noqa: BLE001 - test captures exact exception below.
                    errors.append(exc)

            threads = [threading.Thread(target=call_regenerate) for _ in range(2)]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(5)
            self.assertEqual(len(starts), 1)
            starts[0]()

        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], avatar_engine.AvatarJobStateError)
        self.assertEqual(len(poyo.calls), 1)

    def test_c9_new_job_collision_is_busy_and_same_job_credential_swap_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            release = threading.Event()
            poyo = FakePoyo(block_event=release)
            engine = self.make_engine(tmp, poyo=poyo)
            session_a = pass_clients("stylize", poyo, FakeRenoise(), "cloud-session-a")
            session_a_again = pass_clients("stylize", FakePoyo(), FakeRenoise(), "cloud-session-a")
            session_b = pass_clients("stylize", FakePoyo(), FakeRenoise(), "cloud-session-b")

            job = engine.start_stylize(b"source-image", pass_clients=session_a)
            with self.assertRaises(avatar_engine.AvatarEngineBusy):
                engine.start_stylize(b"same-session", pass_clients=session_a_again)
            with self.assertRaises(avatar_engine.AvatarEngineBusy):
                engine.start_stylize(b"swapped-session", pass_clients=session_b)
            with self.assertRaisesRegex(avatar_engine.AvatarCredentialConflict, "cannot change"):
                engine.regenerate_base(session_b)

            release.set()
            self.assertTrue(job.wait(5), job.snapshot())
            regenerated = engine.regenerate_base(session_b)
            self.assertTrue(regenerated.wait(5), regenerated.snapshot())
            self.assertEqual(regenerated.snapshot()["stage"], "awaiting_base_approval")

    def test_c9_byok_cloud_alternation_across_consecutive_jobs_both_orders(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = self.make_engine(tmp)
            for order in (("byok", "cloud"), ("cloud", "byok")):
                with self.subTest(order=order):
                    for source in order:
                        clients = pass_clients(
                            "stylize",
                            FakePoyo(),
                            FakeRenoise(),
                            f"{source}-credential",
                            source=source,
                        )
                        self.assertEqual(clients.source, source)
                        job = engine.start_stylize(b"source-image", pass_clients=clients)
                        self.assertTrue(job.wait(5), job.snapshot())
                        self.assertEqual(job.snapshot()["stage"], "awaiting_base_approval")
                        engine.cancel()

    def test_c9_relay_401_402_abort_set_pass_fail_cleanly_and_cancel_releases_state(self):
        for status in (401, 402):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                engine = self.make_engine(tmp)
                job = engine.start_stylize(b"source-image")
                self.assertTrue(job.wait(5), job.snapshot())
                calls = []

                class RelayFailurePoyo(FakePoyo):
                    def generate(self, prompt, image_urls, output_path):
                        calls.append(output_path)
                        raise urllib.error.HTTPError(
                            "https://api.caty.talk/v1/avatar/api/generate/submit",
                            status,
                            "relay rejected",
                            hdrs={},
                            fp=None,
                        )

                failing = pass_clients(
                    "set",
                    RelayFailurePoyo(),
                    FakeRenoise(),
                    f"expired-cloud-{status}",
                    source="cloud",
                )
                engine.approve_base("same identity", failing)
                self.assertTrue(job.wait(5), job.snapshot())
                self.assertEqual(job.snapshot()["stage"], "failed")
                self.assertEqual(len(calls), 1)
                engine.cancel()
                self.assertIsNone(avatar_engine._active_job)
                self.assertEqual(engine.snapshot()["stage"], "idle")

                restarted = self.make_engine(Path(tmp) / "restart")
                restarted_job = restarted.start_stylize(b"after-restart")
                self.assertTrue(restarted_job.wait(5), restarted_job.snapshot())
                self.assertEqual(restarted_job.snapshot()["stage"], "awaiting_base_approval")
                restarted.cancel()

    def test_c9_key_free_construction_has_no_style_default(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {
                "CATY_AVATAR_STYLE_REF": "",
                "POYO_API_KEY": "",
                "RENOISE_API_KEY": "",
                "RENOISE_AUTH_TOKEN": "",
            },
            clear=False,
        ):
            engine = avatar_engine.AvatarEngine(work_dir=tmp)
            self.assertIsNone(engine.style_ref_path)

    def test_c9_missing_style_fails_lazily_at_pass_start(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {
                "CATY_AVATAR_STYLE_REF": "",
                "POYO_API_KEY": "",
                "RENOISE_API_KEY": "",
                "RENOISE_AUTH_TOKEN": "",
            },
            clear=False,
        ):
            engine = avatar_engine.AvatarEngine(work_dir=tmp)
            with self.assertRaisesRegex(avatar_engine.AvatarEngineDisabled, "CATY_AVATAR_STYLE_REF"):
                engine.start_stylize(b"source")

if __name__ == "__main__":
    unittest.main()
