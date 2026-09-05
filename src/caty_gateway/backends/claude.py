import glob
import json
import os
import queue
import re
import signal
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Callable, Iterator, Optional

from .base import Backend
from .openclaw import PTT_HINT


# Stable namespace for phone session continuity. Changing this would strand existing
# CatyPhone session mappings in Claude Code's local conversation store.
CLAUDE_SESSION_NAMESPACE = uuid.UUID("7a2f9d90-5c8e-48d2-8c47-7e8a8b0b7a20")
CLAUDE_STREAM_QUEUE_POLL_SEC = 0.25
CLAUDE_TERMINATE_WAIT_SEC = 2.0


class ClaudeStreamError(RuntimeError):
    """Raised when the Claude CLI stream-json path fails."""


class ClaudeStreamTimeout(TimeoutError):
    """Raised when the Claude CLI stream-json path exceeds its deadline."""


def _iso_from_timestamp(value):
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return None


def _truncate(text, limit):
    text = text or ""
    return text if len(text) <= limit else text[:limit]


def _content_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    texts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
    return "\n".join(texts)


def _parse_transcript(path):
    turns = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                if record.get("isSidechain") is True:
                    continue
                role = record.get("type")
                if role not in ("user", "assistant"):
                    continue
                message = record.get("message") if isinstance(record.get("message"), dict) else {}
                text = _content_text(message.get("content")).strip()
                if not text:
                    continue
                turns.append({"role": role, "text": text, "ts": record.get("timestamp")})
    except Exception:
        return []
    return turns


