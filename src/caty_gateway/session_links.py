#!/usr/bin/env python3
"""File-backed sid -> native session-id links for caty_gateway.

This module is deliberately stdlib-only and has no gateway/backend imports.
All public functions are best-effort: disabled or failing storage returns a
None/no-op result instead of raising into the gateway request path.
"""

import datetime
import json
import os
import tempfile
import threading
from typing import Optional


_LOCK = threading.RLock()


def _base_dir():
    raw = os.environ.get("CATY_HISTORY_DIR", "").strip()
    if not raw:
        return None
    return os.path.realpath(os.path.expanduser(raw))


def _links_path():
    base = _base_dir()
    if not base:
        return None
    path = os.path.realpath(os.path.join(base, "links.json"))
    if os.path.commonpath([base, path]) != base:
        return None
    return path


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load(path):
    if not path or not os.path.exists(path):
        return {"links": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"links": {}}
    if not isinstance(data, dict) or not isinstance(data.get("links"), dict):
        return {"links": {}}
    return data


def _write(path, data):
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


def get(sid) -> Optional[dict]:
    try:
        if sid is None:
            return None
        path = _links_path()
        if not path:
            return None
        with _LOCK:
            item = _load(path).get("links", {}).get(str(sid))
        if not isinstance(item, dict):
            return None
        backend = item.get("backend")
        native = item.get("native")
        if not isinstance(backend, str) or not isinstance(native, str):
            return None
        return {"backend": backend, "native": native}
    except Exception:
        return None


def put(sid, backend, native):
    try:
        if sid is None or backend is None or native is None:
            return
        path = _links_path()
        if not path:
            return
        with _LOCK:
            data = _load(path)
            links = data.setdefault("links", {})
            if not isinstance(links, dict):
                links = {}
                data["links"] = links
            links[str(sid)] = {
                "backend": str(backend),
                "native": str(native),
                "created": _now_iso(),
            }
            _write(path, data)
    except Exception:
        return


def find_by_native(native) -> Optional[str]:
    try:
        if native is None:
            return None
        path = _links_path()
        if not path:
            return None
        needle = str(native)
        with _LOCK:
            links = _load(path).get("links", {})
            for sid, item in links.items():
                if isinstance(item, dict) and item.get("native") == needle:
                    return str(sid)
        return None
    except Exception:
        return None
