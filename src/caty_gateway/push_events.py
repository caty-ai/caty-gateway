"""Thread-safe bounded event log for gateway push delivery."""

from collections import deque
import secrets
import threading
import time


class DuplicateKeyError(Exception):
    """Raised when an event key is reused with different event content."""


_SESSION_ID_SOURCES = ("explicit", "auto", "none")


def _normalize_tracked_source(tracked_source, fallback):
    """Map a stored source to explicit/auto/none.

    Legacy entries stored a bool (session_id_explicit): True is certainly
    "explicit"; False cannot distinguish auto/none, so fall back to the
    caller-supplied source (pre-#812 behavior).
    """
    if tracked_source in _SESSION_ID_SOURCES:
        return tracked_source
    if tracked_source is True:
        return "explicit"
    return fallback


class PushEventQueue:
    def __init__(self, maxlen=200):
        self.maxlen = maxlen
        self.boot_id = secrets.token_hex(4)
        self.seq = 0
        self._events = deque(maxlen=maxlen)
        self._condition = threading.Condition()

    def _cursor(self, seq):
        return f"evt_{self.boot_id}-{seq}"

    def _parse_cursor(self, cursor):
        prefix = "evt_"
        if not isinstance(cursor, str) or not cursor.startswith(prefix):
            return None, None
        value = cursor[len(prefix):]
        boot_id, separator, raw_seq = value.partition("-")
        if not separator:
            return None, None
        try:
            seq = int(raw_seq)
        except ValueError:
            return None, None
        if seq < 0:
            return None, None
        return boot_id, seq

    def publish(
        self,
        kind,
        payload,
        audience,
        session_id=None,
        event_key=None,
        ttl_s=600,
        producer=None,
        session_id_source="explicit",
    ):
        """Publish an event and return its envelope.

        Reusing an idempotency key with identical content returns the original
        envelope without incrementing the sequence.
        """
        envelope, _, _ = self.publish_with_status(
            kind,
            payload,
            audience,
            session_id=session_id,
            session_id_source=session_id_source,
            event_key=event_key,
            ttl_s=ttl_s,
            producer=producer,
        )
        return envelope

    def publish_with_status(
        self,
        kind,
        payload,
        audience,
        session_id=None,
        event_key=None,
        ttl_s=600,
        producer=None,
        session_id_source="explicit",
    ):
        """Publish an event and return ``(envelope, duplicate, session_id_source)``.

        The returned ``session_id_source`` describes the attempt that actually
        produced the returned envelope's ``session_id``: on a duplicate hit it
        is the stored entry's source, otherwise the caller-supplied one.
        """
        if session_id_source not in _SESSION_ID_SOURCES:
            raise ValueError(f"invalid session_id_source: {session_id_source!r}")
        ttl_s = max(10, min(3600, ttl_s))
        with self._condition:
            if event_key is not None:
                for tracked_key, tracked_envelope, tracked_source in self._events:
                    if tracked_key != event_key:
                        continue
                    session_id_explicit = session_id_source == "explicit"
                    tracked_explicit = (
                        tracked_source == "explicit" or tracked_source is True
                    )
                    if (
                        tracked_envelope["kind"] == kind
                        and tracked_envelope["payload"] == payload
                        and tracked_envelope["audience"] == audience
                        and (
                            (
                                not session_id_explicit
                                and not tracked_explicit
                            )
                            or tracked_envelope["session_id"] == session_id
                        )
                    ):
                        return (
                            tracked_envelope,
                            True,
                            _normalize_tracked_source(
                                tracked_source, session_id_source
                            ),
                        )
                    raise DuplicateKeyError(event_key)

            self.seq += 1
            received_at = time.time()
            envelope = {
                "v": 1,
                "id": self._cursor(self.seq),
                "received_at": received_at,
                "expires_at": received_at + ttl_s,
                "audience": audience,
                "session_id": session_id,
                "kind": kind,
                "payload": payload,
                "producer": producer,
            }
            self._events.append((event_key, envelope, session_id_source))
            self._condition.notify_all()
            return envelope, False, session_id_source

    def read(self, cursor=None, wait_s=0, limit=50):
        """Return ``(events, next_cursor, gap)`` after the supplied cursor."""
        wait_s = max(0, min(25, wait_s))
        limit = max(1, min(100, limit))
        deadline = time.monotonic() + wait_s

        with self._condition:
            if cursor is None:
                cursor_seq = self.seq
                next_cursor = self._cursor(cursor_seq)
                gap = False
            else:
                cursor_boot_id, cursor_seq = self._parse_cursor(cursor)
                gap = cursor_boot_id != self.boot_id
                if gap:
                    oldest_seq = self._event_seq(self._events[0][1]) if self._events else self.seq + 1
                    cursor_seq = oldest_seq - 1
                    next_cursor = self._cursor(cursor_seq)
                else:
                    oldest_seq = self._event_seq(self._events[0][1]) if self._events else None
                    if oldest_seq is not None and cursor_seq < oldest_seq:
                        gap = True
                        cursor_seq = oldest_seq - 1
                    next_cursor = self._cursor(cursor_seq)

            while True:
                events, next_cursor = self._read_after(cursor_seq, limit, next_cursor)
                if events or gap:
                    return events, next_cursor, gap

                cursor_seq = self._parse_cursor(next_cursor)[1]
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return [], next_cursor, False
                self._condition.wait(remaining)

    @staticmethod
    def _event_seq(envelope):
        return int(envelope["id"].rsplit("-", 1)[-1])

    def _read_after(self, cursor_seq, limit, next_cursor):
        now = time.time()
        events = []
        for _, envelope, _ in self._events:
            event_seq = self._event_seq(envelope)
            if event_seq <= cursor_seq:
                continue
            next_cursor = envelope["id"]
            if envelope["expires_at"] <= now:
                continue
            events.append(envelope)
            if len(events) >= limit:
                break
        return events, next_cursor
