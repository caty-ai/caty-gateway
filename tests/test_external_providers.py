import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock


from caty_gateway.backends.base import Backend
from caty_gateway.backends.claude import ClaudeCodeBackend
from caty_gateway.backends.openclaw import OpenClawBackend


class FakeRun:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, cmd, timeout):
        self.calls.append((list(cmd), timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class BaseExternalProviderTest(unittest.TestCase):
    def test_default_external_methods_are_noop(self):
        class MinimalBackend(Backend):
            def generate(self, text, session_id, timeout, route=None):
                return ""

            def stream(self, text, session_id, timeout, route=None):
                return iter(())

        backend = MinimalBackend()

        self.assertEqual(backend.list_external(), [])
        self.assertEqual(backend.read_external("x"), [])


class OpenClawExternalProviderTest(unittest.TestCase):
    def backend(self):
        return OpenClawBackend(
            openclaw_bin="openclaw",
            agent="main",
            voice_hint="voice-hint\n",
            session_key_prefix="caty-",
            log=lambda *args: None,
            is_no_reply=lambda text: False,
            sanitize_for_tts=lambda text: text,
        )

    def listing_stdout(self, store_path, sessions):
        return json.dumps({
            "path": store_path,
            "count": len(sessions),
            "sessions": sessions,
        }) + "\n[plugins] loaded foo\n"

    def write_openclaw_transcript(self, directory, session_id):
        path = os.path.join(directory, f"{session_id}.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "message",
                "timestamp": "2026-07-05T01:00:00Z",
                "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            }) + "\n")
            f.write(json.dumps({
                "type": "message",
                "timestamp": "2026-07-05T01:00:01Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "name": "ignored"},
                        {"type": "text", "text": "assistant reply"},
                    ],
                },
            }) + "\n")
        return path

    def test_list_external_raw_decode_denylist_and_store_path_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_dir = os.path.join(tmp, "custom-store")
            os.mkdir(store_dir)
            store_path = os.path.join(store_dir, "sessions.json")
            self.write_openclaw_transcript(store_dir, "external-session")
            sessions = [
                {"key": "agent:main:caty-owned", "updatedAt": 1781996695999, "sessionId": "owned"},
                {"key": "agent:main:cron:daily", "updatedAt": 1781996695888, "sessionId": "cron"},
                {"key": "agent:main:run:job", "updatedAt": 1781996695777, "sessionId": "run"},
                {"key": "agent:main:external-chat", "updatedAt": 1781996695666, "sessionId": "external-session"},
            ]
            fake = FakeRun([(0, self.listing_stdout(store_path, sessions), "")])

            with mock.patch("caty_gateway.backends.openclaw.run", side_effect=fake):
                rows = self.backend().list_external()

        self.assertEqual(fake.calls[0][0], ["openclaw", "sessions", "--json"])
        self.assertEqual([row["native_id"] for row in rows], ["agent:main:external-chat"])
        self.assertEqual(rows[0]["label"], "external-chat")
        self.assertEqual(rows[0]["preview"], "assistant reply")
        self.assertTrue(rows[0]["updated_at"].endswith("Z"))

    def test_read_external_uses_key_lookup_and_returns_oldest_to_newest(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_path = os.path.join(tmp, "sessions.json")
            self.write_openclaw_transcript(tmp, "external-session")
            sessions = [{"key": "agent:main:external-chat", "updatedAt": 1781996695666, "sessionId": "external-session"}]
            fake = FakeRun([(0, self.listing_stdout(store_path, sessions), "")])

            with mock.patch("caty_gateway.backends.openclaw.run", side_effect=fake):
                turns = self.backend().read_external("agent:main:external-chat")

        self.assertEqual(turns, [
            {"role": "user", "text": "hello", "ts": "2026-07-05T01:00:00Z"},
            {"role": "assistant", "text": "assistant reply", "ts": "2026-07-05T01:00:01Z"},
        ])

    def test_read_external_denies_owned_key_even_when_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_path = os.path.join(tmp, "sessions.json")
            self.write_openclaw_transcript(tmp, "owned")
            sessions = [{"key": "agent:main:caty-owned", "updatedAt": 1781996695666, "sessionId": "owned"}]
            fake = FakeRun([(0, self.listing_stdout(store_path, sessions), "")])

            with mock.patch("caty_gateway.backends.openclaw.run", side_effect=fake):
                turns = self.backend().read_external("agent:main:caty-owned")

        self.assertEqual(turns, [])

    def test_read_external_unknown_native_id_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_path = os.path.join(tmp, "sessions.json")
            sessions = [{"key": "agent:main:external-chat", "updatedAt": 1781996695666, "sessionId": "external-session"}]
            fake = FakeRun([(0, self.listing_stdout(store_path, sessions), "")])

            with mock.patch("caty_gateway.backends.openclaw.run", side_effect=fake):
                self.assertEqual(self.backend().read_external("agent:main:missing"), [])

    def test_list_external_cli_failure_returns_empty(self):
        fake = FakeRun([(1, "", "boom")])

        with mock.patch("caty_gateway.backends.openclaw.run", side_effect=fake):
            self.assertEqual(self.backend().list_external(), [])


class ClaudeExternalProviderTest(unittest.TestCase):
    def backend(self, cwd):
        return ClaudeCodeBackend(
            claude_bin="/tmp/claude",
            model="",
            cwd=cwd,
            voice_hint="voice-hint\n",
            log=lambda *args: None,
        )

    def project_dir(self, root, cwd):
        slug = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(cwd))
        path = os.path.join(root, slug)
        os.makedirs(path)
        return path

    def write_jsonl(self, directory, stem, records, mtime):
        path = os.path.join(directory, f"{stem}.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record) + "\n")
        os.utime(path, (mtime, mtime))
        return path

    def records(self, first_text, final_text):
        return [
            {
                "type": "user",
                "timestamp": "2026-07-05T01:00:00Z",
                "isSidechain": False,
                "message": {"role": "user", "content": first_text},
            },
            {
                "type": "assistant",
                "timestamp": "2026-07-05T01:00:01Z",
                "isSidechain": True,
                "message": {"role": "assistant", "content": [{"type": "text", "text": "skip me"}]},
            },
            {
                "type": "assistant",
                "timestamp": "2026-07-05T01:00:02Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "name": "ignored"},
                        {"type": "text", "text": final_text},
                    ],
                },
            },
        ]

    def test_list_external_orders_by_mtime_limits_and_truncates_preview(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as cwd:
            project_dir = self.project_dir(root, cwd)
            long_preview = "x" * 100
            self.write_jsonl(project_dir, "older", self.records("older user", "older final"), 100)
            self.write_jsonl(project_dir, "newer", self.records("newer user", long_preview), 300)
            self.write_jsonl(project_dir, "middle", self.records("middle user", "middle final"), 200)

            with mock.patch.dict(os.environ, {"CATY_CLAUDE_PROJECTS_DIR": root}, clear=False):
                rows = self.backend(cwd).list_external(limit=2)

        self.assertEqual([row["native_id"] for row in rows], ["newer", "middle"])
        self.assertEqual(rows[0]["label"], "newer user")
        self.assertEqual(len(rows[0]["preview"]), 80)

    def test_list_external_label_falls_back_to_stem_without_user_turns(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as cwd:
            project_dir = self.project_dir(root, cwd)
            self.write_jsonl(project_dir, "assistant-only", [
                {
                    "type": "assistant",
                    "timestamp": "2026-07-05T01:00:02Z",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "assistant final"}]},
                },
            ], 100)

            with mock.patch.dict(os.environ, {"CATY_CLAUDE_PROJECTS_DIR": root}, clear=False):
                rows = self.backend(cwd).list_external()

        self.assertEqual(rows[0]["native_id"], "assistant-only")
        self.assertEqual(rows[0]["label"], "assistant-only")

    def test_read_external_validates_native_id_membership_and_limits(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as cwd:
            project_dir = self.project_dir(root, cwd)
            self.write_jsonl(project_dir, "known-id", self.records("first user", "assistant final"), 100)
            backend = self.backend(cwd)

            with mock.patch.dict(os.environ, {"CATY_CLAUDE_PROJECTS_DIR": root}, clear=False):
                self.assertEqual(backend.read_external("../evil"), [])
                self.assertEqual(backend.read_external("unknown-id"), [])
                turns = backend.read_external("known-id", limit=1)

        self.assertEqual(turns, [{"role": "assistant", "text": "assistant final", "ts": "2026-07-05T01:00:02Z"}])

    def test_read_external_limit_zero_returns_empty(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as cwd:
            project_dir = self.project_dir(root, cwd)
            self.write_jsonl(project_dir, "known-id", self.records("first user", "assistant final"), 100)
            backend = self.backend(cwd)

            with mock.patch.dict(os.environ, {"CATY_CLAUDE_PROJECTS_DIR": root}, clear=False):
                turns = backend.read_external("known-id", limit=0)

        self.assertEqual(turns, [])

    def test_missing_project_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as cwd:
            with mock.patch.dict(os.environ, {"CATY_CLAUDE_PROJECTS_DIR": root}, clear=False):
                backend = self.backend(cwd)
                self.assertEqual(backend.list_external(), [])
                self.assertEqual(backend.read_external("known-id"), [])


if __name__ == "__main__":
    unittest.main()
