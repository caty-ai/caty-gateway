import os
import sys
import time
import unittest
from contextlib import contextmanager
from unittest import mock


from caty_gateway import caty_gateway as cg


class FakeBackend:
    agent = "fake-agent"

    def __init__(self, streaming=False):
        self.streaming = streaming

    def supports_stream(self):
        return self.streaming

    def stream(self, _text, _session_id=None, _brain_timeout=180, route=None):
        yield f"stream reply {route or 'none'}"

    def generate(self, _text, _session_id=None, _brain_timeout=180, route=None):
        return f"legacy reply {route or 'none'}"


class StreamFailsBeforeChunksBackend(FakeBackend):
    def __init__(self):
        super().__init__(streaming=True)

    def stream(self, _text, _session_id=None, _brain_timeout=180, route=None):
        raise RuntimeError("stream failed before first sentence")
        yield  # pragma: no cover - makes this a generator that fails on first next()


@contextmanager
def captured_gateway_logs():
    lines = []
    with mock.patch.object(cg, "log", side_effect=lambda *a: lines.append(" ".join(str(x) for x in a))):
        yield lines


def push_fake_audio(_text, job):
    job.push(b"mp3")
    return 3


class TurnLogTest(unittest.TestCase):
    def run_pipeline(self, job, *, backend, stream_enabled, route=None):
        with captured_gateway_logs() as lines, \
                mock.patch.object(cg, "BACKEND", backend), \
                mock.patch.object(cg, "BACKEND_NAME", "openclaw"), \
                mock.patch.object(cg, "STREAM_TTS_ENABLED", stream_enabled), \
                mock.patch.object(cg, "tts_stream_to_job", side_effect=push_fake_audio), \
                mock.patch.object(cg.history_store, "append_turn"):
            cg.stream_pipeline(job, "user text", time.time() - 0.1, route=route)
        return lines

    def turn_lines(self, lines):
        return [line for line in lines if line.startswith("🎚 turn ")]

    def test_streaming_turn_emits_one_summary_line(self):
        job = cg.Job("user text", session_id="stream-turn")

        lines = self.run_pipeline(
            job,
            backend=FakeBackend(streaming=True),
            stream_enabled=True,
            route="live",
        )

        turns = self.turn_lines(lines)
        self.assertEqual(len(turns), 1)
        self.assertIn("route=live", turns[0])
        self.assertIn("backend=openclaw:fake-agent", turns[0])
        self.assertIn("stt=-", turns[0])
        self.assertIn("total=", turns[0])
        self.assertIn("mode=stream", turns[0])
        self.assertTrue(job.done)
        self.assertIsNone(job.error)

    def test_legacy_turn_emits_one_summary_line(self):
        job = cg.Job("user text", session_id="legacy-turn")
        job.stt_s = 1.23

        lines = self.run_pipeline(
            job,
            backend=FakeBackend(streaming=False),
            stream_enabled=False,
        )

        turns = self.turn_lines(lines)
        self.assertEqual(len(turns), 1)
        self.assertIn("route=-", turns[0])
        self.assertIn("backend=openclaw:fake-agent", turns[0])
        self.assertIn("stt=1.2s", turns[0])
        self.assertIn("total=", turns[0])
        self.assertIn("mode=legacy", turns[0])
        self.assertTrue(job.done)
        self.assertIsNone(job.error)

    def test_stream_failure_before_chunks_emits_one_legacy_summary_line(self):
        job = cg.Job("user text", session_id="stream-fail-before-chunks")

        lines = self.run_pipeline(
            job,
            backend=StreamFailsBeforeChunksBackend(),
            stream_enabled=True,
            route="live",
        )

        turns = self.turn_lines(lines)
        self.assertEqual(len(turns), 1)
        self.assertIn("route=live", turns[0])
        self.assertIn("backend=openclaw:fake-agent", turns[0])
        self.assertIn("mode=legacy", turns[0])
        self.assertNotIn("mode=stream", turns[0])
        self.assertNotIn("mode=fallback", turns[0])
        self.assertEqual(job.chunks, [b"mp3"])
        self.assertEqual(job.reply, "legacy reply live")
        self.assertTrue(job.done)
        self.assertIsNone(job.error)

    def test_summary_formatting_exception_does_not_break_turn(self):
        job = cg.Job("user text", session_id="summary-error")

        with captured_gateway_logs(), \
                mock.patch.object(cg, "BACKEND", FakeBackend(streaming=False)), \
                mock.patch.object(cg, "STREAM_TTS_ENABLED", False), \
                mock.patch.object(cg, "tts_stream_to_job", side_effect=push_fake_audio), \
                mock.patch.object(cg, "_backend_desc", side_effect=RuntimeError("boom")), \
                mock.patch.object(cg.history_store, "append_turn"):
            cg.stream_pipeline(job, "user text", time.time() - 0.1)

        self.assertEqual(job.chunks, [b"mp3"])
        self.assertTrue(job.done)
        self.assertIsNone(job.error)


if __name__ == "__main__":
    unittest.main()
