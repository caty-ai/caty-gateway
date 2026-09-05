import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


from caty_gateway import caty_gateway as cg
from caty_gateway import push_events
from tests.test_config_api import MemoryServer, MemorySocket


def event_payload(url="https://example.com", title="Example"):
    return {"url": url, "title": title}


class PushEventQueueTest(unittest.TestCase):
    def test_publish_read_basic_round_trip(self):
        queue = push_events.PushEventQueue()
        published = [
            queue.publish("open_url", event_payload(f"https://example.com/{index}"), "all")
            for index in range(3)
        ]

        events, next_cursor, gap = queue.read(f"evt_{queue.boot_id}-0")

        self.assertEqual(events, published)
        self.assertEqual(next_cursor, published[-1]["id"])
        self.assertTrue(gap)

    def test_cursor_advances_across_repeated_reads(self):
        queue = push_events.PushEventQueue()
        first = queue.publish("open_url", event_payload("https://example.com/1"), "all")
        second = queue.publish("open_url", event_payload("https://example.com/2"), "all")

        events, cursor, _ = queue.read(f"evt_{queue.boot_id}-0", limit=1)
        self.assertEqual(events, [first])
        self.assertEqual(cursor, first["id"])

        events, cursor, gap = queue.read(cursor, limit=1)
        self.assertEqual(events, [second])
        self.assertEqual(cursor, second["id"])
        self.assertFalse(gap)

    def test_cursor_none_starts_at_tail(self):
        queue = push_events.PushEventQueue()
        old = queue.publish("open_url", event_payload("https://example.com/old"), "all")
        timer = threading.Timer(
            0.03,
            lambda: queue.publish("open_url", event_payload("https://example.com/new"), "all"),
        )
        timer.start()
        try:
            events, cursor, gap = queue.read(cursor=None, wait_s=0.2)
        finally:
            timer.join()

        self.assertEqual(len(events), 1)
        self.assertNotEqual(events[0]["id"], old["id"])
        self.assertEqual(cursor, events[0]["id"])
        self.assertFalse(gap)

    def test_expired_event_is_skipped_and_advances_cursor(self):
        queue = push_events.PushEventQueue()
        with mock.patch("caty_gateway.push_events.time.time", return_value=100.0):
            event = queue.publish("open_url", event_payload(), "all", ttl_s=10)
        with mock.patch("caty_gateway.push_events.time.time", return_value=111.0):
            events, cursor, _ = queue.read(f"evt_{queue.boot_id}-0")

        self.assertEqual(events, [])
        self.assertEqual(cursor, event["id"])

    def test_ring_overflow_marks_stale_cursor_as_gap(self):
        queue = push_events.PushEventQueue(maxlen=2)
        first = queue.publish("open_url", event_payload("https://example.com/1"), "all")
        second = queue.publish("open_url", event_payload("https://example.com/2"), "all")
        third = queue.publish("open_url", event_payload("https://example.com/3"), "all")

        events, cursor, gap = queue.read(first["id"])

        self.assertEqual(events, [second, third])
        self.assertEqual(cursor, third["id"])
        self.assertTrue(gap)

    def test_boot_id_mismatch_resyncs_from_oldest(self):
        queue = push_events.PushEventQueue()
        event = queue.publish("open_url", event_payload(), "all")

        events, cursor, gap = queue.read("evt_deadbeef-99")

        self.assertEqual(events, [event])
        self.assertEqual(cursor, event["id"])
        self.assertTrue(gap)

    def test_event_key_idempotency_and_conflict(self):
        queue = push_events.PushEventQueue()
        payload = event_payload()
        first = queue.publish("open_url", payload, "all", event_key="same-key")
        duplicate = queue.publish("open_url", payload, "all", event_key="same-key")

        self.assertIs(duplicate, first)
        self.assertEqual(queue.seq, 1)
        with self.assertRaises(push_events.DuplicateKeyError):
            queue.publish(
                "open_url",
                event_payload("https://example.com/different"),
                "all",
                event_key="same-key",
            )
        self.assertEqual(queue.seq, 1)

    def test_event_key_explicit_session_id_mismatch_is_conflict(self):
        queue = push_events.PushEventQueue()
        payload = event_payload()
        first = queue.publish(
            "open_url",
            payload,
            "all",
            session_id="session-1",
            event_key="same-key",
        )

        duplicate = queue.publish(
            "open_url",
            payload,
            "all",
            session_id="session-1",
            event_key="same-key",
        )

        self.assertIs(duplicate, first)

        with self.assertRaises(push_events.DuplicateKeyError):
            queue.publish(
                "open_url",
                payload,
                "all",
                session_id="session-2",
                event_key="same-key",
            )
        self.assertEqual(queue.seq, 1)

    def test_event_key_non_explicit_session_id_change_is_duplicate(self):
        queue = push_events.PushEventQueue()
        payload = event_payload()
        first = queue.publish(
            "open_url",
            payload,
            "all",
            session_id="auto-session-a",
            session_id_source="auto",
            event_key="same-key",
        )

        duplicate = queue.publish(
            "open_url",
            payload,
            "all",
            session_id="auto-session-b",
            session_id_source="auto",
            event_key="same-key",
        )

        self.assertIs(duplicate, first)
        self.assertEqual(queue.seq, 1)

    def test_event_key_explicit_none_session_ids_are_duplicate(self):
        queue = push_events.PushEventQueue()
        payload = event_payload()
        first = queue.publish(
            "open_url",
            payload,
            "all",
            session_id=None,
            session_id_source="explicit",
            event_key="same-key",
        )

        duplicate = queue.publish(
            "open_url",
            payload,
            "all",
            session_id=None,
            session_id_source="explicit",
            event_key="same-key",
        )

        self.assertIs(duplicate, first)
        self.assertEqual(queue.seq, 1)

    def test_event_key_non_explicit_none_session_ids_are_duplicate(self):
        queue = push_events.PushEventQueue()
        payload = event_payload()
        first = queue.publish(
            "open_url",
            payload,
            "all",
            session_id=None,
            session_id_source="auto",
            event_key="same-key",
        )

        duplicate = queue.publish(
            "open_url",
            payload,
            "all",
            session_id=None,
            session_id_source="auto",
            event_key="same-key",
        )

        self.assertIs(duplicate, first)
        self.assertEqual(queue.seq, 1)

    def test_event_key_cross_flag_equal_session_ids_are_duplicate(self):
        queue = push_events.PushEventQueue()
        payload = event_payload()
        first = queue.publish(
            "open_url",
            payload,
            "all",
            session_id="shared-session",
            session_id_source="auto",
            event_key="same-key",
        )

        duplicate = queue.publish(
            "open_url",
            payload,
            "all",
            session_id="shared-session",
            session_id_source="explicit",
            event_key="same-key",
        )

        self.assertIs(duplicate, first)
        self.assertEqual(queue.seq, 1)

    def test_event_key_non_explicit_payload_mismatch_is_still_conflict(self):
        queue = push_events.PushEventQueue()
        queue.publish(
            "open_url",
            event_payload("https://example.com/first"),
            "all",
            session_id="auto-session-a",
            session_id_source="auto",
            event_key="same-key",
        )

        with self.assertRaises(push_events.DuplicateKeyError):
            queue.publish(
                "open_url",
                event_payload("https://example.com/second"),
                "all",
                session_id="auto-session-b",
                session_id_source="auto",
                event_key="same-key",
            )
        self.assertEqual(queue.seq, 1)

    def test_event_key_explicit_then_non_explicit_session_id_mismatch_is_conflict(self):
        queue = push_events.PushEventQueue()
        payload = event_payload()
        queue.publish(
            "open_url",
            payload,
            "all",
            session_id="explicit-session",
            session_id_source="explicit",
            event_key="same-key",
        )

        with self.assertRaises(push_events.DuplicateKeyError):
            queue.publish(
                "open_url",
                payload,
                "all",
                session_id=None,
                session_id_source="auto",
                event_key="same-key",
            )
        self.assertEqual(queue.seq, 1)

    def test_event_key_non_explicit_then_explicit_session_id_mismatch_is_conflict(self):
        queue = push_events.PushEventQueue()
        payload = event_payload()
        queue.publish(
            "open_url",
            payload,
            "all",
            session_id="auto-session",
            session_id_source="auto",
            event_key="same-key",
        )

        with self.assertRaises(push_events.DuplicateKeyError):
            queue.publish(
                "open_url",
                payload,
                "all",
                session_id="explicit-session",
                session_id_source="explicit",
                event_key="same-key",
            )
        self.assertEqual(queue.seq, 1)

    def test_wait_expiry_returns_without_hanging(self):
        queue = push_events.PushEventQueue()
        started = time.monotonic()

        events, cursor, gap = queue.read(wait_s=0.05)
        elapsed = time.monotonic() - started

        self.assertEqual(events, [])
        self.assertEqual(cursor, f"evt_{queue.boot_id}-0")
        self.assertFalse(gap)
        self.assertGreaterEqual(elapsed, 0.04)
        self.assertLess(elapsed, 0.5)

    def test_limit_zero_clamps_to_one_and_wait_expiry_still_returns_empty(self):
        queue = push_events.PushEventQueue()
        first = queue.publish("open_url", event_payload("https://example.com/1"), "all")
        queue.publish("open_url", event_payload("https://example.com/2"), "all")

        events, cursor, gap = queue.read(f"evt_{queue.boot_id}-0", limit=0)

        self.assertEqual(events, [first])
        self.assertEqual(cursor, first["id"])
        self.assertTrue(gap)

        empty_queue = push_events.PushEventQueue()
        started = time.monotonic()
        events, cursor, gap = empty_queue.read(wait_s=0.02, limit=0)
        elapsed = time.monotonic() - started

        self.assertEqual(events, [])
        self.assertEqual(cursor, f"evt_{empty_queue.boot_id}-0")
        self.assertFalse(gap)
        self.assertGreaterEqual(elapsed, 0.015)

    def test_boot_id_mismatch_on_empty_queue_returns_gap(self):
        queue = push_events.PushEventQueue()

        events, cursor, gap = queue.read("evt_deadbeef-99")

        self.assertEqual(events, [])
        self.assertEqual(cursor, f"evt_{queue.boot_id}-0")
        self.assertTrue(gap)

    def test_unparseable_cursor_sequences_resync_without_crashing(self):
        queue = push_events.PushEventQueue()

        for cursor in (f"evt_{queue.boot_id}--5", f"evt_{queue.boot_id}-abc"):
            with self.subTest(cursor=cursor):
                self.assertEqual(queue._parse_cursor(cursor), (None, None))
                events, next_cursor, gap = queue.read(cursor)
                self.assertEqual(events, [])
                self.assertEqual(next_cursor, f"evt_{queue.boot_id}-0")
                self.assertTrue(gap)

    def test_ttl_is_clamped_to_minimum_and_maximum(self):
        queue = push_events.PushEventQueue()
        with mock.patch("caty_gateway.push_events.time.time", side_effect=[100.0, 200.0]):
            minimum = queue.publish("open_url", event_payload("https://example.com/min"), "all", ttl_s=-1)
            maximum = queue.publish("open_url", event_payload("https://example.com/max"), "all", ttl_s=9999)

        self.assertEqual(minimum["expires_at"], 110.0)
        self.assertEqual(maximum["expires_at"], 3800.0)


