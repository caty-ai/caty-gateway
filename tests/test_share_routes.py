import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import uuid
from contextlib import ExitStack
from io import BytesIO
from unittest import mock


from caty_gateway import caty_gateway as cg
from caty_gateway import share_store


PNG = b"\x89PNG\r\n\x1a\n" + b"image-payload"
JPEG = b"\xff\xd8\xff" + b"image-payload"
PDF = b"%PDF-1.7\nfile-payload"


class NonClosingBytesIO(BytesIO):
    def close(self):
        pass


class MemorySocket:
    def __init__(self, request_bytes):
        self.input = BytesIO(request_bytes)
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


class ImmediateThread:
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


def multipart(fields, file_mime="application/octet-stream"):
    boundary = "----catyshare" + uuid.uuid4().hex
    chunks = []
    for name, data in fields.items():
        chunks.append(f"--{boundary}\r\n".encode())
        disposition = f'Content-Disposition: form-data; name="{name}"'
        content_type = "text/plain; charset=utf-8"
        if name == "file":
            disposition += '; filename="upload.bin"'
            content_type = file_mime
        chunks.append(
            f"{disposition}\r\nContent-Type: {content_type}\r\n\r\n".encode()
        )
        chunks.append(data)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class ShareRoutesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="caty-share-routes-")
        self.store = share_store.ShareStore(
            os.path.join(self.tmp.name, "spool"),
            ttl_seconds=cg.SHARE_TTL_SECONDS,
            sweep_interval_seconds=0,
        )
        self._saved = (cg.CATY_TOKEN, cg.CATY_ADMIN_TOKEN)
        self.old_store = cg._share_store
        cg.CATY_TOKEN = "member-token"
        cg.CATY_ADMIN_TOKEN = ""
        cg._share_store = self.store
        cg.JOBS.clear()
        self.captured_turns = []

    def tearDown(self):
        cg.CATY_TOKEN, cg.CATY_ADMIN_TOKEN = self._saved
        cg._share_store = self.old_store
        cg.JOBS.clear()
        self.store.close()
        self.tmp.cleanup()

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

    def post_share(
        self,
        data=b"hello file",
        kind="file",
        session_id="session-a",
        filename="notes.txt",
        mime="text/plain",
        token="member-token",
        idempotency_key=None,
        fields=None,
        extra_headers=None,
    ):
        if fields is None:
            fields = {
                "file": data,
                "kind": kind.encode(),
                "session_id": session_id.encode(),
                "filename": filename.encode(),
            }
        body, content_type = multipart(fields, file_mime=mime)
        headers = {"Content-Type": content_type}
        if token is not None:
            headers["X-Caty-Token"] = token
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        headers.update(extra_headers or {})
        status, response_headers, response_body = self.request(
            "POST", "/share", body=body, headers=headers
        )
        return status, response_headers, json.loads(response_body), body, headers

    def stage(self, **changes):
        values = {
            "session_id": "session-a",
            "kind": "file",
            "filename": "notes.txt",
            "mime": "text/plain",
            "data": b"hello attachment",
        }
        values.update(changes)
        return self.store.put(**values)["share_id"]

    def fake_pipeline(self, job, brain_text, _started, route=None, plan=None):
        entry = plan["stream"] if plan is not None else None
        self.captured_turns.append(
            entry.brain_text if entry is not None else brain_text
        )
        job.update_reply("ok")
        job.finish()

    def talk_with_share(
        self,
        share_id,
        text="question",
        session_id="session-a",
        pipeline_side_effect=None,
        patch_thread_class=ImmediateThread,
    ):
        headers = {
            "X-Caty-Token": "member-token",
            "X-Caty-Share-Id": share_id,
        }
        if session_id is not None:
            headers["X-Session-Id"] = session_id
        if text is not None:
            headers["X-Caty-Text"] = urllib.parse.quote(text)
        if pipeline_side_effect is None:
            pipeline_side_effect = self.fake_pipeline
        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(
                    cg, "stream_pipeline", side_effect=pipeline_side_effect
                )
            )
            if patch_thread_class is not None:
                stack.enter_context(
                    mock.patch.object(cg.threading, "Thread", patch_thread_class)
                )
            return self.request("POST", "/talk2", headers=headers)

    def test_share_write_auth_is_fail_closed_and_rejects_wrong_token(self):
        cg.CATY_TOKEN = ""
        status, _, payload, _, _ = self.post_share(token=None)
        self.assertEqual(status, 403)
        self.assertEqual(
            payload, {"ok": False, "error": "writes disabled: no token configured"}
        )

        cg.CATY_TOKEN = "member-token"
        status, _, payload, _, _ = self.post_share(token="wrong")
        self.assertEqual(status, 401)
        self.assertEqual(payload, {"ok": False, "error": "unauthorized"})

    def test_share_happy_path_image_and_file(self):
        cases = (
            ("image", PNG, "screen.png", "image/png"),
            ("file", b"hello file", "notes.txt", "text/plain"),
        )
        for kind, data, filename, mime in cases:
            with self.subTest(kind=kind):
                status, _, payload, _, _ = self.post_share(
                    data=data, kind=kind, filename=filename, mime=mime
                )
                self.assertEqual(status, 200)
                self.assertTrue(payload["ok"])
                self.assertRegex(payload["share_id"], r"^[0-9a-f]{32}$")
                self.assertGreater(payload["expires_at"], time.time())
                consumed = self.store.consume(payload["share_id"], "session-a")
                self.assertEqual(consumed["data"], data)
                self.assertEqual(consumed["mime"], mime)
                self.assertEqual(consumed["filename"], filename)

    def test_share_rejects_missing_required_parts(self):
        cases = (
            ({"kind": b"file", "session_id": b"s"}, "missing file"),
            ({"file": b"data", "session_id": b"s"}, "missing kind"),
            ({"file": b"data", "kind": b"file"}, "missing session_id"),
        )
        for fields, error in cases:
            with self.subTest(error=error):
                status, _, payload, _, _ = self.post_share(fields=fields)
                self.assertEqual(status, 400)
                self.assertEqual(payload, {"ok": False, "error": error})

    def test_share_rejects_per_kind_and_envelope_oversize(self):
        with mock.patch.object(cg, "SHARE_IMAGE_LIMIT", len(PNG) - 1):
            status, _, payload, _, _ = self.post_share(
                data=PNG, kind="image", mime="image/png"
            )
            self.assertEqual(status, 413)
            self.assertEqual(payload["error"], "payload too large")

        with mock.patch.object(cg, "SHARE_FILE_LIMIT", 4):
            status, _, payload, _, _ = self.post_share(data=b"12345")
            self.assertEqual(status, 413)
            self.assertEqual(payload["error"], "payload too large")

        body, content_type = multipart({
            "file": b"data", "kind": b"file", "session_id": b"session-a"
        })
        with mock.patch.object(cg, "SHARE_BODY_LIMIT", len(body) - 1):
            status, _, response = self.request(
                "POST",
                "/share",
                body=body,
                headers={
                    "Content-Type": content_type,
                    "X-Caty-Token": "member-token",
                },
            )
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(response)["error"], "payload too large")

    def test_share_rejects_bad_image_magic(self):
        status, _, payload, _, _ = self.post_share(
            data=b"not an image", kind="image", mime="image/png"
        )
        self.assertEqual(status, 415)
        self.assertEqual(payload, {"ok": False, "error": "unsupported media type"})

    def test_share_idempotency_conflict_and_reuse(self):
        first = self.post_share(idempotency_key="idem-1")[2]
        second = self.post_share(idempotency_key="idem-1")[2]
        self.assertEqual(first["share_id"], second["share_id"])

        status, _, payload, _, _ = self.post_share(
            data=b"changed", idempotency_key="idem-1"
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload, {"ok": False, "error": "idempotency conflict"})

    def test_share_route_enforces_live_quota_and_recovers_after_consume_and_expiry(self):
        share_ids = []
        for index in range(4):
            status, _, payload, _, _ = self.post_share(
                filename=f"notes-{index}.txt",
                idempotency_key=f"idem-{index}",
            )
            self.assertEqual(status, 200)
            share_ids.append(payload["share_id"])

        status, _, payload, _, _ = self.post_share(
            filename="overflow.txt",
            idempotency_key="overflow",
        )
        self.assertEqual(status, 429)
        self.assertEqual(payload, {"ok": False, "error": "too many staged shares"})

        self.store.consume(share_ids[0], "session-a")
        status, _, payload, _, _ = self.post_share(
            filename="after-consume.txt",
            idempotency_key="after-consume",
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

        self.store._metadata[share_ids[1]]["created_at"] = time.time() - 901
        status, _, payload, _, _ = self.post_share(
            filename="after-expiry.txt",
            idempotency_key="after-expiry",
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_share_route_returns_500_for_staging_integrity_failure(self):
        with mock.patch.object(
            self.store,
            "_validate_staged",
            side_effect=share_store.ShareStagingError("staging failed"),
        ):
            status, _, payload, _, _ = self.post_share()

        self.assertEqual(status, 500)
        self.assertEqual(payload, {"ok": False, "error": "share staging failed"})

    def test_share_rejects_unsafe_filename_and_idempotency_key(self):
        for filename in ("../secret", "folder/secret", "folder\\secret", "bad\x00name"):
            with self.subTest(filename=filename):
                status, _, payload, _, _ = self.post_share(filename=filename)
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"], "invalid filename")
        status, _, payload, _, _ = self.post_share(idempotency_key="x" * 129)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid idempotency key")

    def test_talk2_returns_http_200_before_attachment_preflight_finishes(self):
        share_id = self.stage(
            kind="image", filename="screen.png", mime="image/png", data=PNG
        )
        preflight_started = threading.Event()
        release_preflight = threading.Event()
        pipeline_finished = threading.Event()
        result = {}
        real_prepare = cg._prepare_binary_attachment

        def delayed_prepare(*args):
            preflight_started.set()
            self.assertTrue(release_preflight.wait(2))
            return real_prepare(*args)

        def fake_pipeline(job, brain_text, _started, route=None, plan=None):
            self.captured_turns.append(plan["stream"].brain_text)
            job.update_reply("ok")
            job.finish()
            pipeline_finished.set()

        def run_request():
            result["response"] = self.request(
                "POST",
                "/talk2",
                headers={
                    "X-Caty-Token": "member-token",
                    "X-Session-Id": "session-a",
                    "X-Caty-Text": urllib.parse.quote("これは何？"),
                    "X-Caty-Share-Id": share_id,
                },
            )

        with mock.patch.object(cg, "_prepare_binary_attachment", side_effect=delayed_prepare), \
                mock.patch.object(cg, "stream_pipeline", side_effect=fake_pipeline):
            request_thread = threading.Thread(target=run_request)
            request_thread.start()
            try:
                self.assertTrue(preflight_started.wait(1))
                request_thread.join(0.5)
                self.assertFalse(request_thread.is_alive())

                status, headers, body = result["response"]
                payload = json.loads(body)
                self.assertEqual(status, 200)
                self.assertEqual(payload["transcript"], "これは何？")
                self.assertIn(payload["id"], cg.JOBS)
                self.assertFalse(cg.JOBS[payload["id"]].done)
                self.assertFalse(pipeline_finished.is_set())
                self.assertIn("x-transcript", headers)
            finally:
                release_preflight.set()
                request_thread.join(1)
            self.assertTrue(pipeline_finished.wait(1))
            self.assertIn("【ユーザーが添付した画像】", self.captured_turns[0])

    def test_talk2_share_composition_failure_finishes_job_with_error(self):
        share_id = self.stage()

        with mock.patch.object(
            cg, "_compose_share_turn", side_effect=RuntimeError("compose failed")
        ):
            status, _, body = self.talk_with_share(share_id)

        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertIn("id", payload)
        job = cg.JOBS[payload["id"]]
        self.assertTrue(job.done)
        self.assertEqual(job.error, "compose failed")

    def test_talk2_worker_start_failure_finishes_job_and_unlinks_claim(self):
        share_id = self.stage(
            kind="image", filename="screen.png", mime="image/png", data=PNG
        )
        jobs = []
        real_job = cg.Job

        def capture_job(*args, **kwargs):
            job = real_job(*args, **kwargs)
            jobs.append(job)
            return job

        with mock.patch.object(cg, "Job", side_effect=capture_job), mock.patch.object(
            cg.threading.Thread, "start", side_effect=RuntimeError("no thread")
        ):
            status, _, body = self.talk_with_share(
                share_id, patch_thread_class=None
            )

        self.assertEqual(status, 500)
        self.assertEqual(
            json.loads(body),
            {"ok": False, "error": "share worker start failed"},
        )
        self.assertEqual(cg.JOBS, {})
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0].done)
        self.assertEqual(jobs[0].error, "no thread")
        self.assertEqual(os.listdir(self.store.claimed_dir), [])

    def test_talk2_broken_pipe_after_worker_start_leaves_job_with_worker(self):
        share_id = self.stage(
            kind="image", filename="screen.png", mime="image/png", data=PNG
        )
        entered = threading.Event()
        release = threading.Event()
        captured = {}
        original_send = cg.Handler._send
        self.addCleanup(release.set)

        class AttachmentBackend:
            def attachment_transports(self):
                return frozenset({"generate", "stream"})

            def supported_attachment_mimes(self):
                return frozenset({"image/png"})

            def attachment_max_bytes(self):
                return None

            def attachment_staging_dir(self):
                return None

        def held_pipeline(job, _text, _started, route=None, plan=None):
            captured["job"] = job
            captured["path"] = plan.stream.attachments[0].path
            entered.set()
            release.wait(5)
            job.update_reply("ok")
            job.finish()

        def broken_success_send(
            handler, code, body=b"", ctype="application/json", extra=None
        ):
            original_send(handler, code, body, ctype, extra)
            if code == 200:
                raise BrokenPipeError("client disconnected")

        with mock.patch.object(cg, "BACKEND", AttachmentBackend()), mock.patch.object(
            cg, "resolved_config", return_value={"attachment_passthrough": ""}
        ), mock.patch.object(
            cg, "stream_pipeline", side_effect=held_pipeline
        ), mock.patch.object(
            cg.Handler, "_send", new=broken_success_send
        ), mock.patch.object(cg.history_store, "append_turn") as append_turn, mock.patch.object(
            cg, "log"
        ) as log:
            status, _, body = self.request(
                "POST",
                "/talk2",
                headers={
                    "X-Caty-Token": "member-token",
                    "X-Session-Id": "session-a",
                    "X-Caty-Text": urllib.parse.quote("question"),
                    "X-Caty-Share-Id": share_id,
                },
            )
            payload = json.loads(body)

            self.assertEqual(status, 200)
            self.assertTrue(entered.wait(1))
            job = captured["job"]
            self.assertFalse(job.done)
            self.assertIsNone(job.error)
            self.assertIs(cg.JOBS[payload["id"]], job)
            self.assertTrue(os.path.exists(captured["path"]))
            append_turn.assert_not_called()
            self.assertTrue(any(
                "stage=respond status=error error_type=BrokenPipeError"
                in " ".join(str(value) for value in call.args)
                for call in log.call_args_list
            ))

            release.set()
            deadline = time.time() + 5
            while time.time() < deadline and not job.done:
                time.sleep(0.01)
            self.assertTrue(job.done)
            self.assertIsNone(job.error)
            self.assertFalse(os.path.exists(captured["path"]))
            self.assertEqual(
                [call.args[1] for call in append_turn.call_args_list],
                ["user", "assistant"],
            )

    def test_talk2_add_cleanup_failure_keeps_handler_ownership(self):
        share_id = self.stage(
            kind="image", filename="screen.png", mime="image/png", data=PNG
        )

        with mock.patch.object(
            cg.Job, "add_cleanup", side_effect=RuntimeError("registration failed")
        ):
            status, _, body = self.talk_with_share(share_id)

        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body)["error"], "share worker start failed")
        self.assertEqual(cg.JOBS, {})
        self.assertEqual(os.listdir(self.store.claimed_dir), [])

    def test_talk2_purge_failure_keeps_handler_ownership(self):
        share_id = self.stage(
            kind="image", filename="screen.png", mime="image/png", data=PNG
        )
        calls = 0

        def fail_once():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("purge failed")

        with mock.patch.object(cg, "_purge_jobs", side_effect=fail_once):
            status, _, body = self.talk_with_share(share_id)

        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body)["error"], "share worker start failed")
        self.assertEqual(calls, 1)
        self.assertEqual(cg.JOBS, {})
        self.assertEqual(os.listdir(self.store.claimed_dir), [])

    def test_talk2_staging_cleanup_registration_failure_leaves_no_orphan(self):
        share_id = self.stage(
            kind="image", filename="screen.png", mime="image/png", data=PNG
        )
        staging_dir = os.path.join(self.tmp.name, "adapter-staging")

        class StagingBackend:
            def attachment_transports(self):
                return frozenset({"generate", "stream"})

            def supported_attachment_mimes(self):
                return frozenset({"image/png"})

            def attachment_max_bytes(self):
                return None

            def attachment_staging_dir(self):
                return staging_dir

        original_add_cleanup = cg.Job.add_cleanup
        calls = 0

        def fail_second_add(job, callback):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("registration failed")
            return original_add_cleanup(job, callback)

        with mock.patch.object(cg, "BACKEND", StagingBackend()), mock.patch.object(
            cg.Job, "add_cleanup", new=fail_second_add
        ):
            status, _, body = self.talk_with_share(share_id)

        self.assertEqual(status, 200)
        job = cg.JOBS[json.loads(body)["id"]]
        self.assertTrue(job.done)
        self.assertEqual(job.error, "registration failed")
        self.assertEqual(os.listdir(self.store.claimed_dir), [])
        self.assertEqual(os.listdir(staging_dir), [])

    def test_talk2_image_share_uses_metadata_plan_and_preserves_transcript(self):
        share_id = self.stage(
            kind="image", filename="screen.png", mime="image/png", data=PNG
        )
        data_path = os.path.join(self.store.root_dir, share_id)

        # Keep the symbol split so the forbidden-remnant grep stays clean.
        self.assertFalse(hasattr(cg, "describe_" + "image"))
        with mock.patch.object(cg, "BACKEND", object()):
            status, headers, body = self.talk_with_share(share_id, text="これは何？")

        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(set(payload), {"id", "transcript"})
        self.assertEqual(payload["transcript"], "これは何？")
        self.assertIn("【ユーザーが添付した画像】", self.captured_turns[0])
        self.assertIn("画像の内容を確認できませんでした。", self.captured_turns[0])
        self.assertIn("【メッセージ】\nこれは何？", self.captured_turns[0])
        self.assertFalse(os.path.exists(data_path))
        self.assertIn("x-transcript", headers)

    def test_talk2_kind_mismatch_uses_sniffed_mime_in_metadata(self):
        share_id = self.stage(
            kind="file", filename="fallback.bin", mime="text/plain", data=PNG
        )
        status, _, _ = self.talk_with_share(share_id)

        self.assertEqual(status, 200)
        prompt = self.captured_turns[0]
        self.assertIn("【ユーザーが添付した画像】", prompt)
        self.assertIn("ファイル名: fallback.bin", prompt)
        self.assertIn("MIMEタイプ: image/png", prompt)
        self.assertIn(f"サイズ: {len(PNG)} bytes", prompt)
        self.assertIn("画像の内容を確認できませんでした。", prompt)

    def test_talk2_png_jpeg_pdf_all_finish_metadata_only_on_non_attachment_capable(self):
        cases = (
            ("image", "photo.png", "image/png", PNG, "【ユーザーが添付した画像】"),
            ("image", "photo.jpg", "image/jpeg", JPEG, "【ユーザーが添付した画像】"),
            ("file", "document.pdf", "application/pdf", PDF, "【ユーザーが添付したファイル】"),
        )
        for kind, filename, mime, data, heading in cases:
            with self.subTest(mime=mime):
                self.captured_turns.clear()
                share_id = self.stage(
                    kind=kind, filename=filename, mime=mime, data=data
                )
                with mock.patch.object(cg, "BACKEND", object()):
                    status, _, _ = self.talk_with_share(share_id)
                self.assertEqual(status, 200)
                self.assertIn(heading, self.captured_turns[0])
                self.assertIn(f"MIMEタイプ: {mime}", self.captured_turns[0])
                self.assertEqual(os.listdir(self.store.claimed_dir), [])

    def test_talk2_text_file_extracts_utf8_bom_and_marks_truncation(self):
        share_id = self.stage(
            filename="memo.txt",
            mime="text/plain",
            data=b"\xef\xbb\xbfhello utf8",
        )
        status, _, _ = self.talk_with_share(share_id)
        self.assertEqual(status, 200)
        self.assertIn("内容:\nhello utf8", self.captured_turns[0])
        self.assertNotIn("\ufeff", self.captured_turns[0])

        self.captured_turns.clear()
        with mock.patch.object(cg, "SHARE_TEXT_EXTRACT_LIMIT", 5):
            share_id = self.stage(data=b"abcdefghij")
            status, _, _ = self.talk_with_share(share_id)
        self.assertEqual(status, 200)
        self.assertIn("内容:\nabcde", self.captured_turns[0])
        self.assertIn("以降は省略しました", self.captured_turns[0])

        self.captured_turns.clear()
        with mock.patch.object(cg, "SHARE_TEXT_EXTRACT_LIMIT", 5):
            share_id = self.stage(
                data="abcあdef".encode("utf-8"),
                filename="boundary.txt",
                mime="text/plain",
            )
            status, _, _ = self.talk_with_share(share_id)
        self.assertEqual(status, 200)
        self.assertIn("内容:\nabc", self.captured_turns[0])
        self.assertIn("以降は省略しました", self.captured_turns[0])

    def test_talk2_binary_file_uses_metadata_only(self):
        share_id = self.stage(
            filename="archive.bin",
            mime="application/octet-stream",
            data=b"\xff\xfe\x00\x80",
        )
        status, _, _ = self.talk_with_share(share_id)
        self.assertEqual(status, 200)
        prompt = self.captured_turns[0]
        self.assertIn("ファイル名: archive.bin", prompt)
        self.assertIn("内容の抽出には対応していません。", prompt)

        self.captured_turns.clear()
        with mock.patch.object(cg, "SHARE_TEXT_EXTRACT_LIMIT", 4):
            share_id = self.stage(
                filename="boundary.bin",
                mime="application/octet-stream",
                data=b"abc\xffrest",
            )
            status, _, _ = self.talk_with_share(share_id)
        self.assertEqual(status, 200)
        self.assertIn("ファイル名: boundary.bin", self.captured_turns[0])
        self.assertIn("内容の抽出には対応していません。", self.captured_turns[0])

    def test_talk2_attachment_only_turn_keeps_empty_public_transcript(self):
        share_id = self.stage(data=b"attachment only")
        status, headers, body = self.talk_with_share(share_id, text=None)

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["transcript"], "")
        self.assertEqual(headers["x-transcript"], "")
        self.assertNotIn("【メッセージ】", self.captured_turns[0])
        self.assertIn("attachment only", self.captured_turns[0])

    def test_talk2_share_not_found_session_mismatch_and_expiry(self):
        status, _, body = self.talk_with_share("f" * 32)
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"], "share not found")
        self.assertEqual(cg.JOBS, {})

        share_id = self.stage()
        data_path = os.path.join(self.store.root_dir, share_id)
        status, _, body = self.talk_with_share(share_id, session_id="session-b")
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["error"], "share session mismatch")
        self.assertTrue(os.path.exists(data_path))
        self.assertEqual(cg.JOBS, {})

        share_id = self.stage()
        data_path = os.path.join(self.store.root_dir, share_id)
        self.store._metadata[share_id]["created_at"] = time.time() - 901
        status, _, body = self.talk_with_share(share_id)
        self.assertEqual(status, 410)
        self.assertEqual(json.loads(body)["error"], "share expired")
        self.assertFalse(os.path.exists(data_path))
        self.assertEqual(cg.JOBS, {})

    def test_talk2_rejects_empty_or_malformed_share_headers_before_job_creation(self):
        for share_id in ("", "   ", "not-a-share-id", "A" * 32):
            with self.subTest(share_id=share_id):
                status, _, body = self.request(
                    "POST",
                    "/talk2",
                    headers={
                        "X-Caty-Token": "member-token",
                        "X-Session-Id": "session-a",
                        "X-Caty-Text": urllib.parse.quote("question"),
                        "X-Caty-Share-Id": share_id,
                    },
                )
                self.assertEqual(status, 404)
                self.assertEqual(json.loads(body)["error"], "share not found")
                self.assertEqual(cg.JOBS, {})

    def test_talk2_share_header_without_session_id_returns_409(self):
        share_id = self.stage()

        status, _, body = self.talk_with_share(share_id, session_id=None)

        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["error"], "share session mismatch")
        self.assertEqual(
            self.store.consume(share_id, "session-a")["data"],
            b"hello attachment",
        )

    def test_talk2_invalid_session_header_returns_409_without_consuming(self):
        share_id = self.stage()

        status, _, body = self.talk_with_share(share_id, session_id="..")

        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["error"], "share session mismatch")
        self.assertEqual(
            self.store.consume(share_id, "session-a")["data"],
            b"hello attachment",
        )


if __name__ == "__main__":
    unittest.main()
