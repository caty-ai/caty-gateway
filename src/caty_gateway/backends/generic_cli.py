import glob
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from typing import Callable, Iterator, Optional

from .base import Backend
from .openclaw import PTT_HINT


# 新しい per-turn CLI はここへ設定だけを追加する。backend class は増やさない。
PRESETS = {
    "codex": {
        "bin": "codex",
        "new_args": ["exec", "--skip-git-repo-check", "--json", "{prompt}"],
        "resume_args": ["exec", "resume", "{native_id}", "--skip-git-repo-check", "--json", "{prompt}"],
        "parse_spec": {
            "mode": "jsonl_events",
            "session_event": "thread.started",
            "session_field": "thread_id",
            "reply_event": "item.completed",
            "reply_filter": "item.type=agent_message",
            "reply_field": "item.text",
            "resume_missing_re": "no rollout found",
        },
        "external": "codex_rollout",
    },
}


RUN_TIMEOUT_MARGIN = 60


def _dotted(data, path):
    value = data
    for key in (path or "").split("."):
        if not key or not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _truncate(text, limit):
    text = text or ""
    return text if len(text) <= limit else text[:limit]


def _iso_from_timestamp(value):
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return None


def _json_value(name, raw, default, required=False):
    if raw is None or raw == "":
        if default is None and required:
            raise RuntimeError(f"{name} is required for GenericCliBackend")
        return default
    try:
        return json.loads(raw)
    except Exception as e:
        raise RuntimeError(f"{name} must be valid JSON: {e}") from e