class PushRoutesTest(unittest.TestCase):
    def setUp(self):
        self.old_token = cg.CATY_TOKEN
        self.old_admin_token = cg.CATY_ADMIN_TOKEN
        self.old_queue = cg.PUSH_EVENTS
        self.old_require_auth = os.environ.get("CATY_REQUIRE_AUTH")
        cg.CATY_TOKEN = "member-secret"
        cg.CATY_ADMIN_TOKEN = ""
        cg.PUSH_EVENTS = push_events.PushEventQueue()
        os.environ.pop("CATY_REQUIRE_AUTH", None)

    def tearDown(self):
        cg.CATY_TOKEN = self.old_token
        cg.CATY_ADMIN_TOKEN = self.old_admin_token
        cg.PUSH_EVENTS = self.old_queue
        if self.old_require_auth is None:
            os.environ.pop("CATY_REQUIRE_AUTH", None)
        else:
            os.environ["CATY_REQUIRE_AUTH"] = self.old_require_auth

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
        return status, json.loads(rest[:length])

    def json_request(self, path, payload, token="member-secret"):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Caty-Token"] = token
        return self.request("POST", path, json.dumps(payload).encode(), headers)

    def valid_push(self, **changes):
        payload = {
            "kind": "open_url",
            "payload": event_payload(),
            "audience": "all",
        }
        payload.update(changes)
        return payload

    def test_events_requires_valid_read_auth(self):
        for headers in ({}, {"X-Caty-Token": "wrong"}):
            status, body = self.request("GET", "/events", headers=headers)
            self.assertEqual(status, 401)
            self.assertEqual(body, {"ok": False, "error": "unauthorized"})

    def test_push_rejects_missing_and_invalid_write_auth(self):
        for token in (None, "wrong"):
            status, body = self.json_request("/push", self.valid_push(), token=token)
            self.assertEqual(status, 401)
            self.assertEqual(body, {"ok": False, "error": "unauthorized"})

        cg.CATY_TOKEN = ""
        status, body = self.json_request("/push", self.valid_push(), token=None)
        self.assertEqual(status, 403)
        self.assertEqual(body, {"ok": False, "error": "writes disabled: no token configured"})

    def test_push_rejects_unimplemented_kind(self):
        status, body = self.json_request("/push", self.valid_push(kind="banner"))
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "kind not enabled")

    def test_push_accepts_media_kind_and_passes_media_type_through(self):
        payload = self.valid_push(kind="media")
        payload["payload"] = dict(event_payload(), media_type="video")
        status, body = self.json_request("/push", payload)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

        events, _, _ = cg.PUSH_EVENTS.read(f"evt_{cg.PUSH_EVENTS.boot_id}-0")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "media")
        self.assertEqual(events[0]["payload"]["media_type"], "video")

    def test_push_media_kind_still_validates_url(self):
        payload = self.valid_push(kind="media")
        payload["payload"] = event_payload(url="ftp://example.com/file.jpg")
        status, body = self.json_request("/push", payload)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid url")

    def test_push_media_kind_accepts_omitted_media_type(self):
        status, body = self.json_request("/push", self.valid_push(kind="media"))
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_push_rejects_unknown_media_type(self):
        payload = self.valid_push(kind="media")
        payload["payload"] = dict(event_payload(), media_type="script")
        status, body = self.json_request("/push", payload)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid media_type")

    def test_push_open_url_kind_still_accepted(self):
        status, body = self.json_request("/push", self.valid_push())
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_push_requires_explicit_audience(self):
        payload = self.valid_push()
        del payload["audience"]
        status, body = self.json_request("/push", payload)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "audience required")

    def test_push_rejects_invalid_member_audiences(self):
        for audience in (
            {"member": ""},
            {"member": "x", "extra": 1},
            {"member": 123},
        ):
            with self.subTest(audience=audience):
                status, body = self.json_request(
                    "/push",
                    self.valid_push(audience=audience),
                )
                self.assertEqual(status, 400)
                self.assertEqual(body["error"], "audience required")

    def test_push_validates_title(self):
        invalid_payloads = (
            {"url": "https://example.com"},
            event_payload(title=""),
            event_payload(title="x" * 201),
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                status, body = self.json_request(
                    "/push",
                    self.valid_push(payload=payload),
                )
                self.assertEqual(status, 400)
                expected = "title too long" if payload.get("title") else "title required"
                self.assertEqual(body["error"], expected)

    def test_push_rejects_non_string_event_key(self):
        status, body = self.json_request("/push", self.valid_push(event_key=123))

        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid event_key")

    def test_push_rejects_non_object_payload(self):
        for payload in ("payload", ["payload"]):
            with self.subTest(payload=payload):
                status, body = self.json_request(
                    "/push",
                    self.valid_push(payload=payload),
                )
                self.assertEqual(status, 400)
                self.assertEqual(body["error"], "invalid payload")

    def test_push_rejects_non_object_json_body(self):
        for payload in (["push"], "push"):
            with self.subTest(payload=payload):
                status, body = self.json_request("/push", payload)
                self.assertEqual(status, 400)
                self.assertEqual(body["error"], "json object required")

    def test_push_rejects_disallowed_url_schemes(self):
        for url in ("javascript:alert(1)", "file:///tmp/a", "shortcuts://run-shortcut"):
            with self.subTest(url=url):
                status, body = self.json_request(
                    "/push",
                    self.valid_push(payload=event_payload(url)),
                )
                self.assertEqual(status, 400)
                self.assertEqual(body["error"], "invalid url")

    def test_push_rejects_urls_with_userinfo(self):
        for url in ("https://" + "user:pass" + "@evil.invalid/x", "https://" + "user" + "@evil.invalid/x"):
            with self.subTest(url=url):
                status, body = self.json_request(
                    "/push",
                    self.valid_push(payload=event_payload(url)),
                )
                self.assertEqual(status, 400)
                self.assertEqual(body["error"], "invalid url")

    def test_push_rejects_url_over_2048_characters(self):
        url = "https://example.com/" + "a" * 2049
        status, body = self.json_request("/push", self.valid_push(payload=event_payload(url)))
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "url too long")

    def test_events_response_shape_for_absent_and_garbage_cursor(self):
        headers = {"X-Caty-Token": "member-secret"}
        for path in ("/events", "/events?cursor=garbage"):
            with self.subTest(path=path):
                status, body = self.request("GET", path, headers=headers)
                self.assertEqual(status, 200)
                self.assertEqual(set(body), {"ok", "events", "next_cursor", "gap"})
                self.assertTrue(body["ok"])
                self.assertIsInstance(body["events"], list)
                self.assertIsInstance(body["next_cursor"], str)
                self.assertIsInstance(body["gap"], bool)

    def test_events_empty_cursor_means_tail(self):
        headers = {"X-Caty-Token": "member-secret"}

        status, body = self.request("GET", "/events?cursor=", headers=headers)

        self.assertEqual(status, 200)
        self.assertFalse(body["gap"])
        self.assertEqual(body["events"], [])
        self.assertIsInstance(body["next_cursor"], str)

    def test_push_success_and_idempotent_duplicate(self):
        payload = self.valid_push(event_key="route-key")
        first_status, first = self.json_request("/push", payload)
        second_status, second = self.json_request("/push", payload)

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["id"], second["id"])

    def test_push_event_key_conflict_returns_409(self):
        first = self.valid_push(event_key="route-key")
        conflict = self.valid_push(
            event_key="route-key",
            payload=event_payload("https://example.com/different"),
        )

        first_status, _ = self.json_request("/push", first)
        conflict_status, conflict_body = self.json_request("/push", conflict)

        self.assertEqual(first_status, 200)
        self.assertEqual(conflict_status, 409)
        self.assertEqual(conflict_body, {"ok": False, "error": "event_key conflict"})

    def test_push_then_events_advances_cursor_without_replaying(self):
        headers = {"X-Caty-Token": "member-secret"}
        _, initial = self.request("GET", "/events", headers=headers)
        first_payload = self.valid_push(
            payload=event_payload("https://example.com/first", "First"),
            audience={"member": "caty"},
        )
        first_status, first_response = self.json_request("/push", first_payload)
        first_events_status, first_events = self.request(
            "GET",
            f"/events?cursor={initial['next_cursor']}",
            headers=headers,
        )

        self.assertEqual(first_status, 200)
        self.assertEqual(first_events_status, 200)
        self.assertEqual(len(first_events["events"]), 1)
        first_envelope = first_events["events"][0]
        self.assertEqual(first_envelope["kind"], first_payload["kind"])
        self.assertEqual(first_envelope["payload"], first_payload["payload"])
        self.assertEqual(first_envelope["audience"], first_payload["audience"])
        self.assertEqual(first_events["next_cursor"], first_response["id"])

        second_payload = self.valid_push(
            payload=event_payload("https://example.com/second", "Second")
        )
        second_status, second_response = self.json_request("/push", second_payload)
        second_events_status, second_events = self.request(
            "GET",
            f"/events?cursor={first_events['next_cursor']}",
            headers=headers,
        )

        self.assertEqual(second_status, 200)
        self.assertEqual(second_events_status, 200)
        self.assertEqual([event["id"] for event in second_events["events"]], [second_response["id"]])
        self.assertEqual(second_events["events"][0]["payload"], second_payload["payload"])
        self.assertEqual(second_events["next_cursor"], second_response["id"])

    def test_events_query_limit_clamp_and_invalid_wait(self):
        headers = {"X-Caty-Token": "member-secret"}
        _, initial = self.request("GET", "/events", headers=headers)
        for index in range(3):
            self.json_request(
                "/push",
                self.valid_push(payload=event_payload(f"https://example.com/{index}")),
            )

        for limit in (1, 0):
            with self.subTest(limit=limit):
                status, body = self.request(
                    "GET",
                    f"/events?cursor={initial['next_cursor']}&limit={limit}",
                    headers=headers,
                )
                self.assertEqual(status, 200)
                self.assertEqual(len(body["events"]), 1)

        tail_cursor = f"evt_{cg.PUSH_EVENTS.boot_id}-{cg.PUSH_EVENTS.seq}"
        started = time.monotonic()
        status, body = self.request(
            "GET",
            f"/events?cursor={tail_cursor}&wait=abc",
            headers=headers,
        )
        elapsed = time.monotonic() - started

        self.assertEqual(status, 200)
        self.assertEqual(body["events"], [])
        self.assertLess(elapsed, 0.5)

    def test_push_rejects_body_over_16kb(self):
        raw = json.dumps({"padding": "x" * (16 * 1024)}).encode()
        status, body = self.request(
            "POST",
            "/push",
            raw,
            headers={
                "Content-Type": "application/json",
                "X-Caty-Token": "member-secret",
            },
        )

        self.assertEqual(status, 413)
        self.assertEqual(body, {"ok": False, "error": "payload too large"})

    def test_push_accepts_whole_float_ttl_and_rejects_fraction(self):
        headers = {"X-Caty-Token": "member-secret"}
        _, initial = self.request("GET", "/events", headers=headers)
        with mock.patch("caty_gateway.push_events.time.time", return_value=100.0):
            accepted_status, _ = self.json_request("/push", self.valid_push(ttl_s=600.0))
            _, events_body = self.request(
                "GET",
                f"/events?cursor={initial['next_cursor']}",
                headers=headers,
            )

        rejected_status, rejected_body = self.json_request(
            "/push",
            self.valid_push(ttl_s=600.5),
        )

        self.assertEqual(accepted_status, 200)
        self.assertEqual(events_body["events"][0]["expires_at"], 700.0)
        self.assertEqual(rejected_status, 400)
        self.assertEqual(rejected_body["error"], "invalid ttl_s")

    def test_push_producer_round_trip(self):
        headers = {"X-Caty-Token": "member-secret"}
        _, initial = self.request("GET", "/events", headers=headers)

        status, _ = self.json_request(
            "/push",
            self.valid_push(producer="some-agent"),
        )
        _, events_body = self.request(
            "GET",
            f"/events?cursor={initial['next_cursor']}",
            headers=headers,
        )

        self.assertEqual(status, 200)
        self.assertEqual(events_body["events"][0]["producer"], "some-agent")

    def test_push_rejects_invalid_producer(self):
        for producer in (123, "x" * 101):
            with self.subTest(producer=producer):
                status, body = self.json_request(
                    "/push",
                    self.valid_push(producer=producer),
                )
                self.assertEqual(status, 400)
                self.assertEqual(body["error"], "invalid producer")


