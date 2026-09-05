import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.parse
from contextlib import redirect_stdout
from unittest import mock


from caty_gateway import caty_gateway as cg
from caty_gateway import share_store
from tests import test_smoke as smoke_helpers


CANARY_TRANSCRIPT = "CANARY_STT_DO_NOT_PERSIST_971"
CANARY_REPLY = "CANARY_REPLY_DO_NOT_PERSIST_971"
CANARY_FRAME = "CANARY_FRAME_BYTES_DO_NOT_PERSIST_971"
CANARY_SECRET = "CANARY_TOKEN_DO_NOT_PERSIST_971"
CANARY_SHARE_FILENAME = "CANARY_FILENAME_DO_NOT_LOG_1061.png"
CANARY_SHARE_CONTENT = "CANARY_FILE_CONTENT_DO_NOT_LOG_1061"
CANARY_SPOOL_COMPONENT = "CANARY_SPOOL_PATH_DO_NOT_LOG_1061"


class FakeBackend:
    agent = "privacy-test"

    def __init__(self, reply=CANARY_REPLY, error=None):
        self.reply = reply
        self.error = error

    def supports_stream(self):
        return False

    def generate(self, _text, _session_id=None, _brain_timeout=180, route=None):
        if self.error is not None:
            raise self.error
        return self.reply


def push_fake_audio(_text, job):
    job.push(b"privacy-test-mp3")
    return len(job.chunks[-1])


