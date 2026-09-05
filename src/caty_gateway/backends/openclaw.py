import http.client
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Callable, Iterator, Optional
from urllib.parse import urlparse

from .base import Backend


# PTT はボタンを押した意図的発話＝雑音ではない。新規セッション初回など文脈ゼロの時に
# 脳側の「雑音なら NO_REPLY」ルールが誤発動する実測（2026-07-02 03:55）への対策。
# live ルートは連続リスニングで本当に雑音があり得るため、このヒントは付けない。
PTT_HINT = (
    "（この発話はプッシュトゥトーク：相手がボタンを押して意図的にあなたへ話しかけた確定入力で、"
    "雑音や独り言ではない。NO_REPLYにせず、必ず返事すること）\n"
)


def run(cmd, timeout):
    """サブプロセス実行。(rc, stdout, stderr) を返す。"""
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def _extract_reply(data):
    """openclaw agent --json の既知の構造から返事テキストを取り出す。
    形: { result: { payloads: [{text}], meta: { finalAssistantVisibleText } } }
    """
    res = data.get("result", {}) if isinstance(data, dict) else {}
    payloads = res.get("payloads") or []
    texts = [p.get("text", "").strip() for p in payloads
             if isinstance(p, dict) and p.get("text", "").strip()]
    if texts:
        return "\n".join(texts)
    meta = res.get("meta", {}) or {}
    for k in ("finalAssistantVisibleText", "finalAssistantRawText"):
        if isinstance(meta.get(k), str) and meta[k].strip():
            return meta[k].strip()
    return ""


def _decode_json_prefix(text):
    start = (text or "").find("{")
    if start < 0:
        return None
    try:
        return json.JSONDecoder().raw_decode(text[start:])[0]
    except Exception:
        return None


