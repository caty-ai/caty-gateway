import json
import os
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock


from caty_gateway import caty_gateway as cg
from caty_gateway import share_store
from tests import test_smoke as smoke_helpers


PNG = b"\x89PNG\r\n\x1a\nattachment-canary"


class DeclaringBackend:
    def __init__(self, transports=("generate", "stream"), mimes=("image/png",), max_bytes=None, staging_dir=None):
        self.transports = frozenset(transports)
        self.mimes = frozenset(mimes)
        self.max_bytes = max_bytes
        self.staging_dir = staging_dir
        self.calls = []

    def attachment_transports(self):
        return self.transports

    def supported_attachment_mimes(self):
        return self.mimes

    def attachment_max_bytes(self):
        return self.max_bytes

    def attachment_staging_dir(self):
        return self.staging_dir

    def supports_stream(self):
        return True

    def generate(self, text, session_id, timeout, route=None, attachments=None):
        self.calls.append(("generate", text, attachments))
        return "fallback"

    def stream(self, text, session_id, timeout, route=None, attachments=None):
        self.calls.append(("stream", text, attachments))
        if False:
            yield ""
        raise RuntimeError("stream failed")


class NonDeclaringBackend:
    def __init__(self):
        self.calls = []

    def supports_stream(self):
        return False

    def generate(self, text, session_id, timeout, route=None):
        self.calls.append(("generate", text))
        return "ok"

    def stream(self, text, session_id, timeout, route=None):
        self.calls.append(("stream", text))
        return iter(("ok",))


class BlockingDeclaringBackend(DeclaringBackend):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def stream(self, text, session_id, timeout, route=None, attachments=None):
        self.calls.append(("stream", text, attachments))
        self.entered.set()
        self.release.wait(5)
        yield "見えています。"


class BlockingNonDeclaringBackend(NonDeclaringBackend):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def generate(self, text, session_id, timeout, route=None):
        self.calls.append(("generate", text))
        self.entered.set()
        self.release.wait(5)
        return "確認できません。"


class AttachmentMechanismTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="caty-attachments-")
        self.old_jobs = dict(cg.JOBS)
        cg.JOBS.clear()

    def tearDown(self):
        cg.JOBS.clear()
        cg.JOBS.update(self.old_jobs)
        self.tmp.cleanup()

    def claimed(self, data=PNG, mime="image/png", kind="image", filename="photo.png"):
        path = os.path.join(self.tmp.name, os.urandom(16).hex())
        with open(path, "wb") as stream:
            stream.write(data)
        os.chmod(path, 0o600)
        return share_store.ClaimedFile(path, mime, len(data), filename, kind)

    def prepare(self, backend, claimed=None, config=None):
        claimed = claimed or self.claimed()
        config = {"attachment_passthrough": ""} if config is None else config
        with mock.patch.object(cg, "BACKEND", backend), mock.patch.object(
            cg, "resolved_config", return_value=config
        ):
            plan = cg._prepare_binary_attachment(claimed, "question", "req-a")
        return claimed, plan

    def test_t0_binary_turn_has_only_delivery_or_metadata_only_endings(self):
        cases = (
            (DeclaringBackend(), cg.Delivery, cg.Delivery),
            (DeclaringBackend(transports=("generate",)), cg.Delivery, cg.MetadataOnly),
            (NonDeclaringBackend(), cg.MetadataOnly, cg.MetadataOnly),
        )
        for backend, generate_type, stream_type in cases:
            with self.subTest(backend=type(backend).__name__, transports=getattr(backend, "transports", None)):
                claimed, plan = self.prepare(backend)
                self.assertIsInstance(plan["generate"], generate_type)
                self.assertIsInstance(plan["stream"], stream_type)
                for entry in (plan["generate"], plan["stream"]):
                    self.assertIn("ファイル名: photo.png", entry.brain_text)
                    self.assertIn("MIMEタイプ: image/png", entry.brain_text)
                self.assertEqual(
                    os.path.exists(claimed.path),
                    cg.Delivery in (generate_type, stream_type),
                )

    def test_t0_non_declaring_backend_is_called_without_attachments_keyword(self):
        backend = NonDeclaringBackend()
        backend.generate = mock.Mock(wraps=backend.generate)
        backend.stream = mock.Mock(wraps=backend.stream)
        attachment = cg.Attachment(
            "image", "image/png", len(PNG), "/tmp/photo.png", "photo.png"
        )
        with mock.patch.object(cg, "BACKEND", backend):
            self.assertEqual(cg.brain("metadata-only", "s", 1), "ok")
            self.assertEqual(list(cg.brain_stream("metadata-only", "s", 1)), ["ok"])
            self.assertEqual(
                cg.brain("metadata-only", "s", 1, attachments=[attachment]),
                "ok",
            )
            self.assertEqual(
                list(cg.brain_stream(
                    "metadata-only", "s", 1, attachments=[attachment]
                )),
                ["ok"],
            )
        self.assertEqual(backend.calls, [
            ("generate", "metadata-only"),
            ("stream", "metadata-only"),
            ("generate", "metadata-only"),
            ("stream", "metadata-only"),
        ])
        for call in backend.generate.call_args_list + backend.stream.call_args_list:
            self.assertNotIn("attachments", call.kwargs)

    def test_t0_missing_plan_with_binary_present_degrades_to_metadata_only(self):
        entry = cg._attachment_plan_entry(
            None, "generate", "question", binary_present=True
        )
        self.assertIsInstance(entry, cg.MetadataOnly)
        self.assertIn("添付ファイルを配達できませんでした。", entry.brain_text)

    def test_t0_rejected_image_never_enters_text_extraction(self):
        rejected = share_store.Rejected(
            "mime-rejected", "fake.png", 12, "image"
        )
        with mock.patch.object(cg, "_extract_share_text") as extract:
            _text, plan = cg._compose_share_turn(rejected, "question", "req-r")
        extract.assert_not_called()
        self.assertTrue(all(
            isinstance(plan[transport], cg.MetadataOnly)
            for transport in ("generate", "stream")
        ))
        self.assertTrue(all(
            plan[transport].reason == "mime-rejected"
            for transport in ("generate", "stream")
        ))

    def test_t1_capability_states_cover_static_and_dynamic_reasons(self):
        attachment = cg.Attachment("image", "image/png", len(PNG), "/tmp/a", "a.png")
        cases = (
            (NonDeclaringBackend(), "generate", {"attachment_passthrough": ""}, "unsupported-backend"),
            (DeclaringBackend(transports=("generate",)), "stream", {"attachment_passthrough": ""}, "transport-unsupported"),
            (DeclaringBackend(mimes=("image/jpeg",)), "generate", {"attachment_passthrough": ""}, "mime-rejected"),
            (DeclaringBackend(max_bytes=1), "generate", {"attachment_passthrough": ""}, "size-over"),
            (DeclaringBackend(), "generate", {"attachment_passthrough": "off"}, "runtime-override"),
            (DeclaringBackend(), "generate", {"attachment_passthrough": "on"}, "runtime-override"),
            (DeclaringBackend(), "generate", {"attachment_passthrough": ""}, "default-on"),
        )
        for backend, transport, config, reason in cases:
            with self.subTest(reason=reason), mock.patch.object(cg, "BACKEND", backend):
                enabled, supported, actual = cg._attachment_passthrough_effective_state(
                    config, transport, attachment
                )
                self.assertEqual(actual, reason)
                self.assertIsInstance(enabled, bool)
                self.assertIsInstance(supported, bool)

    def test_t4_size_mime_runtime_and_temp_failures_are_metadata_only(self):
        cases = (
            (DeclaringBackend(mimes=("image/jpeg",)), self.claimed(), {}, "mime-rejected"),
            (DeclaringBackend(max_bytes=1), self.claimed(), {}, "size-over"),
            (DeclaringBackend(), self.claimed(), {"attachment_passthrough": "off"}, "runtime-override"),
        )
        for backend, claimed, config, reason in cases:
            with self.subTest(reason=reason):
                claimed, plan = self.prepare(backend, claimed, config)
                self.assertTrue(all(isinstance(plan[t], cg.MetadataOnly) for t in ("generate", "stream")))
                self.assertEqual({plan[t].reason for t in ("generate", "stream")}, {reason})
                self.assertFalse(os.path.exists(claimed.path))

        staging = os.path.join(self.tmp.name, "staging")
        backend = DeclaringBackend(staging_dir=staging)
        claimed = self.claimed()
        with mock.patch.object(cg, "BACKEND", backend), mock.patch.object(
            cg, "resolved_config", return_value={"attachment_passthrough": ""}
        ), mock.patch.object(
            cg, "_copy_attachment_to_staging", side_effect=PermissionError("denied")
        ):
            plan = cg._prepare_binary_attachment(claimed, "question", "req-temp")
        self.assertTrue(all(plan[t].reason == "temp-write-failed" for t in ("generate", "stream")))
        self.assertFalse(os.path.exists(claimed.path))

    def test_t3_stream_failure_reuses_claimed_file_for_generate_fallback(self):
        backend = DeclaringBackend()
        claimed, plan = self.prepare(backend)
        job = cg.Job("question", "session-a")
        job.binary_attachment_present = True
        job.add_cleanup(lambda: cg._unlink_attachment_path(claimed.path))

        original_generate = backend.generate

        def generate(text, session_id, timeout, route=None, attachments=None):
            self.assertTrue(os.path.exists(attachments[0].path))
            with open(attachments[0].path, "rb") as stream:
                self.assertEqual(stream.read(), PNG)
            return original_generate(text, session_id, timeout, route, attachments)

        backend.generate = generate

        def push_audio(_text, target_job):
            target_job.push(b"audio")
            return 5

        with mock.patch.object(cg, "BACKEND", backend), mock.patch.object(
            cg, "stream_tts_effective_state", return_value=(True, True, "test")
        ), mock.patch.object(cg, "tts_stream_to_job", side_effect=push_audio), mock.patch.object(
            cg.history_store, "append_turn"
        ):
            cg.stream_pipeline(job, "question", time.time(), plan=plan)

        self.assertEqual([call[0] for call in backend.calls], ["stream", "generate"])
        self.assertEqual(backend.calls[0][2][0].path, backend.calls[1][2][0].path)
        self.assertTrue(job.done)
        self.assertFalse(os.path.exists(claimed.path))

    def test_mixed_transport_plan_uses_generate_entry_for_legacy_fallback(self):
        cases = (
            (("generate",), cg.Delivery, cg.MetadataOnly, True),
            (("stream",), cg.MetadataOnly, cg.Delivery, False),
        )
        for transports, generate_type, stream_type, generate_has_attachments in cases:
            with self.subTest(transports=transports):
                backend = DeclaringBackend(transports=transports)
                claimed, plan = self.prepare(backend)
                job = cg.Job("question", "session-a")
                job.binary_attachment_present = True
                job.add_cleanup(lambda: cg._unlink_attachment_path(claimed.path))

                def push_audio(_text, target_job):
                    target_job.push(b"audio")
                    return 5

                with mock.patch.object(cg, "BACKEND", backend), mock.patch.object(
                    cg, "stream_tts_effective_state", return_value=(True, True, "test")
                ), mock.patch.object(
                    cg, "tts_stream_to_job", side_effect=push_audio
                ), mock.patch.object(cg.history_store, "append_turn"):
                    cg.stream_pipeline(job, "question", time.time(), plan=plan)

                self.assertIsInstance(plan["generate"], generate_type)
                self.assertIsInstance(plan["stream"], stream_type)
                self.assertEqual(
                    [call[0] for call in backend.calls], ["stream", "generate"]
                )
                generate_call = backend.calls[1]
                self.assertEqual(bool(generate_call[2]), generate_has_attachments)
                self.assertEqual(generate_call[1], plan["generate"].brain_text)
                self.assertEqual(job.reply, "fallback")
                self.assertTrue(job.done)
                self.assertFalse(os.path.exists(claimed.path))

    def test_t9_job_cleanup_is_once_isolated_and_not_run_by_purge(self):
        path = os.path.join(self.tmp.name, "claimed")
        with open(path, "wb") as stream:
            stream.write(PNG)
        calls = []
        job = cg.Job("question", "session-a")
        callback = lambda: (calls.append("unlink"), cg._unlink_attachment_path(path))
        job.add_cleanup(callback)
        job.add_cleanup(callback)
        job.add_cleanup(lambda: (_ for _ in ()).throw(RuntimeError("cleanup failed")))
        job.add_cleanup(lambda: calls.append("after-failure"))
        job.created = 0
        job.ttl = 0
        cg.JOBS["old"] = job

        cg._purge_jobs()
        self.assertNotIn("old", cg.JOBS)
        self.assertTrue(os.path.exists(path))

        with mock.patch.object(cg.history_store, "append_turn"):
            job.finish()
            job.finish()
        self.assertEqual(calls, ["unlink", "after-failure"])
        self.assertFalse(os.path.exists(path))

    def test_startup_claim_cleanup_runs_before_server_construction(self):
        events = []

        class PairingConfig:
            allow_nontailnet = False

        class PairingStore:
            def start_sweeper(self):
                pass

        class Server:
            def __init__(self, *_args):
                events.append("server")

            def serve_forever(self):
                raise KeyboardInterrupt()

        class Readiness:
            def start(self):
                pass

        with mock.patch.object(sys, "argv", ["caty_gateway.py"]), mock.patch.object(
            cg, "_get_pairing_config", return_value=PairingConfig()
        ), mock.patch.object(cg, "_get_pairing_store", return_value=PairingStore()), mock.patch.object(
            cg.share_store, "cleanup_claimed_orphans",
            side_effect=lambda _root: events.append("cleanup"),
        ), mock.patch.object(cg, "_get_neutral_voice_readiness", return_value=Readiness()), mock.patch.object(
            cg, "_GatewayHTTPServer", Server
        ), mock.patch.object(cg, "load_fillers"), mock.patch.object(
            cg, "report_content_logging_mode"
        ), mock.patch.object(cg, "_pairing_token_configured", return_value=True), mock.patch.object(
            cg, "CATY_TOKEN", "token"
        ):
            cg.main()

        self.assertEqual(events[:2], ["cleanup", "server"])