class ScreenPushHintTest(unittest.TestCase):
    """#782: push 案内は「話し方」(voice_hint) と別枠の常時注入で、編集経路に混入しない。"""

    def setUp(self):
        self.old_backend = cg.BACKEND_NAME
        # 実マシンの overlay/env を読ませない（#782 の前提である「汚染された
        # overlay」が存在する機械では、隔離なしだと本クラスが誤検知で赤くなる）。
        self.tmpdir = tempfile.TemporaryDirectory()
        self.old_config_dir = os.environ.get("CATY_CONFIG_DIR")
        os.environ["CATY_CONFIG_DIR"] = self.tmpdir.name
        self.old_env_hint = os.environ.pop("CATY_VOICE_HINT", None)

    def tearDown(self):
        cg.BACKEND_NAME = self.old_backend
        if self.old_config_dir is None:
            os.environ.pop("CATY_CONFIG_DIR", None)
        else:
            os.environ["CATY_CONFIG_DIR"] = self.old_config_dir
        if self.old_env_hint is not None:
            os.environ["CATY_VOICE_HINT"] = self.old_env_hint
        self.tmpdir.cleanup()

    def test_live_hint_appends_push_guidance_for_openclaw(self):
        cg.BACKEND_NAME = "openclaw"
        hint = str(cg.LiveVoiceHint())
        self.assertIn("caty_gateway.caty_push", hint)

    def test_live_hint_omits_push_guidance_for_member_backends(self):
        for backend in ("hermes", "claude"):
            with self.subTest(backend=backend):
                cg.BACKEND_NAME = backend
                self.assertNotIn("caty_gateway.caty_push", str(cg.LiveVoiceHint()))

    def test_push_guidance_survives_custom_voice_hint_overlay(self):
        cg.BACKEND_NAME = "openclaw"
        with mock.patch.object(
            cg, "resolved_config", return_value={"voice_hint": "カスタム話し方。"}
        ):
            hint = str(cg.LiveVoiceHint())
        self.assertTrue(hint.startswith("カスタム話し方。"))
        self.assertIn("caty_gateway.caty_push", hint)

    def test_suffix_concatenation_keeps_push_guidance_before_suffix(self):
        # backends は voice_hint + PTT_HINT + user_text の `+` 連結で使う:
        # 案内は話し方の直後・後続ヒントの前に居なければならない。
        cg.BACKEND_NAME = "openclaw"
        combined = cg.LiveVoiceHint() + "（後続ヒント）ユーザー発話"
        self.assertIn("caty_gateway.caty_push", combined)
        self.assertLess(
            combined.index("caty_gateway.caty_push"),
            combined.index("（後続ヒント）"),
        )

    def test_config_payload_voice_hint_excludes_push_guidance(self):
        cg.BACKEND_NAME = "openclaw"
        payload_hint = cg.config_payload()["voice_hint"]
        for fragment in ("caty_gateway.caty_push", "画面表示", "PUSH.md"):
            self.assertNotIn(fragment, payload_hint)

    def test_default_voice_hint_no_longer_embeds_push_guidance(self):
        for fragment in ("caty_gateway.caty_push", "画面表示", "PUSH.md"):
            self.assertNotIn(fragment, cg.DEFAULT_VOICE_HINT)

    def test_screen_push_hint_points_at_installed_helper(self):
        import importlib.util

        self.assertIsNotNone(importlib.util.find_spec("caty_gateway.caty_push"))
        self.assertIn("caty_gateway.caty_push", cg._screen_push_hint())


if __name__ == "__main__":
    unittest.main()
