import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock


from caty_gateway.backends.claude import CLAUDE_SESSION_NAMESPACE, ClaudeCodeBackend, ClaudeStreamError, ClaudeStreamTimeout


class FakeRun:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((list(cmd), dict(kwargs)))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return subprocess.CompletedProcess(cmd, response[0], response[1], response[2])


def result_json(text, is_error=False):
    return json.dumps({"result": text, "is_error": is_error})


class ScriptedStream:
    def __init__(self, events, on_finish=None):
        self._queue = []
        self._cond = threading.Condition()
        self._closed = False
        self._finished = False
        self._on_finish = on_finish

        def writer():
            for delay_s, value in events:
                if delay_s:
                    time.sleep(delay_s)
                with self._cond:
                    if self._closed:
                        break
                    self._queue.append(value)
                    self._cond.notify_all()
            self._finish()

        threading.Thread(target=writer, daemon=True).start()

    def _finish(self):
        callback = None
        with self._cond:
            if self._finished:
                return
            self._finished = True
            self._closed = True
            callback = self._on_finish
            self._cond.notify_all()
        if callback:
            callback()

    def __iter__(self):
        return self

    def __next__(self):
        with self._cond:
            while not self._queue and not self._closed:
                self._cond.wait(timeout=0.1)
            if self._queue:
                return self._queue.pop(0)
            raise StopIteration

    def close(self):
        self._finish()


class FakeProcess:
    _next_pid = 1000

    def __init__(self, stdout_events=(), stderr_events=(), returncode=0):
        self.returncode = returncode
        self.pid = None
        self.terminated = False
        self.killed = False
        self.wait_calls = 0
        self._done = threading.Event()
        self._open_streams = 2
        self._lock = threading.Lock()
        FakeProcess._next_pid += 1
        self.pid = FakeProcess._next_pid
        self.stdout = ScriptedStream(stdout_events, on_finish=self._stream_finished)
        self.stderr = ScriptedStream(stderr_events, on_finish=self._stream_finished)

    def _stream_finished(self):
        with self._lock:
            self._open_streams -= 1
            if self._open_streams <= 0:
                self._done.set()

    def poll(self):
        return self.returncode if self._done.is_set() else None

    def wait(self, timeout=None):
        self.wait_calls += 1
        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired("claude", timeout)
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.stdout.close()
        self.stderr.close()
        self._done.set()

    def kill(self):
        self.killed = True
        self.terminate()