class SeeAttachmentRouteTest(unittest.TestCase):
    request = smoke_helpers.GatewaySmokeTest.request

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="caty-see-attachments-")
        self.store = share_store.ShareStore(
            os.path.join(self.tmp.name, "spool"),
            sweep_interval_seconds=0,
        )
        self.old_store = cg._share_store
        self.old_tokens = (cg.CATY_TOKEN, cg.CATY_ADMIN_TOKEN)
        self.old_jobs = dict(cg.JOBS)
        cg._share_store = self.store
        cg.CATY_TOKEN = ""
        cg.CATY_ADMIN_TOKEN = ""
        cg.JOBS.clear()

    def tearDown(self):
        cg.JOBS.clear()
        cg.JOBS.update(self.old_jobs)
        cg.CATY_TOKEN, cg.CATY_ADMIN_TOKEN = self.old_tokens
        cg._share_store = self.old_store
        self.store.close()
        self.tmp.cleanup()

    def multipart(self, image=PNG):
        boundary = "----catysee" + uuid.uuid4().hex
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="audio"; filename="turn.m4a"\r\n'
            "Content-Type: audio/mp4\r\n\r\n"
        ).encode() + b"audio-bytes" + (
            f"\r\n--{boundary}\r\n"
            'Content-Disposition: form-data; name="image"; filename="screen.png"\r\n'
            "Content-Type: image/png\r\n\r\n"
        ).encode() + image + f"\r\n--{boundary}--\r\n".encode()
        return body, f"multipart/form-data; boundary={boundary}"

    def post_see(self, image=PNG):
        body, content_type = self.multipart(image)
        return self.request(
            "POST", "/see", body=body,
            headers={"Content-Type": content_type},
        )

    def wait_done(self, job):
        deadline = time.time() + 5
        while time.time() < deadline and not job.done:
            time.sleep(0.01)
        self.assertTrue(job.done)

    @staticmethod
    def push_audio(_text, job):
        job.push(b"audio")
        return len(job.chunks[-1])

    def test_t5_attachment_capable_see_delivers_frame_until_job_finish(self):
        backend = BlockingDeclaringBackend()
        self.addCleanup(backend.release.set)
        with mock.patch.object(cg, "BACKEND", backend), mock.patch.object(
            cg, "resolved_config", return_value={"attachment_passthrough": ""}
        ), mock.patch.object(
            cg, "to_wav16k", side_effect=lambda path: path
        ), mock.patch.object(
            cg, "stt", return_value="screen question"
        ), mock.patch.object(
            cg, "stream_tts_effective_state", return_value=(True, True, "test")
        ), mock.patch.object(
            cg, "tts_stream_to_job", side_effect=self.push_audio
        ), mock.patch.object(cg.history_store, "append_turn"):
            status, headers, body = self.post_see()
            payload = json.loads(body)
            self.assertEqual(status, 200)
            self.assertEqual(set(payload), {"id", "transcript"})
            self.assertEqual(payload["transcript"], "screen question")
            self.assertEqual(headers["x-transcript"], "screen question")
            self.assertTrue(backend.entered.wait(1))
            attachment = backend.calls[0][2][0]
            self.assertEqual(attachment.kind, "image")
            self.assertEqual(attachment.mime, "image/png")
            self.assertEqual(attachment.filename, "screen.png")
            self.assertTrue(os.path.exists(attachment.path))
            with open(attachment.path, "rb") as stream:
                self.assertEqual(stream.read(), PNG)

            job = cg.JOBS[payload["id"]]
            self.assertFalse(job.done)
            backend.release.set()
            self.wait_done(job)
            self.assertFalse(os.path.exists(attachment.path))

    def test_t5_non_declaring_see_is_metadata_only_and_unlinks_immediately(self):
        backend = BlockingNonDeclaringBackend()
        backend.generate = mock.Mock(wraps=backend.generate)
        self.addCleanup(backend.release.set)
        with mock.patch.object(cg, "BACKEND", backend), mock.patch.object(
            cg, "resolved_config", return_value={"attachment_passthrough": ""}
        ), mock.patch.object(
            cg, "to_wav16k", side_effect=lambda path: path
        ), mock.patch.object(
            cg, "stt", return_value="これは何？"
        ), mock.patch.object(
            cg, "stream_tts_effective_state", return_value=(False, True, "test")
        ), mock.patch.object(
            cg, "tts_stream_to_job", side_effect=self.push_audio
        ), mock.patch.object(cg.history_store, "append_turn"):
            status, _, body = self.post_see()
            payload = json.loads(body)
            self.assertEqual(status, 200)
            self.assertTrue(backend.entered.wait(1))
            self.assertEqual(os.listdir(self.store.claimed_dir), [])
            call = backend.generate.call_args
            self.assertNotIn("attachments", call.kwargs)
            brain_text = call.args[0]
            self.assertIn("【いまユーザーが見ている画面】", brain_text)
            self.assertIn("画面の内容を確認できませんでした。", brain_text)
            self.assertIn("【ユーザーの質問】\nこれは何？", brain_text)

            backend.release.set()
            self.wait_done(cg.JOBS[payload["id"]])

    def test_t5_stt_empty_returns_204_without_staging_frame(self):
        with mock.patch.object(
            cg, "to_wav16k", side_effect=lambda path: path
        ), mock.patch.object(cg, "stt", return_value=""), mock.patch.object(
            self.store, "stage_claimed_bytes", wraps=self.store.stage_claimed_bytes
        ) as stage:
            status, _, body = self.post_see()

        self.assertEqual(status, 204)
        self.assertEqual(body, b"")
        stage.assert_not_called()
        self.assertEqual(os.listdir(self.store.claimed_dir), [])

    def test_t5_stt_error_returns_500_without_staging_frame(self):
        with mock.patch.object(
            cg, "to_wav16k", side_effect=lambda path: path
        ), mock.patch.object(
            cg, "stt", side_effect=RuntimeError("stt failed")
        ), mock.patch.object(
            self.store, "stage_claimed_bytes", wraps=self.store.stage_claimed_bytes
        ) as stage:
            status, _, _ = self.post_see()

        self.assertEqual(status, 500)
        stage.assert_not_called()
        self.assertEqual(os.listdir(self.store.claimed_dir), [])

    def test_t5_purge_error_unlinks_handler_owned_claim(self):
        backend = DeclaringBackend()
        with mock.patch.object(cg, "BACKEND", backend), mock.patch.object(
            cg, "resolved_config", return_value={"attachment_passthrough": ""}
        ), mock.patch.object(
            cg, "to_wav16k", side_effect=lambda path: path
        ), mock.patch.object(
            cg, "stt", return_value="これは何？"
        ), mock.patch.object(
            cg, "_purge_jobs", side_effect=RuntimeError("purge failed")
        ):
            status, _, _ = self.post_see()

        self.assertEqual(status, 500)
        self.assertEqual(os.listdir(self.store.claimed_dir), [])
        self.assertEqual(cg.JOBS, {})

    def test_t5_sniff_mismatch_uses_screen_metadata_only_without_claim(self):
        backend = NonDeclaringBackend()
        with mock.patch.object(cg, "BACKEND", backend), mock.patch.object(
            cg, "to_wav16k", side_effect=lambda path: path
        ), mock.patch.object(
            cg, "stt", return_value="読める？"
        ), mock.patch.object(
            cg, "stream_tts_effective_state", return_value=(False, True, "test")
        ), mock.patch.object(
            cg, "tts_stream_to_job", side_effect=self.push_audio
        ), mock.patch.object(cg.history_store, "append_turn"):
            status, _, body = self.post_see(b"not-an-image")
            payload = json.loads(body)
            self.assertEqual(status, 200)
            self.wait_done(cg.JOBS[payload["id"]])

        self.assertEqual(os.listdir(self.store.claimed_dir), [])
        brain_text = backend.calls[0][1]
        self.assertIn("【いまユーザーが見ている画面】", brain_text)
        self.assertIn("画面の内容を確認できませんでした。", brain_text)
        self.assertIn("【ユーザーの質問】\n読める？", brain_text)

    def test_t5_handler_handoff_preserves_frame_and_removes_audio_temps(self):
        entered = threading.Event()
        release = threading.Event()
        captured = {}
        temp_paths = []
        original_temp = cg._temp_path_with_bytes
        self.addCleanup(release.set)

        def tracked_temp(data, suffix):
            path = original_temp(data, suffix)
            temp_paths.append(path)
            return path

        def converted_wav(_src):
            path = original_temp(b"wav", ".wav")
            temp_paths.append(path)
            return path

        def held_pipeline(job, text, t0, route=None, plan=None):
            captured["job"] = job
            captured["plan"] = plan
            entered.set()
            release.wait(5)
            job.finish()

        backend = DeclaringBackend()
        with mock.patch.object(cg, "BACKEND", backend), mock.patch.object(
            cg, "resolved_config", return_value={"attachment_passthrough": ""}
        ), mock.patch.object(
            cg, "_temp_path_with_bytes", side_effect=tracked_temp
        ), mock.patch.object(
            cg, "to_wav16k", side_effect=converted_wav
        ), mock.patch.object(
            cg, "stt", return_value="これは何？"
        ), mock.patch.object(
            cg, "stream_pipeline", side_effect=held_pipeline
        ), mock.patch.object(cg.history_store, "append_turn"):
            status, _, body = self.post_see()
            payload = json.loads(body)
            self.assertEqual(status, 200)
            self.assertTrue(entered.wait(1))
            attachment = captured["plan"].stream.attachments[0]
            self.assertTrue(os.path.exists(attachment.path))
            self.assertTrue(temp_paths)
            self.assertTrue(all(not os.path.exists(path) for path in temp_paths))

            release.set()
            self.wait_done(cg.JOBS[payload["id"]])
            self.assertFalse(os.path.exists(attachment.path))

    def test_t5_worker_start_failure_finishes_job_and_removes_claim(self):
        class FailingThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("worker start failed")

        backend = DeclaringBackend()
        jobs = []
        real_job = cg.Job

        def capture_job(*args, **kwargs):
            job = real_job(*args, **kwargs)
            jobs.append(job)
            return job

        with mock.patch.object(cg, "BACKEND", backend), mock.patch.object(
            cg, "resolved_config", return_value={"attachment_passthrough": ""}
        ), mock.patch.object(
            cg, "to_wav16k", side_effect=lambda path: path
        ), mock.patch.object(
            cg, "stt", return_value="これは何？"
        ), mock.patch.object(
            cg.threading, "Thread", FailingThread
        ), mock.patch.object(
            cg, "Job", side_effect=capture_job
        ), mock.patch.object(cg.history_store, "append_turn"):
            status, _, _ = self.post_see()

        self.assertEqual(status, 500)
        self.assertEqual(cg.JOBS, {})
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0].done)
        self.assertEqual(jobs[0].error, "worker start failed")
        self.assertEqual(os.listdir(self.store.claimed_dir), [])

    def test_t5_broken_pipe_after_worker_start_leaves_job_with_worker(self):
        backend = BlockingDeclaringBackend()
        original_send = cg.Handler._send
        self.addCleanup(backend.release.set)

        def broken_success_send(
            handler, code, body=b"", ctype="application/json", extra=None
        ):
            original_send(handler, code, body, ctype, extra)
            if code == 200:
                raise BrokenPipeError("client disconnected")

        with mock.patch.object(cg, "BACKEND", backend), mock.patch.object(
            cg, "resolved_config", return_value={"attachment_passthrough": ""}
        ), mock.patch.object(
            cg, "to_wav16k", side_effect=lambda path: path
        ), mock.patch.object(
            cg, "stt", return_value="これは何？"
        ), mock.patch.object(
            cg, "stream_tts_effective_state", return_value=(True, True, "test")
        ), mock.patch.object(
            cg, "tts_stream_to_job", side_effect=self.push_audio
        ), mock.patch.object(
            cg.Handler, "_send", new=broken_success_send
        ), mock.patch.object(cg.history_store, "append_turn") as append_turn, mock.patch.object(
            cg, "log"
        ) as log:
            status, _, body = self.post_see()
            payload = json.loads(body)
            job = cg.JOBS[payload["id"]]

            self.assertEqual(status, 200)
            self.assertTrue(backend.entered.wait(1))
            attachment = backend.calls[0][2][0]
            self.assertFalse(job.done)
            self.assertIsNone(job.error)
            self.assertTrue(os.path.exists(attachment.path))
            append_turn.assert_not_called()
            self.assertTrue(any(
                "stage=respond status=error error_type=BrokenPipeError"
                in " ".join(str(value) for value in call.args)
                for call in log.call_args_list
            ))

            backend.release.set()
            self.wait_done(job)
            self.assertIsNone(job.error)
            self.assertFalse(os.path.exists(attachment.path))
            self.assertEqual(
                [call.args[1] for call in append_turn.call_args_list],
                ["user", "assistant"],
            )


if __name__ == "__main__":
    unittest.main()
