#!/usr/bin/env python3
"""File-backed conversation history for caty_gateway.

This module is deliberately stdlib-only and has no gateway/openclaw imports.
All public functions are best-effort: disabled or failing storage returns an
empty/no-op result instead of raising into the gateway request path.
"""

import datetime
import json
import os
import re
import sys
import tempfile
import threading


_SESSION_RE = re.compile(r"[^A-Za-z0-9._-]")
_LOCK = threading.RLock()


def _log(*args):
    print("[history_store]", *args, file=sys.stderr, flush=True)


def enabled():
    return bool(os.environ.get("CATY_HISTORY_DIR", "").strip())


def _base_dir():
    raw = os.environ.get("CATY_HISTORY_DIR", "").strip()
    if not raw:
        return None
    return os.path.realpath(os.path.expanduser(raw))


def _sanitize_session_id(raw):
    if not raw:
        return None
    text = str(raw).strip()
    sanitized = _SESSION_RE.sub("", text)
    if not sanitized or sanitized != text:
        return None
    return sanitized


def _safe_path(base, *parts):
    path = os.path.realpath(os.path.join(base, *parts))
    if os.path.commonpath([base, path]) != base:
        return None
    return path


def _session_paths(session_id):
    base = _base_dir()
    sid = _sanitize_session_id(session_id)
    if not base or not sid:
        return None, None, None, None
    sessions = _safe_path(base, "sessions")
    jsonl = _safe_path(base, "sessions", sid + ".jsonl")
    md = _safe_path(base, "sessions", sid + ".md")
    index = _safe_path(base, "index.json")
    if not sessions or not jsonl or not md or not index:
        return None, None, None, None
    return sessions, jsonl, md, index


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read_jsonl(path):
    turns = []
    if not path or not os.path.exists(path):
        return turns
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                turns.append(obj)
    return turns


def _write_jsonl(path, turns):
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".history-", suffix=".jsonl", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for turn in turns:
                f.write(json.dumps(turn, ensure_ascii=False, separators=(",", ":")))
                f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _append_jsonl(path, turn):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(turn, ensure_ascii=False, separators=(",", ":")))
        f.write("\n")


def _max_turns():
    raw = os.environ.get("CATY_HISTORY_MAX_TURNS", "0").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _load_index(path):
    if not path or not os.path.exists(path):
        return {"sessions": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"sessions": []}
    if not isinstance(data, dict) or not isinstance(data.get("sessions"), list):
        return {"sessions": []}
    return data


def _is_expired_tombstone(entry):
    """Return True if a tombstone is older than CATY_TOMBSTONE_TTL_DAYS."""
    if not entry.get("archived"):
        return False
    try:
        ttl_days = int(os.environ.get("CATY_TOMBSTONE_TTL_DAYS", "7"))
        at_str = entry.get("archived_at", "")
        at_str = at_str.replace("Z", "+00:00")
        at = datetime.datetime.fromisoformat(at_str)
        now = datetime.datetime.now(datetime.timezone.utc)
        return (now - at).days >= ttl_days
    except Exception:
        return False


def _write_index(path, data):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".history-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _title(text):
    text = " ".join((text or "").split())
    if len(text) <= 40:
        return text
    return text[:40] + "..."


def _upsert_index(path, session_id, role, text, ts, turns_count):
    data = _load_index(path)
    sessions = [s for s in data.get("sessions", []) if isinstance(s, dict)]
    sessions = [s for s in sessions if not _is_expired_tombstone(s)]
    found = None
    for item in sessions:
        if item.get("id") == session_id:
            found = item
            break
    if found is None:
        found = {
            "id": session_id,
            "title": _title(text) if role == "user" else "",
            "created": ts,
            "last_used": ts,
            "turns": turns_count,
        }
        sessions.append(found)
    else:
        found.setdefault("created", ts)
        if not found.get("title") and role == "user":
            found["title"] = _title(text)
        found["last_used"] = ts
        found["turns"] = turns_count
    sessions.sort(key=lambda s: s.get("last_used", ""), reverse=True)
    _write_index(path, {"sessions": sessions})


def _render_md(session_id, turns):
    name = os.environ.get("CATY_NAME", "Caty").strip() or "Caty"
    first_ts = turns[0].get("ts", "") if turns else ""
    lines = [f"# Conversation {session_id} - {name} - {first_ts}", ""]
    for turn in turns:
        role = turn.get("role")
        label = "User" if role == "user" else name
        ts = str(turn.get("ts", ""))
        text = str(turn.get("text", ""))
        lines.append(f"**{label}** ({ts}): {text}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write_md(path, session_id, turns):
    if os.environ.get("CATY_HISTORY_MD", "1") == "0":
        return
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".history-", suffix=".md", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_render_md(session_id, turns))
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def append_turn(session_id, role, text, ts=None):
    if not enabled():
        return
    try:
        if role not in ("user", "assistant"):
            return
        if text is None:
            return
        sessions_dir, jsonl_path, md_path, index_path = _session_paths(session_id)
        sid = _sanitize_session_id(session_id)
        if not sessions_dir or not jsonl_path or not md_path or not index_path or not sid:
            return
        timestamp = ts or _now_iso()
        with _LOCK:
            data = _load_index(index_path)
            for item in data.get("sessions", []):
                if (isinstance(item, dict) and item.get("id") == sid
                        and item.get("archived") and not _is_expired_tombstone(item)):
                    _log("append_turn skipped archived session:", sid)
                    return
            os.makedirs(sessions_dir, exist_ok=True)
            turns = _read_jsonl(jsonl_path)
            last_seq = 0
            for turn in turns:
                try:
                    last_seq = max(last_seq, int(turn.get("seq", 0)))
                except (TypeError, ValueError):
                    continue
            new_turn = {
                "ts": timestamp,
                "seq": last_seq + 1,
                "session_id": sid,
                "role": role,
                "text": str(text),
            }
            turns.append(new_turn)
            cap = _max_turns()
            if cap > 0 and len(turns) > cap:
                turns = turns[-cap:]
                _write_jsonl(jsonl_path, turns)
            else:
                _append_jsonl(jsonl_path, new_turn)
            _upsert_index(index_path, sid, role, str(text), timestamp, len(turns))
            _write_md(md_path, sid, turns)
    except Exception as exc:
        _log("append_turn failed:", repr(exc))


