import json
import os
import sys
import unittest
from unittest import mock


from caty_gateway import caty_gateway as cg
from caty_gateway import presence_state as presence


class PresenceStateTest(unittest.TestCase):
    def setUp(self):
        cg.JOBS.clear()
        presence.set_logger(cg.log)

    def tearDown(self):
        cg.JOBS.clear()
        presence.set_logger(cg.log)

    def test_flag_off_is_a_byte_identical_no_op(self):
        with mock.patch.object(presence, "PRESENCE_MODE2_ENABLED", False), \
                mock.patch.object(presence, "_logger") as logger:
            job = cg.Job("hello")
            self.assertFalse(hasattr(job, "presence_state"))
            presence.transition(job, presence.MODEL_WAITING)
            job.finish()
            self.assertFalse(hasattr(job, "presence_state"))
            logger.assert_not_called()

            pending = cg.Job("pending")
            with cg.JOBS_LOCK:
                cg.JOBS["pending"] = pending
            handler = object.__new__(cg.Handler)
            handler._send = mock.Mock()
            handler._do_reply("pending")
            self.assertEqual(handler._send.call_args.args, (
                202, b'{"ok":true,"status":"thinking"}',
            ))

    def test_flag_on_records_monotonic_transitions_and_extended_202_body(self):
        with mock.patch.object(presence, "PRESENCE_MODE2_ENABLED", True), \
                mock.patch.object(presence, "_now_ms", side_effect=(100, 110, 110, 120)):
            job = cg.Job("hello")
            presence.transition(job, presence.MODEL_WAITING)
            presence.transition(job, presence.STREAMING)
            with cg.JOBS_LOCK:
                cg.JOBS["presence-job"] = job
            handler = object.__new__(cg.Handler)
            handler._send = mock.Mock()
            handler._do_reply("presence-job")

        status, body = handler._send.call_args.args
        self.assertEqual(status, 202)
        payload = json.loads(body)
        self.assertEqual(payload["phase"], "streaming")
        self.assertEqual(payload["accepted_ms"], 100)
        self.assertEqual(payload["phase_started_ms"], 110)
        self.assertEqual(payload["epistemic"], "observed")
        self.assertEqual(payload["speakability"], "exact")
        timestamps = [event["at_ms"] for event in job.presence_state["events"]]
        self.assertEqual(timestamps, sorted(timestamps))

    def test_finish_emits_coverage_log_when_enabled(self):
        lines = []
        with mock.patch.object(presence, "PRESENCE_MODE2_ENABLED", True):
            presence.set_logger(lines.append)
            job = cg.Job("hello")
            presence.set_job_id(job, "abc123")
            presence.transition(job, presence.MODEL_WAITING)
            job.finish()

        self.assertEqual(len(lines), 1)
        self.assertIn("presence: job=abc123", lines[0])
        self.assertIn("phases=['queued', 'model-waiting', 'done']", lines[0])
        self.assertIn("covered=True", lines[0])

    def test_speakability_redacts_tool_names_and_errors(self):
        raw_tool_name = "curl --header Authorization: Bearer secret"
        raw_error = "backend error token=super-secret"
        with mock.patch.object(presence, "PRESENCE_MODE2_ENABLED", True):
            job = cg.Job("hello")
            presence.transition(job, presence.TOOL_RUNNING, tool=raw_tool_name)
            tool_payload = json.loads(presence.thinking_body(job))
            self.assertEqual(tool_payload["speakability"], "generic")
            self.assertNotIn(raw_tool_name, json.dumps(tool_payload))

            presence.transition(job, presence.FAILED, error=raw_error)
            error_payload = json.loads(presence.thinking_body(job))
            self.assertEqual(error_payload["speakability"], "never-aloud")
            self.assertNotIn(raw_error, json.dumps(error_payload))

    def test_terminal_phase_is_sticky(self):
        lines = []
        with mock.patch.object(presence, "PRESENCE_MODE2_ENABLED", True):
            presence.set_logger(lines.append)
            job = cg.Job("hello")
            job.finish()
            events_after_finish = len(job.presence_state["events"])
            presence.transition(job, presence.STREAMING)
            job.finish()

        self.assertEqual(job.presence_state["phase"], presence.DONE)
        self.assertEqual(len(job.presence_state["events"]), events_after_finish)
        self.assertEqual(len(lines), 1)

    def test_finish_with_error_records_failed_and_falsy_error_is_done(self):
        lines = []
        with mock.patch.object(presence, "PRESENCE_MODE2_ENABLED", True):
            presence.set_logger(lines.append)
            failed = cg.Job("boom")
            failed.finish(error="backend exploded")
            ok = cg.Job("fine")
            ok.finish(error="")

        self.assertEqual(failed.presence_state["phase"], presence.FAILED)
        self.assertIn("'failed']", lines[0])
        self.assertIn("covered=True", lines[0])
        # error="" is served as a success by _do_reply's `if error:` gate
        self.assertEqual(ok.presence_state["phase"], presence.DONE)

    def test_epistemic_goes_stale_past_threshold(self):
        with mock.patch.object(presence, "PRESENCE_MODE2_ENABLED", True), \
                mock.patch.object(presence, "STALE_AFTER_MS", -1):
            job = cg.Job("hello")
            presence.transition(job, presence.MODEL_WAITING)
            payload = json.loads(presence.thinking_body(job))
        self.assertEqual(payload["epistemic"], "stale")

    def test_thinking_body_flag_on_without_state_falls_back_to_legacy_bytes(self):
        # A job created while the flag was OFF has no presence_state even if
        # the flag is later seen ON (import-time flag; tests patch it).
        with mock.patch.object(presence, "PRESENCE_MODE2_ENABLED", False):
            job = cg.Job("hello")
        with mock.patch.object(presence, "PRESENCE_MODE2_ENABLED", True):
            self.assertEqual(presence.thinking_body(job),
                             b'{"ok":true,"status":"thinking"}')

    def test_speakability_classifier_mapping(self):
        self.assertEqual(presence._speakability(presence.STREAMING, {}), "exact")
        self.assertEqual(presence._speakability(presence.TOOL_RUNNING, {}), "generic")
        self.assertEqual(presence._speakability(presence.RETRYING, {}), "generic")
        self.assertEqual(presence._speakability(presence.FAILED, {}), "never-aloud")
        self.assertEqual(presence._speakability(presence.DONE, {"error": "detail"}), "never-aloud")


if __name__ == "__main__":
    unittest.main()