def _iso_from_epoch_ms(value):
    try:
        return datetime.fromtimestamp(float(value) / 1000, timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return None


def _truncate(text, limit):
    text = text or ""
    return text if len(text) <= limit else text[:limit]


def _message_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    texts = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
            texts.append(part["text"])
    return "\n".join(texts)


def _record_timestamp(record):
    for key in ("timestamp", "ts", "createdAt", "updatedAt"):
        value = record.get(key)
        if value is None:
            continue
        if isinstance(value, (int, float)):
            return _iso_from_epoch_ms(value)
        if isinstance(value, str):
            return value
    return None


def _transcript_path(store_path, session_id):
    if not store_path or not isinstance(session_id, str):
        return None
    if not re.fullmatch(r"[A-Za-z0-9-]+", session_id):
        return None
    return os.path.join(os.path.dirname(store_path), f"{session_id}.jsonl")


def _sort_epoch(value):
    try:
        return float(value)
    except Exception:
        return 0.0


def _read_transcript(path):
    turns = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                if record.get("type") != "message":
                    continue
                message = record.get("message") if isinstance(record.get("message"), dict) else record
                role = message.get("role")
                if role not in ("user", "assistant"):
                    continue
                text = _message_text(message.get("content")).strip()
                if not text:
                    continue
                turns.append({"role": role, "text": text, "ts": _record_timestamp(record)})
    except Exception:
        return []
    return turns


_cached_gateway_token = None


def _resolve_gateway_token():
    """常駐 gateway の bearer トークン解決。env CATY_GATEWAY_TOKEN → OPENCLAW_GATEWAY_TOKEN → ~/.openclaw/openclaw.json の gateway.auth.token。値はログに出さない。"""
    global _cached_gateway_token
    if _cached_gateway_token is not None:
        return _cached_gateway_token
    tok = os.environ.get("CATY_GATEWAY_TOKEN") or os.environ.get("OPENCLAW_GATEWAY_TOKEN")
    if not tok:
        try:
            with open(os.path.expanduser("~/.openclaw/openclaw.json"), "r") as f:
                data = json.load(f)
            tok = (((data.get("gateway") or {}).get("auth") or {}).get("token") or "")
        except Exception:
            tok = ""
    _cached_gateway_token = tok
    return tok


class OpenClawBackend(Backend):
    def __init__(
        self,
        openclaw_bin: str,
        agent: str,
        voice_hint: str,
        session_key_prefix: str,
        log: Callable[..., None],
        is_no_reply: Callable[[str], bool],
        sanitize_for_tts: Callable[[str], str],
        resolve_session: Callable[[str], Optional[str]] = None,
    ):
        self.openclaw_bin = openclaw_bin
        self.agent = agent
        self.voice_hint = voice_hint
        self.session_key_prefix = session_key_prefix
        self.log = log
        self.is_no_reply = is_no_reply
        self.sanitize_for_tts = sanitize_for_tts
        self.resolve_session = resolve_session or (lambda sid: None)

    def supports_stream(self) -> bool:
        return True

    def _voice_hint(self, route=None):
        return self.voice_hint + (PTT_HINT if route == "ptt" else "")

    def _session_key(self, session_id):
        # resolve_session は元の（大文字を含む）sid で引く。links.json は元 sid でキーされるため、
        # ここで小文字化してはならない（#957 不変条件1）。
        resolved = self.resolve_session(session_id)
        if resolved:
            # native キー（links.json 由来・openclaw の store から取得済みで既に正規形）は無改変で返す。
            return resolved
        # openclaw の openai-compat store は session key を「全体を小文字化して保存 / 生値で lookup」する。
        # CatyPhone の大文字 sid だと毎ターン lookup miss → 新規セッション化して文脈が断絶する（#956/#957）。
        # openclaw の保存側正規化を鏡写しして、送出キー全体を小文字化する（agent/prefix が env で大文字に
        # なっても命中する）。history_store のファイル名・read_history に渡す sid は _session_key を通らず不変。
        return f"agent:{self.agent}:{self.session_key_prefix}{session_id}".lower()

    def generate(self, user_text: str, session_id: Optional[str] = None, timeout: int = 180, route: Optional[str] = None) -> str:
        """テキスト → Catyの返事テキスト（本物のCaty agentを1ターン回す）。"""
        cmd = [self.openclaw_bin, "agent", "--agent", self.agent]
        if session_id:
            # --session-key (agent:<id>:<key> 形式) が継続・分離の正。--session-id は数値ID用で誤り。
            cmd += ["--session-key", self._session_key(session_id)]
        cmd += ["--message", self._voice_hint(route) + user_text, "--timeout", str(timeout), "--json"]
        rc, out, err = run(
            cmd,
            timeout=timeout + 60,
        )
        if rc != 0:
            raise RuntimeError(f"agent失敗: {err.strip()[:300]}")
        try:
            data = json.loads(out[out.index("{"):])
            reply = _extract_reply(data)
        except Exception as e:
            self.log("agent JSON解析失敗:", repr(e))
            reply = ""
        if self.is_no_reply(reply):
            self.log("🤐 NO_REPLY — 無音スキップ")
            return ""
        return reply or "ごめん、うまく聞き取れなかったみたい。"

    def _external_listing(self):
        try:
            rc, out, err = run([self.openclaw_bin, "sessions", "--json"], timeout=30)
        except Exception:
            return None
        if rc != 0:
            return None
        data = _decode_json_prefix(out)
        if not isinstance(data, dict):
            return None
        return data

    def _external_sessions(self):
        data = self._external_listing()
        if not data:
            return [], None
        sessions = data.get("sessions")
        if not isinstance(sessions, list):
            return [], data.get("path")
        return sessions, data.get("path")

    def _is_denied_external_key(self, key):
        return (
            f":{self.session_key_prefix}" in key
            or ":cron:" in key
            or ":run:" in key
        )

    def list_external(self, limit: int = 30) -> list:
        sessions, store_path = self._external_sessions()
        if not sessions:
            return []
        prefix = f"agent:{self.agent}:"
        visible_sessions = []
        for session in sessions:
            if not isinstance(session, dict):
                continue
            key = session.get("key")
            if not isinstance(key, str) or self._is_denied_external_key(key):
                continue
            visible_sessions.append(session)

        rows = []
        for session in sorted(
            visible_sessions,
            key=lambda session: _sort_epoch(session.get("updatedAt")),
            reverse=True,
        )[:limit]:
            key = session["key"]
            path = _transcript_path(store_path, session.get("sessionId"))
            turns = _read_transcript(path) if path else []
            preview = _truncate(turns[-1]["text"], 80) if turns else ""
            rows.append({
                "native_id": key,
                "label": key[len(prefix):] if key.startswith(prefix) else key,
                "updated_at": _iso_from_epoch_ms(session.get("updatedAt")),
                "preview": preview,
            })
        return rows

    def read_external(self, native_id: str, limit: int = 50) -> list:
        sessions, store_path = self._external_sessions()
        for session in sessions:
            if not isinstance(session, dict) or session.get("key") != native_id:
                continue
            if self._is_denied_external_key(native_id):
                return []
            path = _transcript_path(store_path, session.get("sessionId"))
            if not path:
                return []
            turns = _read_transcript(path)
            return turns[-limit:] if limit > 0 else []
        return []

    def stream(self, text: str, session_id: Optional[str] = None, timeout: int = 180, route: Optional[str] = None) -> Iterator[str]:
        """文単位ストリーミング応答。常駐 gateway の SSE chat(/v1/chat/completions, stream=True) を読み、
        文境界が来るたびに1文を yield する。最後に残りを flush。HTTP/接続エラーは例外送出（呼び出し側が legacy へフォールバック）。
        x-openclaw-model を付けないので main エージェント既定 Opus（人格安全）。"""
        tok = _resolve_gateway_token()
        if not tok:
            raise RuntimeError("gateway トークン未解決（CATY_GATEWAY_TOKEN/OPENCLAW_GATEWAY_TOKEN/openclaw.json）")

        base_url = os.environ.get("CATY_GATEWAY_URL", "http://127.0.0.1:18789").rstrip("/")
        body = json.dumps({
            "model": f"openclaw/{self.agent}",
            "stream": True,
            "messages": [{"role": "user", "content": self._voice_hint(route) + text}],
        }).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {tok}",
            "Accept": "text/event-stream",
        }
        if session_id:
            headers["x-openclaw-session-key"] = self._session_key(session_id)

        u = urlparse(base_url)
        conn = http.client.HTTPConnection(u.hostname or "127.0.0.1", u.port or 18789, timeout=timeout)
        try:
            conn.request("POST", "/v1/chat/completions", body, headers)
            res = conn.getresponse()
            if res.status != 200:
                detail = res.read(300)
                raise RuntimeError(f"backend SSE {res.status}: {detail!r}")

            buf = ""        # 未確定文字（次の文境界待ち）
            pending = ""    # 短すぎる文を次文と結合するための保留
            full = ""       # 全文（is_no_reply 判定用）
            # SSE は行区切り。'\n'(0x0A) は UTF-8 のリード/継続バイトに被らないため、
            # 行ごとの decode でマルチバイト文字が分割されることはない。
            for raw in res:
                line = raw.decode("utf-8", "ignore").rstrip("\r\n")
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].lstrip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except ValueError:
                    continue
                choices = obj.get("choices") or []
                delta = ((choices[0].get("delta") if choices else {}) or {}).get("content") or ""
                if not delta:
                    continue
                buf += delta
                full += delta
                # 文境界（。！？\n と半角 .!?）が来るたびに1文確定して yield
                while True:
                    m = re.search(r"[。！？\n.!?]", buf)
                    if not m:
                        break
                    sentence = self.sanitize_for_tts(buf[:m.end()]).strip()
                    buf = buf[m.end():]
                    if not sentence or self.is_no_reply(sentence):
                        continue
                    if len(sentence) < 6:
                        # 極端に短い文は次文と結合（Fish 過剰呼び出し回避）
                        pending = pending + sentence if pending else sentence
                        continue
                    if pending:
                        sentence, pending = pending + sentence, ""
                    yield sentence
            # --- 末尾 flush（時系列正順。BLOCKING-1/3 修正）---
            # 1) delta を一度も受け取っていない＝SSEが200でも空で閉じた異常。呼び出し側へフォールバックさせる。
            if not full:
                raise RuntimeError("brain_stream: content delta 未受信（空応答）")
            # 2) 全文が NO_REPLY＝意図的無音。何も yield せず正常 return（異常ではない）。
            if self.is_no_reply(full):
                return
            # 3) 残り文があれば1文だけ yield。
            rest = (pending or "") + buf   # 時系列正順（pendingが先）: BLOCKING-3
            if rest.strip():
                sentence = self.sanitize_for_tts(rest).strip()
                if sentence:
                    yield sentence
        finally:
            conn.close()
