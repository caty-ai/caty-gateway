import json
import os
import sys
import unittest


from caty_gateway import caty_gateway as cg
from caty_gateway import push_events
from tests.test_config_api import MemoryServer, MemorySocket


def event_payload(url="https://example.com", title="Example"):
    return {"url": url, "title": title}


class PushSessionAttachTest(unittest.TestCase):
    def setUp(self):
        self.old_token = cg.CATY_TOKEN
        self.old_admin_token = cg.CATY_ADMIN_TOKEN
        self.old_queue = cg.PUSH_EVENTS
        with cg.LAST_VOICE_SESSION_LOCK:
            self.old_voice_session = dict(cg.LAST_VOICE_SESSION)
            cg.LAST_VOICE_SESSION.update(id=None, at=0.0)
        cg.CATY_TOKEN = "member-secret"
        cg.CATY_ADMIN_TOKEN = ""
        cg.PUSH_EVENTS = push_events.PushEventQueue()

    def tearDown(self):
        cg.CATY_TOKEN = self.old_token
        cg.CATY_ADMIN_TOKEN = self.old_admin_token
        cg.PUSH_EVENTS = self.old_queue
        with cg.LAST_VOICE_SESSION_LOCK:
            cg.LAST_VOICE_SESSION.update(self.old_voice_session)

    def request(self, payload):
        body = json.dumps(payload).encode()
        headers = {
            "Host": "127.0.0.1",
            "Connection": "close",
            "Content-Length": str(len(body)),
            "Content-Type": "application/json",
            "X-Caty-Token": "member-secret",
        }
        raw = ["POST /push HTTP/1.1"]
        raw.extend(f"{key}: {value}" for key, value in headers.items())
        sock = MemorySocket(("\r\n".join(raw) + "\r\n\r\n").encode("latin-1") + body)
        cg.Handler(sock, ("127.0.0.1", 0), MemoryServer())
        head, _, rest = sock.output.getvalue().partition(b"\r\n\r\n")
        lines = head.decode("iso-8859-1").split("\r\n")
        status = int(lines[0].split()[1])
        response_headers = dict(
            line.split(":", 1) for line in lines[1:] if ":" in line
        )
        length = int(response_headers["Content-Length"])
        return status, json.loads(rest[:length])

    def valid_push(self, **changes):
        payload = {"kind": "open_url", "payload": event_payload(), "audience": "all"}
        payload.update(changes)
        return payload

    def published_event(self):
        events, _, _ = cg.PUSH_EVENTS.read(f"evt_{cg.PUSH_EVENTS.boot_id}-0")
        self.assertEqual(len(events), 1)
        return events[0]

    def test_missing_session_id_attaches_recent_voice_session(self):
        cg.record_voice_session("voice-session")

        status, body = self.request(self.valid_push())

        self.assertEqual(status, 200)
        self.assertEqual(body["session_id"], "voice-session")
        self.assertEqual(body["session_id_source"], "auto")
        self.assertEqual(self.published_event()["session_id"], "voice-session")

    def test_explicit_session_id_is_not_overwritten_by_recent_voice_session(self):
        cg.record_voice_session("voice-session")

        status, body = self.request(self.valid_push(session_id="explicit-session"))

        self.assertEqual(status, 200)
        self.assertEqual(body["session_id"], "explicit-session")
        self.assertEqual(body["session_id_source"], "explicit")
        self.assertEqual(self.published_event()["session_id"], "explicit-session")

    def test_expired_voice_session_is_not_attached(self):
        cg.record_voice_session("expired-session")
        with cg.LAST_VOICE_SESSION_LOCK:
            cg.LAST_VOICE_SESSION["at"] -= 301.0

        status, body = self.request(self.valid_push())

        self.assertEqual(status, 200)
        self.assertIsNone(body["session_id"])
        self.assertEqual(body["session_id_source"], "none")
        self.assertIsNone(self.published_event()["session_id"])

    def test_event_key_retry_with_changed_auto_session_is_duplicate(self):
        cg.record_voice_session("voice-session-a")
        payload = self.valid_push(event_key="same-key")

        first_status, first = self.request(payload)
        cg.record_voice_session("voice-session-b")
        second_status, second = self.request(payload)

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["session_id"], "voice-session-a")
        self.assertEqual(second["session_id_source"], "auto")

    def test_event_key_retry_after_auto_session_expiry_is_duplicate(self):
        cg.record_voice_session("voice-session")
        payload = self.valid_push(event_key="same-key")

        first_status, first = self.request(payload)
        with cg.LAST_VOICE_SESSION_LOCK:
            cg.LAST_VOICE_SESSION["at"] -= 301.0
        second_status, second = self.request(payload)

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["session_id"], "voice-session")
        # 返却する session_id を実際に生んだのは初回の auto 解決 — リトライ時点の
        # "none" ではなく保存側 source を返す (#812)。
        self.assertEqual(second["session_id_source"], "auto")

    def test_event_key_explicit_then_matching_auto_retry_returns_stored_source(self):
        first_status, first = self.request(
            self.valid_push(event_key="same-key", session_id="voice-session")
        )
        cg.record_voice_session("voice-session")
        second_status, second = self.request(self.valid_push(event_key="same-key"))

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertFalse(first["duplicate"])
        self.assertEqual(first["session_id_source"], "explicit")
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["session_id"], "voice-session")
        self.assertEqual(second["session_id_source"], "explicit")

    def test_legacy_bool_true_entry_reports_explicit_source(self):
        queue = cg.PUSH_EVENTS
        queue.publish(
            "open_url", event_payload(), "all",
            session_id="stored-session", event_key="legacy-key",
        )
        tracked_key, tracked_envelope, _ = queue._events[-1]
        queue._events[-1] = (tracked_key, tracked_envelope, True)

        envelope, duplicate, source = queue.publish_with_status(
            "open_url", event_payload(), "all",
            session_id="stored-session", session_id_source="explicit",
            event_key="legacy-key",
        )

        self.assertTrue(duplicate)
        self.assertEqual(envelope["session_id"], "stored-session")
        self.assertEqual(source, "explicit")

    def test_legacy_bool_false_entry_falls_back_to_request_source(self):
        queue = cg.PUSH_EVENTS
        queue.publish(
            "open_url", event_payload(), "all",
            session_id="stored-session", session_id_source="auto",
            event_key="legacy-key",
        )
        tracked_key, tracked_envelope, _ = queue._events[-1]
        queue._events[-1] = (tracked_key, tracked_envelope, False)

        envelope, duplicate, source = queue.publish_with_status(
            "open_url", event_payload(), "all",
            session_id=None, session_id_source="none",
            event_key="legacy-key",
        )

        self.assertTrue(duplicate)
        self.assertEqual(envelope["session_id"], "stored-session")
        # legacy False は auto/none を区別できない — #812 以前の挙動
        # (リトライ時点の source) に縮退する。
        self.assertEqual(source, "none")

    def test_stored_none_source_is_returned_verbatim_on_duplicate(self):
        queue = cg.PUSH_EVENTS
        queue.publish(
            "open_url", event_payload(), "all",
            session_id=None, session_id_source="none", event_key="k",
        )

        _, duplicate, source = queue.publish_with_status(
            "open_url", event_payload(), "all",
            session_id=None, session_id_source="auto", event_key="k",
        )

        self.assertTrue(duplicate)
        self.assertEqual(source, "none")

    def test_unknown_stored_source_falls_back_to_request_source(self):
        queue = cg.PUSH_EVENTS
        queue.publish(
            "open_url", event_payload(), "all",
            session_id=None, session_id_source="none", event_key="k",
        )
        tracked_key, tracked_envelope, _ = queue._events[-1]
        queue._events[-1] = (tracked_key, tracked_envelope, "True")

        _, duplicate, source = queue.publish_with_status(
            "open_url", event_payload(), "all",
            session_id=None, session_id_source="auto", event_key="k",
        )

        self.assertTrue(duplicate)
        self.assertEqual(source, "auto")

    def test_legacy_bool_false_entry_with_auto_retry_falls_back_to_auto(self):
        queue = cg.PUSH_EVENTS
        queue.publish(
            "open_url", event_payload(), "all",
            session_id="stored-session", session_id_source="auto", event_key="k",
        )
        tracked_key, tracked_envelope, _ = queue._events[-1]
        queue._events[-1] = (tracked_key, tracked_envelope, False)

        _, duplicate, source = queue.publish_with_status(
            "open_url", event_payload(), "all",
            session_id="other-session", session_id_source="auto", event_key="k",
        )

        self.assertTrue(duplicate)
        self.assertEqual(source, "auto")

    def test_legacy_bool_true_entry_with_auto_retry_reports_explicit_source(self):
        queue = cg.PUSH_EVENTS
        queue.publish(
            "open_url", event_payload(), "all",
            session_id="stored-session", event_key="k",
        )
        tracked_key, tracked_envelope, _ = queue._events[-1]
        queue._events[-1] = (tracked_key, tracked_envelope, True)

        _, duplicate, source = queue.publish_with_status(
            "open_url", event_payload(), "all",
            session_id="stored-session", session_id_source="auto", event_key="k",
        )

        self.assertTrue(duplicate)
        self.assertEqual(source, "explicit")

    def test_invalid_session_id_source_raises_value_error(self):
        queue = cg.PUSH_EVENTS
        for bad in ("Explicit", "", None, True):
            with self.assertRaises(ValueError):
                queue.publish_with_status(
                    "open_url", event_payload(), "all",
                    session_id=None, session_id_source=bad,
                )

    def test_event_key_explicit_session_id_conflict_returns_409(self):
        first_status, _ = self.request(
            self.valid_push(event_key="same-key", session_id="session-a")
        )
        second_status, second = self.request(
            self.valid_push(event_key="same-key", session_id="session-b")
        )

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 409)
        self.assertEqual(second, {"ok": False, "error": "event_key conflict"})

    def test_event_key_explicit_then_auto_session_mismatch_returns_409(self):
        first_status, _ = self.request(
            self.valid_push(event_key="same-key", session_id="explicit-session")
        )
        cg.record_voice_session("auto-session")
        second_status, second = self.request(self.valid_push(event_key="same-key"))

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 409)
        self.assertEqual(second, {"ok": False, "error": "event_key conflict"})

    def test_event_key_auto_then_explicit_session_mismatch_returns_409(self):
        cg.record_voice_session("auto-session")
        first_status, _ = self.request(self.valid_push(event_key="same-key"))
        second_status, second = self.request(
            self.valid_push(event_key="same-key", session_id="explicit-session")
        )

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 409)
        self.assertEqual(second, {"ok": False, "error": "event_key conflict"})

    def test_empty_voice_session_does_not_replace_recent_session(self):
        cg.record_voice_session("voice-session")
        with cg.LAST_VOICE_SESSION_LOCK:
            before = dict(cg.LAST_VOICE_SESSION)

        cg.record_voice_session("")

        with cg.LAST_VOICE_SESSION_LOCK:
            self.assertEqual(cg.LAST_VOICE_SESSION, before)

    def test_explicit_empty_session_id_does_not_fall_back_to_auto(self):
        cg.record_voice_session("voice-session")

        status, body = self.request(self.valid_push(session_id=""))

        self.assertEqual(status, 400)
        self.assertEqual(body, {"ok": False, "error": "invalid session_id"})

    def test_rejection_preserves_existing_error_string(self):
        status, body = self.request(self.valid_push(kind="banner"))

        self.assertEqual(status, 400)
        self.assertEqual(body, {"ok": False, "error": "kind not enabled"})