class ClaudeCodeBackend(Backend):
    def __init__(
        self,
        claude_bin: str,
        model: str,
        cwd: str,
        voice_hint: str,
        log: Callable[..., None],
        resolve_session: Callable[[str], Optional[str]] = None,
    ):
        self.claude_bin = claude_bin
        self.model = model.strip()
        self.cwd = os.path.expanduser(cwd)
        self.voice_hint = voice_hint
        self.log = log
        self.resolve_session = resolve_session or (lambda sid: None)
        self._known_sessions = set()  # 起動後に生存確認済みの sid（初手フラグ選択用）
        self._last_runtime_error_at = 0.0

    def supports_stream(self) -> bool:
        return False

    def health(self) -> bool:
        resolved = self._resolved_bin()
        if not resolved or not os.path.isfile(resolved) or not os.access(resolved, os.X_OK):
            return False
        return (time.monotonic() - self._last_runtime_error_at) >= 30

    def _resolved_bin(self):
        expanded = os.path.expanduser(self.claude_bin)
        if os.path.dirname(expanded):
            return expanded
        return shutil.which(expanded)

    def _projects_dir(self):
        # Test fixtures can redirect this without touching real Claude Code project data.
        root = os.environ.get("CATY_CLAUDE_PROJECTS_DIR") or os.path.expanduser("~/.claude/projects")
        slug = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(self.cwd))
        return os.path.join(root, slug)

    def _external_files(self):
        project_dir = self._projects_dir()
        if not os.path.isdir(project_dir):
            return []
        paths = []
        for path in glob.glob(os.path.join(project_dir, "*.jsonl")):
            try:
                paths.append((os.path.getmtime(path), path))
            except OSError:
                continue
        paths.sort(reverse=True)
        return [path for _, path in paths]

    def _voice_hint(self, route=None):
        return self.voice_hint + (PTT_HINT if route == "ptt" else "")

    def _session_uuid(self, session_id: str) -> str:
        resolved = self.resolve_session(session_id)
        if resolved:
            self._known_sessions.add(resolved)
            return resolved
        return str(uuid.uuid5(CLAUDE_SESSION_NAMESPACE, f"caty-{session_id}"))

    def _base_cmd(self, text: str, route=None):
        # ヒントは system prompt のみ。user text へ前置すると本人の会話履歴に
        # ヒント文が恒久保存される（記憶汚染）ため -p は素の text を渡す。
        cmd = [
            self.claude_bin,
            "-p",
            text,
            "--output-format",
            "json",
            "--append-system-prompt",
            self._voice_hint(route),
        ]
        if self.model:
            cmd += ["--model", self.model]
        return cmd

    def _stream_base_cmd(self, text: str, route=None):
        cmd = [
            self.claude_bin,
            "-p",
            text,
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "--verbose",
            "--append-system-prompt",
            self._voice_hint(route),
        ]
        if self.model:
            cmd += ["--model", self.model]
        return cmd

    def _run(self, cmd, timeout):
        return subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout + 60,
            cwd=self.cwd,
        )

    def _parse_result(self, proc):
        if proc.returncode != 0 and not (proc.stdout or "").strip():
            detail = (proc.stderr or "unknown error").strip()
            raise RuntimeError(f"claude失敗: {detail[:300]}")
        out = proc.stdout or ""
        start = out.find("{")
        if start > 0:
            out = out[start:]  # 先頭バナー等のノイズ耐性（openclaw.py と同方針）
        try:
            data = json.loads(out)
        except Exception as e:
            raise RuntimeError(f"claude JSON解析失敗: {e}") from e

        result = data.get("result")
        if proc.returncode != 0 or data.get("is_error") is True:
            detail = str(result or data.get("error") or proc.stderr or "unknown error").strip()
            raise RuntimeError(f"claude失敗: {detail[:300]}")
        if not isinstance(result, str):
            raise RuntimeError("claude JSON解析失敗: result missing")
        return result

    def _run_once(self, base_cmd, timeout, session_flag=None):
        cmd = list(base_cmd)
        if session_flag:
            cmd += [session_flag[0], session_flag[1]]
        return self._run(cmd, timeout)

    def _spawn_stream_once(self, base_cmd, session_flag=None):
        cmd = list(base_cmd)
        if session_flag:
            cmd += [session_flag[0], session_flag[1]]
        return subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=self.cwd,
            start_new_session=True,
        )

    def _start_reader(self, name, stream, events):
        def pump():
            try:
                for line in stream:
                    events.put((name, line))
            finally:
                events.put((name, None))

        thread = threading.Thread(target=pump, daemon=True)
        thread.start()
        return thread

    def _extract_text_delta(self, line):
        raw = (line or "").strip()
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except ValueError as e:
            raise ClaudeStreamError("claude stream emitted invalid json") from e
        if payload.get("type") == "result":
            if payload.get("is_error") is True or payload.get("subtype") == "error" or payload.get("error"):
                detail = self._sanitize_stream_detail(
                    str(payload.get("result") or payload.get("error") or "unknown error")
                )
                raise ClaudeStreamError(f"claude failed: {detail}")
            return None
        if payload.get("type") != "stream_event":
            return None
        event = payload.get("event")
        if not isinstance(event, dict) or event.get("type") != "content_block_delta":
            return None
        delta = event.get("delta")
        if not isinstance(delta, dict) or delta.get("type") != "text_delta":
            return None
        text = delta.get("text")
        return text if isinstance(text, str) and text else None

    def _sanitize_stream_detail(self, stderr_text):
        detail = (stderr_text or "").strip()
        if "No conversation found" in detail:
            return "No conversation found"
        if "already in use" in detail:
            return "already in use"
        if "Not logged in" in detail or "/login" in detail:
            return "Not logged in"
        if detail:
            return detail[:300]
        return "unknown error"

    def _terminate_stream_process(self, proc):
        if proc is None:
            return
        if proc.poll() is None:
            try:
                if getattr(proc, "pid", None):
                    os.killpg(proc.pid, signal.SIGTERM)
                else:
                    proc.terminate()
            except ProcessLookupError:
                try:
                    proc.terminate()
                except Exception:
                    pass
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass
            try:
                proc.wait(timeout=CLAUDE_TERMINATE_WAIT_SEC)
            except Exception:
                try:
                    if getattr(proc, "pid", None):
                        os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        proc.kill()
                except ProcessLookupError:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                try:
                    proc.wait(timeout=CLAUDE_TERMINATE_WAIT_SEC)
                except Exception:
                    pass
        for stream in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass

    def _stream_attempt(
        self,
        proc,
        deadline,
        on_heartbeat=None,
        heartbeat_interval=5.0,
    ):
        events = queue.Queue()
        threads = [
            self._start_reader("stdout", proc.stdout, events),
            self._start_reader("stderr", proc.stderr, events),
        ]
        eof = set()
        stderr_lines = []
        emitted_text = False
        last_heartbeat = time.monotonic()
        try:
            while len(eof) < 2:
                now = time.monotonic()
                if now >= deadline:
                    raise ClaudeStreamTimeout("claude stream timed out")
                wait_s = min(CLAUDE_STREAM_QUEUE_POLL_SEC, max(0.0, deadline - now))
                try:
                    name, value = events.get(timeout=wait_s)
                except queue.Empty:
                    if on_heartbeat and now - last_heartbeat >= heartbeat_interval:
                        on_heartbeat()
                        last_heartbeat = now
                    continue
                if value is None:
                    eof.add(name)
                    continue
                if name == "stderr":
                    stderr_lines.append(value)
                    continue
                delta = self._extract_text_delta(value)
                if delta is None:
                    continue
                emitted_text = True
                yield delta
                last_heartbeat = time.monotonic()
            try:
                returncode = proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as e:
                raise ClaudeStreamTimeout("claude stream timed out") from e
            if returncode == 0:
                if not emitted_text:
                    raise ClaudeStreamError("claude stream returned no text deltas")
                return
            detail = self._sanitize_stream_detail("".join(stderr_lines))
            raise ClaudeStreamError(f"claude failed: {detail}")
        finally:
            for thread in threads:
                thread.join(timeout=0.1)

    def generate(self, text: str, session_id: Optional[str], timeout: int, route: Optional[str] = None) -> str:
        base_cmd = self._base_cmd(text, route=route)
        try:
            if not session_id:
                return self._parse_result(self._run_once(base_cmd, timeout))

            sid = self._session_uuid(session_id)
            if sid in self._known_sessions:
                # 生存確認済み → resume 先行。transcript 消失時のみ新規作成へ。
                proc = self._run_once(base_cmd, timeout, ("--resume", sid))
                if proc.returncode != 0 and "No conversation found" in (proc.stderr or ""):
                    proc = self._run_once(base_cmd, timeout, ("--session-id", sid))
                    if proc.returncode != 0 and "already in use" in (proc.stderr or ""):
                        # 並行ターンとの作成競合 → 相手が作った会話に resume で合流
                        proc = self._run_once(base_cmd, timeout, ("--resume", sid))
            else:
                # 未知 sid → session-id 先行（新規セッションを subprocess 1回で開始）。
                # gateway 再起動後の既存セッションは already in use → resume で復旧。
                proc = self._run_once(base_cmd, timeout, ("--session-id", sid))
                if proc.returncode != 0 and "already in use" in (proc.stderr or ""):
                    proc = self._run_once(base_cmd, timeout, ("--resume", sid))

            if proc.returncode == 0:
                self._known_sessions.add(sid)
            return self._parse_result(proc)
        except subprocess.TimeoutExpired as e:
            self._last_runtime_error_at = time.monotonic()
            raise RuntimeError("claudeタイムアウト") from e
        except RuntimeError:
            self._last_runtime_error_at = time.monotonic()
            raise

    def list_external(self, limit: int = 30) -> list:
        rows = []
        for path in self._external_files()[:limit]:
            stem = os.path.splitext(os.path.basename(path))[0]
            turns = _parse_transcript(path)
            first_user = next((turn["text"] for turn in turns if turn["role"] == "user"), "")
            preview = turns[-1]["text"] if turns else ""
            try:
                updated_at = _iso_from_timestamp(os.path.getmtime(path))
            except OSError:
                updated_at = None
            rows.append({
                "native_id": stem,
                "label": _truncate(first_user, 60) if first_user else stem,
                "updated_at": updated_at,
                "preview": _truncate(preview, 80),
            })
        return rows

    def read_external(self, native_id: str, limit: int = 50) -> list:
        if not re.match(r"^[A-Za-z0-9-]+$", native_id or ""):
            return []
        files = {
            os.path.splitext(os.path.basename(path))[0]: path
            for path in self._external_files()
        }
        path = files.get(native_id)
        if not path:
            return []
        turns = _parse_transcript(path)
        return turns[-limit:] if limit > 0 else []

    def openai_stream(
        self,
        text: str,
        session_id: Optional[str],
        timeout: int,
        route: Optional[str] = None,
        on_heartbeat=None,
        heartbeat_interval: float = 5.0,
    ) -> Iterator[str]:
        base_cmd = self._stream_base_cmd(text, route=route)
        sid = self._session_uuid(session_id) if session_id else None
        deadline = time.monotonic() + timeout
        current_proc = None
        try:
            if not sid:
                attempts = [None]
            elif sid in self._known_sessions:
                attempts = [("--resume", sid), ("--session-id", sid), ("--resume", sid)]
            else:
                attempts = [("--session-id", sid), ("--resume", sid)]

            for index, session_flag in enumerate(attempts):
                current_proc = self._spawn_stream_once(base_cmd, session_flag=session_flag)
                emitted_text = False
                retry = False
                try:
                    for delta in self._stream_attempt(
                        current_proc,
                        deadline,
                        on_heartbeat=on_heartbeat,
                        heartbeat_interval=heartbeat_interval,
                    ):
                        emitted_text = True
                        yield delta
                    if sid:
                        self._known_sessions.add(sid)
                    return
                except ClaudeStreamError as e:
                    detail = str(e)
                    if (
                        not emitted_text
                        and sid
                        and index == 0
                        and session_flag
                        and session_flag[0] == "--session-id"
                        and "already in use" in detail
                    ):
                        retry = True
                    elif (
                        not emitted_text
                        and sid
                        and index == 0
                        and session_flag
                        and session_flag[0] == "--resume"
                        and "No conversation found" in detail
                    ):
                        retry = True
                    elif (
                        not emitted_text
                        and sid
                        and index == 1
                        and session_flag
                        and session_flag[0] == "--session-id"
                        and "already in use" in detail
                    ):
                        retry = True
                    if retry:
                        self._terminate_stream_process(current_proc)
                        current_proc = None
                        continue
                    self._last_runtime_error_at = time.monotonic()
                    raise
                finally:
                    if current_proc is not None and current_proc.poll() is None:
                        self._terminate_stream_process(current_proc)
                    current_proc = None
            self._last_runtime_error_at = time.monotonic()
            raise ClaudeStreamError("claude stream failed")
        except ClaudeStreamTimeout:
            self._last_runtime_error_at = time.monotonic()
            raise
        finally:
            if current_proc is not None:
                self._terminate_stream_process(current_proc)

    def openai_complete(self, text: str, session_id: Optional[str], timeout: int, route: Optional[str] = None) -> str:
        parts = []
        for delta in self.openai_stream(text, session_id, timeout, route=route):
            parts.append(delta)
        return "".join(parts)

    def stream(self, text: str, session_id: Optional[str], timeout: int, route: Optional[str] = None) -> Iterator[str]:
        raise NotImplementedError("Claude Code backend does not support streaming")