class GenericCliBackend(Backend):
    def __init__(
        self,
        preset: Optional[str],
        voice_hint: str,
        log: Callable[..., None],
        resolve_session: Callable[[str], Optional[str]] = None,
        save_session: Callable[[str, str], None] = None,
    ):
        if preset is not None and preset not in PRESETS:
            raise RuntimeError(f"unknown GenericCliBackend preset={preset!r}")
        config = PRESETS.get(preset, {})
        self.name = preset or "generic"
        self.bin = os.environ.get("CATY_GCLI_BIN") or config.get("bin")
        if not self.bin:
            raise RuntimeError("CATY_GCLI_BIN is required for GenericCliBackend")
        self.cwd = os.path.expanduser(os.environ.get("CATY_GCLI_CWD") or "~")
        self.new_args = _json_value(
            "CATY_GCLI_NEW_ARGS", os.environ.get("CATY_GCLI_NEW_ARGS"), config.get("new_args"), required=True
        )
        self.resume_args = _json_value(
            "CATY_GCLI_RESUME_ARGS", os.environ.get("CATY_GCLI_RESUME_ARGS"), config.get("resume_args"), required=True
        )
        self.extra_args = _json_value("CATY_GCLI_EXTRA_ARGS", os.environ.get("CATY_GCLI_EXTRA_ARGS"), [], required=False)
        self.parse_spec = _json_value(
            "CATY_GCLI_PARSE_SPEC", os.environ.get("CATY_GCLI_PARSE_SPEC"), config.get("parse_spec"), required=True
        )
        self.external = os.environ.get("CATY_GCLI_EXTERNAL") or config.get("external") or "none"
        self.session_store = os.path.expanduser(
            os.environ.get("CATY_GCLI_SESSION_STORE")
            or os.path.join(os.environ.get("CODEX_HOME") or "~/.codex", "sessions")
        )
        for name, value in (("NEW_ARGS", self.new_args), ("RESUME_ARGS", self.resume_args), ("EXTRA_ARGS", self.extra_args)):
            if not isinstance(value, list) or not all(isinstance(arg, str) for arg in value):
                raise RuntimeError(f"CATY_GCLI_{name} must be a JSON array of strings")
        if not isinstance(self.parse_spec, dict) or self.parse_spec.get("mode") not in ("jsonl_events", "text"):
            raise RuntimeError("CATY_GCLI_PARSE_SPEC must be an object with mode jsonl_events or text")
        self.voice_hint = voice_hint
        self.log = log
        self.resolve_session = resolve_session or (lambda sid: None)
        self.save_session = save_session or (lambda sid, native: None)
        self._sessions = {}
        self._last_runtime_error_at = 0.0

    def supports_stream(self) -> bool:
        return False

    def health(self) -> bool:
        resolved = self._resolved_bin()
        if not resolved or not os.path.isfile(resolved) or not os.access(resolved, os.X_OK):
            return False
        return (time.monotonic() - self._last_runtime_error_at) >= 30

    def _resolved_bin(self):
        expanded = os.path.expanduser(self.bin)
        return expanded if os.path.dirname(expanded) else shutil.which(expanded)

    def _voice_hint(self, route=None):
        return str(self.voice_hint) + (PTT_HINT if route == "ptt" else "")

    def _args(self, template, prompt, native_id=None):
        # str.format は使わない。ユーザー文中の波括弧を argv 展開させないため。
        values = {"{prompt}": prompt, "{native_id}": native_id}
        return [values.get(arg, arg) for arg in template] + list(self.extra_args)

    def _run(self, args, timeout):
        return subprocess.run(
            [self._resolved_bin() or self.bin] + args,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout + RUN_TIMEOUT_MARGIN,
            cwd=self.cwd,
        )

    def _parse_result(self, proc):
        if self.parse_spec["mode"] == "text":
            reply = (proc.stdout or "").strip()
            if proc.returncode != 0 or not reply:
                detail = (proc.stderr or "no reply in output").strip()
                raise RuntimeError(f"{self.name}失敗: {detail[:300]}")
            return reply, None

        session_id = None
        reply = None
        errors = []
        reply_filter = self.parse_spec.get("reply_filter", "")
        filter_path, _, filter_value = reply_filter.partition("=")
        for line in (proc.stdout or "").splitlines():
            try:
                event = json.loads(line)
            except Exception:
                continue
            if not isinstance(event, dict):
                continue
            if session_id is None and event.get("type") == self.parse_spec.get("session_event"):
                value = _dotted(event, self.parse_spec.get("session_field"))
                if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
                    session_id = value
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            if event.get("type") == "item.completed" and item.get("type") == "error":
                message = item.get("message")
                if message:
                    errors.append(str(message))
            if event.get("type") != self.parse_spec.get("reply_event"):
                continue
            if filter_path and str(_dotted(event, filter_path)) != filter_value:
                continue
            value = _dotted(event, self.parse_spec.get("reply_field"))
            if isinstance(value, str) and value:
                reply = value
        if proc.returncode != 0 or reply is None:
            detail = (errors[-1] if errors else (proc.stderr or "").strip() or "no reply in output")
            raise RuntimeError(f"{self.name}失敗: {str(detail)[:300]}")
        return reply, session_id

    def _run_once(self, template, prompt, timeout, native_id=None):
        return self._run(self._args(template, prompt, native_id), timeout)

    def _save(self, sid, native):
        self._sessions[sid] = native
        try:
            self.save_session(sid, native)
        except Exception as e:
            self.log(f"{self.name} save_session failed: {e!r}")

    def generate(self, text: str, session_id: Optional[str], timeout: int, route: Optional[str] = None) -> str:
        prompt = self._voice_hint(route) + text
        try:
            if not session_id:
                return self._parse_result(self._run_once(self.new_args, prompt, timeout))[0]
            # 同一 sid の並行ターンは new-session を二重生成し得る（後勝ちで一方が孤児化・クラッシュなし）。
            # codex は作成時 id 指定不可のため claude.py の "already in use" 型防御は不可能で、許容とする。
            native = self._sessions.get(session_id)
            if not native:
                try:
                    native = self.resolve_session(session_id)
                except Exception:
                    native = None
            if native:
                proc = self._run_once(self.resume_args, prompt, timeout, native)
                missing_re = self.parse_spec.get("resume_missing_re", "")
                if proc.returncode != 0 and missing_re and re.search(missing_re, proc.stderr or "", re.I):
                    proc = self._run_once(self.new_args, prompt, timeout)
                    reply, new_native = self._parse_result(proc)
                    if new_native:
                        self._save(session_id, new_native)
                    return reply
                reply, resumed_native = self._parse_result(proc)
                if resumed_native and resumed_native != native:
                    self._save(session_id, resumed_native)
                else:
                    self._sessions[session_id] = resumed_native or native
                return reply
            reply, native = self._parse_result(self._run_once(self.new_args, prompt, timeout))
            if native:
                self._save(session_id, native)
            return reply
        except subprocess.TimeoutExpired as e:
            self._last_runtime_error_at = time.monotonic()
            raise RuntimeError(f"{self.name}タイムアウト") from e
        except RuntimeError:
            self._last_runtime_error_at = time.monotonic()
            raise

    def _rollout_files(self):
        if self.external != "codex_rollout":
            return []
        paths = []
        for path in glob.glob(os.path.join(self.session_store, "**", "rollout-*.jsonl"), recursive=True):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    meta = json.loads(f.readline())
                native = _dotted(meta, "payload.session_id") or _dotted(meta, "payload.id")
                if native and _dotted(meta, "payload.cwd") == os.path.realpath(self.cwd):
                    paths.append((os.path.getmtime(path), path, meta))
            except Exception:
                continue
        return sorted(paths, reverse=True)

    def _rollout_turns(self, path):
        turns = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        record = json.loads(line)
                    except Exception:
                        continue
                    kind = _dotted(record, "payload.type")
                    if kind not in ("user_message", "agent_message"):
                        continue
                    text = _dotted(record, "payload.message")
                    if isinstance(text, str) and text.strip():
                        turns.append({"role": "user" if kind == "user_message" else "assistant", "text": text.strip(), "ts": record.get("timestamp")})
        except Exception:
            return []
        return turns

    def list_external(self, limit: int = 30) -> list:
        rows = []
        for mtime, path, meta in self._rollout_files()[:limit]:
            native = _dotted(meta, "payload.session_id") or _dotted(meta, "payload.id")
            turns = self._rollout_turns(path)
            first_user = next((turn["text"] for turn in turns if turn["role"] == "user"), "")
            rows.append({"native_id": native, "label": _truncate(first_user, 60) if first_user else native,
                         "updated_at": _iso_from_timestamp(mtime), "preview": _truncate(turns[-1]["text"] if turns else "", 80)})
        return rows

    def read_external(self, native_id: str, limit: int = 50) -> list:
        if self.external != "codex_rollout" or not re.fullmatch(r"[A-Za-z0-9-]+", native_id or ""):
            return []
        for _, path, meta in self._rollout_files():
            if (_dotted(meta, "payload.session_id") or _dotted(meta, "payload.id")) == native_id:
                turns = self._rollout_turns(path)
                return turns[-limit:] if limit > 0 else []
        return []

    def stream(self, text: str, session_id: Optional[str], timeout: int, route: Optional[str] = None) -> Iterator[str]:
        raise NotImplementedError("generic CLI backend does not support streaming")
