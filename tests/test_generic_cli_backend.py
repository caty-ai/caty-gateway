import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


from caty_gateway.backends.generic_cli import GenericCliBackend, RUN_TIMEOUT_MARGIN
from caty_gateway.backends.openclaw import PTT_HINT


def events(thread="native", replies=("reply",), error=None):
    rows = [{"type": "thread.started", "thread_id": thread}]
    if error:
        rows.append({"type": "item.completed", "item": {"type": "error", "message": error}})
    for reply in replies:
        rows.append({"type": "item.completed", "item": {"type": "agent_message", "text": reply}})
    return "\n".join(json.dumps(row) for row in rows)


class GenericCliBackendTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.control = self.root / "control.json"
        self.log = self.root / "argv.jsonl"
        self.bin = self.root / "fake"
        self._write_fake()
        self.env = {
            "FAKE_ROOT": str(self.root),
            "CATY_GCLI_BIN": str(self.bin),
            "CATY_GCLI_CWD": str(self.root),
        }
        self.env_patcher = mock.patch.dict(os.environ, self.env, clear=False)
        self.env_patcher.start()
        self.set_control(stdout=events())

    def tearDown(self):
        self.env_patcher.stop()
        self.tmp.cleanup()

    def _write_fake(self):
        self.bin.write_text("""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
root = Path(os.environ['FAKE_ROOT'])
control = json.loads((root / 'control.json').read_text())
with (root / 'argv.jsonl').open('a') as f:
    f.write(json.dumps(sys.argv[1:]) + '\\n')
for line in control.get('stdout', []):
    print(line)
if control.get('stderr'):
    print(control['stderr'], file=sys.stderr)
sys.exit(control.get('rc', 0))
""")
        self.bin.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

    def set_control(self, rc=0, stderr="", stdout=""):
        self.control.write_text(json.dumps({"rc": rc, "stderr": stderr, "stdout": stdout.splitlines()}))

    def backend(self, preset="codex", **kwargs):
        return GenericCliBackend(preset, "voice\n", lambda *args: None, **kwargs)

    def argv(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_no_session_parses_reply_and_prefixes_voice_hint(self):
        self.set_control(stdout=events(replies=("reply",)))
        self.assertEqual(self.backend().generate("hello", None, 1), "reply")
        self.assertEqual(self.argv()[0][-1], "voice\nhello")

    def test_last_agent_message_wins_and_error_item_is_tolerated(self):
        self.set_control(stdout=events(replies=("first", "last"), error="notice"))
        self.assertEqual(self.backend().generate("hello", None, 1), "last")

    def test_session_new_then_resume_saves_native_id(self):
        saved = []
        backend = self.backend(save_session=lambda sid, native: saved.append((sid, native)))
        self.set_control(stdout=events("native-1", ("new",)))
        self.assertEqual(backend.generate("one", "phone", 1), "new")
        self.set_control(stdout=events("native-1", ("resumed",)))
        self.assertEqual(backend.generate("two", "phone", 1), "resumed")
        self.assertEqual(saved, [("phone", "native-1")])
        self.assertEqual(self.argv()[1][:3], ["exec", "resume", "native-1"])

    def test_resolve_session_uses_resume_directly(self):
        self.set_control(stdout=events("taken", ("ok",)))
        backend = self.backend(resolve_session=lambda sid: "taken")
        self.assertEqual(backend.generate("hello", "phone", 1), "ok")
        self.assertEqual(self.argv()[0][:3], ["exec", "resume", "taken"])

    def test_missing_resume_falls_back_to_new_session(self):
        saved = []
        self.bin.write_text("""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
root = Path(os.environ['FAKE_ROOT'])
count = len((root / 'argv.jsonl').read_text().splitlines()) if (root / 'argv.jsonl').exists() else 0
with (root / 'argv.jsonl').open('a') as f:
    f.write(json.dumps(sys.argv[1:]) + '\\n')
if count == 0:
    print('no rollout found', file=sys.stderr)
    sys.exit(1)
print(json.dumps({'type': 'thread.started', 'thread_id': 'fresh'}))
print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'fresh'}}))
""")
        self.bin.chmod(0o700)
        backend = self.backend(resolve_session=lambda sid: "gone", save_session=lambda *args: saved.append(args))
        self.assertEqual(backend.generate("hello", "phone", 1), "fresh")
        self.assertEqual(saved, [("phone", "fresh")])
        self.assertEqual(self.argv()[0][:3], ["exec", "resume", "gone"])
        self.assertEqual(self.argv()[1][0], "exec")

    def test_extra_args_are_appended_to_new_and_resume(self):
        with mock.patch.dict(os.environ, {"CATY_GCLI_EXTRA_ARGS": '["--profile", "x"]'}):
            backend = self.backend()
            self.set_control(stdout=events("native", ("new",)))
            backend.generate("one", "phone", 1)
            self.set_control(stdout=events("native", ("resume",)))
            backend.generate("two", "phone", 1)
        self.assertEqual(self.argv()[0][-2:], ["--profile", "x"])
        self.assertEqual(self.argv()[1][-2:], ["--profile", "x"])

    def test_prompt_placeholders_in_user_text_are_not_interpolated(self):
        text = "literal {prompt} and {native_id}"
        self.set_control(stdout=events(replies=("ok",)))
        self.assertEqual(self.backend().generate(text, None, 1), "ok")
        self.assertEqual(self.argv()[0][-1], "voice\n" + text)

    def test_non_matching_resume_failure_does_not_start_new_session(self):
        self.set_control(rc=1, stderr="model not found")
        backend = self.backend(resolve_session=lambda sid: "native")
        with self.assertRaisesRegex(RuntimeError, "model not found"):
            backend.generate("hello", "phone", 1)
        self.assertEqual(len(self.argv()), 1)
        self.assertEqual(self.argv()[0][:3], ["exec", "resume", "native"])

    def test_resume_with_changed_native_id_resaves_mapping(self):
        saved = []
        self.set_control(stdout=events("forked", ("ok",)))
        backend = self.backend(
            resolve_session=lambda sid: "original",
            save_session=lambda sid, native: saved.append((sid, native)),
        )
        self.assertEqual(backend.generate("hello", "phone", 1), "ok")
        self.assertEqual(saved, [("phone", "forked")])

    def test_invalid_captured_native_id_is_not_saved(self):
        saved = []
        self.set_control(stdout=events("--last", ("ok",)))
        backend = self.backend(save_session=lambda sid, native: saved.append((sid, native)))
        self.assertEqual(backend.generate("hello", "phone", 1), "ok")
        self.assertEqual(saved, [])

    def test_malformed_json_config_raises_at_construction(self):
        with mock.patch.dict(os.environ, {"CATY_GCLI_NEW_ARGS": "[bad"}):
            with self.assertRaisesRegex(RuntimeError, "CATY_GCLI_NEW_ARGS must be valid JSON"):
                self.backend()

    def test_unknown_preset_raises_at_construction(self):
        with self.assertRaisesRegex(RuntimeError, "unknown GenericCliBackend preset"):
            self.backend("kimi")

    def test_failures_backoff_and_no_reply(self):
        self.set_control(rc=1, stderr="x" * 500)
        backend = self.backend()
        with self.assertRaisesRegex(RuntimeError, "codex失敗") as raised:
            backend.generate("hello", None, 1)
        self.assertLessEqual(len(str(raised.exception)), 310)
        self.assertFalse(backend.health())
        backend._last_runtime_error_at = time.monotonic() - 31
        self.assertTrue(backend.health())
        self.set_control(stdout=json.dumps({"type": "thread.started", "thread_id": "native"}))
        with self.assertRaisesRegex(RuntimeError, "no reply"):
            self.backend().generate("hello", None, 1)

    def test_ptt_hint_is_added_to_prompt(self):
        self.set_control(stdout=events(replies=("ok",)))
        self.assertEqual(self.backend().generate("hello", None, 1, route="ptt"), "ok")
        self.assertIn(PTT_HINT, self.argv()[0][-1])

    def test_generic_mode_is_config_driven_and_requires_new_args(self):
        config = {
            "CATY_GCLI_NEW_ARGS": '["ask", "{prompt}"]',
            "CATY_GCLI_RESUME_ARGS": '["again", "{native_id}", "{prompt}"]',
            "CATY_GCLI_PARSE_SPEC": '{"mode":"text"}',
        }
        self.set_control(stdout="made up reply")
        with mock.patch.dict(os.environ, config):
            self.assertEqual(self.backend(None).generate("hello", None, 1), "made up reply")
        with mock.patch.dict(os.environ, {"CATY_GCLI_BIN": str(self.bin)}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "NEW_ARGS"):
                GenericCliBackend(None, "", lambda *args: None)

    def test_timeout_uses_standard_margin(self):
        with mock.patch("caty_gateway.backends.generic_cli.subprocess.run", side_effect=subprocess.TimeoutExpired([], 1)) as run:
            with self.assertRaisesRegex(RuntimeError, "タイムアウト"):
                self.backend().generate("hello", None, 2)
        self.assertEqual(run.call_args.kwargs["timeout"], 2 + RUN_TIMEOUT_MARGIN)

    def test_rollout_store_filters_sorts_and_reads(self):
        store = self.root / "sessions"
        store.mkdir()
        cwd = str(self.root.resolve())
        self._write_rollout(store / "rollout-a.jsonl", "a", cwd, "one", 1)
        self._write_rollout(store / "rollout-b.jsonl", "b", cwd, "two", 2)
        self._write_rollout(store / "rollout-x.jsonl", "x", "/other", "hidden", 3)
        with mock.patch.dict(os.environ, {"CATY_GCLI_SESSION_STORE": str(store)}):
            backend = self.backend()
            rows = backend.list_external()
            self.assertEqual([row["native_id"] for row in rows], ["b", "a"])
            self.assertEqual(set(rows[0]), {"native_id", "label", "updated_at", "preview"})
            self.assertEqual(backend.read_external("a", 1)[0]["role"], "assistant")
            self.assertEqual(backend.read_external("bad/id"), [])
        with mock.patch.dict(os.environ, {"CATY_GCLI_EXTERNAL": "none"}):
            self.assertEqual(self.backend().list_external(), [])
            self.assertEqual(self.backend().read_external("a"), [])

    def test_rollout_payload_id_fallback_is_listed_and_readable(self):
        store = self.root / "sessions"
        store.mkdir()
        path = store / "rollout-id-only.jsonl"
        rows = [
            {"type": "session_meta", "payload": {"id": "id-only", "cwd": str(self.root.resolve())}},
            {"type": "event_msg", "timestamp": "1", "payload": {"type": "user_message", "message": "hello"}},
            {"type": "event_msg", "timestamp": "2", "payload": {"type": "agent_message", "message": "answer"}},
        ]
        path.write_text("\n".join(json.dumps(row) for row in rows))
        with mock.patch.dict(os.environ, {"CATY_GCLI_SESSION_STORE": str(store)}):
            backend = self.backend()
            self.assertEqual([row["native_id"] for row in backend.list_external()], ["id-only"])
            self.assertEqual(backend.read_external("id-only")[0]["role"], "user")

    def _write_rollout(self, path, native, cwd, text, mtime):
        rows = [
            {"type": "session_meta", "payload": {"session_id": native, "cwd": cwd}},
            {"type": "event_msg", "timestamp": "1", "payload": {"type": "user_message", "message": text}},
            {"type": "event_msg", "timestamp": "2", "payload": {"type": "agent_message", "message": "answer"}},
        ]
        path.write_text("\n".join(json.dumps(row) for row in rows))
        os.utime(path, (mtime, mtime))


if __name__ == "__main__":
    unittest.main()