class ClaudeCodeBackendTest(unittest.TestCase):
    def backend(self, model="", cwd="/tmp/caty-claude-cwd", bin_path="/tmp/claude"):
        return ClaudeCodeBackend(
            claude_bin=bin_path,
            model=model,
            cwd=cwd,
            voice_hint="voice-hint\n",
            log=lambda *args: None,
        )

    def sid(self, session_id):
        return str(uuid.uuid5(CLAUDE_SESSION_NAMESPACE, f"caty-{session_id}"))

    def test_happy_path_parses_result_json(self):
        fake = FakeRun([(0, result_json("返事です"), "")])
        with mock.patch("caty_gateway.backends.claude.subprocess.run", side_effect=fake):
            reply = self.backend().generate("hello", None, 5)

        self.assertEqual(reply, "返事です")
        self.assertIn("-p", fake.calls[0][0])
        cmd = fake.calls[0][0]
        self.assertEqual(cmd[cmd.index("-p") + 1], "hello")
        self.assertEqual(cmd[cmd.index("--append-system-prompt") + 1], "voice-hint\n")

    def test_new_session_starts_with_session_id_single_call_then_resumes(self):
        fake = FakeRun([
            (0, result_json("started"), ""),
            (0, result_json("second"), ""),
        ])
        backend = self.backend()

        with mock.patch("caty_gateway.backends.claude.subprocess.run", side_effect=fake):
            self.assertEqual(backend.generate("hi", "phone-a", 5), "started")
            self.assertEqual(backend.generate("more", "phone-a", 5), "second")

        sid = self.sid("phone-a")
        # 新規 sid は --session-id 先行（subprocess 1回）、成功後は --resume 先行
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(fake.calls[0][0][-2:], ["--session-id", sid])
        self.assertEqual(fake.calls[1][0][-2:], ["--resume", sid])

    def test_restart_recovery_session_id_in_use_falls_back_to_resume(self):
        # gateway 再起動後: cache は空だが Claude 側に会話が残っているケース
        fake = FakeRun([
            (1, "", "Session ID xxx is already in use."),
            (0, result_json("resumed"), ""),
        ])

        with mock.patch("caty_gateway.backends.claude.subprocess.run", side_effect=fake):
            self.assertEqual(self.backend().generate("hi", "phone-b", 5), "resumed")

        sid = self.sid("phone-b")
        self.assertEqual(fake.calls[0][0][-2:], ["--session-id", sid])
        self.assertEqual(fake.calls[1][0][-2:], ["--resume", sid])

    def test_known_session_resume_miss_recreates_then_joins_race_winner(self):
        # 生存確認済み sid の transcript 消失 → 新規作成 → 並行ターンと競合 → resume 合流
        fake = FakeRun([
            (1, "", "No conversation found"),
            (1, "", "session already in use"),
            (0, result_json("resumed"), ""),
        ])
        backend = self.backend()
        sid = self.sid("phone-c")
        backend._known_sessions.add(sid)

        with mock.patch("caty_gateway.backends.claude.subprocess.run", side_effect=fake):
            self.assertEqual(backend.generate("hi", "phone-c", 5), "resumed")

        self.assertEqual(fake.calls[0][0][-2:], ["--resume", sid])
        self.assertEqual(fake.calls[1][0][-2:], ["--session-id", sid])
        self.assertEqual(fake.calls[2][0][-2:], ["--resume", sid])

    def test_error_json_envelope_raises_runtime_error(self):
        fake = FakeRun([(1, result_json("Not logged in · Please run /login", is_error=True), "")])

        with mock.patch("caty_gateway.backends.claude.subprocess.run", side_effect=fake):
            with self.assertRaisesRegex(RuntimeError, "Not logged in"):
                self.backend().generate("hi", None, 5)

    def test_run_uses_devnull_and_configured_cwd(self):
        fake = FakeRun([(0, result_json("ok"), "")])

        with mock.patch("caty_gateway.backends.claude.subprocess.run", side_effect=fake):
            self.backend(cwd="/tmp/member-project").generate("hi", None, 5)

        kwargs = fake.calls[0][1]
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["cwd"], "/tmp/member-project")
        self.assertTrue(kwargs["capture_output"])
        self.assertTrue(kwargs["text"])
        self.assertEqual(kwargs["timeout"], 65)

    def test_model_flag_is_omitted_when_empty_and_present_when_set(self):
        fake = FakeRun([
            (0, result_json("default model"), ""),
            (0, result_json("configured model"), ""),
        ])

        with mock.patch("caty_gateway.backends.claude.subprocess.run", side_effect=fake):
            self.backend(model="").generate("hi", None, 5)
            self.backend(model="claude-sonnet-4-6").generate("hi", None, 5)

        self.assertNotIn("--model", fake.calls[0][0])
        self.assertEqual(
            fake.calls[1][0][fake.calls[1][0].index("--model") + 1],
            "claude-sonnet-4-6",
        )

    def test_health_false_after_failure_and_when_binary_missing(self):
        with tempfile.NamedTemporaryFile() as f:
            os.chmod(f.name, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            backend = self.backend(bin_path=f.name)
            self.assertTrue(backend.health())

            fake = FakeRun([(1, result_json("Not logged in", is_error=True), "")])
            with mock.patch("caty_gateway.backends.claude.subprocess.run", side_effect=fake):
                with self.assertRaises(RuntimeError):
                    backend.generate("hi", None, 5)
            self.assertFalse(backend.health())

        self.assertFalse(self.backend(bin_path="/tmp/definitely-missing-claude").health())

    def test_openai_complete_joins_only_text_delta_events(self):
        lines = [
            (0.0, json.dumps({"type": "stream_event", "event": {"type": "message_start"}}) + "\n"),
            (0.0, json.dumps({
                "type": "stream_event",
                "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "こんにちは"}},
            }) + "\n"),
            (0.0, json.dumps({"type": "result", "result": "ignored"}) + "\n"),
            (0.0, json.dumps({
                "type": "stream_event",
                "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "世界"}},
            }) + "\n"),
        ]
        proc = FakeProcess(stdout_events=lines, stderr_events=(), returncode=0)
        backend = self.backend()

        with mock.patch.object(backend, "_spawn_stream_once", return_value=proc):
            reply = backend.openai_complete("hi", "meetmate:user", 5)

        self.assertEqual(reply, "こんにちは世界")
        self.assertIn("--verbose", backend._stream_base_cmd("hi"))

    def test_openai_stream_new_session_retries_resume_after_session_id_in_use(self):
        backend = self.backend()
        calls = []
        first = FakeProcess(stdout_events=(), stderr_events=[(0.0, "Session ID xxx is already in use.\n")], returncode=1)
        second = FakeProcess(stdout_events=[(0.0, json.dumps({
            "type": "stream_event",
            "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "ok"}},
        }) + "\n")], stderr_events=(), returncode=0)

        def spawn(base_cmd, session_flag=None):
            calls.append(session_flag)
            return [first, second][len(calls) - 1]

        with mock.patch.object(backend, "_spawn_stream_once", side_effect=spawn):
            self.assertEqual(list(backend.openai_stream("hi", "meetmate:user", 5)), ["ok"])

        sid = self.sid("meetmate:user")
        self.assertEqual(calls, [("--session-id", sid), ("--resume", sid)])

    def test_openai_stream_known_session_resume_miss_recreates_then_resumes(self):
        backend = self.backend()
        sid = self.sid("meetmate:user")
        backend._known_sessions.add(sid)
        calls = []
        first = FakeProcess(stdout_events=(), stderr_events=[(0.0, "No conversation found\n")], returncode=1)
        second = FakeProcess(stdout_events=(), stderr_events=[(0.0, "session already in use\n")], returncode=1)
        third = FakeProcess(stdout_events=[(0.0, json.dumps({
            "type": "stream_event",
            "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "joined"}},
        }) + "\n")], stderr_events=(), returncode=0)

        def spawn(base_cmd, session_flag=None):
            calls.append(session_flag)
            return [first, second, third][len(calls) - 1]

        with mock.patch.object(backend, "_spawn_stream_once", side_effect=spawn):
            self.assertEqual(list(backend.openai_stream("hi", "meetmate:user", 5)), ["joined"])

        self.assertEqual(calls, [("--resume", sid), ("--session-id", sid), ("--resume", sid)])

    def test_openai_stream_timeout_terminates_process(self):
        backend = self.backend()
        proc = FakeProcess(stdout_events=[(1.0, json.dumps({"ignored": True}) + "\n")], stderr_events=[(1.0, "")], returncode=0)

        with mock.patch.object(backend, "_spawn_stream_once", return_value=proc):
            with self.assertRaises(ClaudeStreamTimeout):
                list(backend.openai_stream("hi", "meetmate:user", 0))

        self.assertTrue(proc.terminated or proc.killed)

    def test_openai_stream_close_terminates_running_process(self):
        backend = self.backend()
        proc = FakeProcess(
            stdout_events=[
                (0.0, json.dumps({
                    "type": "stream_event",
                    "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "first"}},
                }) + "\n"),
                (1.0, json.dumps({
                    "type": "stream_event",
                    "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "second"}},
                }) + "\n"),
            ],
            stderr_events=(),
            returncode=0,
        )

        with mock.patch.object(backend, "_spawn_stream_once", return_value=proc):
            gen = backend.openai_stream("hi", "meetmate:user", 5)
            self.assertEqual(next(gen), "first")
            gen.close()

        self.assertTrue(proc.terminated or proc.killed)

    def test_openai_stream_raises_for_empty_success(self):
        backend = self.backend()
        proc = FakeProcess(
            stdout_events=[(0.0, json.dumps({"type": "stream_event", "event": {"type": "message_stop"}}) + "\n")],
            stderr_events=(),
            returncode=0,
        )

        with mock.patch.object(backend, "_spawn_stream_once", return_value=proc):
            with self.assertRaises(ClaudeStreamError):
                list(backend.openai_stream("hi", "meetmate:user", 5))

    def test_openai_stream_invalid_json_fails_loud(self):
        backend = self.backend()
        proc = FakeProcess(stdout_events=[(0.0, "not-json\n")], stderr_events=(), returncode=0)

        with mock.patch.object(backend, "_spawn_stream_once", return_value=proc):
            with self.assertRaisesRegex(ClaudeStreamError, "invalid json"):
                list(backend.openai_stream("hi", "meetmate:user", 5))

    def test_openai_stream_result_error_fails_even_with_zero_exit(self):
        backend = self.backend()
        proc = FakeProcess(stdout_events=[(0.0, json.dumps({
            "type": "result",
            "is_error": True,
            "result": "Not logged in · Please run /login",
        }) + "\n")], stderr_events=(), returncode=0)

        with mock.patch.object(backend, "_spawn_stream_once", return_value=proc):
            with self.assertRaisesRegex(ClaudeStreamError, "Not logged in"):
                list(backend.openai_stream("hi", "meetmate:user", 5))


if __name__ == "__main__":
    unittest.main()