def list_sessions():
    if not enabled():
        return {"sessions": []}
    try:
        base = _base_dir()
        if not base:
            return {"sessions": []}
        index_path = _safe_path(base, "index.json")
        if not index_path:
            return {"sessions": []}
        with _LOCK:
            data = _load_index(index_path)
            all_sessions = [s for s in data.get("sessions", []) if isinstance(s, dict)]
            active = [s for s in all_sessions if not s.get("archived")]
            archived_ids = [
                s["id"] for s in all_sessions
                if s.get("archived") and s.get("id") and not _is_expired_tombstone(s)
            ]
            return {"sessions": active, "archived": archived_ids}
    except Exception as exc:
        _log("list_sessions failed:", repr(exc))
        return {"sessions": []}


def set_title(session_id, title):
    try:
        if not enabled():
            return False
        sid = _sanitize_session_id(session_id)
        clean_title = " ".join(str(title or "").split())
        if not sid or not clean_title:
            return False
        base = _base_dir()
        if not base:
            return False
        index_path = _safe_path(base, "index.json")
        if not index_path:
            return False
        with _LOCK:
            data = _load_index(index_path)
            sessions = [s for s in data.get("sessions", []) if isinstance(s, dict)]
            for item in sessions:
                if item.get("id") == sid and not item.get("archived"):
                    item["title"] = _title(clean_title)
                    _write_index(index_path, {"sessions": sessions})
                    return True
        return False
    except Exception as exc:
        _log("set_title failed:", repr(exc))
        return False


def archive_session(session_id):
    try:
        if not enabled():
            return False
        sid = _sanitize_session_id(session_id)
        if not sid:
            return False
        base = _base_dir()
        if not base:
            return False
        index_path = _safe_path(base, "index.json")
        archived_dir = _safe_path(base, "archived")
        sessions_dir = _safe_path(base, "sessions")
        if not index_path or not archived_dir or not sessions_dir:
            return False
        os.makedirs(archived_dir, exist_ok=True)
        with _LOCK:
            data = _load_index(index_path)
            sessions = [s for s in data.get("sessions", []) if isinstance(s, dict)]
            found_index = None
            for idx, item in enumerate(sessions):
                if item.get("id") == sid:
                    found_index = idx
                    break
            if found_index is None:
                return False
            # Already-tombstoned: idempotent no-op. Do NOT rewrite archived_at
            # (that would reset the TTL countdown and let a re-archive keep the
            # tombstone alive forever) and do NOT report 200 as if a live session
            # was just archived.
            if sessions[found_index].get("archived"):
                return False

            archived_at = _now_iso()
            sessions[found_index] = {"id": sid, "archived": True, "archived_at": archived_at}
            _write_index(index_path, {"sessions": sessions})

            stamp = archived_at.replace(":", "").replace("+", "")
            base_name = f"{sid}-{stamp}"
            for ext in (".jsonl", ".md"):
                src = _safe_path(sessions_dir, f"{sid}{ext}")
                if not src or not os.path.exists(src):
                    continue
                dst = os.path.join(archived_dir, f"{base_name}{ext}")
                n = 1
                while os.path.exists(dst):
                    dst = os.path.join(archived_dir, f"{base_name}-{n}{ext}")
                    n += 1
                try:
                    os.replace(src, dst)
                except Exception as exc:
                    _log("archive_session move failed:", repr(exc))
            return True
    except Exception as exc:
        _log("archive_session failed:", repr(exc))
        return False


def read_session(session_id, since=None, limit=None, before=None):
    if not enabled():
        return []
    try:
        _, jsonl_path, _, _ = _session_paths(session_id)
        if not jsonl_path:
            return []
        with _LOCK:
            turns = _read_jsonl(jsonl_path)
        if since is not None:
            turns = [t for t in turns if _seq(t) > int(since)]
        if before is not None:
            turns = [t for t in turns if _seq(t) < int(before)]
        if limit is not None:
            n = int(limit)
            if n > 0:
                turns = turns[-n:] if before is not None else turns[:n]
        return turns
    except Exception as exc:
        _log("read_session failed:", repr(exc))
        return []


def _seq(turn):
    try:
        return int(turn.get("seq", 0))
    except (TypeError, ValueError):
        return 0