class PrivacyLoggingTest(unittest.TestCase):
    request = smoke_helpers.GatewaySmokeTest.request
    wait_for_reply = smoke_helpers.GatewaySmokeTest.wait_for_reply

    def setUp(self):
        self.original_debug_env = os.environ.pop(cg.UNSAFE_CONTENT_LOG_ENV, None)
        cg._reset_content_log_state()
        cg.JOBS.clear()

    def tearDown(self):
        if self.original_debug_env is None:
            os.environ.pop(cg.UNSAFE_CONTENT_LOG_ENV, None)
        else:
            os.environ[cg.UNSAFE_CONTENT_LOG_ENV] = self.original_debug_env
        cg._reset_content_log_state()
        cg.JOBS.clear()

    def capture(self, callback):
        output = io.StringIO()
        with redirect_stdout(output):
            callback()
        return output.getvalue()

    def run_pipeline(self, backend):
        job = cg.Job(CANARY_TRANSCRIPT, session_id="privacy-session")
        job.request_id = "req-privacy-971"
        with mock.patch.object(cg, "BACKEND", backend), \
                mock.patch.object(cg, "BACKEND_NAME", "openclaw"), \
                mock.patch.object(cg, "STREAM_TTS_ENABLED", False), \
                mock.patch.object(cg, "tts_stream_to_job", side_effect=push_fake_audio), \
                mock.patch.object(cg.history_store, "append_turn"):
            cg.stream_pipeline(job, CANARY_TRANSCRIPT, time.time() - 0.1)
        return job

    def test_unset_defaults_to_metadata_only(self):
        output = self.capture(
            lambda: cg.log_conversation_content(
                "req-unset", "stt", CANARY_TRANSCRIPT
            )
        )

        self.assertNotIn(CANARY_TRANSCRIPT, output)
        self.assertIn("request_id=req-unset", output)
        self.assertIn("stage=stt", output)
        self.assertIn("status=ok", output)
        self.assertIn(f"chars={len(CANARY_TRANSCRIPT)}", output)

    def test_explicit_false_values_remain_metadata_only(self):
        for value in ("", "0", "false", "False"):
            with self.subTest(value=value):
                os.environ[cg.UNSAFE_CONTENT_LOG_ENV] = value
                cg._reset_content_log_state()
                output = self.capture(
                    lambda: cg.log_conversation_content(
                        "req-false", "reply", CANARY_REPLY
                    )
                )
                self.assertNotIn(CANARY_REPLY, output)
                self.assertIn(f"chars={len(CANARY_REPLY)}", output)

    def test_invalid_value_fails_closed_without_echoing_value(self):
        invalid = "invalid-CANARY_ENV_VALUE_971"
        os.environ[cg.UNSAFE_CONTENT_LOG_ENV] = invalid

        output = self.capture(cg.report_content_logging_mode)
        content_output = self.capture(
            lambda: cg.log_conversation_content(
                "req-invalid", "stt", CANARY_TRANSCRIPT
            )
        )

        self.assertIn("status=invalid_disabled", output)
        self.assertNotIn(invalid, output)
        self.assertNotIn(CANARY_TRANSCRIPT, content_output)

    def test_debug_opt_in_warns_and_logs_content(self):
        os.environ[cg.UNSAFE_CONTENT_LOG_ENV] = "1"

        output = self.capture(
            lambda: cg.log_conversation_content(
                "req-debug", "reply", CANARY_REPLY
            )
        )

        self.assertIn("UNSAFE_CONTENT_LOGGING", output)
        self.assertIn("expires_in_s=900", output)
        self.assertIn("request_id=req-debug", output)
        self.assertIn(CANARY_REPLY, output)

    def test_debug_opt_in_expires_after_fifteen_minutes(self):
        os.environ[cg.UNSAFE_CONTENT_LOG_ENV] = "1"

        with mock.patch.object(cg.time, "monotonic", side_effect=(100.0, 100.0)):
            first = self.capture(
                lambda: cg.log_conversation_content(
                    "req-expiry", "reply", CANARY_REPLY
                )
            )
        with mock.patch.object(cg.time, "monotonic", return_value=1001.0):
            expired = self.capture(
                lambda: cg.log_conversation_content(
                    "req-expiry", "reply", CANARY_REPLY
                )
            )

        self.assertIn(CANARY_REPLY, first)
        self.assertNotIn(CANARY_REPLY, expired)
        self.assertIn("status=expired", expired)

    def test_default_pipeline_does_not_emit_transcript_or_reply_canaries(self):
        output = self.capture(lambda: self.run_pipeline(FakeBackend()))

        self.assertNotIn(CANARY_TRANSCRIPT, output)
        self.assertNotIn(CANARY_REPLY, output)
        self.assertIn("request_id=req-privacy-971", output)
        self.assertIn(f"transcript_chars={len(CANARY_TRANSCRIPT)}", output)
        self.assertIn(f"reply_chars={len(CANARY_REPLY)}", output)
        self.assertIn("audio_bytes=16", output)

    def test_default_http_turn_keeps_response_bytes_without_logging_canaries(self):
        def run_turn():
            encoded = urllib.parse.quote(CANARY_TRANSCRIPT)
            with smoke_helpers.patched_environment(CATY_STREAM_TTS=None), \
                    mock.patch.object(cg, "BACKEND", FakeBackend()), \
                    mock.patch.object(
                        cg, "tts_stream_to_job", side_effect=push_fake_audio
                    ), \
                    mock.patch.object(
                        cg.history_store, "append_turn"
                    ), \
                    mock.patch(
                        "http.client.HTTPConnection",
                        smoke_helpers.FakeHTTPConnection,
                    ):
                status, headers, body = self.request(
                    "POST",
                    "/talk2",
                    headers={"X-Caty-Text": encoded},
                )
                self.assertEqual(status, 200)
                payload = json.loads(body)
                status, reply_headers, reply_body = self.wait_for_reply(
                    payload["id"]
                )
                self.assertEqual(status, 200)
                self.assertEqual(reply_headers["x-reply"], CANARY_REPLY)
                self.assertEqual(reply_body, b"privacy-test-mp3")
                self.assertEqual(headers["x-transcript"], CANARY_TRANSCRIPT)

        output = self.capture(run_turn)

        self.assertNotIn(CANARY_TRANSCRIPT, output)
        self.assertNotIn(CANARY_REPLY, output)
        self.assertIn(f"transcript_chars={len(CANARY_TRANSCRIPT)}", output)
        self.assertIn(f"reply_chars={len(CANARY_REPLY)}", output)

    def test_default_legacy_talk_omits_canaries_and_keeps_audio_bytes(self):
        audio = b"legacy-audio-response"
        fd, audio_path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        with open(audio_path, "wb") as stream:
            stream.write(audio)
        self.addCleanup(
            lambda: os.path.exists(audio_path) and os.remove(audio_path)
        )

        def run_turn():
            with mock.patch.object(cg, "to_wav16k", side_effect=lambda path: path), \
                    mock.patch.object(cg, "stt", return_value=CANARY_TRANSCRIPT), \
                    mock.patch.object(cg, "brain", return_value=CANARY_REPLY), \
                    mock.patch.object(cg, "tts", return_value=audio_path):
                status, headers, body = self.request(
                    "POST",
                    "/talk",
                    body=b"legacy-request-audio",
                    headers={"Content-Type": "audio/mp4"},
                )
            self.assertEqual(status, 200)
            self.assertEqual(headers["x-transcript"], CANARY_TRANSCRIPT)
            self.assertEqual(headers["x-reply"], CANARY_REPLY)
            self.assertEqual(body, audio)

        output = self.capture(run_turn)

        self.assertNotIn(CANARY_TRANSCRIPT, output)
        self.assertNotIn(CANARY_REPLY, output)
        self.assertIn(f"chars={len(CANARY_TRANSCRIPT)}", output)
        self.assertIn(f"audio_bytes={len(audio)}", output)

    def test_default_see_omits_stt_frame_and_reply_canaries(self):
        boundary = "----catyprivacy971"
        multipart = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="audio"; filename="turn.m4a"\r\n'
            "Content-Type: audio/mp4\r\n\r\n"
        ).encode() + b"audio-bytes" + (
            f"\r\n--{boundary}\r\n"
            'Content-Disposition: form-data; name="image"; filename="screen.jpg"\r\n'
            "Content-Type: image/jpeg\r\n\r\n"
        ).encode() + (
            b"\x89PNG\r\n\x1a\n" + CANARY_FRAME.encode()
        ) + f"\r\n--{boundary}--\r\n".encode()

        def run_turn():
            with tempfile.TemporaryDirectory(prefix="caty-see-privacy-") as tmp:
                store = share_store.ShareStore(
                    os.path.join(tmp, "spool"), sweep_interval_seconds=0
                )
                try:
                    with mock.patch.object(cg, "_get_share_store", return_value=store), \
                            mock.patch.object(cg, "to_wav16k", side_effect=lambda path: path), \
                            mock.patch.object(cg, "stt", return_value=CANARY_TRANSCRIPT), \
                            mock.patch.object(cg, "BACKEND", FakeBackend()), \
                            mock.patch.object(
                                cg, "tts_stream_to_job", side_effect=push_fake_audio
                            ), \
                            mock.patch.object(cg.history_store, "append_turn") as append_turn:
                        status, headers, body = self.request(
                            "POST",
                            "/see",
                            body=multipart,
                            headers={
                                "Content-Type": (
                                    f"multipart/form-data; boundary={boundary}"
                                )
                            },
                        )
                        self.assertEqual(status, 200)
                        payload = json.loads(body)
                        self.assertEqual(headers["x-transcript"], CANARY_TRANSCRIPT)
                        status, reply_headers, reply_body = self.wait_for_reply(
                            payload["id"]
                        )
                        self.assertEqual(status, 200)
                        self.assertEqual(reply_headers["x-reply"], CANARY_REPLY)
                        self.assertEqual(reply_body, b"privacy-test-mp3")
                        self.assertNotIn(CANARY_FRAME.encode(), body)
                        for call in append_turn.call_args_list:
                            self.assertNotIn(CANARY_FRAME, repr(call))
                finally:
                    store.close()

        output = self.capture(run_turn)

        self.assertNotIn(CANARY_TRANSCRIPT, output)
        self.assertNotIn(CANARY_FRAME, output)
        self.assertNotIn(CANARY_REPLY, output)
        self.assertNotIn("stage=vision", output)

    def test_share_and_talk2_flow_never_logs_file_or_spool_canaries(self):
        with tempfile.TemporaryDirectory(prefix="caty-share-privacy-") as tmp:
            spool = os.path.join(tmp, CANARY_SPOOL_COMPONENT)
            store = share_store.ShareStore(spool)
            boundary = "----catyshareprivacy1061"
            prefix = (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="file"; filename="upload.bin"\r\n'
                "Content-Type: image/png\r\n\r\n"
            ).encode()
            suffix = (
                "\r\n"
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="kind"\r\n\r\n'
                "image\r\n"
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="session_id"\r\n\r\n'
                "privacy-session\r\n"
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="filename"\r\n\r\n'
                f"{CANARY_SHARE_FILENAME}\r\n"
                f"--{boundary}--\r\n"
            ).encode()
            attachment_bytes = (
                b"\x89PNG\r\n\x1a\n" + CANARY_SHARE_CONTENT.encode()
            )
            multipart = prefix + attachment_bytes + suffix

            def run_flow():
                with mock.patch.object(cg, "CATY_TOKEN", "privacy-token"), \
                        mock.patch.object(cg, "CATY_ADMIN_TOKEN", ""), \
                        mock.patch.object(cg, "_share_store", store), \
                        mock.patch.object(cg, "BACKEND", FakeBackend()), \
                        mock.patch.object(
                            cg, "tts_stream_to_job", side_effect=push_fake_audio
                        ), \
                        mock.patch.object(
                            cg.history_store, "append_turn"
                        ) as append_turn:
                    status, _, body = self.request(
                        "POST",
                        "/share",
                        body=multipart,
                        headers={
                            "Content-Type": (
                                f"multipart/form-data; boundary={boundary}"
                            ),
                            "X-Caty-Token": "privacy-token",
                        },
                    )
                    self.assertEqual(status, 200)
                    share_id = json.loads(body)["share_id"]
                    status, _, body = self.request(
                        "POST",
                        "/talk2",
                        headers={
                            "X-Caty-Token": "privacy-token",
                            "X-Session-Id": "privacy-session",
                            "X-Caty-Text": urllib.parse.quote("read it"),
                            "X-Caty-Share-Id": share_id,
                        },
                    )
                    self.assertEqual(status, 200)
                    job_id = json.loads(body)["id"]
                    deadline = time.time() + 5
                    while time.time() < deadline and not cg.JOBS[job_id].done:
                        time.sleep(0.01)
                    self.assertTrue(cg.JOBS[job_id].done)
                    self.assertNotIn(
                        CANARY_SHARE_CONTENT,
                        " ".join(str(call) for call in append_turn.call_args_list),
                    )

            output = self.capture(run_flow)

        self.assertNotIn(CANARY_SHARE_FILENAME, output)
        self.assertNotIn(CANARY_SHARE_CONTENT, output)
        self.assertNotIn(CANARY_SPOOL_COMPONENT, output)
        self.assertIn("kind=image", output)
        self.assertIn("share_id=", output)

    def test_failure_path_omits_exception_content(self):
        error = RuntimeError(
            f"{CANARY_TRANSCRIPT} Authorization: Bearer {CANARY_SECRET} "
            f"https://example.test/fail?token={CANARY_SECRET}"
        )

        output = self.capture(
            lambda: self.run_pipeline(FakeBackend(error=error))
        )

        self.assertNotIn(CANARY_TRANSCRIPT, output)
        self.assertNotIn(CANARY_SECRET, output)
        self.assertNotIn("?token=", output)
        self.assertIn("status=error", output)
        self.assertIn("error_type=RuntimeError", output)

    def test_log_sink_redacts_credentials_authorization_and_url_queries(self):
        with mock.patch.dict(
            os.environ,
            {
                "CATY_TOKEN": CANARY_SECRET,
                "CATY_HERMES_API_KEY": CANARY_SECRET,
                "RENOISE_AUTH_TOKEN": CANARY_SECRET,
            },
            clear=False,
        ):
            output = self.capture(
                lambda: cg.log(
                    "Authorization: Bearer "
                    f"{CANARY_SECRET} url=https://example.test/a?token={CANARY_SECRET}"
                )
            )

        self.assertNotIn(CANARY_SECRET, output)
        self.assertNotIn("?token=", output)
        self.assertIn("[REDACTED]", output)
        self.assertIn("?<redacted>", output)

    def test_log_sink_redacts_non_bearer_and_structured_credentials(self):
        output = self.capture(
            lambda: cg.log(
                "Authorization: Basic dXNlcjpwYXNz "
                '{"token":"STRUCTURED_TOKEN_971",'
                '"api_key":"STRUCTURED_API_KEY_971"}'
            )
        )

        self.assertNotIn("dXNlcjpwYXNz", output)
        self.assertNotIn("STRUCTURED_TOKEN_971", output)
        self.assertNotIn("STRUCTURED_API_KEY_971", output)
        self.assertIn("Authorization: [REDACTED]", output)

    def test_short_configured_secret_does_not_corrupt_metadata_words(self):
        with mock.patch.dict(os.environ, {"CATY_TOKEN": "dev"}, clear=False):
            output = self.capture(
                lambda: cg.log("stage=device_stt credential=dev")
            )

        self.assertIn("stage=device_stt", output)
        self.assertNotIn("credential=dev", output)
        self.assertIn("credential=[REDACTED]", output)

    def test_backend_logger_omits_detail_content_by_default(self):
        output = self.capture(
            lambda: cg.backend_log("backend detail", CANARY_REPLY)
        )

        self.assertNotIn(CANARY_REPLY, output)
        self.assertIn("stage=backend", output)
        self.assertIn("detail_chars=", output)


if __name__ == "__main__":
    unittest.main()
