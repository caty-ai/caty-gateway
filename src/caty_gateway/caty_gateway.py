#!/usr/bin/env python3
"""
Voice conversation HTTP gateway
===============================
Apple Watch（や将来のembedded device端末）から音声を受け取り、
  STT → Caty(本物の脳) → TTS(Catyの声)
を回して、返事の音声を返すだけのシンプルなHTTPサーバ。

全部 `openclaw` CLI 経由なので追加のAPIキーは不要：
  - STT : openclaw capability audio transcribe   (ローカル/無料)
  - 脳  : openclaw agent --agent caty             (本物のCaty)
  - TTS : openclaw capability tts convert         (Catyの声 / mp3)

エンドポイント:
  POST /talk    body=録音音声(m4a/wav)         → 返事音声(mp3)を返す（一括・旧方式）
                ヘッダ X-Transcript / X-Reply に文字も入れる(デバッグ用)
  POST /talk2   body=録音音声(m4a/wav)         → STT後すぐ {"id","transcript"} を返す（streaming方式）
  POST /see     multipart audio(m4a)+image(jpg/png) → 画面フレームを対応backendへ原本パススルー
  GET  /stream/<id>                            → 返事音声(mp3)をchunkで流す（AVPlayerでそのまま再生）
  GET  /health  → {"ok":true}

起動:
  python3 caty_gateway.py
  （起動時に「時計に入れるURL」を表示します）

環境変数:
  CATY_GATEWAY_PORT  既定 8788
  CATY_GATEWAY_BIND  既定 0.0.0.0
  CATY_AGENT         既定 caty
  CATY_TTS_VOICE     任意。Catyの声のvoice id（capability tts voices で確認）
  CATY_LANG          STT言語ヒント 既定 ja
  CATY_REQUIRE_AUTH  1/true でtoken未設定時も認証を必須化（既定 無効）
  CATY_EXTERNAL_SESSIONS  1/true で外部セッション取り込みAPIを有効化（既定 無効）
  CATY_EXTERNAL_SEED_TURNS  外部セッション取り込み時の最大seed turns（既定 50）
  CATY_EXTERNAL_PREVIEW  0/false で外部セッション一覧のpreviewを伏せる（既定 表示）
  CATY_UNSAFE_DEBUG_LOG_CONTENT  1で会話本文を最大15分だけログ（既定 無効）
"""

import argparse
import http.client
import base64
import datetime
import hmac
import io
import ipaddress
import json
import math
import mimetypes
import os
import random
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urlparse

_DEGRADED_FALLBACK_MP3 = base64.b64decode(
    "SUQzBAAAAAAAIlRTU0UAAAAOAAADTGF2ZjYyLjMuMTAwAAAAAAAAAAAAAAD/84TAAAAAAAAAAAAA"
    "SW5mbwAAAA8AAAAPAAACKABkZGRkZGRvb29vb29venp6enp6hYWFhYWFhZCQkJCQkJCbm5ubm5um"
    "pqampqamsrKysrKysr29vb29vcjIyMjIyMjT09PT09PT3t7e3t7e6enp6enp6fT09PT09PT/////"
    "//8AAAAATGF2YzYyLjExAAAAAAAAAAAAAAAAJANgAAAAAAAAAihnpA4nAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAD/8xTEAAAAA0gAAAAATEFNRTMuMTAwVVX/8xTECwAAA0gAAAAAVVVVVVVVVVVVVVX/"
    "8xTEFgAAA0gAAAAAVVVVVVVVVVVVVVX/8xTEIQAAA0gAAAAAVVVVVVVVVVVVVVX/8xTELAAAA0gA"
    "AAAAVVVVVVVVVVVVVVX/8xTENwAAA0gAAAAAVVVVVVVVVVVVVVX/8xTEQgAAA0gAAAAAVVVVVVVV"
    "VVVVVVX/8xTETQAAA0gAAAAAVVVVVVVVVVVVVVX/8xTEWAAAA0gAAAAAVVVVVVVVVVVVVVX/8xTE"
    "YwAAA0gAAAAAVVVVVVVVVVVVVVX/8xTEbgAAA0gAAAAAVVVVVVVVVVVVVVX/8xTEeQAAA0gAAAAA"
    "VVVVVVVVVVVVVVX/8xTEhAAAA0gAAAAAVVVVVVVVVVVVVVX/8xTEjwAAA0gAAAAAVVVVVVVVVVVV"
    "VVX/8xTEmgAAA0gAAAAAVVVVVVVVVVVVVVU="
)

from caty_gateway import caty_config as gateway_config
from caty_gateway import filler_pack
from caty_gateway import filler_texts
from caty_gateway import fish_tts_contract
from caty_gateway import history_store
from caty_gateway import presence_state
from caty_gateway import pairing_store
from caty_gateway import push_events
from caty_gateway import session_links
from caty_gateway import share_store
from caty_gateway import tts_fish
from caty_gateway import voice_catalog
from caty_gateway import voice_activation
from caty_gateway import voice_presets
from caty_gateway import voice_preview
from caty_gateway.backends.claude import ClaudeStreamError, ClaudeStreamTimeout
from caty_gateway.avatar_engine import (
    AvatarCredentialConflict,
    AvatarEngine,
    AvatarEngineBusy,
    AvatarEngineDisabled,
    AvatarJobStateError,
    AvatarPassClients,
    GATEWAY_SLOTS,
    GENERIC_IDENTITY_DESCRIPTION,
)
from caty_gateway.vision_describer import VisionDescriber, VisionDescriberError


def _bind_host():
    return os.environ.get("CATY_GATEWAY_BIND", "0.0.0.0").strip() or "0.0.0.0"


PORT      = int(os.environ.get("CATY_GATEWAY_PORT", "8788"))
BIND_HOST = _bind_host()
AGENT     = os.environ.get("CATY_AGENT", "main")   # Catyは main エージェント（openclaw agents list）
BACKEND_NAME = os.environ.get("CATY_BACKEND", "openclaw").strip().lower()
# Closed vocabulary clients may receive.
RUNTIME_KINDS = frozenset({"openclaw", "hermes", "claude-code", "codex-cli", "local-llm", "unknown"})
_RUNTIME_KIND_BY_BACKEND = {
    "openclaw": "openclaw",
    "hermes": "hermes",
    "claude": "claude-code",
    "codex": "codex-cli",
    "openai-compat": "local-llm",
    "openai_compat": "local-llm",
}


def runtime_kind_for_backend(backend_name: Optional[str]) -> str:
    """Map a backend to the closed runtime-kind vocabulary.

    Clients must treat unfamiliar runtime-kind strings as ``unknown`` and use a
    neutral fallback.
    """
    normalized = "" if backend_name is None else str(backend_name).strip().lower()
    return _RUNTIME_KIND_BY_BACKEND.get(normalized, "unknown")


def current_runtime_kind() -> str:
    return runtime_kind_for_backend(BACKEND_NAME)


SESSION_KEY_PREFIX = os.environ.get("CATY_SESSION_KEY_PREFIX", "caty-")
# Deprecated import-time mirror kept for compatibility with downstream imports
# and the existing test seam. Normal runtime decisions read the environment in
# stream_tts_effective_state() instead.
_STREAM_TTS_ENABLED_AT_IMPORT = os.environ.get("CATY_STREAM_TTS", "") == "1"
STREAM_TTS_ENABLED = _STREAM_TTS_ENABLED_AT_IMPORT
TTS_VOICE = os.environ.get("CATY_TTS_VOICE", "").strip()
LANG      = os.environ.get("CATY_LANG", "ja")
OPENCLAW  = os.environ.get("OPENCLAW_BIN", "openclaw")
FFMPEG    = os.environ.get("FFMPEG_BIN", "ffmpeg")
CATY_CLAUDE_BIN = os.environ.get("CATY_CLAUDE_BIN", "claude")
CATY_CLAUDE_MODEL = os.environ.get("CATY_CLAUDE_MODEL", "").strip()
CATY_CLAUDE_CWD = os.environ.get("CATY_CLAUDE_CWD", os.path.expanduser("~"))
CATY_HERMES_URL = os.environ.get("CATY_HERMES_URL", "http://127.0.0.1:8642")
CATY_HERMES_API_KEY = os.environ.get("CATY_HERMES_API_KEY", "").strip()
CATY_OPENAI_BASE_URL = os.environ.get("CATY_OPENAI_BASE_URL", "").strip().rstrip("/")
CATY_OPENAI_MODEL = os.environ.get("CATY_OPENAI_MODEL", "").strip()
CATY_OPENAI_API_KEY = os.environ.get("CATY_OPENAI_API_KEY", "").strip()
CATY_OPENAI_MAX_HISTORY_CHARS = os.environ.get("CATY_OPENAI_MAX_HISTORY_CHARS", "24000")
CATY_OPENAI_CHAT_TOKEN = os.environ.get("CATY_OPENAI_CHAT_TOKEN", "").strip()
# streaming TTS用。Fish AudioプロキシのOpenAI互換エンドポイント（chunkで音声が届く）
TTS_PROXY = os.environ.get("CATY_TTS_PROXY", "http://localhost:5100/v1/audio/speech")
# PTT 長尺対応: brain タイムアウトと JOBS TTL を延長する
CATY_PTT_BRAIN_TIMEOUT = int(os.environ.get("CATY_PTT_BRAIN_TIMEOUT", "1800"))
CATY_PTT_JOB_TTL       = int(os.environ.get("CATY_PTT_JOB_TTL", "2100"))
CATY_TOKEN             = os.environ.get("CATY_TOKEN", "")
CATY_ADMIN_TOKEN       = os.environ.get("CATY_ADMIN_TOKEN", "")
IDENTITY_ID            = os.environ.get("CATY_ID", "caty")
IDENTITY_NAME          = os.environ.get("CATY_NAME", "Caty")
IDENTITY_ACCENT_COLOR  = os.environ.get("CATY_ACCENT_COLOR", "#FF8FB1")
IDENTITY_ASSETS_VERSION = int(os.environ.get("CATY_ASSETS_VERSION", "1"))
ASSET_DIR              = os.environ.get(
    "CATY_ASSET_DIR",
    str(resources.files("caty_gateway").joinpath("assets")),
)

AUDIO_BODY_LIMIT = 25 * 1024 * 1024
ASSET_FILE_LIMIT = 2 * 1024 * 1024
ASSET_BATCH_LIMIT = 12 * 1024 * 1024
IDENTITY_DESCRIPTION_LIMIT = 1000
FILLERS_BODY_LIMIT = 5 * 1024 * 1024
CONFIG_BODY_LIMIT = 64 * 1024
EXTERNAL_BODY_LIMIT = 64 * 1024
EXTERNAL_SESSIONS_MAX_LIMIT = 100
PUSH_BODY_LIMIT = 16384
OPENAI_CHAT_BODY_LIMIT = 128 * 1024
VOICE_PREVIEW_BODY_LIMIT = 16 * 1024
VOICE_ACTIVATION_BODY_LIMIT = 16 * 1024
SHARE_BODY_LIMIT = 22 * 1024 * 1024
SHARE_IMAGE_LIMIT = 10 * 1024 * 1024
SHARE_FILE_LIMIT = 20 * 1024 * 1024
SHARE_TEXT_EXTRACT_LIMIT = 256 * 1024
SHARE_TTL_SECONDS = 900
ATTACHMENT_PASSTHROUGH_MAX_BYTES = 8 * 1024 * 1024
OPENAI_CHAT_TIMEOUT = int(os.environ.get("CATY_OPENAI_CHAT_TIMEOUT", "60"))
OPENAI_CHAT_USER_MAX_LEN = int(os.environ.get("CATY_OPENAI_CHAT_USER_MAX_LEN", "512"))
OPENAI_CHAT_MAX_CONCURRENCY = max(1, int(os.environ.get("CATY_OPENAI_CHAT_MAX_CONCURRENCY", "2")))
OPENAI_CHAT_HEARTBEAT_SEC = max(1.0, float(os.environ.get("CATY_OPENAI_CHAT_HEARTBEAT_SEC", "5")))
OPENAI_CHAT_SESSION_PREFIX = "meetmate:"
UNSAFE_CONTENT_LOG_ENV = "CATY_UNSAFE_DEBUG_LOG_CONTENT"
UNSAFE_CONTENT_LOG_TTL_SECONDS = 15 * 60
PUSH_EVENTS = push_events.PushEventQueue()
# 単一ユーザー端末では、直近の音声会話への紐付けが履歴表示を分かりやすくする。
# push の影響は履歴表示だけであり、欠落より短時間の誤付与を許容して5分に限定する。
LAST_VOICE_SESSION = {"id": None, "at": 0.0}
LAST_VOICE_SESSION_LOCK = threading.Lock()
ASSET_LOCK = threading.Lock()
# Serializes external takeover seeding/linking so concurrent requests stay idempotent.
_EXTERNAL_TAKEOVER_LOCK = threading.Lock()
_avatar_engine = None
_avatar_engine_factory = AvatarEngine
_avatar_engine_lock = threading.Lock()
_vision_describer = None
_vision_describer_factory = VisionDescriber
_vision_describer_lock = threading.Lock()
_share_store = None
_share_store_lock = threading.Lock()
_pairing_store = None
_pairing_store_lock = threading.Lock()
_pairing_config = None
_OPENAI_CHAT_ACTIVE = set()
_OPENAI_CHAT_ACTIVE_LOCK = threading.Lock()
_OPENAI_CHAT_CONCURRENCY = threading.BoundedSemaphore(OPENAI_CHAT_MAX_CONCURRENCY)
_voice_catalog_service = None
_voice_catalog_service_lock = threading.Lock()
_voice_preview_service = None
_voice_preview_service_lock = threading.Lock()
_voice_activation_service = None
_voice_activation_service_lock = threading.Lock()
_neutral_voice_readiness = None
_neutral_voice_readiness_lock = threading.Lock()
_JSON_READ_ERROR = object()
_NO_CLOUD_SESSION = object()
# #1012 can install a callable(tokens, capability) -> stable principal/None.
# Until then CATY_TOKEN/CATY_ADMIN_TOKEN remain the compatible credentials and
# these routes stay fail-closed when neither credential authorizes the request.
VOICE_SCOPE_AUTHORIZER = None
_CONTENT_LOG_STATE = {
    "mode": None,
    "deadline": None,
    "enabled_warned": False,
    "expired_warned": False,
}
_CONTENT_LOG_LOCK = threading.Lock()

_LOG_SECRET_ENV_NAMES = (
    "ANTHROPIC_API_KEY",
    "CATY_ADMIN_TOKEN",
    "CATY_GATEWAY_TOKEN",
    "CATY_HERMES_API_KEY",
    "CATY_OPENAI_API_KEY",
    "CATY_OPENAI_CHAT_TOKEN",
    "CATY_TOKEN",
    "FISH_API_KEY",
    "OPENCLAW_GATEWAY_TOKEN",
    "POYO_API_KEY",
    "RENOISE_API_KEY",
    "RENOISE_AUTH_TOKEN",
)


def _get_voice_catalog_service():
    global _voice_catalog_service
    with _voice_catalog_service_lock:
        if _voice_catalog_service is None:
            _voice_catalog_service = voice_catalog.VoiceCatalogService(
                tts_fish.get_json,
                installation_id=IDENTITY_ID,
            )
        return _voice_catalog_service


def _get_voice_preview_service():
    global _voice_preview_service
    with _voice_preview_service_lock:
        if _voice_preview_service is None:
            _voice_preview_service = voice_preview.VoicePreviewService(
                _get_voice_catalog_service(),
                tts_fish.synthesize_preview,
                fish_tts_contract.inference_contract_version,
                installation_id=IDENTITY_ID,
            )
        return _voice_preview_service


def _voice_engine_truth():
    return "fish" if _tts_engine() == "fish" else "openclaw"


def _get_voice_activation_service():
    global _voice_activation_service
    with _voice_activation_service_lock:
        if _voice_activation_service is None:
            registry = filler_pack.FillerPackRegistry.for_member(IDENTITY_ID)
            _voice_activation_service = voice_activation.VoiceActivationService(
                CONFIG,
                _get_voice_catalog_service(),
                registry,
                member_id=IDENTITY_ID,
                synthesizer=tts_fish.synthesize_filler,
                inference_contract_version=fish_tts_contract.inference_contract_version,
                engine_truth=_voice_engine_truth,
            )
        return _voice_activation_service


def _utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


class _NeutralVoiceReadiness:
    def __init__(
        self,
        *,
        catalog_service_getter,
        preset_id,
        reference_id,
        clock=None,
        now_iso=None,
        sleep=None,
        ttl_seconds=15 * 60,
        max_staleness_seconds=None,
        retry_attempts=3,
        initial_backoff_seconds=0.2,
    ):
        self._catalog_service_getter = catalog_service_getter
        self._preset_id = preset_id
        self._reference_id = reference_id
        self._clock = clock or time.time
        self._now_iso = now_iso or _utc_now_iso
        self._sleep = sleep or time.sleep
        self._ttl_seconds = max(1.0, float(ttl_seconds))
        self._max_staleness_seconds = (
            self._ttl_seconds * 4
            if max_staleness_seconds is None
            else max(self._ttl_seconds, float(max_staleness_seconds))
        )
        self._retry_attempts = max(1, int(retry_attempts))
        self._initial_backoff_seconds = max(0.0, float(initial_backoff_seconds))
        self._lock = threading.Lock()
        self._refreshing = False
        self._started = False
        self._last_attempt_at = None
        self._published_availability = "unknown"
        self._published_checked_at = None
        self._published_stale = False
        self._last_definite_availability = None
        self._last_definite_checked_at = None

    def _payload_locked(self, now):
        if (
            self._last_definite_availability in {"available", "unavailable"}
            and self._last_definite_checked_at is not None
            and now - self._last_definite_checked_at > self._max_staleness_seconds
        ):
            self._published_availability = "unknown"
            self._published_checked_at = _utc_from_epoch(self._last_definite_checked_at)
            self._published_stale = True
        payload = {
            "preset_id": self._preset_id,
            "reference_id": self._reference_id,
            "availability": self._published_availability,
            "checked_at": self._published_checked_at,
        }
        expired = self._last_attempt_at is None or (
            now - self._last_attempt_at >= self._ttl_seconds
        )
        if self._published_stale or (
            expired and self._published_availability in {"available", "unavailable"}
        ):
            payload["stale"] = True
        return payload, expired

    def _record_definite_locked(self, availability, checked_at_epoch, checked_at_iso):
        self._last_attempt_at = checked_at_epoch
        self._published_availability = availability
        self._published_checked_at = checked_at_iso
        self._published_stale = False
        self._last_definite_availability = availability
        self._last_definite_checked_at = checked_at_epoch

    def _record_transient_locked(self, now):
        self._last_attempt_at = now
        stale_definite = (
            self._last_definite_availability in {"available", "unavailable"}
            and self._last_definite_checked_at is not None
            and now - self._last_definite_checked_at <= self._max_staleness_seconds
        )
        if stale_definite:
            self._published_availability = self._last_definite_availability
            self._published_checked_at = _utc_from_epoch(self._last_definite_checked_at)
            self._published_stale = True
            return
        self._published_availability = "unknown"
        self._published_checked_at = (
            _utc_from_epoch(self._last_definite_checked_at)
            if self._last_definite_checked_at is not None
            else None
        )
        self._published_stale = self._last_definite_checked_at is not None

    def _refresh_now(self):
        error = None
        for attempt in range(self._retry_attempts):
            checked_at_epoch = self._clock()
            checked_at_iso = self._now_iso()
            try:
                resolved = self._catalog_service_getter().resolve_preview(
                    catalog_id=self._preset_id
                )
            except voice_catalog.CatalogVoiceUnavailable:
                with self._lock:
                    self._record_definite_locked(
                        "unavailable", checked_at_epoch, checked_at_iso
                    )
                return
            except voice_catalog.CatalogError as exc:
                error = exc
            except Exception as exc:
                error = exc
            else:
                availability = resolved.get("availability")
                if availability == "available":
                    with self._lock:
                        self._record_definite_locked(
                            "available", checked_at_epoch, checked_at_iso
                        )
                    return
                if availability in {"hidden", "unavailable"}:
                    with self._lock:
                        self._record_definite_locked(
                            "unavailable", checked_at_epoch, checked_at_iso
                        )
                    return
                else:
                    error = RuntimeError("neutral preset returned unknown availability")
            if attempt + 1 < self._retry_attempts and self._initial_backoff_seconds:
                self._sleep(self._initial_backoff_seconds * (2 ** attempt))
        with self._lock:
            self._record_transient_locked(self._clock())
        return error

    def _refresh_async(self):
        try:
            self._refresh_now()
        finally:
            with self._lock:
                self._refreshing = False

    def _launch_refresh_locked(self):
        if self._refreshing:
            return False
        self._refreshing = True
        thread = threading.Thread(
            target=self._refresh_async,
            name="caty-neutral-voice-refresh",
            daemon=True,
        )
        thread.start()
        return True

    def start(self):
        with self._lock:
            if self._started:
                return
            self._started = True
            self._launch_refresh_locked()

    def state(self):
        now = self._clock()
        with self._lock:
            payload, expired = self._payload_locked(now)
            if self._started and expired:
                self._launch_refresh_locked()
        return payload


def _utc_from_epoch(value):
    if value is None:
        return None
    return datetime.datetime.fromtimestamp(
        float(value), tz=datetime.timezone.utc
    ).isoformat().replace("+00:00", "Z")


def _get_neutral_voice_readiness():
    global _neutral_voice_readiness
    with _neutral_voice_readiness_lock:
        if _neutral_voice_readiness is None:
            preset = voice_presets.PRESETS["fish-neutral-ja-v1"]
            _neutral_voice_readiness = _NeutralVoiceReadiness(
                catalog_service_getter=_get_voice_catalog_service,
                preset_id="fish-neutral-ja-v1",
                reference_id=preset["reference_id"],
            )
        return _neutral_voice_readiness


def _get_avatar_engine():
    global _avatar_engine
    with _avatar_engine_lock:
        if _avatar_engine is None:
            _avatar_engine = _avatar_engine_factory()
        return _avatar_engine


def _avatar_pass_clients(kind, cloud_session=_NO_CLOUD_SESSION):
    """Resolve one immutable pass credential bundle (cloud takes precedence)."""
    if cloud_session is _NO_CLOUD_SESSION:
        return AvatarPassClients.from_environment(kind)
    if not isinstance(cloud_session, dict) or set(cloud_session) != {"base_url", "token"}:
        raise gateway_config.InvalidConfig(
            "cloud_session must contain exactly base_url and token"
        )
    token = cloud_session.get("token")
    if (
        not isinstance(token, str)
        or not token
        or len(token) > 8192
        or any(ord(char) < 0x21 or ord(char) > 0x7E for char in token)
    ):
        raise gateway_config.InvalidConfig("cloud_session.token must be a non-empty string")
    origin = gateway_config.normalize_caty_cloud_origin(cloud_session.get("base_url"))
    return AvatarPassClients.from_cloud(kind, origin, token)


def _get_vision_describer():
    global _vision_describer
    with _vision_describer_lock:
        if _vision_describer is None:
            _vision_describer = _vision_describer_factory()
        return _vision_describer


def _get_share_store():
    global _share_store
    with _share_store_lock:
        if _share_store is None:
            _share_store = share_store.ShareStore(
                share_store.default_share_root(),
                ttl_seconds=SHARE_TTL_SECONDS,
            )
        return _share_store


def _get_pairing_config():
    global _pairing_config
    if _pairing_config is None:
        _pairing_config = pairing_store.load_config(
            warn=lambda message: log(f"WARN {message}")
        )
    return _pairing_config


def _get_pairing_store():
    global _pairing_store
    with _pairing_store_lock:
        if _pairing_store is None:
            _pairing_store = pairing_store.PairingStore(
                pairing_store.default_pairing_root(),
                # Same derivation as the store directory (§7-3) so the record's
                # member_id can never disagree with the namespace it lives in.
                member_id=pairing_store.default_pairing_member(),
                config=_get_pairing_config(),
                warn=lambda message: log(f"WARN pairing {message}"),
            )
        return _pairing_store


class _PairClaimRateLimiter:
    """Process-local fixed-window limiter keyed by the raw peer address."""

    def __init__(self, max_entries=1024, clock=None):
        self.max_entries = max_entries
        self.clock = clock or time.time
        self.entries = OrderedDict()
        self.logged = OrderedDict()
        self.lock = threading.Lock()

    def allow(self, peer, limit):
        window = int(self.clock() // 60)
        with self.lock:
            current = self.entries.pop(peer, None)
            count = 0 if current is None or current[0] != window else current[1]
            count += 1
            self.entries[peer] = (window, count)
            while len(self.entries) > self.max_entries:
                self.entries.popitem(last=False)
            return count <= limit

    def note_rejection(self, peer):
        """True at most once per peer per window, bounding rejection logging."""
        window = int(self.clock() // 60)
        with self.lock:
            if self.logged.get(peer) == window:
                return False
            self.logged.pop(peer, None)
            self.logged[peer] = window
            while len(self.logged) > self.max_entries:
                self.logged.popitem(last=False)
            return True


_pair_claim_rate_limiter = _PairClaimRateLimiter()

QR_DELIVERY_MODES = ("auto", "tty", "url")


def _redact_log_text(value):
    text = str(value)
    for name in _LOG_SECRET_ENV_NAMES:
        secret = os.environ.get(name, "")
        if secret:
            if len(secret) >= 8:
                text = text.replace(secret, "[REDACTED]")
            else:
                text = re.sub(
                    rf"(?<![A-Za-z0-9]){re.escape(secret)}(?![A-Za-z0-9])",
                    "[REDACTED]",
                    text,
                )
    text = re.sub(
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+",
        "Bearer [REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)(\bAuthorization(?:\s+header)?\s*[:=]\s*)"
        r"(?:[A-Za-z][A-Za-z0-9._-]*\s+)?[^\s,;\]}]+",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)([\"']?\b(?:api[_ -]?key|access[_ -]?token|token)[\"']?"
        r"\s*[:=]\s*)[\"']?[^\s,;\]}\"']+[\"']?",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(
        r"\b[0-9a-f]{8}\.[0-9a-f]{32}\b",
        "[REDACTED]",
        text,
    )
    return re.sub(
        r"(https?://[^\s?'\"<>]+)\?[^\s'\"<>]*",
        r"\1?<redacted>",
        text,
    )


def log(*a):
    safe = tuple(_redact_log_text(value) for value in a)
    print(f"[{time.strftime('%H:%M:%S')}]", *safe, flush=True)


def _reset_content_log_state():
    """Reset the process-local debug window (tests and env mode transitions)."""
    with _CONTENT_LOG_LOCK:
        _CONTENT_LOG_STATE.update({
            "mode": None,
            "deadline": None,
            "enabled_warned": False,
            "expired_warned": False,
        })


def _content_logging_status(now=None):
    raw = os.environ.get("CATY_UNSAFE_DEBUG_LOG_CONTENT")
    normalized = "" if raw is None else raw.strip().lower()
    if normalized in ("", "0", "false"):
        mode = "disabled"
    elif normalized == "1":
        mode = "enabled"
    else:
        mode = "invalid"

    current = time.monotonic() if now is None else now
    with _CONTENT_LOG_LOCK:
        if _CONTENT_LOG_STATE["mode"] != mode:
            _CONTENT_LOG_STATE.update({
                "mode": mode,
                "deadline": (
                    current + UNSAFE_CONTENT_LOG_TTL_SECONDS
                    if mode == "enabled"
                    else None
                ),
                "enabled_warned": False,
                "expired_warned": False,
            })
        if mode != "enabled":
            return mode
        if current >= _CONTENT_LOG_STATE["deadline"]:
            return "expired"
        return "enabled"


def _warn_unsafe_content_logging(status):
    with _CONTENT_LOG_LOCK:
        if status == "enabled":
            if _CONTENT_LOG_STATE["enabled_warned"]:
                return
            _CONTENT_LOG_STATE["enabled_warned"] = True
            message = (
                "⚠️ UNSAFE_CONTENT_LOGGING status=enabled "
                f"expires_in_s={UNSAFE_CONTENT_LOG_TTL_SECONDS} "
                "conversation content may be persisted by stdout collectors"
            )
        elif status == "expired":
            if _CONTENT_LOG_STATE["expired_warned"]:
                return
            _CONTENT_LOG_STATE["expired_warned"] = True
            message = "privacy content_logging=metadata_only status=expired"
        else:
            return
    log(message)


def report_content_logging_mode():
    status = _content_logging_status()
    if status == "enabled":
        _warn_unsafe_content_logging(status)
    elif status == "invalid":
        log(
            "privacy content_logging=metadata_only status=invalid_disabled "
            f"env={UNSAFE_CONTENT_LOG_ENV}"
        )
    elif status == "expired":
        _warn_unsafe_content_logging(status)
    else:
        log("privacy content_logging=metadata_only status=default")


def _request_id(value):
    sanitized = re.sub(r"[^A-Za-z0-9._-]", "", str(value or ""))
    return sanitized[:64] or "-"


def _log_unsafe_content(request_id, stage, content):
    status = _content_logging_status()
    if status == "enabled":
        _warn_unsafe_content_logging(status)
        log(
            "UNSAFE_CONTENT "
            f"request_id={_request_id(request_id)} stage={stage} "
            f"content={content!r}"
        )
    elif status == "expired":
        _warn_unsafe_content_logging(status)


def log_conversation_content(request_id, stage, content, status="ok"):
    text = "" if content is None else str(content)
    log(
        f"request_id={_request_id(request_id)} stage={stage} "
        f"status={status} chars={len(text)}"
    )
    _log_unsafe_content(request_id, stage, text)


def log_failure(request_id, stage, error, status="error"):
    log(
        f"request_id={_request_id(request_id)} stage={stage} "
        f"status={status} error_type={type(error).__name__}"
    )
    _log_unsafe_content(request_id, f"{stage}_error", repr(error))


def backend_log(*a):
    detail = " ".join(str(value) for value in a)
    log(
        f"request_id=- stage=backend status=notice backend={BACKEND_NAME} "
        f"detail_chars={len(detail)}"
    )
    _log_unsafe_content("-", "backend_detail", detail)


presence_state.set_logger(log)


def _backend_desc():
    detail = (
        getattr(BACKEND, "model", "")
        or getattr(BACKEND, "agent", "")
        or getattr(BACKEND, "url", "")
        or ""
    )
    return f"{BACKEND_NAME}:{detail}".rstrip(":")


def _fmt_turn_s(value):
    if value is None:
        return "-"
    return f"{value:.1f}s"


def _log_turn_summary(
    route,
    t0,
    stt_s,
    gen_first_s,
    gen_s,
    tts_first_s,
    mode,
    *,
    job=None,
    status="ok",
):
    try:
        request_id = _request_id(getattr(job, "request_id", None))
        transcript_chars = len(getattr(job, "transcript", "") or "")
        reply_chars = len(getattr(job, "reply", "") or "")
        audio_bytes = sum(len(chunk) for chunk in (getattr(job, "chunks", ()) or ()))
        log(
            "🎚 turn "
            f"request_id={request_id} "
            f"route={route or '-'} "
            f"backend={_backend_desc()} "
            f"status={status} "
            f"stt={_fmt_turn_s(stt_s)} "
            f"gen_first={_fmt_turn_s(gen_first_s)} "
            f"gen={_fmt_turn_s(gen_s)} "
            f"tts_first={_fmt_turn_s(tts_first_s)} "
            f"total={time.time()-t0:.1f}s "
            f"mode={mode} "
            f"transcript_chars={transcript_chars} "
            f"reply_chars={reply_chars} "
            f"audio_bytes={audio_bytes}"
        )
    except Exception:
        pass


def is_no_reply(text: str) -> bool:
    t = text.strip().strip("'\"` ")
    if t == "NO_REPLY" or t.rstrip("。．.!！?？") == "NO_REPLY":
        return True
    # "NO_REPLY\n\n<メタ発言>" のように前置きで漏れるケースを拾う（2026-07-02 03:42 実測。
    # 漏れテキストをそのまま TTS で読み上げないための保険）
    return t.startswith("NO_REPLY")


def run(cmd, timeout):
    """サブプロセス実行。(rc, stdout, stderr) を返す。"""
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def to_wav16k(src_path):
    """受け取った音声を 16kHz mono wav に正規化（STTを安定させる）。失敗時は元を返す。"""
    dst = src_path + ".16k.wav"
    rc, _, err = run([FFMPEG, "-y", "-i", src_path, "-ar", "16000", "-ac", "1", dst], timeout=60)
    if rc == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0:
        return dst
    log_failure(
        "-", "audio_normalize", RuntimeError(err.strip()), status="stt_fallback"
    )
    return src_path


def stt(audio_path):
    """音声 → テキスト。"""
    rc, out, err = run(
        [OPENCLAW, "capability", "audio", "transcribe",
         "--file", audio_path, "--language", LANG, "--json"],
        timeout=120,
    )
    if rc != 0:
        raise RuntimeError(f"STT失敗: {err.strip()[:300]}")
    data = json.loads(out[out.index("{"):])
    for o in data.get("outputs", []):
        t = (o.get("text") or "").strip()
        if t:
            return t
    return ""


def _share_metadata_lines(share):
    def value(name, default=None):
        if isinstance(share, dict):
            return share.get(name, default)
        return getattr(share, name, default)

    filename = value("filename", "") or "（ファイル名なし）"
    return [
        f"ファイル名: {filename}",
        f"MIMEタイプ: {value('mime', 'application/octet-stream')}",
        f"サイズ: {value('size', 0)} bytes",
    ]


def _has_truncated_utf8_tail(data, error_start):
    if error_start < 0 or error_start < len(data) - 3 or error_start >= len(data):
        return False
    lead = data[error_start]
    if 0xC2 <= lead <= 0xDF:
        expected = 2
    elif 0xE0 <= lead <= 0xEF:
        expected = 3
    elif 0xF0 <= lead <= 0xF4:
        expected = 4
    else:
        return False
    tail = data[error_start + 1:]
    if len(tail) >= expected - 1:
        return False
    return all(0x80 <= byte <= 0xBF for byte in tail)


def _extract_share_text(data):
    sample = data[:SHARE_TEXT_EXTRACT_LIMIT]
    try:
        return sample.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        if len(data) <= SHARE_TEXT_EXTRACT_LIMIT:
            return None
        if not _has_truncated_utf8_tail(sample, error.start):
            return None
        max_trim = min(3, len(sample))
        for trim in range(1, max_trim + 1):
            try:
                return sample[:-trim].decode("utf-8-sig")
            except UnicodeDecodeError:
                continue
    return None


def _user_name():
    return os.environ.get("CATY_USER_NAME") or "ユーザー"


def _attachment_block(attachment, *, available, source="share"):
    if source == "screen":
        heading = f"【いま{_user_name()}が見ている画面】"
    elif source == "share":
        heading = (
            f"【{_user_name()}が添付した画像】"
            if attachment.mime.startswith("image/")
            else f"【{_user_name()}が添付したファイル】"
        )
    else:
        raise ValueError("invalid attachment source")
    lines = _share_metadata_lines(attachment)
    if not available:
        lines.append(
            "画面の内容を確認できませんでした。"
            if source == "screen"
            else (
                "画像の内容を確認できませんでした。"
                if attachment.mime.startswith("image/")
                else "ファイルの内容を確認できませんでした。"
            )
        )
    return heading + "\n" + "\n".join(lines)


def _append_share_message(block, text, source="share"):
    label = f"【{_user_name()}の質問】" if source == "screen" else "【メッセージ】"
    return f"{block}\n\n{label}\n{text}" if text else block


def _unlink_attachment_path(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as error:
        log(
            "stage=attachment_cleanup status=failed "
            f"error_type={type(error).__name__}"
        )


def _copy_attachment_to_staging(attachment, staging_dir):
    suffix = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "application/pdf": ".pdf",
    }.get(attachment.mime, ".bin")
    descriptor, path = tempfile.mkstemp(
        prefix="caty-attachment-", suffix=suffix, dir=staging_dir
    )
    try:
        target = os.fdopen(descriptor, "wb")
        descriptor = None
        with target, open(attachment.path, "rb") as source:
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(path, 0o600)
        return Attachment(
            kind=attachment.kind,
            mime=attachment.mime,
            size=attachment.size,
            path=path,
            filename=attachment.filename,
        )
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        _unlink_attachment_path(path)
        raise


def _prepare_binary_attachment(claimed, text, request_id, source="share"):
    """Freeze generate and stream attachment delivery decisions once."""
    attachment = Attachment(
        kind=claimed.declared_kind,
        mime=claimed.sniffed_mime,
        size=claimed.size,
        path=claimed.path,
        filename=claimed.filename,
    )
    config = resolved_config()
    states = {
        transport: _attachment_passthrough_effective_state(
            config, transport, attachment
        )
        for transport in ("generate", "stream")
    }
    delivery_attachment = attachment
    staging_dir = getattr(BACKEND, "attachment_staging_dir", lambda: None)()
    if staging_dir is not None and any(state[0] for state in states.values()):
        try:
            delivery_attachment = _copy_attachment_to_staging(
                attachment, staging_dir
            )
        except Exception:
            states = {
                transport: (
                    (False, supported, "temp-write-failed")
                    if enabled
                    else (enabled, supported, reason)
                )
                for transport, (enabled, supported, reason) in states.items()
            }

    delivery_text = _append_share_message(
        _attachment_block(attachment, available=True, source=source),
        text,
        source,
    )
    metadata_text = _append_share_message(
        _attachment_block(attachment, available=False, source=source),
        text,
        source,
    )
    entries = {}
    for transport, (enabled, _supported, reason) in states.items():
        entries[transport] = (
            Delivery([delivery_attachment], delivery_text)
            if enabled
            else MetadataOnly(metadata_text, reason)
        )
    plan = AttachmentPlan(
        generate=entries["generate"], stream=entries["stream"]
    )
    log(
        f"request_id={_request_id(request_id)} stage=attachment "
        f"kind={attachment.kind} mime={attachment.mime} size={attachment.size} "
        f"generate_reason={states['generate'][2]} "
        f"stream_reason={states['stream'][2]}"
    )
    if all(isinstance(entry, MetadataOnly) for entry in entries.values()):
        _unlink_attachment_path(claimed.path)
        if delivery_attachment.path != claimed.path:
            _unlink_attachment_path(delivery_attachment.path)
    return plan


def _rejected_attachment_plan(rejected, text, source="share"):
    attachment = Attachment(
        kind=rejected.declared_kind,
        mime="application/octet-stream",
        size=rejected.size,
        path="",
        filename=rejected.filename,
    )
    brain_text = _append_share_message(
        _attachment_block(attachment, available=False, source=source),
        text,
        source,
    )
    return AttachmentPlan(
        generate=MetadataOnly(brain_text, rejected.reason),
        stream=MetadataOnly(brain_text, rejected.reason),
    )


def _compose_share_turn(taken, text, request_id):
    """Compose backend-only context from one atomic ShareStore.take result."""
    if isinstance(taken, share_store.ClaimedFile):
        return text, _prepare_binary_attachment(taken, text, request_id)
    if isinstance(taken, share_store.Rejected):
        plan = _rejected_attachment_plan(taken, text)
        log(
            f"request_id={_request_id(request_id)} stage=attachment "
            f"kind={taken.declared_kind} mime=application/octet-stream "
            f"size={taken.size} generate_reason={taken.reason} "
            f"stream_reason={taken.reason}"
        )
        return plan.generate.brain_text, plan
    if not isinstance(taken, share_store.TextBytes):
        raise TypeError("unexpected share take result")

    lines = _share_metadata_lines(taken)
    extracted = _extract_share_text(taken.data)
    if extracted is None:
        lines.append("内容の抽出には対応していません。")
    else:
        lines.extend(("内容:", extracted))
        if len(taken.data) > SHARE_TEXT_EXTRACT_LIMIT:
            lines.append(
                f"（先頭{SHARE_TEXT_EXTRACT_LIMIT}バイトまで。以降は省略しました。）"
            )
    block = f"【{_user_name()}が添付したファイル】\n" + "\n".join(lines)
    return _append_share_message(block, text), None


def sanitize_session_id(raw):
    """HTTPヘッダ由来のsession idをopenclaw session-key安全な文字だけにする。"""
    if not raw:
        return None
    sid = re.sub(r"[^A-Za-z0-9._-]", "", raw.strip())
    return sid or None


def record_voice_session(session_id):
    """直近に試行された音声会話のセッションを記録する。"""
    if not session_id:
        return
    with LAST_VOICE_SESSION_LOCK:
        LAST_VOICE_SESSION["id"] = session_id
        LAST_VOICE_SESSION["at"] = time.monotonic()


def recent_voice_session(window_s=300.0):
    """指定窓内に試行された音声会話のセッションIDを返す。"""
    with LAST_VOICE_SESSION_LOCK:
        session_id = LAST_VOICE_SESSION["id"]
        recorded_at = LAST_VOICE_SESSION["at"]
        if session_id and time.monotonic() - recorded_at <= window_s:
            return session_id
    return None


def openai_chat_backend():
    if hasattr(BACKEND, "openai_stream") and hasattr(BACKEND, "openai_complete"):
        return BACKEND
    return None


def openai_chat_message_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    texts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str):
            texts.append(text)
    return "\n".join(texts)


def latest_openai_user_text(messages):
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        return openai_chat_message_text(message.get("content"))
    return None


def openai_chat_response_model(requested):
    if isinstance(requested, str) and requested.strip():
        return requested.strip()
    if CATY_CLAUDE_MODEL:
        return CATY_CLAUDE_MODEL
    return "claude"


def openai_chat_session_id(user):
    return OPENAI_CHAT_SESSION_PREFIX + user


def try_begin_openai_chat(session_id):
    with _OPENAI_CHAT_ACTIVE_LOCK:
        if session_id in _OPENAI_CHAT_ACTIVE:
            return "busy", None
        _OPENAI_CHAT_ACTIVE.add(session_id)
    if not _OPENAI_CHAT_CONCURRENCY.acquire(blocking=False):
        with _OPENAI_CHAT_ACTIVE_LOCK:
            _OPENAI_CHAT_ACTIVE.discard(session_id)
        return "overloaded", None

    released = False

    def release():
        nonlocal released
        if released:
            return
        released = True
        _OPENAI_CHAT_CONCURRENCY.release()
        with _OPENAI_CHAT_ACTIVE_LOCK:
            _OPENAI_CHAT_ACTIVE.discard(session_id)

    return None, release


# 音声会話モードの指示。長い返事はTTSが40秒超になり、Watch側が画面オフで再生失敗するため短さを強制する
_VOICE_HINT_CATY_INTRO = "（caty-gateway 音声会話モード：この返事はそのまま音声で読み上げられる。"
_VOICE_HINT_CATY_CASUAL_TONE = "基本は友達と話すような自然な話し言葉で対話する。"
_VOICE_HINT_TTS_STYLE = "markdown・箇条書き・絵文字は使わない。"
_VOICE_HINT_TTS_LENGTH = "長さは会話として自然な長さ（目安は数文）。"
_VOICE_HINT_LONG_REPLY = "詳しい説明が必要な場合も、音声では要点だけ話す。"
_VOICE_HINT_CATY_EMOTIONAL_INTERJECTION = (
    "たまに（毎回ではなく自然に）、文頭に自分の感情の一言を添えると人間らしくなる"
    "（例：「わー、それ楽しそう！」「いいね、最高だね！」「それ嬉しいな」）。"
)
_VOICE_HINT_ALWAYS_REPLY = "どんな時も必ず一声は声に出して返す（無言・無反応にしない）。"
_VOICE_HINT_CATY_SWEET_JOKE = "ごくごく稀に（本当にたまに）、恋人や親しい友達のような甘くて軽い冗談をさらっと混ぜてもいい。"
_VOICE_HINT_EMOTION_TAGS = (
    "また、ここぞという時だけ・たまに、次の感情タグを文中で使ってよい（音声に反映される）："
    "[chuckling]くすっと笑う [laughing]声を出して笑う [gasping]はっと驚く [yawning]あくび "
    "[sighing]ひと息 [moved]じーんと感動 [whispering]そっと囁く [soft tone]やさしい声 [break]短い間。"
    "これら以外のタグ・記号は使わない）\n"
)

DEFAULT_VOICE_HINT = "".join((
    _VOICE_HINT_CATY_INTRO,
    _VOICE_HINT_CATY_CASUAL_TONE,
    _VOICE_HINT_TTS_STYLE,
    _VOICE_HINT_TTS_LENGTH,
    _VOICE_HINT_LONG_REPLY,
    _VOICE_HINT_CATY_EMOTIONAL_INTERJECTION,
    _VOICE_HINT_ALWAYS_REPLY,
    _VOICE_HINT_CATY_SWEET_JOKE,
    _VOICE_HINT_EMOTION_TAGS,
))
# Legacy literal from an early build in which the push guidance was embedded in the default
# voice hint. Kept so that saved overlays equal to either historical value are recognised.
_LEGACY_CATY_VOICE_HINT_B2E74A9 = DEFAULT_VOICE_HINT + (
    "（画面表示：利用者に「画面に出して」「スマホに送って」などと頼まれたら、"
    "シェルで gateway ディレクトリの caty_push.py を実行するとスマホ画面に URL・画像・動画を出せる。"
    "使い方は同ディレクトリの PUSH.md を読むこと。"
    "端末がアクティブかどうか等を理由に断らない — そのような制約は存在しない）\n"
)
_LEGACY_CATY_VOICE_HINTS = (DEFAULT_VOICE_HINT, _LEGACY_CATY_VOICE_HINT_B2E74A9)

NEUTRAL_VOICE_HINT = "".join((
    "（音声会話モード：この返事はそのまま音声で読み上げられる。"
    "自然な話し言葉で対話する。",
    _VOICE_HINT_TTS_STYLE,
    _VOICE_HINT_TTS_LENGTH,
    _VOICE_HINT_ALWAYS_REPLY,
    _VOICE_HINT_EMOTION_TAGS,
))
VOICE_HINT = os.environ.get("CATY_VOICE_HINT", DEFAULT_VOICE_HINT)

# 画面表示（push）の機能案内。編集可能な「話し方」(voice_hint overlay) とは別枠で、
# openclaw backend では毎ターン常時連結する（#782）。DEFAULT_VOICE_HINT に埋めると
# アプリの話し方編集の保存スナップショットで凍結・消滅するため、ここに置いてはいけない。
# 実 E2E で「gateway ディレクトリの」では場所を探し当てられなかったため絶対パスを焼き込む。
def _screen_push_hint():
    return (
        f"（画面表示：{_user_name()}に『画面に出して』『スマホに送って』などと頼まれたら、"
        "シェルで `python -m caty_gateway.caty_push open-url <URL> --title <題>` を実行すると"
        "スマホ画面に出せる。token は CATY_TOKEN 環境変数から読む。"
        "送れるのは Web 上の http/https URL のみ。）\n"
    )

# 本人モード backend（hermes/claude）には薄い既定ヒントを使う。openclaw backend も
# CATY_ID が caty 以外なら、人格に関わるCaty用指示を除いた NEUTRAL_VOICE_HINT を使う。
THIN_MEMBER_VOICE_HINT = (
    "（音声通話です。この返事はそのまま音声で読み上げられます。"
    "1〜2文の短く自然な日本語で答えてください。"
    "markdown・箇条書き・記号・絵文字は使わないでください）\n"
)
MEMBER_VOICE_HINT = os.environ.get("CATY_VOICE_HINT", THIN_MEMBER_VOICE_HINT)


def default_voice_hint_for_backend(backend_name):
    env_hint = os.environ.get("CATY_VOICE_HINT")
    if env_hint is not None:
        return env_hint
    if backend_name == "openclaw":
        # 空文字の CATY_ID も未設定と同じ caty 既定に倒す。
        if (os.environ.get("CATY_ID") or "caty") == "caty":
            return DEFAULT_VOICE_HINT
        return NEUTRAL_VOICE_HINT
    return THIN_MEMBER_VOICE_HINT


def _config_defaults():
    return {
        "config_version": 1,
        "backend": BACKEND_NAME,
        "name": IDENTITY_NAME,
        "accent_color": IDENTITY_ACCENT_COLOR,
        "voice_id": TTS_VOICE,
        "voice_hint": default_voice_hint_for_backend(BACKEND_NAME),
        "stream_tts": "",
        "attachment_passthrough": "",
        "assets_version": IDENTITY_ASSETS_VERSION,
        "fillers_version": 1,
    }


CONFIG = gateway_config.OverlayConfig(_config_defaults)


def _env_truthy(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true")


def _external_seed_ceiling():
    raw = os.environ.get("CATY_EXTERNAL_SEED_TURNS", "50").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 50


def external_sessions_enabled():
    return _env_truthy("CATY_EXTERNAL_SESSIONS")


def require_auth_enabled():
    return _env_truthy("CATY_REQUIRE_AUTH")


def external_preview_enabled():
    return _env_truthy("CATY_EXTERNAL_PREVIEW", True)


def _neutralize_legacy_voice_hint(cfg):
    # 旧 Caty 既定のスナップショットが overlay に焼き込まれた配備の提示時中和（#901）。
    # 判定は歴代 Caty 既定 literal との完全一致のみ（部分一致・正規化比較は禁止）。
    if (
        cfg.get("voice_hint") in _LEGACY_CATY_VOICE_HINTS
        and (os.environ.get("CATY_ID") or "caty") != "caty"
        and os.environ.get("CATY_VOICE_HINT") is None
    ):
        cfg["voice_hint"] = default_voice_hint_for_backend(BACKEND_NAME)
    return cfg


def resolved_config():
    return _neutralize_legacy_voice_hint(CONFIG.get())


def _stream_tts_effective_state(config):
    supported = bool(BACKEND.supports_stream())
    if not supported:
        return False, False, "unsupported-backend"
    desired = config.get("stream_tts", "")
    if desired in ("on", "off"):
        return desired == "on", True, "runtime-override"
    legacy_env_enabled = os.environ.get("CATY_STREAM_TTS") == "1"
    if STREAM_TTS_ENABLED != _STREAM_TTS_ENABLED_AT_IMPORT:
        # Preserve the established module-level test/downstream override while
        # keeping the unmodified production path live-env based.
        legacy_env_enabled = bool(STREAM_TTS_ENABLED)
    if legacy_env_enabled:
        return True, True, "legacy-env"
    return False, True, "default-off"


def stream_tts_effective_state():
    """Resolve the streaming mode for the next turn from live runtime state."""
    return _stream_tts_effective_state(CONFIG.get())


@dataclass(frozen=True)
class Attachment:
    kind: str
    mime: str
    size: int
    path: str
    filename: str


@dataclass(frozen=True)
class Delivery:
    attachments: list
    brain_text: str


@dataclass(frozen=True)
class MetadataOnly:
    brain_text: str
    reason: str


@dataclass(frozen=True)
class AttachmentPlan:
    generate: object
    stream: object

    def __getitem__(self, transport):
        if transport not in ("generate", "stream"):
            raise KeyError(transport)
        return getattr(self, transport)


ClaimedFile = share_store.ClaimedFile


def _backend_attachment_transports():
    declaration = getattr(BACKEND, "attachment_transports", None)
    if declaration is None:
        return frozenset()
    try:
        value = declaration()
        return frozenset(value) if isinstance(value, (set, frozenset, list, tuple)) else frozenset()
    except (TypeError, ValueError):
        return frozenset()


def _backend_attachment_mimes():
    declaration = getattr(BACKEND, "supported_attachment_mimes", None)
    if declaration is None:
        return frozenset()
    try:
        value = declaration()
        return frozenset(value) if isinstance(value, (set, frozenset, list, tuple)) else frozenset()
    except (TypeError, ValueError):
        return frozenset()


def _attachment_passthrough_effective_state(config, transport, attachment):
    transports = _backend_attachment_transports()
    if not transports:
        return False, False, "unsupported-backend"
    desired = config.get("attachment_passthrough", "")
    if desired == "off":
        return False, True, "runtime-override"
    if transport not in transports:
        return False, True, "transport-unsupported"
    if attachment.mime not in _backend_attachment_mimes():
        return False, True, "mime-rejected"
    adapter_limit = getattr(BACKEND, "attachment_max_bytes", lambda: None)()
    size_limit = ATTACHMENT_PASSTHROUGH_MAX_BYTES
    if adapter_limit is not None:
        size_limit = min(size_limit, adapter_limit)
    if attachment.size > size_limit:
        return False, True, "size-over"
    staging_dir = getattr(BACKEND, "attachment_staging_dir", lambda: None)()
    if staging_dir is not None:
        try:
            os.makedirs(staging_dir, mode=0o700, exist_ok=True)
            if not os.path.isdir(staging_dir) or not os.access(staging_dir, os.W_OK):
                return False, True, "temp-write-failed"
        except OSError:
            return False, True, "temp-write-failed"
    reason = "runtime-override" if desired == "on" else "default-on"
    return True, True, reason


def _attachment_passthrough_config_state(config):
    supported = bool(
        _backend_attachment_transports() and _backend_attachment_mimes()
    )
    if not supported:
        return False, False, "unsupported-backend"
    desired = config.get("attachment_passthrough", "")
    if desired in ("on", "off"):
        return desired == "on", True, "runtime-override"
    return True, True, "default-on"


def get_tts_voice():
    return resolved_config()["voice_id"].strip()


def _tts_engine():
    return os.environ.get("CATY_TTS_ENGINE", "").strip().lower()


class LiveVoiceHint:
    """Backend互換のまま、各ターンで overlay voice_hint を解決する薄いラッパー。

    注意: str の完全な代替ではない。backends/ は voice_hint を `+` 連結
    （__add__/__radd__ → str 化）でのみ使う前提。len()/bool()/dict キー/
    .encode() 等を使う backend を追加する場合は str(voice_hint) を経由すること。
    """

    def __str__(self):
        hint = resolved_config()["voice_hint"]
        # 機能案内は「話し方」と別枠の常時注入（#782）: overlay（アプリ編集値）が
        # あっても消えず、config_payload（話し方エディタの往復）にも混入しない。
        # 本人モード backend（hermes/claude）は対象外 — 案内文がユーザー宛のため。
        if BACKEND_NAME == "openclaw":
            hint += _screen_push_hint()
        return hint

    def __add__(self, other):
        return str(self) + other

    def __radd__(self, other):
        return other + str(self)

    def __eq__(self, other):
        return str(self) == other

    def __contains__(self, item):
        return item in str(self)


# backend が無言/NO_REPLY を返した時でも、話しかけられたら必ず一声返す用の定型（#77）
NO_REPLY_FALLBACKS = [
    "ごめん、今ちょっと聞き取れなかったかも。もう一回だけ言ってくれる？",
    "あれ、うまく聞こえなかったみたい。もう一度お願いしてもいい？",
    "ごめんね、今の聞き取れなかったかも。もう一回だけいい？",
]


def sanitize_for_tts(text):
    """TTSに渡す前のテキスト整形。markdown記法・絵文字等を除去して読み上げ事故を防ぐ。"""
    import re
    t = text
    t = re.sub(r"```.*?```", "", t, flags=re.S)          # コードブロック
    t = re.sub(r"\*\*([^*]+)\*\*", r"\1", t)             # **bold**
    t = re.sub(r"(?m)^\s*[-*・]\s+", "", t)              # 箇条書き
    t = re.sub(r"(?m)^#+\s*", "", t)                     # 見出し
    t = re.sub(r"`([^`]+)`", r"\1", t)                   # inline code
    t = re.sub(r"[\U0001F000-\U0001FAFF☀-➿️]", "", t)  # 絵文字
    t = re.sub(r"\n{2,}", "\n", t).strip()
    return t or text


def _resolve_session_link(sid):
    link = session_links.get(sid)
    if not link:
        return None
    return link.get("native")


def _build_backend():
    if BACKEND_NAME == "openclaw":
        from caty_gateway.backends.openclaw import OpenClawBackend
        return OpenClawBackend(
            openclaw_bin=OPENCLAW,
            agent=AGENT,
            voice_hint=LiveVoiceHint(),
            session_key_prefix=SESSION_KEY_PREFIX,
            log=backend_log,
            is_no_reply=is_no_reply,
            sanitize_for_tts=sanitize_for_tts,
            resolve_session=_resolve_session_link,
        )
    if BACKEND_NAME == "hermes":
        from caty_gateway.backends.hermes import HermesBackend
        if not CATY_HERMES_API_KEY:
            raise RuntimeError("CATY_HERMES_API_KEY is required when CATY_BACKEND=hermes")
        return HermesBackend(
            url=CATY_HERMES_URL,
            api_key=CATY_HERMES_API_KEY,
            voice_hint=LiveVoiceHint(),
            log=backend_log,
            resolve_session=_resolve_session_link,
        )
    if BACKEND_NAME in ("openai-compat", "openai_compat"):
        from caty_gateway.backends.openai_compat import OpenAICompatBackend
        if not CATY_OPENAI_BASE_URL:
            raise RuntimeError("CATY_OPENAI_BASE_URL is required when CATY_BACKEND=openai-compat")
        if not CATY_OPENAI_MODEL:
            raise RuntimeError("CATY_OPENAI_MODEL is required when CATY_BACKEND=openai-compat")
        parsed = urlparse(CATY_OPENAI_BASE_URL)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            safe_value = _redact_log_text(CATY_OPENAI_BASE_URL)
            raise RuntimeError(
                "CATY_OPENAI_BASE_URL must be an absolute http(s) URL "
                f"(got {safe_value!r})"
            )
        try:
            max_history_chars = int(CATY_OPENAI_MAX_HISTORY_CHARS)
        except ValueError:
            raise RuntimeError(
                f"CATY_OPENAI_MAX_HISTORY_CHARS must be an integer, got {CATY_OPENAI_MAX_HISTORY_CHARS!r}"
            )
        return OpenAICompatBackend(
            base_url=CATY_OPENAI_BASE_URL,
            model=CATY_OPENAI_MODEL,
            api_key=CATY_OPENAI_API_KEY,
            voice_hint=LiveVoiceHint(),
            log=backend_log,
            is_no_reply=is_no_reply,
            sanitize_for_tts=sanitize_for_tts,
            read_history=lambda sid: history_store.read_session(sid),
            max_history_chars=max_history_chars,
        )
    if BACKEND_NAME == "claude":
        from caty_gateway.backends.claude import ClaudeCodeBackend
        return ClaudeCodeBackend(
            claude_bin=CATY_CLAUDE_BIN,
            model=CATY_CLAUDE_MODEL,
            cwd=CATY_CLAUDE_CWD,
            voice_hint=LiveVoiceHint(),
            log=backend_log,
            resolve_session=_resolve_session_link,
        )
    from caty_gateway.backends.generic_cli import GenericCliBackend, PRESETS as GENERIC_CLI_PRESETS
    if BACKEND_NAME == "generic" or BACKEND_NAME in GENERIC_CLI_PRESETS:
        return GenericCliBackend(
            preset=BACKEND_NAME if BACKEND_NAME in GENERIC_CLI_PRESETS else None,
            voice_hint=LiveVoiceHint(),
            log=backend_log,
            resolve_session=_resolve_session_link,
            save_session=lambda sid, native: session_links.put(sid, BACKEND_NAME, native),
        )
    raise RuntimeError(
        f"unknown CATY_BACKEND={BACKEND_NAME!r} "
        f"(expected openclaw, hermes, claude, openai-compat, generic, or one of {', '.join(GENERIC_CLI_PRESETS)})"
    )


BACKEND = _build_backend()


def brain(user_text, session_id=None, brain_timeout=180, route=None, attachments=None):
    if attachments and "generate" in _backend_attachment_transports():
        return BACKEND.generate(
            user_text, session_id, brain_timeout, route=route,
            attachments=attachments,
        )
    return BACKEND.generate(user_text, session_id, brain_timeout, route=route)


def brain_stream(text, session_id=None, brain_timeout=180, route=None, attachments=None):
    if attachments and "stream" in _backend_attachment_transports():
        return BACKEND.stream(
            text, session_id, brain_timeout, route=route,
            attachments=attachments,
        )
    return BACKEND.stream(text, session_id, brain_timeout, route=route)


def _temp_path_with_bytes(data, suffix):
    fd, path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return path
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise


def _reserved_temp_path(suffix):
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    return path


def tts(text):
    """テキスト → Catyの声(mp3 path)。"""
    text = sanitize_for_tts(text)
    out_path = _reserved_temp_path(".mp3")
    if _tts_engine() == "fish":
        try:
            data = tts_fish.synthesize(text, get_tts_voice())
            with open(out_path, "wb") as f:
                f.write(data)
            if os.path.getsize(out_path) <= 0:
                raise RuntimeError("Fish Audio TTS returned empty audio")
            return out_path
        except Exception:
            try:
                os.remove(out_path)
            except OSError:
                pass
            raise

    cmd = [OPENCLAW, "capability", "tts", "convert", "--text", text, "--output", out_path, "--json"]
    voice_id = get_tts_voice()
    if voice_id:
        cmd += ["--voice", voice_id]
    rc, _, err = run(cmd, timeout=120)
    if rc != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) <= 0:
        # mkstemp は失敗時も0バイトファイルが残るので掃除してから raise
        try:
            os.remove(out_path)
        except OSError:
            pass
        raise RuntimeError(f"TTS失敗: {err.strip()[:300]}")
    return out_path


# ---------------------------------------------------------------- streaming
class Job:
    """1つの返事のストリーミング状態。chunksに音声が溜まり、doneで完了。"""
    def __init__(self, transcript, session_id=None):
        self.transcript = transcript
        self.session_id = session_id
        self.request_id = None
        self.reply = ""
        self._pending_reply = None
        self.partial_reply_enabled = False
        self.stream_enabled = None
        self.chunks = []
        self.done = False
        self.error = None
        self.degraded = None
        self.cond = threading.Condition()
        self.created = time.time()
        self.route = None   # 'ptt' | 'live' | None
        self.ttl   = 600    # default TTL seconds; PTT jobs use CATY_PTT_JOB_TTL
        self.stt_s = None
        self._persisted = False
        self.binary_attachment_present = False
        self._cleanup_callbacks = []
        self._cleanup_seen = set()
        presence_state.attach(self)

    def push(self, data):
        with self.cond:
            if self._pending_reply is not None:
                self.reply = self._pending_reply
                self._pending_reply = None
            self.chunks.append(data)
            self.cond.notify_all()

    def stage_reply(self, reply):
        """Publish reply text atomically with the sentence's first audio chunk."""
        with self.cond:
            self._pending_reply = reply

    def enable_partial_reply(self, enabled):
        with self.cond:
            if self.stream_enabled is None and enabled:
                # Compatibility for callers constructing a streaming Job directly;
                # real turns always set the snapshot at stream_pipeline entry.
                self.stream_enabled = True
            self.partial_reply_enabled = enabled
            if not enabled:
                self._pending_reply = None

    def update_reply(self, reply):
        with self.cond:
            self.reply = reply
            self._pending_reply = None
            self.cond.notify_all()

    def add_cleanup(self, callback):
        if not callable(callback):
            raise TypeError("cleanup callback must be callable")
        run_now = False
        with self.cond:
            if callback in self._cleanup_seen:
                return
            self._cleanup_seen.add(callback)
            if self.done:
                run_now = True
            else:
                self._cleanup_callbacks.append(callback)
        if run_now:
            try:
                callback()
            except Exception as error:
                log(
                    "stage=job_cleanup status=failed "
                    f"error_type={type(error).__name__}"
                )

    def finish(self, error=None):
        with self.cond:
            self.error = error
            self.done = True
            should_persist = not self._persisted
            self._persisted = True
            session_id = self.session_id
            transcript = self.transcript
            reply = self.reply
            cleanup_callbacks = self._cleanup_callbacks
            self._cleanup_callbacks = []
            self.cond.notify_all()
        # cond 解放後に呼ぶ（history 永続化と同型）。transition 内の coverage ログ I/O を
        # lock 外に出すため — 再入ホールドだと deferred emit も lock 下になる。
        try:
            presence_state.finish(self, error)
        finally:
            for callback in cleanup_callbacks:
                try:
                    callback()
                except Exception as cleanup_error:
                    log(
                        "stage=job_cleanup status=failed "
                        f"error_type={type(cleanup_error).__name__}"
                    )
        if should_persist:
            try:
                if transcript and transcript.strip():
                    history_store.append_turn(session_id, "user", transcript)
                    if reply and reply.strip():
                        history_store.append_turn(session_id, "assistant", reply)
            except Exception as e:
                log("⚠️ history persistence failed:", repr(e))


JOBS = {}
JOBS_LOCK = threading.Lock()

# Filler audio is supplied per member at runtime; no voice audio ships in the package.
BUNDLED_FILLER_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "fillers",
)


def _member_filler_dir(member_id):
    if (
        not re.fullmatch(r"[A-Za-z0-9._-]+", member_id)
        or member_id in (".", "..")
    ):
        raise ValueError(
            "CATY_ID must contain only letters, numbers, dot, underscore, or hyphen"
        )
    return os.path.expanduser(
        f"~/.local/share/caty-gateway/{member_id}/fillers"
    )


def _same_path(path_a, path_b):
    try:
        return os.path.samefile(path_a, path_b)
    except OSError:
        return os.path.normcase(os.path.realpath(path_a)) == os.path.normcase(
            os.path.realpath(path_b)
        )


def _resolve_filler_dir():
    member_id = os.environ.get("CATY_ID") or "caty"
    configured = os.environ.get("CATY_FILLER_DIR")
    if configured is not None:
        return configured
    return _member_filler_dir(member_id)


FILLER_DIR = _resolve_filler_dir()
FILLERS = []      # [(bytes, 再生秒数)]
FILLER_METADATA = []  # [{"name": str, "duration_sec": float, "size": int, "text": str|None}]
SILENCE_1S = None  # (bytes, 1.0)
FILLER_LOCK = threading.RLock()
FILLER_TEXT_MAX = 500
# filler dir が使用可能かどうか（"ok" | "unavailable"）。/fillers 応答の
# filler_dir_status に additive に載せ、無音の縮退をユーザー可視にする。
FILLER_DIR_STATUS = "ok"


def _filler_texts_path():
    return os.path.abspath(FILLER_DIR) + "-texts.json"


def _load_filler_texts():
    """Load legacy MP3-name labels, not managed kind-based filler_texts."""
    path = _filler_texts_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        # 破損したまま放置すると毎ロードで警告が続くので、空で書き戻して自己修復する
        # （呼び出し元は全員 FILLER_LOCK 保持済み）。
        log("⚠️ filler texts unreadable; resetting to empty:", repr(e))
        _repair_filler_texts()
        return {}
    if not isinstance(raw, dict):
        log("⚠️ filler texts must be a JSON object; resetting to empty")
        _repair_filler_texts()
        return {}
    return {str(name): text for name, text in raw.items() if isinstance(text, str)}


def _repair_filler_texts():
    try:
        _save_filler_texts({})
    except Exception as e:
        log("⚠️ filler texts repair failed:", repr(e))


def _is_system_filler(name):
    # silence* は思考中の無音つなぎに使うシステムファイル。
    # 一覧非表示・追加/削除/テキスト編集不可・PUT 全置換でも温存する。
    return name.startswith("silence")


def _save_filler_texts(texts):
    path = _filler_texts_path()
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".fillers-texts-", suffix=".json", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(texts, f, ensure_ascii=False, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _mp3_duration(path):
    try:
        rc, out, _ = run([FFMPEG.replace("ffmpeg", "ffprobe"), "-v", "error",
                          "-show_entries", "format=duration", "-of", "csv=p=0", path], timeout=15)
    except Exception:
        return 3.0
    try:
        return float(out.strip())
    except ValueError:
        return 3.0


def _mp3_duration_bytes(data):
    fd, path = tempfile.mkstemp(prefix=".filler-duration-", suffix=".mp3")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return _mp3_duration(path)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _valid_mp3_bytes(data):
    return data[:3] == b"ID3" or (
        len(data) >= 2
        and data[0] == 0xFF
        and (data[1] & 0xE0) == 0xE0
    )


def _read_filler_mp3(path, reject_bundled=False):
    if os.path.islink(path):
        raise ValueError("symbolic links are not allowed")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("not a regular file")
        if reject_bundled:
            bundled_path = os.path.join(BUNDLED_FILLER_DIR, os.path.basename(path))
            try:
                bundled_stat = os.stat(bundled_path)
            except OSError:
                bundled_stat = None
            if bundled_stat and (
                file_stat.st_dev,
                file_stat.st_ino,
            ) == (
                bundled_stat.st_dev,
                bundled_stat.st_ino,
            ):
                raise ValueError("bundled Caty filler is not allowed")
        with os.fdopen(fd, "rb") as f:
            fd = -1
            data = f.read()
    finally:
        if fd >= 0:
            os.close(fd)
    if not _valid_mp3_bytes(data):
        raise ValueError("invalid or empty mp3")
    return data


def _ensure_filler_dir():
    """FILLER_DIR が無ければ mkdir -p で作成し、可用性を FILLER_DIR_STATUS に
    反映する。戻り値は「ディレクトリが使用可能か」。作成不能（権限エラー等）は
    既存の warning ログ経路を維持したまま unavailable として扱う。"""
    global FILLER_DIR_STATUS
    if not FILLER_DIR:
        FILLER_DIR_STATUS = "unavailable"
        return False
    if os.path.isdir(FILLER_DIR):
        FILLER_DIR_STATUS = "ok"
        return True
    try:
        os.makedirs(FILLER_DIR, exist_ok=True)
    except OSError as e:
        FILLER_DIR_STATUS = "unavailable"
        log(f"⚠️ filler directory could not be created: {FILLER_DIR}: {e}")
        return False
    FILLER_DIR_STATUS = "ok"
    return True


def load_fillers():
    global SILENCE_1S, FILLER_DIR_STATUS
    with FILLER_LOCK:
        FILLERS.clear()
        FILLER_METADATA.clear()
        SILENCE_1S = None
        if not FILLER_DIR:
            FILLER_DIR_STATUS = "unavailable"
            log("⚠️ filler directory unavailable; no spoken fillers loaded: <disabled>")
            return
        member_id = os.environ.get("CATY_ID") or "caty"
        non_caty = member_id != "caty"
        if non_caty and _same_path(FILLER_DIR, BUNDLED_FILLER_DIR):
            FILLER_DIR_STATUS = "unavailable"
            log(
                "⚠️ refusing bundled Caty fillers for non-Caty member;"
                f" member={member_id} fillers disabled"
            )
            return
        texts = _load_filler_texts()
        if not _ensure_filler_dir():
            if texts:
                _save_filler_texts({})
            log(
                "⚠️ filler directory unavailable; no spoken fillers loaded:"
                f" {FILLER_DIR}"
            )
            return
        current_names = set()
        for name in sorted(os.listdir(FILLER_DIR)):
            if not name.endswith(".mp3"):
                continue
            path = os.path.join(FILLER_DIR, name)
            try:
                data = _read_filler_mp3(path, reject_bundled=non_caty)
            except (OSError, ValueError) as e:
                log(f"⚠️ skipping invalid filler {name}: {e}")
                continue
            current_names.add(name)
            dur = _mp3_duration_bytes(data)
            FILLER_METADATA.append({
                "name": name,
                "duration_sec": dur,
                "size": len(data),
                "text": texts.get(name),
            })
            if name.startswith("silence"):
                SILENCE_1S = (data, dur)
            else:
                FILLERS.append((data, dur))
        pruned_texts = {name: text for name, text in texts.items() if name in current_names}
        if pruned_texts != texts:
            _save_filler_texts(pruned_texts)
        log(f"相槌 {len(FILLERS)}種 + 無音{'あり' if SILENCE_1S else 'なし'} をロード")


def _purge_jobs():
    now = time.time()
    with JOBS_LOCK:
        for k in [k for k, j in JOBS.items() if now - j.created > j.ttl]:
            del JOBS[k]


def fish_inference_model():
    return fish_tts_contract.resolve_model()


def fish_inference_contract_version():
    return fish_tts_contract.inference_contract_version()


def tts_stream_to_job(text, job):
    """Fishプロキシへ直接HTTPし、mp3 chunkを届いたそばからjobへ流す。"""
    text = sanitize_for_tts(text)
    if _tts_engine() == "fish":
        total = 0
        for chunk in tts_fish.synthesize_stream(text, get_tts_voice()):
            total += len(chunk)
            job.push(chunk)
        return total

    u = urlparse(TTS_PROXY)
    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=120)
    body = json.dumps({
        "input": text,
        "voice": get_tts_voice(),
        "model": fish_inference_model(),
        "response_format": "mp3",
    })
    conn.request("POST", u.path, body, {"Content-Type": "application/json"})
    res = conn.getresponse()
    if res.status != 200:
        detail = res.read(300)
        raise RuntimeError(f"TTSプロキシ {res.status}: {detail!r}")
    total = 0
    while True:
        chunk = res.read(4096)
        if not chunk:
            break
        total += len(chunk)
        job.push(chunk)
    conn.close()
    return total


def _attachment_plan_entry(plan, transport, text, binary_present=False):
    if plan is not None:
        return plan[transport]
    if binary_present:
        brain_text = _append_share_message(
            f"【{_user_name()}が添付したファイル】\n"
            "添付ファイルを配達できませんでした。",
            text,
        )
        return MetadataOnly(brain_text, "unsupported-backend")
    return None


def stream_pipeline(job, text, t0, route=None, plan=None):
    """バックグラウンド: 脳(並行) + 相槌/無音パディング → streaming TTS → job完了。"""
    stt_s = getattr(job, "stt_s", None)
    gen_start = None
    gen_first_s = None
    gen_s = None
    tts_first_s = None
    stream_entry = _attachment_plan_entry(
        plan, "stream", text, job.binary_attachment_present
    )
    generate_entry = _attachment_plan_entry(
        plan, "generate", text, job.binary_attachment_present
    )
    stream_text = text if stream_entry is None else stream_entry.brain_text
    stream_attachments = (
        stream_entry.attachments if isinstance(stream_entry, Delivery) else None
    )
    generate_text = text if generate_entry is None else generate_entry.brain_text
    generate_attachments = (
        generate_entry.attachments if isinstance(generate_entry, Delivery) else None
    )
    presence_state.transition(job, presence_state.MODEL_WAITING)
    try:
        job.stream_enabled, _, _ = stream_tts_effective_state()
        # --- 文単位ストリーミングTTS（例外時は下位legacyへフォールバック） ---
        if job.stream_enabled:
            job.enable_partial_reply(True)
            try:
                bt = CATY_PTT_BRAIN_TIMEOUT if route in ("ptt", "live") else 180
                parts = []
                gen_start = time.time()
                for sentence in brain_stream(
                    stream_text, job.session_id, brain_timeout=bt, route=route,
                    attachments=stream_attachments,
                ):
                    if gen_first_s is None:
                        gen_first_s = time.time() - gen_start
                    gen_s = time.time() - gen_start
                    parts.append(sentence)
                    job.stage_reply("".join(parts))
                    presence_state.transition(job, presence_state.STREAMING)
                    sentence_audio_bytes = tts_stream_to_job(sentence, job)
                    if not sentence_audio_bytes:
                        raise RuntimeError("stream TTS returned no audio")
                    if tts_first_s is None:
                        tts_first_s = time.time() - gen_start
                if not parts:
                    # NO_REPLY: 無音終了せず legacy(CLI)でリトライ（必ず一声返すため #77）
                    raise RuntimeError("brain_stream NO_REPLY → legacy retry")
                log_conversation_content(job.request_id, "reply", job.reply)
                log(
                    f"request_id={_request_id(job.request_id)} "
                    f"stage=stream status=ok sentences={len(parts)} "
                    f"audio_bytes={sum(len(chunk) for chunk in job.chunks)} "
                    f"latency_s={time.time()-t0:.1f}"
                )
                _log_turn_summary(
                    route, t0, stt_s, gen_first_s, gen_s, tts_first_s, "stream",
                    job=job,
                )
                job.finish()
                return
            except Exception as e:
                # 判定は実際の音声バッファ job.chunks で行う（tts_stream_to_job は途中まで
                # push して例外を投げ得るため、フラグより job.chunks が堅牢）。
                if job.chunks:
                    # 既に音声を流した → legacy に落とすと二重再生＋brain()二重実行＋会話履歴二重投入。
                    # 既push音声で確定終了する。
                    log_failure(
                        job.request_id,
                        "stream_tts",
                        e,
                        status="partial_audio_kept",
                    )
                    if gen_start is not None and tts_first_s is None:
                        tts_first_s = time.time() - gen_start
                    _log_turn_summary(
                        route, t0, stt_s, gen_first_s, gen_s, tts_first_s,
                        "fallback", job=job, status="degraded",
                    )
                    job.finish()
                    return
                # 1チャンクも流していない → 安全に legacy 経路へフォールバック
                job.enable_partial_reply(False)
                log_failure(
                    job.request_id,
                    "stream_tts",
                    e,
                    status="legacy_fallback",
                )
                presence_state.transition(job, presence_state.RETRYING, attempt=1)

        # 脳は別スレッドで回し、待ち時間に相槌を流す
        result = {}

        def think():
            try:
                bt = CATY_PTT_BRAIN_TIMEOUT if route in ("ptt", "live") else 180
                result["reply"] = brain(
                    generate_text, job.session_id, brain_timeout=bt, route=route,
                    attachments=generate_attachments,
                )
            except Exception as e:
                result["error"] = e

        gen_start = time.time()
        th = threading.Thread(target=think, daemon=True)
        th.start()

        # 相槌はWatchアプリ側で再生する方式（GET /filler）。ここでは脳の完了を待つだけ
        th.join()
        gen_s = time.time() - gen_start
        gen_first_s = gen_s

        if "error" in result:
            raise result["error"]
        reply = result.get("reply")
        if reply == "":
            # backend が無言/NO_REPLY でも、話しかけられたら必ず一声返す（無音終了しない #77）
            line = random.choice(NO_REPLY_FALLBACKS)
            job.update_reply(line)
            presence_state.transition(job, presence_state.STREAMING)
            log_conversation_content(
                job.request_id, "reply", line, status="no_reply_fallback"
            )
            try:
                tts_stream_to_job(line, job)
                tts_first_s = time.time() - gen_start
            except Exception as e:
                log_failure(job.request_id, "fallback_tts", e)
                if job.chunks and tts_first_s is None:
                    tts_first_s = time.time() - gen_start
                if not job.chunks:
                    job.degraded = "tts"
                    job.push(SILENCE_1S[0] if SILENCE_1S else _DEGRADED_FALLBACK_MP3)
            _log_turn_summary(
                route, t0, stt_s, gen_first_s, gen_s, tts_first_s, "fallback",
                job=job,
                status="degraded" if job.degraded else "ok",
            )
            job.finish()
            return
        reply = reply or "ごめん、うまく聞き取れなかったみたい。"
        job.update_reply(reply)
        presence_state.transition(job, presence_state.STREAMING)
        log_conversation_content(job.request_id, "reply", reply)
        try:
            total = tts_stream_to_job(reply, job)
            tts_first_s = time.time() - gen_start
        except Exception as e:
            if job.chunks and tts_first_s is None:
                tts_first_s = time.time() - gen_start
            if job.chunks:
                log_failure(
                    job.request_id,
                    "stream_tts",
                    e,
                    status="partial_audio_kept",
                )
                _log_turn_summary(
                    route, t0, stt_s, gen_first_s, gen_s, tts_first_s,
                    "fallback", job=job, status="degraded",
                )
                job.finish()
                return
            # プロキシ直結に失敗したら旧方式（ファイル一括）でフォールバック
            log_failure(
                job.request_id,
                "stream_tts",
                e,
                status="batch_fallback",
            )
            try:
                mp3 = tts(reply)
            except Exception as fallback_error:
                job.degraded = "tts"
                job.push(SILENCE_1S[0] if SILENCE_1S else _DEGRADED_FALLBACK_MP3)
                log_failure(
                    job.request_id,
                    "batch_tts",
                    fallback_error,
                    status="text_only",
                )
                _log_turn_summary(
                    route, t0, stt_s, gen_first_s, gen_s, tts_first_s,
                    "text_only", job=job, status="degraded",
                )
                job.finish()
                return
            with open(mp3, "rb") as f:
                data = f.read()
            job.push(data)
            if tts_first_s is None:
                tts_first_s = time.time() - gen_start
            total = len(data)
            os.remove(mp3)
        log(
            f"request_id={_request_id(job.request_id)} stage=stream status=ok "
            f"audio_bytes={total} latency_s={time.time()-t0:.1f}"
        )
        _log_turn_summary(
            route, t0, stt_s, gen_first_s, gen_s, tts_first_s, "legacy",
            job=job,
        )
        job.finish()
    except Exception as e:
        log_failure(job.request_id, "stream", e)
        if gen_start is not None and job.chunks and tts_first_s is None:
            tts_first_s = time.time() - gen_start
        _log_turn_summary(
            route, t0, stt_s, gen_first_s, gen_s, tts_first_s, "fallback",
            job=job, status="error",
        )
        job.finish(error=str(e))


def parse_multipart_form(headers, body, include_metadata=False):
    """multipart/form-dataをstdlibだけで取り出す。値はbytesで返す。"""
    content_type = headers.get("Content-Type", "")
    if "multipart/form-data" not in content_type.lower():
        raise ValueError("multipart/form-data required")

    header = (
        f"Content-Type: {content_type}\r\n"
        "MIME-Version: 1.0\r\n\r\n"
    ).encode("ascii", "ignore")
    message = BytesParser(policy=policy.default).parsebytes(header + body)
    if not message.is_multipart():
        raise ValueError("invalid multipart body")

    parts = {}
    metadata = {}
    for part in message.iter_parts():
        disposition = part.get("Content-Disposition", "")
        if not disposition.lower().startswith("form-data"):
            continue
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            payload = b""
        parts[name] = payload
        content_type = part.get("Content-Type") or "application/octet-stream"
        metadata[name] = {
            "content_type": content_type.split(";", 1)[0].strip().lower()
        }
    if include_metadata:
        return parts, metadata
    return parts


_IDENTITY_HEALTH_TTL = 30
_IDENTITY_HEALTH_CACHE = {"checked_at": -_IDENTITY_HEALTH_TTL, "available": True}
_IDENTITY_HEALTH_LOCK = threading.Lock()


def backend_available():
    # ThreadingHTTPServer 下で /identity が並行に来るため check-then-set を Lock で束ねる
    with _IDENTITY_HEALTH_LOCK:
        now = time.monotonic()
        if now - _IDENTITY_HEALTH_CACHE["checked_at"] < _IDENTITY_HEALTH_TTL:
            return _IDENTITY_HEALTH_CACHE["available"]

        health = getattr(BACKEND, "health", lambda: True)
        try:
            available = bool(health())
        except Exception as e:
            log("⚠️ backend health check failed:", repr(e))
            available = False
        _IDENTITY_HEALTH_CACHE["checked_at"] = now
        _IDENTITY_HEALTH_CACHE["available"] = available
        return available


def config_payload(config=None):
    cfg = (
        resolved_config()
        if config is None
        else _neutralize_legacy_voice_hint(dict(config))
    )
    stream_enabled, stream_supported, stream_reason = (
        _stream_tts_effective_state(cfg)
    )
    attachment_enabled, attachment_supported, attachment_reason = (
        _attachment_passthrough_config_state(cfg)
    )
    return {
        "config_version": cfg["config_version"],
        "backend": cfg["backend"],
        "runtime_kind": current_runtime_kind(),
        "name": cfg["name"],
        "accent_color": cfg["accent_color"],
        "voice_id": cfg["voice_id"],
        "voice_hint": cfg["voice_hint"],
        "stream_tts": cfg.get("stream_tts", ""),
        "stream_tts_effective": "on" if stream_enabled else "off",
        "stream_tts_supported": stream_supported,
        "stream_tts_reason": stream_reason,
        "attachment_passthrough": cfg.get("attachment_passthrough", ""),
        "attachment_passthrough_effective": (
            "on" if attachment_enabled else "off"
        ),
        "attachment_passthrough_supported": attachment_supported,
        "attachment_passthrough_reason": attachment_reason,
        "assets_version": cfg["assets_version"],
        "fillers_version": cfg["fillers_version"],
    }


def identity_payload():
    cfg = resolved_config()
    voice_engine = _voice_engine_truth()
    voice = {
        "engine": voice_engine,
        "picker": voice_engine == "fish",
    }
    if voice["picker"]:
        voice["activation_api"] = "/tts/voice-activations"
    return {
        "id": IDENTITY_ID,
        "name": cfg["name"],
        "accent_color": cfg["accent_color"],
        "runtime_kind": current_runtime_kind(),
        "voice": voice,
        "assets_version": cfg["assets_version"],
        "assets": {
            "icon": "/asset/icon.png",
            "frames": {
                "idle": "/asset/idle.png",
                "talk": ["/asset/talk1.png", "/asset/talk2.png", "/asset/talk3.png"],
                "blink": "/asset/blink.png",
                "talk_blink": "/asset/talk_blink.png",
                "listen": "/asset/listen.png",
            },
        },
        "available": backend_available(),
        "protocol_version": 1,
    }


def _connection_payload(pair=None):
    # CATY_PUBLIC_URL: QR に載せる到達先を明示指定（Tailscale IP / MagicDNS / Funnel TLS 等）。
    # 未設定なら従来どおり lan_ip()+PORT を自動検出（host の Caty は現状維持）。
    # VPS 等で動かすメンバーは自分の Tailscale IP を入れる: CATY_PUBLIC_URL=http://100.x.y.z:8788
    base = os.environ.get("CATY_PUBLIC_URL", "").strip().rstrip("/")
    if not base:
        base = f"http://{lan_ip()}:{PORT}"
    payload = {
        "v": 1,
        "url": base,
        "id": IDENTITY_ID,
    }
    if pair is not None:
        payload["pair"] = pair
    return payload


def _resident_pair_new_url():
    # Target the address the resident server actually binds.  Assuming loopback
    # under a non-loopback CATY_GATEWAY_BIND would both miss the running server
    # (silently taking the direct-store path §7-2 rule 7 reserves for "server not
    # running") and hand CATY_TOKEN to whatever local process squats the port.
    host = BIND_HOST.strip()
    if host in ("", "0.0.0.0", "::", "*"):
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{PORT}/pair/new"


def _issue_pairing_from_resident_server():
    request = urllib.request.Request(
        _resident_pair_new_url(),
        data=b"",
        method="POST",
        headers={"Authorization": f"Bearer {CATY_TOKEN}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        raise RuntimeError(
            f"resident gateway rejected pairing issuance ({error.code})"
        ) from error
    payload = json.loads(body.decode("utf-8"))
    pair = payload.get("pair") if isinstance(payload, dict) else None
    if not isinstance(pair, str) or not pairing_store.PAIR_RE.fullmatch(pair):
        raise RuntimeError("resident gateway returned an invalid pairing response")
    return payload


def _issue_pairing_for_qr():
    try:
        payload = _issue_pairing_from_resident_server()
        return payload
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        # Connection failure only: a reachable server's HTTP/config error must
        # not silently fall back to a second issuer process.
        issued = _get_pairing_store().issue()
        return {
            "ok": True,
            **_connection_payload(issued["pair"]),
            "expires_at": issued["expires_at"],
        }


def _redacted_pair_payload(payload):
    safe = dict(payload)
    pair = safe.get("pair")
    if isinstance(pair, str) and pairing_store.PAIR_RE.fullmatch(pair):
        pid, _, _ = pair.partition(".")
        safe["pair"] = f"{pid}.[REDACTED]"
    return safe


def _pairing_token_configured():
    # §9-1 freezes one predicate for all pairing-enabled checks: trim only for the
    # enable/disable decision, never for the stored or returned client credential.
    return bool(CATY_TOKEN.strip())


def _print_qr_tty():
    if not _pairing_token_configured():
        print(
            "pairing is disabled: set a non-empty CATY_TOKEN before running qr",
            file=sys.stderr,
        )
        return False
    try:
        # §5-3: preflight qrcode before issuance so a missing dependency does not
        # revoke an older live credential or burn a fresh one-time credential.
        import qrcode
    except ImportError:
        print(
            'QR を表示できません。qrcode を導入してください: pip install "qrcode[pil]"',
            file=sys.stderr,
        )
        return False
    try:
        issued = _issue_pairing_for_qr()
    except (pairing_store.PairingStoreError, RuntimeError, ValueError) as error:
        # stderr bypasses log(), so redact here: a future exception text carrying a
        # pair-shaped value must not reach the journal (§9-4).
        print(_redact_log_text(f"pairing issuance failed: {error}"), file=sys.stderr)
        return False
    pair = issued["pair"]
    payload = json.dumps(
        _connection_payload(pair),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    safe_payload = json.dumps(
        _redacted_pair_payload(_connection_payload(pair)),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    qr = qrcode.QRCode(border=1)
    qr.add_data(payload)
    qr.make(fit=True)
    qr.print_ascii(invert=True)
    print(safe_payload)
    return True


def _qr_delivery_mode(cli_value=None, stdout=None):
    value = cli_value or os.environ.get("CATY_QR_DELIVERY", "auto").strip().lower()
    if value not in QR_DELIVERY_MODES:
        raise ValueError(
            "CATY_QR_DELIVERY/--qr-delivery must be one of: auto, tty, url"
        )
    if value == "auto":
        output = stdout or sys.stdout
        return "tty" if output.isatty() else "url"
    return value


def _qr_cli_args(argv):
    parser = argparse.ArgumentParser(prog="caty_gateway.py qr")
    parser.add_argument("--qr-delivery", choices=QR_DELIVERY_MODES)
    parser.add_argument("--wait-visible-seconds", type=int)
    return parser.parse_args(argv)


def _load_qr_png_dependencies():
    # Import both packages before issuing.  qrcode can import successfully while
    # its Pillow image factory is unavailable, which must not burn a one-time
    # credential and only then discover that URL delivery cannot render it.
    import qrcode
    from PIL import Image  # noqa: F401 -- explicit delivery preflight

    # Exercise qrcode's actual Pillow image factory and PNG encoder in memory.
    # Import-only checks miss broken/partial Pillow installations.
    try:
        probe = qrcode.QRCode(border=1)
        probe.add_data("caty-qr-render-preflight")
        probe.make(fit=True)
        buffer = io.BytesIO()
        probe.make_image().save(buffer, format="PNG")
        if not buffer.getvalue().startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("renderer returned non-PNG data")
    except Exception as error:
        raise ImportError("qrcode Pillow PNG renderer is unavailable") from error
    return qrcode


def _render_qr_png(qrcode_module, payload, path):
    qr = qrcode_module.QRCode(border=1)
    qr.add_data(payload)
    qr.make(fit=True)
    image = qr.make_image()
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            image.save(handle, format="PNG")
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def _tailnet_or_loopback_peer(peer):
    try:
        address = ipaddress.ip_address(peer)
    except (TypeError, ValueError):
        return False
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.is_loopback:
        return True
    return address.version == 4 and address in ipaddress.ip_network("100.64.0.0/10")


def _qr_delivery_bind_target():
    configured = os.environ.get("CATY_PUBLIC_URL", "").strip()
    if not configured:
        raise ValueError(
            "URL QR delivery requires CATY_PUBLIC_URL with a locally reachable host"
        )
    parsed = urllib.parse.urlsplit(configured)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError(
            "URL QR delivery requires an http(s) CATY_PUBLIC_URL without userinfo"
        )
    if parsed.hostname in {"0.0.0.0", "::"}:
        raise ValueError("URL QR delivery refuses wildcard CATY_PUBLIC_URL hosts")
    try:
        resolved = socket.getaddrinfo(
            parsed.hostname,
            0,
            family=socket.AF_INET,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as error:
        raise ValueError("CATY_PUBLIC_URL host does not resolve locally") from error
    addresses = []
    for result in resolved:
        address = result[4][0]
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise ValueError("CATY_PUBLIC_URL host has no IPv4 address")
    unsafe = [address for address in addresses if not _tailnet_or_loopback_peer(address)]
    if unsafe:
        raise ValueError(
            "CATY_PUBLIC_URL host must resolve only to loopback or Tailscale IPv4 addresses"
        )
    return parsed.hostname, addresses


def _qr_delivery_handler(path, png_path, delivered, expires_at):
    fetch_lock = threading.Lock()
    fetch_state = {"reserved": False}

    class QRDeliveryHandler(BaseHTTPRequestHandler):
        # Load-bearing for this single-threaded server: a silent client must not
        # park the wait loop past its fail-loud deadline (at worst, by 5 seconds).
        timeout = 5
        delivery_path = path
        delivery_expires_at = expires_at

        def log_message(self, *args):
            pass

        def _not_found(self):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            # client_address is the only source of truth.  In particular, never
            # honor X-Forwarded-For: this is a directly bound ephemeral server.
            peer = self.client_address[0] if self.client_address else ""
            if self.path != path or not _tailnet_or_loopback_peer(peer):
                self._not_found()
                return
            with fetch_lock:
                if (
                    delivered.is_set()
                    or fetch_state["reserved"]
                    or time.time() >= float(expires_at)
                ):
                    self._not_found()
                    return
                # Reserve before writing headers/body so concurrent GETs cannot
                # both pass the one-shot gate.  A disconnected first reader
                # releases the reservation, allowing a real subsequent fetch.
                fetch_state["reserved"] = True
            try:
                with open(png_path, "rb") as handle:
                    body = handle.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                with fetch_lock:
                    fetch_state["reserved"] = False
                return
            with fetch_lock:
                delivered.set()
                fetch_state["reserved"] = False

    return QRDeliveryHandler


def _bind_qr_delivery_server(handler, addresses):
    last_error = None
    for address in addresses:
        try:
            return HTTPServer((address, 0), handler)
        except OSError as error:
            last_error = error
    raise OSError(
        "CATY_PUBLIC_URL host resolves, but none of its exact IPv4 addresses can be bound"
    ) from last_error


def _pairing_was_claimed(pid):
    try:
        root = _get_pairing_store().root_dir
        record_path = os.path.join(root, f"{pid}.json")
        tombstone_path = os.path.join(root, f"{pid}.tombstone")
        if os.path.exists(record_path):
            return False
        with open(tombstone_path, encoding="utf-8") as handle:
            tombstone = json.load(handle)
        return (
            isinstance(tombstone, dict)
            and tombstone.get("pid") == pid
            and tombstone.get("state") == "consumed"
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _url_qr_wait_seconds(requested, expires_at):
    remaining = max(1, int(math.ceil(float(expires_at) - time.time())))
    maximum = remaining if requested is None else max(1, int(requested))
    return max(1, min(maximum, remaining))


def _cleanup_qr_delivery(server, temporary_dir):
    sigterm_ignored = False
    try:
        previous_sigterm_handler = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        sigterm_ignored = True
    except ValueError:
        previous_sigterm_handler = None
    try:
        errors = []
        if server is not None:
            try:
                server.server_close()
            except Exception as error:
                errors.append(f"server close failed ({type(error).__name__}): {error}")
        if temporary_dir is not None and os.path.lexists(temporary_dir):
            try:
                shutil.rmtree(temporary_dir)
            except Exception as error:
                errors.append(f"temporary PNG cleanup failed ({type(error).__name__}): {error}")
        if temporary_dir is not None and os.path.lexists(temporary_dir):
            errors.append("temporary PNG directory still exists after cleanup")
        if errors:
            print(
                _redact_log_text("URL QR cleanup failed: " + "; ".join(errors)),
                file=sys.stderr,
            )
            return False
        return True
    finally:
        if sigterm_ignored:
            signal.signal(signal.SIGTERM, previous_sigterm_handler)


@contextmanager
def _sigterm_raises_system_exit():
    previous_handler = signal.getsignal(signal.SIGTERM)

    def handle_sigterm(_signum, _frame):
        raise SystemExit(143)

    try:
        signal.signal(signal.SIGTERM, handle_sigterm)
    except ValueError:
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


def print_qr_url(wait_visible_seconds=None):
    with _sigterm_raises_system_exit():
        if not _pairing_token_configured():
            print(
                "pairing is disabled: set a non-empty CATY_TOKEN before running qr",
                file=sys.stderr,
            )
            return False

        server = None
        temporary_dir = None
        succeeded = False
        try:
            qrcode_module = _load_qr_png_dependencies()
            _, addresses = _qr_delivery_bind_target()
            path = "/qr/" + secrets.token_urlsafe(24)
            delivered = threading.Event()

            temporary_dir = tempfile.mkdtemp(prefix="caty-pairing-qr-")
            os.chmod(temporary_dir, 0o700)
            png_path = os.path.join(temporary_dir, "pairing.png")
            # Bind first with a handler factory installed after issuance.  The exact
            # address is already reserved without creating a credential yet.
            server = _bind_qr_delivery_server(BaseHTTPRequestHandler, addresses)

            # All rendering prerequisites and the exact bind are ready.  From this
            # point onward there is exactly one issuance call for this delivery.
            issued = _issue_pairing_for_qr()
            pair = issued.get("pair") if isinstance(issued, dict) else None
            expires_at = issued.get("expires_at") if isinstance(issued, dict) else None
            if (
                not isinstance(pair, str)
                or not pairing_store.PAIR_RE.fullmatch(pair)
                or not isinstance(expires_at, (int, float))
            ):
                raise RuntimeError("pairing issuer returned no valid expiry")
            pid, _, _ = pair.partition(".")
            payload = json.dumps(
                _connection_payload(pair),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            _render_qr_png(qrcode_module, payload, png_path)
            server.RequestHandlerClass = _qr_delivery_handler(
                path, png_path, delivered, expires_at
            )

            # Emit the exact address that accepted the bind.  Reusing a hostname
            # with multiple A records could send the phone to a different address
            # where this one-shot listener is not running.
            bound_host, port = server.server_address[:2]
            qr_url = f"http://{bound_host}:{port}{path}"
            expires_iso = datetime.datetime.fromtimestamp(
                float(expires_at), datetime.timezone.utc
            ).isoformat().replace("+00:00", "Z")
            remaining_minutes = max(
                1, int(math.ceil((float(expires_at) - time.time()) / 60.0))
            )
            print(f"QR URL: {qr_url}", flush=True)
            print(
                f"Expires: {expires_iso} ({remaining_minutes} minutes remaining)",
                flush=True,
            )
            print(f"PNG: {png_path}", flush=True)
            print(
                "Relay only the QR URL in the SAME private conversation; never paste pair strings or raw command output.",
                flush=True,
            )
            print(
                "Do not open the QR URL yourself: the first fetch consumes it, and only the person pairing should open it.",
                flush=True,
            )
            print(
                "Alternatively upload the PNG there, delete the local PNG immediately after sending, and delete the uploaded copy after pairing.",
                flush=True,
            )
            print(
                "On the phone, open the URL, save or screenshot the QR, then import it from Photos in CatyPhone.",
                flush=True,
            )

            deadline = time.monotonic() + _url_qr_wait_seconds(
                wait_visible_seconds, expires_at
            )
            server.timeout = 0.2
            while True:
                if delivered.is_set() or _pairing_was_claimed(pid):
                    succeeded = True
                    break
                if time.monotonic() >= deadline or time.time() >= float(expires_at):
                    print(
                        "QR delivery timed out before the URL was fetched or pairing was claimed; "
                        "rerun caty_gateway.py qr to issue a fresh one-time QR",
                        file=sys.stderr,
                    )
                    break
                server.handle_request()
        except ImportError:
            print(
                'URL QR delivery requires qrcode[pil]: pip install "qrcode[pil]"',
                file=sys.stderr,
            )
        except (OSError, pairing_store.PairingStoreError, RuntimeError, ValueError) as error:
            print(_redact_log_text(f"URL QR delivery failed: {error}"), file=sys.stderr)
        except Exception as error:
            # Unexpected renderer/HTTP/datetime failures are still fail-loud, but
            # their text may carry the issued pair.  KeyboardInterrupt and
            # SystemExit deliberately remain outside this catch; finally still
            # closes/removes everything.
            print(
                _redact_log_text(
                    f"URL QR delivery failed unexpectedly ({type(error).__name__}): {error}"
                ),
                file=sys.stderr,
            )
        finally:
            if not _cleanup_qr_delivery(server, temporary_dir):
                succeeded = False
        return succeeded


def print_qr(delivery=None, wait_visible_seconds=None):
    try:
        mode = _qr_delivery_mode(delivery)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return False
    if mode == "tty":
        return _print_qr_tty()
    return print_qr_url(wait_visible_seconds)


def _token_match(candidate, expected):
    # bytes 比較にするのは、str の compare_digest がヘッダ由来の非ASCII文字
    # (latin-1 decode で 0x80-0xFF が混入しうる) で TypeError を投げるため。
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body=b"", ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            # ヘッダはASCII安全に（日本語はそのまま入れられないのでlatin-1で握り潰す）。
            # #98 改行(CR/LF)が値に混じるとHTTPレスポンスのヘッダ枠組みが壊れ、X-Reply
            #     (返事テキスト)の改行以降が body に漏れて、アプリ側で音声が再生不能
            #     (typ?/OSStatus 1954115647)になる。長文の返事は段落改行を含むため必ず除去する。
            safe = v.replace("\r", " ").replace("\n", " ")
            self.send_header(k, safe.encode("utf-8", "ignore").decode("latin-1", "ignore"))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, *a):
        pass  # デフォルトのアクセスログは黙らせる（自前logを使う）

    def _session_id(self):
        return sanitize_session_id(self.headers.get("X-Session-Id", ""))

    def _authorized(self):
        if not CATY_TOKEN:
            return not require_auth_enabled()
        return any(_token_match(token, CATY_TOKEN) for token in self._request_tokens())

    def _require_auth(self):
        if self._authorized():
            return True
        self._send(401, b'{"ok":false,"error":"unauthorized"}')
        return False

    def _require_voice_auth(self, capability):
        """Return a non-secret rate-limit principal or fail closed."""
        tokens = tuple(token for token in self._request_tokens() if token)
        for expected in (CATY_ADMIN_TOKEN, CATY_TOKEN):
            if expected and any(_token_match(token, expected) for token in tokens):
                return voice_preview.request_principal(expected)
        if callable(VOICE_SCOPE_AUTHORIZER):
            try:
                principal = VOICE_SCOPE_AUTHORIZER(tokens, capability)
            except Exception:
                principal = None
            if isinstance(principal, str) and principal:
                return voice_preview.request_principal(principal)
        self._send(
            401,
            b'{"ok":false,"error":"unauthorized"}',
            extra={"WWW-Authenticate": "Bearer"},
        )
        return None

    def _request_tokens(self):
        tokens = []
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            tokens.append(auth[len("Bearer "):])
        tokens.append(self.headers.get("X-Caty-Token", ""))
        return tokens

    def _require_write_auth(self):
        # fail-closed: token が一切設定されていない gateway では書き込み全拒否。
        # CATY_ADMIN_TOKEN 単独設定でも書き込み有効（admin token で認証）。
        # 両方設定時はどちらのトークンでも書き込み可（admin は member への追加credential・#278）。
        expected_tokens = {token for token in (CATY_ADMIN_TOKEN, CATY_TOKEN) if token}
        if not expected_tokens:
            self._send(403, b'{"ok":false,"error":"writes disabled: no token configured"}')
            return False
        if any(_token_match(token, expected) for token in self._request_tokens() for expected in expected_tokens):
            return True
        self._send(401, b'{"ok":false,"error":"unauthorized"}')
        return False

    def _send_openai_error(self, status, message, error_type, code=None, extra=None):
        payload = {"error": {"message": message, "type": error_type}}
        if code is not None:
            payload["error"]["code"] = code
        self._send(status, json.dumps(payload, ensure_ascii=False).encode(), extra=extra)

    def _write_openai_sse(self, payload=None, event=None, comment=None):
        if comment is not None:
            line = f": {comment}\n\n".encode("utf-8")
        else:
            parts = []
            if event:
                parts.append(f"event: {event}\n".encode("utf-8"))
            parts.append(b"data: " + json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n\n")
            line = b"".join(parts)
        self.wfile.write(line)
        self.wfile.flush()

    def _openai_bearer_token(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        return auth[len("Bearer "):]

    def _require_openai_chat_auth(self):
        if self.headers.get("X-Caty-Agent-Trust", "").strip().lower() != "trusted":
            self._send_openai_error(403, "trusted meeting required", "permission_error", "trusted_meeting_required")
            return False
        if not CATY_OPENAI_CHAT_TOKEN:
            self._send_openai_error(503, "chat completions unavailable", "server_error", "chat_completions_unavailable")
            return False
        token = self._openai_bearer_token()
        if token and _token_match(token, CATY_OPENAI_CHAT_TOKEN):
            return True
        self._send_openai_error(
            401,
            "unauthorized",
            "authentication_error",
            "unauthorized",
            extra={"WWW-Authenticate": "Bearer"},
        )
        return False

    def _validate_openai_chat_body(self):
        payload = self._read_json_body(OPENAI_CHAT_BODY_LIMIT)
        if payload is None:
            return None
        if not isinstance(payload, dict):
            self._send_openai_error(400, "json object required", "invalid_request_error", "invalid_request")
            return None
        if "stream" in payload and not isinstance(payload.get("stream"), bool):
            self._send_openai_error(400, "stream must be a boolean", "invalid_request_error", "invalid_stream")
            return None
        messages = payload.get("messages")
        if not isinstance(messages, list):
            self._send_openai_error(400, "messages must be a list", "invalid_request_error", "invalid_messages")
            return None
        user = payload.get("user")
        if not isinstance(user, str) or not user:
            self._send_openai_error(400, "user is required", "invalid_request_error", "missing_user")
            return None
        if len(user.encode("utf-8")) > OPENAI_CHAT_USER_MAX_LEN:
            self._send_openai_error(400, "user is too long", "invalid_request_error", "user_too_long")
            return None
        latest_user = latest_openai_user_text(messages)
        if latest_user is None:
            self._send_openai_error(400, "latest user message is required", "invalid_request_error", "missing_user_message")
            return None
        if not latest_user.strip():
            self._send_openai_error(400, "latest user message is empty", "invalid_request_error", "empty_user_message")
            return None
        payload["_latest_user_text"] = latest_user
        return payload

    def _stream_openai_chat(self, backend, prompt, session_id, response_model):
        chat_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        stream = backend.openai_stream(
            prompt,
            session_id,
            OPENAI_CHAT_TIMEOUT,
            on_heartbeat=lambda: self._write_openai_sse(comment="heartbeat"),
            heartbeat_interval=OPENAI_CHAT_HEARTBEAT_SEC,
        )
        try:
            for delta in stream:
                self._write_openai_sse({
                    "id": chat_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": response_model,
                    "choices": [{
                        "index": 0,
                        "delta": {"content": delta},
                        "finish_reason": None,
                    }],
                })
            self._write_openai_sse({
                "id": chat_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": response_model,
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop",
                }],
            })
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except ClaudeStreamTimeout:
            try:
                self._write_openai_sse({
                    "error": {
                        "message": "chat completion timed out",
                        "type": "timeout_error",
                        "code": "timeout",
                    }
                }, event="error")
            except (BrokenPipeError, ConnectionResetError):
                pass
        except ClaudeStreamError:
            try:
                self._write_openai_sse({
                    "error": {
                        "message": "chat completion failed",
                        "type": "server_error",
                        "code": "backend_failure",
                    }
                }, event="error")
            except (BrokenPipeError, ConnectionResetError):
                pass
        finally:
            stream.close()

    def _do_openai_chat_completions(self):
        if not self._require_openai_chat_auth():
            return
        backend = openai_chat_backend()
        if backend is None:
            self._send_openai_error(503, "chat completions unavailable", "server_error", "chat_completions_unavailable")
            return
        payload = self._validate_openai_chat_body()
        if payload is None:
            return
        prompt = payload["_latest_user_text"]
        session_id = openai_chat_session_id(payload["user"])
        response_model = openai_chat_response_model(payload.get("model"))
        error, release = try_begin_openai_chat(session_id)
        if error == "busy":
            self._send_openai_error(409, "chat completion already in progress for this session", "conflict_error", "session_busy")
            return
        if error == "overloaded":
            self._send_openai_error(429, "chat completions are busy", "rate_limit_error", "gateway_busy")
            return
        try:
            if payload.get("stream") is True:
                try:
                    self._stream_openai_chat(backend, prompt, session_id, response_model)
                except (BrokenPipeError, ConnectionResetError):
                    log("⚠️ chat completions stream disconnected")
                return

            try:
                reply = backend.openai_complete(prompt, session_id, OPENAI_CHAT_TIMEOUT)
            except ClaudeStreamTimeout:
                self._send_openai_error(504, "chat completion timed out", "timeout_error", "timeout")
                return
            except ClaudeStreamError:
                self._send_openai_error(502, "chat completion failed", "server_error", "backend_failure")
                return
            if not isinstance(reply, str) or not reply:
                self._send_openai_error(502, "chat completion returned no text", "server_error", "empty_response")
                return
            self._send_json(200, {
                "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": response_model,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": reply},
                    "finish_reason": "stop",
                }],
            })
        finally:
            if release is not None:
                release()

    def _read_body_limited(self, limit, empty_error="empty body"):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send(400, json.dumps({"ok": False, "error": "invalid content length"}).encode())
            return None
        if length <= 0:
            self._send(400, json.dumps({"ok": False, "error": empty_error}).encode())
            return None
        if length > limit:
            self._send(413, json.dumps({"ok": False, "error": "payload too large"}).encode())
            return None
        return self.rfile.read(length)

    def _read_json_body(self, limit, error_value=None):
        raw = self._read_body_limited(limit, "empty json body")
        if raw is None:
            return error_value
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            self._send(400, b'{"ok":false,"error":"invalid json"}')
            return error_value

    def _send_json(self, code, payload):
        self._send(code, json.dumps(payload, ensure_ascii=False).encode())

    def _send_voice_error(self, error):
        status = getattr(error, "status", 500)
        code = getattr(error, "code", "voice_api_error")
        retry_after = getattr(error, "retry_after", None)
        extra = None
        if retry_after is not None:
            try:
                retry_after = max(1, min(3600, int(math.ceil(float(retry_after)))))
                extra = {"Retry-After": str(retry_after)}
            except (TypeError, ValueError):
                retry_after = None
        payload = {
            "ok": False,
            "error": code,
            "retryable": getattr(error, "retryable", status in {429, 502, 503, 504}),
        }
        if retry_after is not None:
            payload["retry_after_seconds"] = retry_after
        details = getattr(error, "details", None)
        if isinstance(details, dict):
            # Only normalized API fields may cross this seam. In particular,
            # details can never replace ok/error/retryable or inject headers.
            for key in (
                "availability", "config_version", "recovery_candidates",
                "state_unchanged",
            ):
                if key in details:
                    payload[key] = details[key]
        self._send(
            status,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            extra=extra,
        )

    def _do_voice_catalog(self, query):
        params = urllib.parse.parse_qs(query, keep_blank_values=True)
        value = lambda key, default=None: params.get(key, [default])[0]
        try:
            payload = _get_voice_catalog_service().list_voices(
                scope=value("scope", "recommended"),
                language=value("language") if "language" in params else None,
                query=value("query", ""),
                direction=value("direction", ""),
                impression=value("impression", ""),
                cursor=value("cursor"),
                page_size=value("page_size", voice_catalog.DEFAULT_PAGE_SIZE),
            )
        except voice_catalog.CatalogError as error:
            self._send_voice_error(error)
            return
        self._send_json(200, {"ok": True, **payload})

    def _do_voice_preview(self, principal):
        payload = self._read_json_body(VOICE_PREVIEW_BODY_LIMIT, _JSON_READ_ERROR)
        if payload is _JSON_READ_ERROR:
            return
        if not isinstance(payload, dict):
            self._send_voice_error(voice_preview.PreviewError("json_object_required", 400))
            return
        if "text" in payload:
            self._send_voice_error(voice_preview.PreviewError("arbitrary_text_not_allowed", 400))
            return
        unknown = sorted(set(payload) - {"catalog_id", "reference_id"})
        if unknown:
            self._send_voice_error(voice_preview.PreviewError("invalid_preview_fields", 400))
            return
        catalog_id = payload.get("catalog_id")
        reference_id = payload.get("reference_id")
        if catalog_id is not None and not isinstance(catalog_id, str):
            self._send_voice_error(voice_preview.PreviewError("invalid_catalog_id", 400))
            return
        if reference_id is not None and not isinstance(reference_id, str):
            self._send_voice_error(voice_preview.PreviewError("invalid_reference_id", 400))
            return
        if bool(catalog_id) == bool(reference_id):
            self._send_voice_error(
                voice_preview.PreviewError("exactly_one_voice_identifier_required", 400)
            )
            return
        try:
            result = _get_voice_preview_service().preview(
                principal,
                catalog_id=catalog_id,
                reference_id=reference_id,
            )
        except voice_preview.PreviewError as error:
            self._send_voice_error(error)
            return
        extra = {
            "Cache-Control": "private, max-age=0",
            "X-Voice-Preview-Script-Id": result["script_id"],
            "X-Inference-Contract-Version": result["inference_contract_version"],
            "X-Voice-Preview-Cache": result["cache"],
            "X-Voice-Preview-Stale": "1" if result["stale"] else "0",
        }
        if result["duration_seconds"] is not None:
            extra["X-Voice-Preview-Duration"] = f"{result['duration_seconds']:.3f}"
        self._send(
            200,
            result["audio"],
            ctype=result["content_type"],
            extra=extra,
        )

    def _do_voice_state(self):
        try:
            state = _get_voice_activation_service().state()
        except Exception:
            self._send_voice_error(
                voice_activation.ActivationError(
                    "voice_state_unavailable", 503, retryable=True
                )
            )
            return
        try:
            state["neutral"] = _get_neutral_voice_readiness().state()
        except Exception:
            preset = voice_presets.PRESETS.get("fish-neutral-ja-v1") or {}
            state["neutral"] = {
                "preset_id": "fish-neutral-ja-v1",
                "reference_id": preset.get(
                    "reference_id",
                    "0089dce5fefb4c6ba9b9f2f0debe1ddc",
                ),
                "availability": "unknown",
                "checked_at": None,
            }
        self._send_json(200, {"ok": True, **state})

    def _do_voice_activation(self):
        payload = self._read_json_body(VOICE_ACTIVATION_BODY_LIMIT, _JSON_READ_ERROR)
        if payload is _JSON_READ_ERROR:
            return
        try:
            service = _get_voice_activation_service()
        except Exception:
            self._send_voice_error(
                voice_activation.ActivationError(
                    "voice_activation_unknown", 503, retryable=True,
                    details={"availability": "unknown", "state_unchanged": True},
                )
            )
            return
        try:
            before_version = CONFIG.get().get("config_version")
        except Exception:
            before_version = None
        try:
            result = service.activate(payload, self.headers.get("If-Match"))
        except voice_activation.ActivationError as error:
            if error.code == "version_conflict":
                self._send_json(409, {
                    "ok": False,
                    "error": "version_conflict",
                    "config_version": error.details.get("config_version"),
                })
                return
            if error.code == "invalid_if_match_header":
                self._send_json(400, {"ok": False, "error": error.code})
                return
            self._send_voice_error(error)
            return
        except Exception:
            details = {"availability": "unknown"}
            if before_version is not None:
                try:
                    details["state_unchanged"] = (
                        CONFIG.get().get("config_version") == before_version
                    )
                except Exception:
                    pass
            self._send_voice_error(
                voice_activation.ActivationError(
                    "voice_activation_unknown", 503, retryable=True,
                    details=details,
                )
            )
            return
        self._send_json(200, result)

    def _filler_texts_payload(self):
        service = _get_voice_activation_service()
        effective = filler_texts.effective(IDENTITY_ID, service.data_root)
        defaults = filler_texts.load_default()
        filler_state = service.state().get("filler", {})
        override = {
            kind: list(effective.texts[kind])
            for kind, source in effective.sources.items()
            if source == "override"
        }
        return {
            "ok": True,
            "version": effective.version,
            "default_version": filler_texts.text_version(defaults),
            "live_pool": (
                "legacy"
                if CONFIG.get().get("voice_management_state") == "legacy"
                else "managed"
            ),
            "effective": effective.texts,
            "defaults": defaults,
            "override": override or None,
            "override_status": effective.override_status,
            "sources": effective.sources,
            "constraints": {
                "kinds": list(filler_texts.KINDS),
                "per_kind_max": filler_texts.max_per_kind(),
                "min_len": filler_texts.MIN_LEN,
                "max_len": filler_texts.MAX_LEN,
            },
            "text_stale": bool(filler_state.get("text_stale", False)),
            "desired_text_version": filler_state.get(
                "desired_text_version", effective.version
            ),
            "active_text_version": filler_state.get("active_text_version"),
        }

    def _do_filler_texts_get(self):
        if not self._require_auth():
            return
        try:
            self._send_json(200, self._filler_texts_payload())
        except Exception:
            self._send_json(503, {"ok": False, "error": "filler_texts_unavailable"})

    def _do_filler_texts_put(self):
        if not self._require_write_auth():
            return
        payload = self._read_json_body(CONFIG_BODY_LIMIT, _JSON_READ_ERROR)
        if payload is _JSON_READ_ERROR:
            return
        kinds = payload.get("kinds") if isinstance(payload, dict) else None
        if not isinstance(payload, dict) or set(payload) != {"kinds"}:
            self._send_json(400, {
                "ok": False, "error": "invalid_filler_texts",
                "errors": {"kinds": ["body must contain only kinds"]},
            })
            return
        errors = filler_texts.validate(kinds)
        if errors:
            self._send_json(400, {
                "ok": False, "error": "invalid_filler_texts", "errors": errors,
            })
            return
        try:
            service = _get_voice_activation_service()
            updated = filler_texts.save_override(
                IDENTITY_ID,
                kinds,
                if_match=self.headers.get("If-Match"),
                data_root=service.data_root,
            )
        except filler_texts.ConflictError:
            self._send_json(409, self._filler_texts_payload())
            return
        except filler_texts.ValidationError as error:
            self._send_json(400, {
                "ok": False,
                "error": "invalid_filler_texts",
                "errors": error.errors,
            })
            return
        except Exception:
            self._send_json(503, {"ok": False, "error": "filler_texts_unavailable"})
            return
        try:
            response = self._filler_texts_payload()
        except Exception:
            response = {"ok": True, "version": updated.version}
        self._send_json(200, response)

    def _do_filler_texts_delete(self):
        if not self._require_write_auth():
            return
        try:
            service = _get_voice_activation_service()
            updated = filler_texts.delete_override(
                IDENTITY_ID,
                if_match=self.headers.get("If-Match"),
                data_root=service.data_root,
            )
        except filler_texts.ConflictError:
            self._send_json(409, self._filler_texts_payload())
            return
        except Exception:
            self._send_json(503, {"ok": False, "error": "filler_texts_unavailable"})
            return
        try:
            response = self._filler_texts_payload()
        except Exception:
            response = {"ok": True, "version": updated.version}
        self._send_json(200, response)

    def _do_filler_regenerate(self):
        if not self._require_write_auth():
            return
        payload = self._read_optional_json_body(
            CONFIG_BODY_LIMIT, require_object=False, error_value=_JSON_READ_ERROR
        )
        if payload is _JSON_READ_ERROR:
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"ok": False, "error": "invalid_request"})
            return
        if (
            set(payload) - {"force"}
            or ("force" in payload and not isinstance(payload["force"], bool))
        ):
            self._send_json(400, {"ok": False, "error": "invalid_request"})
            return
        try:
            result = _get_voice_activation_service().regenerate(
                force=payload.get("force", False)
            )
        except voice_activation.ActivationError as error:
            if error.code == "override_invalid":
                self._send_json(409, {"ok": False, "error": "override_invalid"})
                return
            self._send_voice_error(error)
            return
        self._send_json(200, result)

    def _do_events(self, query):
        params = urllib.parse.parse_qs(query)
        cursor = params.get("cursor", [None])[0]
        if not cursor:
            cursor = None
        wait_s = self._query_int(query, "wait")
        limit = self._query_int(query, "limit")
        events, next_cursor, gap = PUSH_EVENTS.read(
            cursor=cursor,
            wait_s=wait_s if wait_s is not None else 0,
            limit=limit if limit is not None else 50,
        )
        self._send_json(200, {
            "ok": True,
            "events": events,
            "next_cursor": next_cursor,
            "gap": gap,
        })

    def _reject_push(self, code, reason):
        log(f"📮 push reject {code} reason={reason}")
        self._send_json(code, {"ok": False, "error": reason})

    def _do_push(self):
        if not self._require_write_auth():
            # 実運用で最頻の拒否原因（token ミス）も #784 の1行ログに含める。
            # レスポンスは _require_write_auth が送信済み。
            log("📮 push reject reason=auth")
            return
        body = self._read_json_body(PUSH_BODY_LIMIT)
        if body is None:
            log("📮 push reject reason=body")
            return
        if not isinstance(body, dict):
            self._reject_push(400, "json object required")
            return
        kind = body.get("kind")
        # Phase 1b (#770): media joins open_url. The app-side allowlist
        # (PushEventClient) already accepts both; URL/title validation below is
        # shared, and payload.media_type passes through as an app-side hint.
        if kind not in ("open_url", "media"):
            self._reject_push(400, "kind not enabled")
            return
        audience = body.get("audience")
        valid_audience = audience == "all" or (
            isinstance(audience, dict)
            and set(audience) == {"member"}
            and isinstance(audience["member"], str)
            and bool(audience["member"])
        )
        if not valid_audience:
            self._reject_push(400, "audience required")
            return
        payload = body.get("payload")
        if not isinstance(payload, dict):
            self._reject_push(400, "invalid payload")
            return
        url = payload.get("url")
        if not isinstance(url, str) or not url:
            self._reject_push(400, "invalid url")
            return
        parsed_url = urllib.parse.urlparse(url)
        if parsed_url.scheme.lower() not in {"http", "https"} or parsed_url.username is not None:
            self._reject_push(400, "invalid url")
            return
        if len(url) > 2048:
            self._reject_push(400, "url too long")
            return
        title = payload.get("title")
        if not isinstance(title, str) or not title:
            self._reject_push(400, "title required")
            return
        if len(title) > 200:
            self._reject_push(400, "title too long")
            return
        media_type = payload.get("media_type")
        if media_type is not None and media_type not in ("image", "video", "youtube"):
            self._reject_push(400, "invalid media_type")
            return
        session_id = body.get("session_id")
        session_source = "explicit"
        if session_id is None:
            session_id = sanitize_session_id(recent_voice_session())
            session_source = "auto" if session_id else "none"
        if session_id is not None:
            if not isinstance(session_id, str):
                self._reject_push(400, "invalid session_id")
                return
            sanitized_session_id = sanitize_session_id(session_id)
            if not sanitized_session_id or sanitized_session_id != session_id.strip():
                self._reject_push(400, "invalid session_id")
                return
            session_id = sanitized_session_id
        event_key = body.get("event_key")
        if event_key is not None and not isinstance(event_key, str):
            self._reject_push(400, "invalid event_key")
            return
        producer = body.get("producer")
        if "producer" in body and (not isinstance(producer, str) or len(producer) > 100):
            self._reject_push(400, "invalid producer")
            return
        ttl_s = body.get("ttl_s", 600)
        if isinstance(ttl_s, bool):
            self._reject_push(400, "invalid ttl_s")
            return
        if isinstance(ttl_s, float):
            if not ttl_s.is_integer():
                self._reject_push(400, "invalid ttl_s")
                return
            ttl_s = int(ttl_s)
        elif not isinstance(ttl_s, int):
            self._reject_push(400, "invalid ttl_s")
            return
        try:
            envelope, duplicate, resolved_session_source = PUSH_EVENTS.publish_with_status(
                kind,
                payload,
                audience,
                session_id=session_id,
                session_id_source=session_source,
                event_key=event_key,
                ttl_s=ttl_s,
                producer=producer,
            )
        except push_events.DuplicateKeyError:
            self._reject_push(409, "event_key conflict")
            return
        # duplicate ヒット時は既存 envelope が正 — レスポンスの session_id と
        # session_id_source は保存済みの値を返す（ハンドラ変数との乖離を作らない）。
        stored_session_id = envelope.get("session_id")
        log(f"📮 push accept kind={kind} id={envelope['id']} session={stored_session_id or '-'} src={resolved_session_source} dup={duplicate}")
        self._send_json(200, {
            "ok": True,
            "id": envelope["id"],
            "duplicate": duplicate,
            "session_id": stored_session_id,
            "session_id_source": resolved_session_source,
        })

    def _do_config_get(self):
        if not self._require_auth():
            return
        self._send_json(200, config_payload())

    def _do_config_put(self):
        if not self._require_write_auth():
            return
        payload = self._read_json_body(CONFIG_BODY_LIMIT)
        if payload is None:
            return
        try:
            updated = CONFIG.update(payload, self.headers.get("If-Match"))
        except gateway_config.VersionConflict as e:
            self._send_json(409, {"ok": False, "error": "version_conflict", "config_version": e.current_version})
            return
        except gateway_config.InvalidConfig as e:
            body = {"ok": False, "error": str(e)}
            if e.invalid_keys:
                body["invalid_keys"] = e.invalid_keys
            self._send_json(400, body)
            return
        self._send_json(200, config_payload(updated))

    def _external_limit(self, query):
        value = self._query_int(query, "limit")
        if value is None:
            value = 30
        return max(1, min(EXTERNAL_SESSIONS_MAX_LIMIT, value))

    def _do_external_sessions(self, query):
        if not self._require_write_auth():
            return
        sessions = []
        for item in BACKEND.list_external(self._external_limit(query)):
            if isinstance(item, dict):
                row = dict(item)
                if not external_preview_enabled():
                    row["label"] = row.get("native_id", "")
                    row["preview"] = ""
                sessions.append(row)
            else:
                sessions.append(item)
        self._send_json(200, {"backend": BACKEND_NAME, "sessions": sessions})

    def _external_listing_by_native(self):
        sessions = BACKEND.list_external(EXTERNAL_SESSIONS_MAX_LIMIT)
        by_native = {}
        for item in sessions:
            if isinstance(item, dict) and isinstance(item.get("native_id"), str):
                by_native[item["native_id"]] = item
        return by_native

    def _do_external_takeover(self):
        if not self._require_write_auth():
            return
        payload = self._read_json_body(EXTERNAL_BODY_LIMIT)
        if payload is None:
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"ok": False, "error": "json object required"})
            return
        native_id = payload.get("native_id")
        if not isinstance(native_id, str) or not native_id.strip():
            self._send_json(400, {"ok": False, "error": "missing native_id"})
            return
        native_id = native_id.strip()

        listing = self._external_listing_by_native()
        item = listing.get(native_id)
        if item is None:
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        title = native_id if not external_preview_enabled() else (str(item.get("label") or "").strip() or native_id)

        seed_n = _external_seed_ceiling()
        if "seed_turns" in payload:
            try:
                seed_n = min(seed_n, max(0, int(payload.get("seed_turns"))))
            except (TypeError, ValueError, OverflowError):
                self._send_json(400, {"ok": False, "error": "invalid seed_turns"})
                return

        with _EXTERNAL_TAKEOVER_LOCK:
            existing_sid = session_links.find_by_native(native_id)
            if existing_sid:
                link = session_links.get(existing_sid)
                if link and link.get("backend") == BACKEND_NAME:
                    self._send_json(200, {
                        "ok": True,
                        "session_id": existing_sid,
                        "title": title,
                        "seeded": 0,
                        "already_linked": True,
                    })
                    return
                self._send_json(409, {"ok": False, "error": "linked by another backend"})
                return

            sid = secrets.token_hex(6)
            turns = BACKEND.read_external(native_id, seed_n)
            seeded = 0
            for turn in turns:
                if not isinstance(turn, dict):
                    continue
                role = turn.get("role")
                text = turn.get("text")
                if role not in ("user", "assistant") or text is None:
                    continue
                history_store.append_turn(sid, role, text, ts=turn.get("ts"))
                seeded += 1
            history_store.set_title(sid, title)
            session_links.put(sid, BACKEND_NAME, native_id)
        log(f"external takeover: sid={sid} native={native_id} backend={BACKEND_NAME} seeded={seeded}")
        self._send_json(200, {
            "ok": True,
            "session_id": sid,
            "title": title,
            "seeded": seeded,
            "already_linked": False,
        })

    def _valid_image_bytes(self, data):
        return data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff")

    def _do_share(self):
        if not self._require_write_auth():
            return
        raw = self._read_body_limited(SHARE_BODY_LIMIT, "no file")
        if raw is None:
            return
        try:
            parts, part_metadata = parse_multipart_form(
                self.headers, raw, include_metadata=True
            )
        except ValueError:
            self._send_json(400, {"ok": False, "error": "invalid multipart body"})
            return

        file_data = parts.get("file")
        if not file_data:
            self._send_json(400, {"ok": False, "error": "missing file"})
            return
        if not parts.get("kind"):
            self._send_json(400, {"ok": False, "error": "missing kind"})
            return
        if not parts.get("session_id"):
            self._send_json(400, {"ok": False, "error": "missing session_id"})
            return

        try:
            kind = parts["kind"].decode("utf-8").strip()
        except UnicodeDecodeError:
            kind = ""
        if kind not in ("image", "file"):
            self._send_json(400, {"ok": False, "error": "invalid kind"})
            return

        try:
            raw_session_id = parts["session_id"].decode("utf-8").strip()
        except UnicodeDecodeError:
            raw_session_id = ""
        if (
            not raw_session_id
            or "\x00" in raw_session_id
            or "/" in raw_session_id
            or "\\" in raw_session_id
            or ".." in raw_session_id
        ):
            self._send_json(400, {"ok": False, "error": "invalid session_id"})
            return
        session_id = sanitize_session_id(raw_session_id)
        if not session_id:
            self._send_json(400, {"ok": False, "error": "invalid session_id"})
            return

        try:
            filename = parts.get("filename", b"").decode("utf-8")
        except UnicodeDecodeError:
            filename = None
        if (
            filename is None
            or "\x00" in filename
            or "/" in filename
            or "\\" in filename
            or ".." in filename
            or os.path.basename(filename) != filename
        ):
            self._send_json(400, {"ok": False, "error": "invalid filename"})
            return

        size_limit = SHARE_IMAGE_LIMIT if kind == "image" else SHARE_FILE_LIMIT
        if len(file_data) > size_limit:
            self._send_json(413, {"ok": False, "error": "payload too large"})
            return
        if kind == "image" and not self._valid_image_bytes(file_data):
            self._send_json(
                415, {"ok": False, "error": "unsupported media type"}
            )
            return

        idempotency_key = self.headers.get("Idempotency-Key")
        if idempotency_key is not None and (
            not idempotency_key
            or len(idempotency_key) > 128
            or any(ord(char) < 0x20 or ord(char) > 0x7e for char in idempotency_key)
        ):
            self._send_json(
                400, {"ok": False, "error": "invalid idempotency key"}
            )
            return

        mime = part_metadata.get("file", {}).get(
            "content_type", "application/octet-stream"
        )
        try:
            result = _get_share_store().put(
                session_id=session_id,
                kind=kind,
                filename=filename,
                mime=mime,
                data=file_data,
                idempotency_key=idempotency_key,
            )
        except share_store.IdempotencyConflict:
            self._send_json(
                409, {"ok": False, "error": "idempotency conflict"}
            )
            return
        except share_store.ShareQuotaExceeded:
            self._send_json(
                429, {"ok": False, "error": "too many staged shares"}
            )
            return
        except share_store.ShareStagingError:
            log(
                "stage=share status=failed "
                f"kind={kind} bytes={len(file_data)} "
                "error_type=ShareStagingError"
            )
            self._send_json(500, {"ok": False, "error": "share staging failed"})
            return
        except ValueError:
            self._send_json(400, {"ok": False, "error": "invalid share metadata"})
            return
        except Exception as error:
            log(
                "stage=share status=failed "
                f"kind={kind} bytes={len(file_data)} "
                f"error_type={type(error).__name__}"
            )
            self._send_json(500, {"ok": False, "error": "share staging failed"})
            return
        log(
            "stage=share status=staged "
            f"share_id={result['share_id']} kind={kind} bytes={len(file_data)}"
        )
        self._send_json(200, {"ok": True, **result})

    def _swap_directory(self, staging, final_dir):
        parent = os.path.dirname(os.path.abspath(final_dir)) or "."
        os.makedirs(parent, exist_ok=True)
        backup = os.path.join(parent, f".backup-{os.path.basename(final_dir)}-{uuid.uuid4().hex}")
        moved_old = False
        try:
            if os.path.exists(final_dir):
                os.replace(final_dir, backup)
                moved_old = True
            os.replace(staging, final_dir)
            if moved_old:
                shutil.rmtree(backup, ignore_errors=True)
        except Exception:
            if os.path.exists(staging):
                shutil.rmtree(staging, ignore_errors=True)
            if moved_old and not os.path.exists(final_dir) and os.path.exists(backup):
                os.replace(backup, final_dir)
            raise

    def _apply_asset_slots(self, slot_bytes):
        final_dir = ASSET_DIR
        parent = os.path.dirname(os.path.abspath(final_dir)) or "."
        os.makedirs(parent, exist_ok=True)
        staging = tempfile.mkdtemp(prefix=".staging-assets-", dir=parent)
        try:
            with ASSET_LOCK:
                for name in GATEWAY_SLOTS:
                    current = os.path.join(final_dir, f"{name}.png")
                    if os.path.exists(current):
                        shutil.copy2(current, os.path.join(staging, f"{name}.png"))
                for name, data in slot_bytes.items():
                    with open(os.path.join(staging, f"{name}.png"), "wb") as f:
                        f.write(data)
                self._swap_directory(staging, final_dir)
                return CONFIG.bump("assets_version")
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def _do_assets_batch(self):
        if not self._require_write_auth():
            return
        raw = self._read_body_limited(ASSET_BATCH_LIMIT, "empty multipart body")
        if raw is None:
            return
        try:
            parts = parse_multipart_form(self.headers, raw)
        except ValueError as e:
            boundary = re.search(r'boundary="?([^";]+)"?', self.headers.get("Content-Type", ""))
            if str(e) == "invalid multipart body" and boundary:
                marker = b"--" + boundary.group(1).encode("ascii", "ignore") + b"--"
                if raw.strip() == marker:
                    parts = {}
                else:
                    self._send_json(400, {"ok": False, "error": str(e)})
                    return
            else:
                self._send_json(400, {"ok": False, "error": str(e)})
                return

        # multipartのfield名だけをslotとして扱い、client指定filenameは無視する。
        slot_set = set(GATEWAY_SLOTS)
        unknown = sorted(set(parts) - slot_set)
        if unknown:
            self._send_json(400, {"ok": False, "error": "unknown asset field", "invalid_fields": unknown})
            return
        provided = [name for name in GATEWAY_SLOTS if name in parts]
        if not provided:
            self._send_json(400, {"ok": False, "error": "missing asset field", "missing_fields": list(GATEWAY_SLOTS)})
            return
        for name in provided:
            data = parts[name]
            if not data:
                self._send_json(400, {"ok": False, "error": "empty asset field", "field": name})
                return
            if len(data) > ASSET_FILE_LIMIT:
                self._send_json(400, {"ok": False, "error": "asset too large", "field": name})
                return
            if not self._valid_image_bytes(data):
                self._send_json(400, {"ok": False, "error": "invalid image", "field": name})
                return

        try:
            version = self._apply_asset_slots({name: parts[name] for name in provided})
        except Exception as e:
            log("❌ assets batch:", repr(e))
            self._send_json(500, {"ok": False, "error": "asset write failed"})
            return
        self._send_json(200, {"ok": True, "assets_version": version})

    def _avatar_engine_or_503(self):
        try:
            return _get_avatar_engine()
        except AvatarEngineDisabled as e:
            self._send_json(503, {"ok": False, "error": "avatar generation disabled", "detail": str(e)})
            return None

    def _avatar_clients_or_error(self, kind, cloud_session=_NO_CLOUD_SESSION):
        try:
            return _avatar_pass_clients(kind, cloud_session)
        except gateway_config.InvalidConfig as e:
            self._send_json(400, {"ok": False, "error": "invalid_cloud_session", "detail": str(e)})
        except AvatarEngineDisabled as e:
            self._send_json(503, {"ok": False, "error": "avatar generation disabled", "detail": str(e)})
        return None

    def _multipart_cloud_session(self, parts):
        if "cloud_session" not in parts:
            return _NO_CLOUD_SESSION
        raw = parts["cloud_session"]
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise gateway_config.InvalidConfig("cloud_session must be valid JSON") from None
        if not isinstance(payload, dict):
            raise gateway_config.InvalidConfig("cloud_session must be a JSON object")
        return payload

    def _avatar_job_payload(self, snapshot):
        if snapshot is None or (snapshot.get("stage") == "idle" and not snapshot.get("job_id")):
            return None
        return snapshot

    def _do_avatar_job_get(self):
        if not self._require_auth():
            return
        if _avatar_engine is None:
            self._send_json(200, {"ok": True, "job": None})
            return
        try:
            snapshot = _avatar_engine.snapshot()
        except AvatarJobStateError:
            snapshot = None
        self._send_json(200, {"ok": True, "job": self._avatar_job_payload(snapshot)})

    def _do_avatar_job_file(self, raw_name):
        if not self._require_auth():
            return
        file_keys = {
            "base": ("base_candidate_512", None),
            "contact-sheet": ("contact_sheet", None),
            **{slot: ("final_pngs", slot) for slot in GATEWAY_SLOTS},
        }
        name = urllib.parse.unquote(raw_name)
        key = file_keys.get(name)
        if key is None or _avatar_engine is None:
            self._send(404, b'{"ok":false,"error":"not found"}')
            return
        snapshot = _avatar_engine.snapshot()
        paths = snapshot.get("paths") or {}
        path = paths.get(key[0])
        if key[1] is not None:
            path = path.get(key[1]) if isinstance(path, dict) else None
        if not path or not os.path.isfile(path):
            self._send(404, b'{"ok":false,"error":"not found"}')
            return
        with open(path, "rb") as f:
            self._send(200, f.read(), ctype="image/png")

    def _do_avatar_stylize(self):
        if not self._require_write_auth():
            return
        raw = self._read_body_limited(ASSET_BATCH_LIMIT, "empty multipart body")
        if raw is None:
            return
        try:
            parts = parse_multipart_form(self.headers, raw)
        except ValueError as e:
            self._send_json(400, {"ok": False, "error": str(e)})
            return
        try:
            cloud_session = self._multipart_cloud_session(parts)
        except gateway_config.InvalidConfig as e:
            self._send_json(400, {"ok": False, "error": "invalid_cloud_session", "detail": str(e)})
            return
        photo = parts.get("photo")
        if not photo:
            self._send_json(400, {"ok": False, "error": "missing photo field"})
            return
        if len(photo) > ASSET_FILE_LIMIT:
            self._send_json(400, {"ok": False, "error": "photo too large"})
            return
        if not self._valid_image_bytes(photo):
            self._send_json(400, {"ok": False, "error": "invalid image", "field": "photo"})
            return
        identity_description = None
        if parts.get("identity_description"):
            identity_description = parts["identity_description"].decode("utf-8", "replace").strip() or None
            if identity_description and len(identity_description) > IDENTITY_DESCRIPTION_LIMIT:
                self._send_json(400, {"ok": False, "error": "identity_description too long"})
                return
        engine = self._avatar_engine_or_503()
        if engine is None:
            return
        pass_clients = self._avatar_clients_or_error("stylize", cloud_session)
        if pass_clients is None:
            return
        try:
            job = engine.start_stylize(photo, identity_description, pass_clients)
        except AvatarEngineDisabled as e:
            self._send_json(503, {"ok": False, "error": "avatar generation disabled", "detail": str(e)})
            return
        except AvatarEngineBusy as e:
            self._send_json(409, {"ok": False, "error": "busy", "detail": str(e)})
            return
        except AvatarCredentialConflict as e:
            self._send_json(409, {"ok": False, "error": "credential_conflict", "detail": str(e)})
            return
        except Exception as e:
            log("❌ avatar stylize:", repr(e))
            self._send_json(500, {"ok": False, "error": "avatar stylize failed"})
            return
        self._send_json(200, {"ok": True, "job": job.snapshot()})

    def _do_avatar_regenerate(self):
        if not self._require_write_auth():
            return
        payload = self._read_optional_json_body(CONFIG_BODY_LIMIT)
        if payload is None:
            return
        engine = self._avatar_engine_or_503()
        if engine is None:
            return
        try:
            stage = engine.snapshot().get("stage")
            if stage in {"stylizing", "awaiting_base_approval"}:
                kind = "stylize"
            elif stage in {"generating", "awaiting_set_approval"}:
                kind = "set"
            else:
                raise AvatarJobStateError(f"cannot regenerate while stage is {stage}")
            pass_clients = self._avatar_clients_or_error(
                kind, payload.get("cloud_session", _NO_CLOUD_SESSION)
            )
            if pass_clients is None:
                return
            engine.assert_pass_credentials(pass_clients)
            if stage == "awaiting_base_approval":
                job = engine.regenerate_base(pass_clients)
            elif stage == "awaiting_set_approval":
                job = engine.regenerate_set(pass_clients)
            else:
                raise AvatarJobStateError(f"cannot regenerate while stage is {stage}")
        except AvatarCredentialConflict as e:
            self._send_json(409, {"ok": False, "error": "credential_conflict", "detail": str(e)})
            return
        except AvatarJobStateError as e:
            self._send_json(409, {"ok": False, "error": "wrong_stage", "detail": str(e)})
            return
        except Exception as e:
            log("❌ avatar regenerate:", repr(e))
            self._send_json(500, {"ok": False, "error": "avatar regenerate failed"})
            return
        self._send_json(200, {"ok": True, "job": job.snapshot()})

    def _do_avatar_cancel(self):
        if not self._require_write_auth():
            return
        engine = self._avatar_engine_or_503()
        if engine is None:
            return
        try:
            engine.cancel()
        except AvatarEngineDisabled as e:
            self._send_json(503, {"ok": False, "error": "avatar generation disabled", "detail": str(e)})
            return
        except AvatarJobStateError as e:
            self._send_json(409, {"ok": False, "error": "wrong_stage", "detail": str(e)})
            return
        except Exception as e:
            log("❌ avatar cancel:", repr(e))
            self._send_json(500, {"ok": False, "error": "avatar cancel failed"})
            return
        self._send_json(200, {"ok": True, "job": None})

    def _read_optional_json_body(
        self, limit, *, require_object=True, error_value=None
    ):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send(400, json.dumps({"ok": False, "error": "invalid content length"}).encode())
            return error_value
        if length <= 0:
            return {}
        if length > limit:
            self._send(413, json.dumps({"ok": False, "error": "payload too large"}).encode())
            return error_value
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            self._send(400, b'{"ok":false,"error":"invalid json"}')
            return error_value
        if require_object and not isinstance(payload, dict):
            self._send_json(400, {"ok": False, "error": "json object required"})
            return error_value
        return payload

    def _do_avatar_approve_base(self):
        if not self._require_write_auth():
            return
        payload = self._read_optional_json_body(CONFIG_BODY_LIMIT)
        if payload is None:
            return
        raw_description = payload.get("identity_description")
        identity_description = raw_description.strip() if isinstance(raw_description, str) else ""
        if len(identity_description) > IDENTITY_DESCRIPTION_LIMIT:
            self._send_json(400, {"ok": False, "error": "identity_description too long"})
            return
        engine = self._avatar_engine_or_503()
        if engine is None:
            return
        pass_clients = self._avatar_clients_or_error(
            "set", payload.get("cloud_session", _NO_CLOUD_SESSION)
        )
        if pass_clients is None:
            return
        approve_description = identity_description or GENERIC_IDENTITY_DESCRIPTION
        description_source = "client" if identity_description else "generic"
        if not identity_description:
            try:
                snapshot = engine.snapshot()
                paths = snapshot.get("paths") or {}
                base_path = paths.get("base_candidate_512")
                if not base_path or not os.path.isfile(base_path):
                    raise VisionDescriberError("base candidate image unavailable for vision description")
                with open(base_path, "rb") as f:
                    image_bytes = f.read()
                approve_description = _get_vision_describer().describe(image_bytes)
                description_source = "vision"
            except Exception as e:
                # Invariant: a describer failure must never fail approve-base —
                # always fall back to the generic description.
                log("⚠️ avatar vision describe unavailable/failed:", repr(e))
                approve_description = GENERIC_IDENTITY_DESCRIPTION
                description_source = "generic"
        try:
            job = engine.approve_base(approve_description, pass_clients)
        except AvatarCredentialConflict as e:
            self._send_json(409, {"ok": False, "error": "credential_conflict", "detail": str(e)})
            return
        except AvatarJobStateError as e:
            self._send_json(409, {"ok": False, "error": "wrong_stage", "detail": str(e)})
            return
        except Exception as e:
            log("❌ avatar approve base:", repr(e))
            self._send_json(500, {"ok": False, "error": "avatar approve base failed"})
            return
        self._send_json(200, {"ok": True, "job": job.snapshot(), "description_source": description_source})

    def _do_avatar_approve_set(self):
        if not self._require_write_auth():
            return
        engine = self._avatar_engine_or_503()
        if engine is None:
            return
        try:
            snapshot = engine.snapshot()
            if snapshot.get("stage") != "awaiting_set_approval":
                raise AvatarJobStateError(f"cannot approve set while stage is {snapshot.get('stage')}")
            final_pngs = (snapshot.get("paths") or {}).get("final_pngs")
            if not isinstance(final_pngs, dict):
                raise AvatarJobStateError("avatar job has no final PNGs")
            slot_bytes = {}
            for slot in GATEWAY_SLOTS:
                path = final_pngs.get(slot)
                if not path or not os.path.isfile(path):
                    raise AvatarJobStateError(f"avatar job missing final PNG for {slot}")
                with open(path, "rb") as f:
                    slot_bytes[slot] = f.read()
            job = engine.approve_set()
            try:
                version = self._apply_asset_slots(slot_bytes)
            except Exception as e:
                # approve_set() is the atomic job-state commit. If publishing fails here,
                # the job is done without live assets; generated PNGs remain in work_dir
                # for manual retry/ops recovery, and the stage is not rolled back.
                log("❌ avatar approve set asset publish:", repr(e))
                self._send_json(
                    500,
                    {
                        "ok": False,
                        "error": "avatar assets publish failed after approve",
                        "job_id": snapshot.get("job_id"),
                    },
                )
                return
        except AvatarJobStateError as e:
            self._send_json(409, {"ok": False, "error": "wrong_stage", "detail": str(e)})
            return
        except Exception as e:
            log("❌ avatar approve set:", repr(e))
            self._send_json(500, {"ok": False, "error": "avatar approve failed"})
            return
        self._send_json(200, {"ok": True, "job": job.snapshot(), "assets_version": version})

    def _safe_filler_name(self, name):
        name = (name or "").strip()
        if not name.endswith(".mp3"):
            name += ".mp3"
        # ".mp3" 単体（元が空文字）は隠しファイル化するので不可
        if name == ".mp3" or ".." in name or os.path.basename(name) != name:
            return None
        return name

    def _decode_filler_name(self, raw_name):
        return self._safe_filler_name(urllib.parse.unquote(raw_name))

    def _valid_mp3_bytes(self, data):
        return _valid_mp3_bytes(data)

    def _generated_filler_name(self):
        existing = set()
        if os.path.isdir(FILLER_DIR):
            existing = {name for name in os.listdir(FILLER_DIR) if name.endswith(".mp3")}
        while True:
            name = f"gen-{uuid.uuid4().hex[:8]}.mp3"
            if name not in existing:
                return name

    def _validate_filler_payload(self, payload):
        if not isinstance(payload, list):
            self._send_json(400, {"ok": False, "error": "json list required"})
            return None

        files = []
        for idx, item in enumerate(payload):
            if not isinstance(item, dict):
                self._send_json(400, {"ok": False, "error": "invalid filler item", "index": idx})
                return None
            unknown = set(item) - {"name", "data_base64", "text"}
            if unknown:
                self._send_json(400, {"ok": False, "error": "unknown filler field", "index": idx})
                return None
            name = self._safe_filler_name(item.get("name") or f"filler{idx + 1}.mp3")
            if not name:
                self._send_json(400, {"ok": False, "error": "invalid filler name", "index": idx})
                return None
            text = None
            if "text" in item and item.get("text") is not None:
                if not isinstance(item.get("text"), str):
                    self._send_json(400, {"ok": False, "error": "invalid filler text", "index": idx})
                    return None
                text = item["text"].strip()
                if len(text) > FILLER_TEXT_MAX:
                    self._send_json(400, {"ok": False, "error": "text too long", "index": idx})
                    return None
            try:
                data = base64.b64decode(item.get("data_base64", ""), validate=True)
            except Exception:
                self._send_json(400, {"ok": False, "error": "invalid filler data", "index": idx})
                return None
            if not data:
                self._send_json(400, {"ok": False, "error": "empty filler", "index": idx})
                return None
            # mp3 magic bytes（ID3 タグ or フレーム同期）。assets の画像検証と同水準
            if not self._valid_mp3_bytes(data):
                self._send_json(400, {"ok": False, "error": "invalid mp3", "index": idx})
                return None
            if _is_system_filler(name):
                self._send_json(403, {"ok": False, "error": "system filler", "index": idx})
                return None
            files.append((name, data, text))
        return files

    def _do_fillers_get(self):
        if not self._require_auth():
            return
        with FILLER_LOCK:
            # アクセスのたびに再確認・自己修復する（起動後に消えた/初回未作成のケース）。
            _ensure_filler_dir()
            # silence* はシステムファイル（無音つなぎ）。ユーザー管理対象ではないので
            # 一覧から隠す（削除・テキスト編集も 403 で保護）。
            fillers = [dict(item) for item in FILLER_METADATA if not _is_system_filler(item["name"])]
            version = resolved_config()["fillers_version"]
            status = FILLER_DIR_STATUS
        self._send_json(200, {
            "ok": True,
            "fillers": fillers,
            "fillers_version": version,
            "filler_dir_status": status,
        })

    def _require_filler_storage(self):
        if FILLER_DIR:
            return True
        self._send_json(
            409,
            {"ok": False, "error": "filler directory disabled"},
        )
        return False

    def _do_filler_file_get(self, raw_name):
        if not self._require_auth():
            return
        name = self._decode_filler_name(raw_name)
        if not name:
            self._send(404, b'{"ok":false,"error":"not found"}')
            return
        with FILLER_LOCK:
            if name not in {item["name"] for item in FILLER_METADATA}:
                self._send(404, b'{"ok":false,"error":"not found"}')
                return
            path = os.path.join(FILLER_DIR, name)
            try:
                data = _read_filler_mp3(
                    path,
                    reject_bundled=(os.environ.get("CATY_ID") or "caty") != "caty",
                )
            except (OSError, ValueError):
                self._send(404, b'{"ok":false,"error":"not found"}')
                return
        self._send(200, data, ctype="audio/mpeg")

    def _do_fillers_put(self):
        if not self._require_write_auth():
            return
        if not self._require_filler_storage():
            return
        payload = self._read_json_body(FILLERS_BODY_LIMIT)
        if payload is None:
            return
        files = self._validate_filler_payload(payload)
        if files is None:
            return

        parent = os.path.dirname(os.path.abspath(FILLER_DIR)) or "."
        os.makedirs(parent, exist_ok=True)
        staging = tempfile.mkdtemp(prefix=".staging-fillers-", dir=parent)
        try:
            texts = {name: text for name, _, text in files if text}
            for name, data, _ in files:
                with open(os.path.join(staging, name), "wb") as f:
                    f.write(data)
            with FILLER_LOCK:
                # 全置換でもシステムファイル（silence*）は温存する
                if os.path.isdir(FILLER_DIR):
                    for current in os.listdir(FILLER_DIR):
                        if current.endswith(".mp3") and _is_system_filler(current):
                            shutil.copy2(os.path.join(FILLER_DIR, current), os.path.join(staging, current))
                self._swap_directory(staging, FILLER_DIR)
                _save_filler_texts(texts)
                load_fillers()
                version = CONFIG.bump("fillers_version")
        except Exception as e:
            shutil.rmtree(staging, ignore_errors=True)
            log("❌ fillers put:", repr(e))
            self._send_json(500, {"ok": False, "error": "filler write failed"})
            return
        self._send_json(200, {"ok": True, "fillers_version": version})

    def _do_fillers_add(self):
        if not self._require_write_auth():
            return
        if not self._require_filler_storage():
            return
        payload = self._read_json_body(FILLERS_BODY_LIMIT)
        if payload is None:
            return
        files = self._validate_filler_payload(payload)
        if files is None:
            return
        if not files:
            self._send_json(200, {"ok": True, "fillers_version": resolved_config()["fillers_version"]})
            return

        parent = os.path.dirname(os.path.abspath(FILLER_DIR)) or "."
        os.makedirs(parent, exist_ok=True)
        staging = tempfile.mkdtemp(prefix=".staging-fillers-", dir=parent)
        try:
            with FILLER_LOCK:
                if os.path.isdir(FILLER_DIR):
                    for current in os.listdir(FILLER_DIR):
                        if current.endswith(".mp3"):
                            shutil.copy2(os.path.join(FILLER_DIR, current), os.path.join(staging, current))
                texts = _load_filler_texts()
                for name, data, text in files:
                    with open(os.path.join(staging, name), "wb") as f:
                        f.write(data)
                    if text is not None:
                        if text:
                            texts[name] = text
                        else:
                            texts.pop(name, None)
                self._swap_directory(staging, FILLER_DIR)
                _save_filler_texts(texts)
                load_fillers()
                version = CONFIG.bump("fillers_version")
        except Exception as e:
            shutil.rmtree(staging, ignore_errors=True)
            log("❌ fillers add:", repr(e))
            self._send_json(500, {"ok": False, "error": "filler write failed"})
            return
        self._send_json(200, {"ok": True, "fillers_version": version})

    def _do_fillers_generate(self):
        if not self._require_write_auth():
            return
        if not self._require_filler_storage():
            return
        payload = self._read_json_body(CONFIG_BODY_LIMIT)
        if payload is None:
            return
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            self._send_json(400, {"ok": False, "error": "invalid filler text"})
            return
        text = payload["text"].strip()
        if not text:
            self._send_json(400, {"ok": False, "error": "empty filler text"})
            return
        if len(text) > FILLER_TEXT_MAX:
            self._send_json(400, {"ok": False, "error": "text too long"})
            return
        explicit_name = "name" in payload and payload.get("name") is not None
        if explicit_name and not isinstance(payload.get("name"), str):
            self._send_json(400, {"ok": False, "error": "invalid filler name"})
            return
        name = self._safe_filler_name(payload.get("name")) if explicit_name else self._generated_filler_name()
        if not name:
            self._send_json(400, {"ok": False, "error": "invalid filler name"})
            return
        if _is_system_filler(name):
            self._send_json(403, {"ok": False, "error": "system filler"})
            return

        tts_path = None
        try:
            tts_path = tts(text)
            if not tts_path or not os.path.exists(tts_path) or os.path.getsize(tts_path) <= 0:
                raise RuntimeError("missing tts output")
            if os.path.getsize(tts_path) > FILLERS_BODY_LIMIT:
                raise RuntimeError("tts output too large")
            with open(tts_path, "rb") as f:
                data = f.read()
            if not self._valid_mp3_bytes(data):
                raise RuntimeError("invalid tts mp3")
        except Exception as e:
            log("❌ fillers generate tts:", repr(e))
            if tts_path:
                try:
                    os.remove(tts_path)
                except OSError:
                    pass
            self._send_json(502, {"ok": False, "error": "tts failed"})
            return

        parent = os.path.dirname(os.path.abspath(FILLER_DIR)) or "."
        os.makedirs(parent, exist_ok=True)
        staging = tempfile.mkdtemp(prefix=".staging-fillers-", dir=parent)
        try:
            with FILLER_LOCK:
                if os.path.isdir(FILLER_DIR):
                    for current in os.listdir(FILLER_DIR):
                        if current.endswith(".mp3"):
                            shutil.copy2(os.path.join(FILLER_DIR, current), os.path.join(staging, current))
                if not explicit_name:
                    while os.path.exists(os.path.join(staging, name)):
                        name = self._generated_filler_name()
                texts = _load_filler_texts()
                with open(os.path.join(staging, name), "wb") as f:
                    f.write(data)
                texts[name] = text
                self._swap_directory(staging, FILLER_DIR)
                _save_filler_texts(texts)
                load_fillers()
                version = CONFIG.bump("fillers_version")
                duration_sec = next((item["duration_sec"] for item in FILLER_METADATA if item["name"] == name), _mp3_duration(os.path.join(FILLER_DIR, name)))
        except Exception as e:
            shutil.rmtree(staging, ignore_errors=True)
            log("❌ fillers generate:", repr(e))
            self._send_json(500, {"ok": False, "error": "filler write failed"})
            return
        finally:
            try:
                if tts_path:
                    os.remove(tts_path)
            except OSError:
                pass
        self._send_json(200, {
            "ok": True,
            "name": name,
            "duration_sec": duration_sec,
            "fillers_version": version,
            "text": text,
        })

    def _do_filler_delete(self, raw_name):
        if not self._require_write_auth():
            return
        if not self._require_filler_storage():
            return
        name = self._decode_filler_name(raw_name)
        if not name:
            self._send(404, b'{"ok":false,"error":"not found"}')
            return
        if _is_system_filler(name):
            self._send_json(403, {"ok": False, "error": "system filler"})
            return
        parent = os.path.dirname(os.path.abspath(FILLER_DIR)) or "."
        os.makedirs(parent, exist_ok=True)
        staging = tempfile.mkdtemp(prefix=".staging-fillers-", dir=parent)
        not_found = False
        version = None
        try:
            with FILLER_LOCK:
                existing = {item["name"] for item in FILLER_METADATA}
                if name not in existing:
                    # 404 の _send はロック外で行う（クライアント滞留で全相槌操作が
                    # グローバルロック待ちになるのを防ぐ）
                    shutil.rmtree(staging, ignore_errors=True)
                    not_found = True
                else:
                    if os.path.isdir(FILLER_DIR):
                        for current in os.listdir(FILLER_DIR):
                            if current.endswith(".mp3") and current != name:
                                shutil.copy2(os.path.join(FILLER_DIR, current), os.path.join(staging, current))
                    texts = _load_filler_texts()
                    texts.pop(name, None)
                    self._swap_directory(staging, FILLER_DIR)
                    _save_filler_texts(texts)
                    load_fillers()
                    version = CONFIG.bump("fillers_version")
        except Exception as e:
            shutil.rmtree(staging, ignore_errors=True)
            log("❌ fillers delete:", repr(e))
            self._send_json(500, {"ok": False, "error": "filler delete failed"})
            return
        if not_found:
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        self._send_json(200, {"ok": True, "fillers_version": version})

    def _do_filler_text(self):
        if not self._require_write_auth():
            return
        if not self._require_filler_storage():
            return
        payload = self._read_json_body(CONFIG_BODY_LIMIT)
        if payload is None:
            return
        if not isinstance(payload, dict) or not isinstance(payload.get("name"), str) or not isinstance(payload.get("text"), str):
            self._send_json(400, {"ok": False, "error": "invalid filler text"})
            return
        name = self._safe_filler_name(payload["name"])
        if not name:
            self._send_json(400, {"ok": False, "error": "invalid filler name"})
            return
        text = payload["text"].strip()
        if len(text) > FILLER_TEXT_MAX:
            self._send_json(400, {"ok": False, "error": "text too long"})
            return
        if _is_system_filler(name):
            self._send_json(403, {"ok": False, "error": "system filler"})
            return
        not_found = False
        version = None
        response_text = None
        try:
            with FILLER_LOCK:
                existing = {item["name"] for item in FILLER_METADATA}
                if name not in existing:
                    # 404 の _send はロック外で行う（グローバルロックの停滞防止）
                    not_found = True
                else:
                    texts = _load_filler_texts()
                    if text:
                        texts[name] = text
                        response_text = text
                    else:
                        texts.pop(name, None)
                        response_text = None
                    _save_filler_texts(texts)
                    load_fillers()
                    version = CONFIG.bump("fillers_version")
        except Exception as e:
            log("❌ filler text:", repr(e))
            self._send_json(500, {"ok": False, "error": "filler text write failed"})
            return
        if not_found:
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        self._send_json(200, {"ok": True, "fillers_version": version, "text": response_text})

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/tts/voices":
            if not self._require_voice_auth("voice_catalog:read"):
                return
            self._do_voice_catalog(parsed.query)
        elif path == "/tts/voice-state":
            if not self._require_voice_auth("voice_state:read"):
                return
            self._do_voice_state()
        elif self.path == "/health":
            if require_auth_enabled() and not self._require_auth():
                return
            self._send(200, json.dumps({"ok": True, "agent": AGENT}).encode())
        elif path == "/config":
            self._do_config_get()
        elif path == "/events":
            if not self._require_auth():
                return
            self._do_events(parsed.query)
        elif external_sessions_enabled() and path == "/external/sessions":
            self._do_external_sessions(parsed.query)
        elif path == "/identity":
            if not self._require_auth():
                return
            self._send(200, json.dumps(identity_payload(), ensure_ascii=False).encode())
        elif path == "/history":
            if not self._require_auth():
                return
            self._send(200, json.dumps(history_store.list_sessions(), ensure_ascii=False).encode())
        elif path.startswith("/history/"):
            if not self._require_auth():
                return
            self._do_history_session(path[len("/history/"):], parsed.query)
        elif path.startswith("/asset/"):
            if not self._require_auth():
                return
            self._do_asset(path[len("/asset/"):])
        elif path == "/avatar/job":
            self._do_avatar_job_get()
        elif path.startswith("/avatar/job/files/"):
            self._do_avatar_job_file(path[len("/avatar/job/files/"):])
        elif path == "/fillers:texts":
            self._do_filler_texts_get()
        elif path == "/fillers":
            self._do_fillers_get()
        elif path.startswith("/fillers/"):
            self._do_filler_file_get(path[len("/fillers/"):])
        elif self.path.startswith("/stream/"):
            if not self._require_auth():
                return
            self._do_stream(self.path[len("/stream/"):])
        elif self.path.startswith("/reply/"):
            if not self._require_auth():
                return
            self._do_reply(self.path[len("/reply/"):])
        elif self.path == "/filler":
            if not self._require_auth():
                return
            try:
                service = _voice_activation_service
                if service is None:
                    # Avoid initializing managed-pack storage for untouched
                    # legacy installs. This is still a single config snapshot;
                    # managed validation receives this same object below.
                    filler_config = CONFIG.get()
                    managed = (
                        None
                        if filler_config.get("voice_management_state") == "legacy"
                        else _get_voice_activation_service().filler_audio(filler_config)
                    )
                else:
                    # filler_audio owns the one config snapshot used for both
                    # the legacy gate and managed pack binding validation.
                    managed = service.filler_audio()
            except Exception:
                managed = {"status": "unavailable", "audio": None}
            if managed is not None:
                audio = managed.get("audio")
                if audio:
                    self._send(200, audio, ctype="audio/mpeg")
                else:
                    self._send_json(404, {
                        "ok": False,
                        "error": "no matching fillers",
                        "filler_effective_status": managed.get("status", "unavailable"),
                    })
                return
            with FILLER_LOCK:
                choice = random.choice(FILLERS)[0] if FILLERS else None
            if choice:
                self._send(200, choice, ctype="audio/mpeg")
            else:
                self._send(404, b'{"ok":false,"error":"no fillers"}')
        else:
            self._send(404, b'{"ok":false}')

    def _query_int(self, query, key):
        vals = urllib.parse.parse_qs(query).get(key)
        if not vals:
            return None
        try:
            return int(vals[0])
        except (TypeError, ValueError):
            return None

    def _do_history_session(self, raw_session_id, query):
        decoded = urllib.parse.unquote(raw_session_id)
        session_id = sanitize_session_id(decoded)
        if not session_id or session_id != decoded.strip():
            self._send(404, b'{"ok":false,"error":"not found"}')
            return
        turns = history_store.read_session(
            session_id,
            since=self._query_int(query, "since"),
            limit=self._query_int(query, "limit"),
            before=self._query_int(query, "before"),
        )
        payload = {"session_id": session_id, "turns": turns}
        self._send(200, json.dumps(payload, ensure_ascii=False).encode())

    def _do_history_archive(self, raw_session_id):
        if not self._require_auth():
            return
        token = os.environ.get("CATY_TOKEN", "")
        if not token.strip():
            self._send(403, b'{"ok":false,"error":"token required for archive"}')
            return
        decoded = urllib.parse.unquote(raw_session_id)
        session_id = sanitize_session_id(decoded)
        if not session_id or session_id != decoded.strip():
            self._send(404, b'{"ok":false,"error":"invalid session id"}')
            return
        ok = history_store.archive_session(session_id)
        if ok:
            self._send(200, json.dumps({"ok": True, "id": session_id}, ensure_ascii=False).encode())
        else:
            self._send(404, b'{"ok":false,"error":"not found"}')

    def _do_asset(self, raw_name):
        name = urllib.parse.unquote(raw_name)
        if not name or ".." in name or os.path.basename(name) != name:
            self._send(404, b'{"ok":false,"error":"not found"}')
            return
        root = os.path.realpath(ASSET_DIR)
        path = os.path.realpath(os.path.join(root, name))
        if os.path.commonpath([root, path]) != root or not os.path.isfile(path):
            self._send(404, b'{"ok":false,"error":"not found"}')
            return
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            self._send(200, f.read(), ctype=ctype)

    def _do_reply(self, job_id):
        """ポーリング方式: 完成したら200+mp3、まだなら202。"""
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if job is None:
            self._send(404, b'{"ok":false,"error":"unknown id"}')
            return
        with job.cond:
            done, error = job.done, job.error
            chunks = list(job.chunks)
            partial_reply = job.reply
            partial_reply_enabled = job.partial_reply_enabled
        if error:
            self._send(500, json.dumps({"ok": False, "error": error}).encode())
        elif not done:
            body = presence_state.thinking_body(job)
            if (job.stream_enabled and partial_reply_enabled
                    and partial_reply and chunks):
                payload = json.loads(body)
                payload["partial_reply"] = partial_reply
                body = json.dumps(
                    payload, ensure_ascii=False, separators=(",", ":")
                ).encode()
            self._send(202, body)
        else:
            audio = b"".join(chunks)
            extra = {"X-Reply": job.reply,
                     "X-Reply-Enc": urllib.parse.quote(job.reply, safe="")}
            if job.degraded:
                extra["X-Degraded"] = job.degraded
            self._send(200, audio, ctype="audio/mpeg",
                       extra=extra)

    def _do_stream(self, job_id):
        """返事音声をchunkで流す（HTTP/1.0・接続クローズ区切り＝ネットラジオ方式）。"""
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if job is None:
            self._send(404, b'{"ok":false,"error":"unknown id"}')
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        sent = 0
        deadline = time.time() + 300
        try:
            while True:
                with job.cond:
                    while len(job.chunks) <= sent and not job.done:
                        if time.time() > deadline:
                            return
                        job.cond.wait(timeout=5)
                    pending = job.chunks[sent:]
                    done = job.done
                for chunk in pending:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                sent += len(pending)
                if done and len(job.chunks) <= sent:
                    return
        except (BrokenPipeError, ConnectionResetError):
            # AVPlayerはprobe目的で接続を即切りすることがある。jobは消さず再接続に備える
            log("⚠️ stream切断（クライアント側・再接続待ち）")
        # jobの削除はTTL（_purge_jobs）に任せる。AVPlayerの2回目のGETでも頭から再生できるように残す

    def _pairing_error(self, status, code, pid=None, log_event=True):
        if log_event:
            detail = f" pid={pid}" if pid else ""
            log(f"stage=pairing status={code}{detail}")
        self._send_json(status, {"ok": False, "error": code})

    def _pairing_peer(self):
        try:
            peer = self.client_address[0]
        except (TypeError, IndexError):
            return "<missing>", None, "missing"
        if peer in (None, ""):
            return "<missing>", None, "missing"
        peer = str(peer)
        try:
            address = ipaddress.ip_address(peer)
        except (ValueError, TypeError):
            return peer, None, "unparseable"
        if address.version == 6 and address.ipv4_mapped is not None:
            return peer, address.ipv4_mapped, "v4"
        return peer, address, f"v{address.version}"

    def _pairing_claim_source_allowed(self, config):
        peer, address, peer_kind = self._pairing_peer()
        if address is None:
            return False, peer, False, peer_kind
        # A dual-stack listener reports an IPv4 peer as ::ffff:a.b.c.d; that is the
        # same address §8-1 names, so compare on the unwrapped form.
        if config.allow_nontailnet:
            return True, peer, address.is_loopback, peer_kind
        # A7 measured production binding on 0.0.0.0 with reachable LAN peers,
        # so this gate is load-bearing.  Tailscale serve/Funnel arrives as raw
        # peer 127.0.0.1; never consult attacker-controlled X-Forwarded-For.
        if address.is_loopback:
            return True, peer, True, peer_kind
        tailnet = ipaddress.ip_network("100.64.0.0/10")
        if address.version == 4 and address in tailnet:
            return True, peer, False, peer_kind
        if address.version == 6:
            # §8-1 adds the Tailscale ULA only to /pair/claim.  Keep URL-delivery
            # fetch gating narrower so the A2 claim allowlist does not silently
            # widen unrelated one-shot QR surfaces.
            tailnet_ula = ipaddress.ip_network("fd7a:115c:a1e0::/48")
            if address in tailnet_ula:
                return True, peer, False, peer_kind
        return False, peer, False, peer_kind

    def _pairing_device_labels(self, payload, pid):
        device = payload.get("device")
        if device is None:
            return {}
        if not isinstance(device, dict):
            log(f"WARN stage=pairing pid={pid} invalid_device ignored")
            return {}
        labels = {}
        for field in ("name", "platform"):
            value = device.get(field)
            if value is None:
                continue
            if not isinstance(value, str) or len(value) > 64:
                log(f"WARN stage=pairing pid={pid} invalid_device_{field} ignored")
                continue
            labels[field] = re.sub(r"[^A-Za-z0-9 ._-]", "", value)
        return labels

    def _do_pair_claim(self):
        try:
            config = _get_pairing_config()
        except ValueError:
            self._pairing_error(503, "pairing_disabled")
            return
        allowed, peer, is_loopback, peer_kind = self._pairing_claim_source_allowed(
            config
        )
        # §8-2 counts every hit to /pair/claim, including the ones the 503 gates
        # below reject, so an off-tailnet flood cannot be replayed for free.  The
        # limiter only touches process-local counters, so §7-5 step 0 still holds:
        # nothing reads the body, the store, or a secret before those gates.
        try:
            permitted = _pair_claim_rate_limiter.allow(
                peer or "<unparsed>", config.rate_per_minute
            )
        except Exception:
            permitted = False
        def reject(status, code, *, source_gate=False):
            # Gate rejections are logged at most once per peer per window; without
            # this an unauthenticated flood writes one journal line per request.
            try:
                notify = _pair_claim_rate_limiter.note_rejection(peer or "<unparsed>")
            except Exception:
                notify = True
            if source_gate and notify:
                # §8-1 requires a WARN for out-of-range claims, while the existing
                # per-peer suppression remains in place to avoid log floods.
                log(
                    "WARN stage=pairing status="
                    f"{code} rejected_source_outside_allowlist peer={peer} "
                    f"peer_kind={peer_kind}"
                )
            self._pairing_error(
                status,
                code,
                log_event=notify and not source_gate,
            )

        if not allowed:
            reject(503, "pairing_disabled", source_gate=True)
            return
        if not _pairing_token_configured():
            reject(503, "pairing_disabled")
            return
        if not permitted:
            reject(429, "rate_limited")
            return

        raw = self._read_body_limited(4096, "pairing_malformed")
        if raw is None:
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            self._pairing_error(400, "pairing_malformed")
            return
        pair = payload.get("pair") if isinstance(payload, dict) else None
        if not isinstance(pair, str) or not pairing_store.PAIR_RE.fullmatch(pair):
            self._pairing_error(400, "pairing_malformed")
            return
        pid, _, _ = pair.partition(".")
        labels = self._pairing_device_labels(payload, pid)
        try:
            credential = _get_pairing_store().claim(
                pair,
                credential_getter=lambda: CATY_TOKEN,
                enforce_cumulative_revoke=not is_loopback,
            )
        except pairing_store.PairingConsumed:
            self._pairing_error(409, "pairing_consumed", pid)
            return
        except pairing_store.PairingExpired:
            self._pairing_error(410, "pairing_expired", pid)
            return
        except pairing_store.PairingRateLimited:
            self._pairing_error(429, "rate_limited", pid)
            return
        except pairing_store.PairingUnavailable:
            self._pairing_error(503, "pairing_disabled", pid)
            return
        except pairing_store.PairingInvalid:
            self._pairing_error(401, "pairing_invalid", pid)
            return
        except Exception as error:
            # §6-1 requires a coded response on every path.  Without this an
            # unexpected error closes the socket with no status line at all, and
            # the client cannot tell it apart from a network failure.
            log(f"WARN stage=pairing pid={pid} claim_failed {type(error).__name__}")
            self._pairing_error(503, "pairing_disabled", pid)
            return
        if not credential or not _pairing_token_configured():
            self._pairing_error(503, "pairing_disabled", pid)
            return
        device_summary = " ".join(
            f"device_{key}={value}" for key, value in sorted(labels.items()) if value
        )
        suffix = f" {device_summary}" if device_summary else ""
        log(f"stage=pairing status=claimed pid={pid}{suffix}")
        # The credential is already consumed here, so a failure while building the
        # response must still answer with a code (§6-1) rather than drop the
        # connection; §7-7 already defines at-most-once for the client.
        try:
            connection = _connection_payload()
            body = {
                "ok": True,
                "v": 1,
                "url": connection["url"],
                "token": credential,
                "id": connection["id"],
            }
        except Exception as error:
            log(f"WARN stage=pairing pid={pid} response_failed {type(error).__name__}")
            self._pairing_error(503, "pairing_disabled", pid)
            return
        self._send_json(200, body)

    def _do_pair_new(self):
        if not self._require_write_auth():
            return
        if not _pairing_token_configured():
            self._pairing_error(503, "pairing_disabled")
            return
        try:
            issued = _get_pairing_store().issue()
        except pairing_store.PairingStoreError:
            self._pairing_error(503, "pairing_disabled")
            return
        except Exception as error:
            log(f"WARN stage=pairing issue_failed {type(error).__name__}")
            self._pairing_error(503, "pairing_disabled")
            return
        try:
            body = {
                "ok": True,
                **_connection_payload(issued["pair"]),
                "expires_at": issued["expires_at"],
            }
        except Exception as error:
            log(f"WARN stage=pairing issue_response_failed {type(error).__name__}")
            self._pairing_error(503, "pairing_disabled")
            return
        log(f"stage=pairing status=issued pid={issued['pid']}")
        self._send_json(200, body)

    def _do_pair_revoke(self):
        if not self._require_write_auth():
            return
        try:
            _get_pairing_store().revoke()
        except pairing_store.PairingStoreError:
            self._pairing_error(503, "pairing_disabled")
            return
        except Exception as error:
            log(f"WARN stage=pairing revoke_failed {type(error).__name__}")
            self._pairing_error(503, "pairing_disabled")
            return
        log("stage=pairing status=revoked")
        self._send_json(200, {"ok": True})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/tts/voices/preview":
            principal = self._require_voice_auth("voice_preview:create")
            if not principal:
                return
            self._do_voice_preview(principal)
            return
        if path == "/tts/voice-activations":
            principal = self._require_voice_auth("voice_activation:create")
            if not principal:
                return
            self._do_voice_activation()
            return
        if path == "/pair/claim":
            self._do_pair_claim()
            return
        if path == "/pair/new":
            self._do_pair_new()
            return
        if path == "/pair/revoke":
            self._do_pair_revoke()
            return
        if path == "/v1/chat/completions":
            self._do_openai_chat_completions()
            return
        if path == "/share":
            self._do_share()
            return
        if path == "/assets:batch":
            self._do_assets_batch()
            return
        if path == "/avatar:stylize":
            self._do_avatar_stylize()
            return
        if path == "/avatar/job:regenerate":
            self._do_avatar_regenerate()
            return
        if path == "/avatar/job:cancel":
            self._do_avatar_cancel()
            return
        if path == "/avatar/job:approve-base":
            self._do_avatar_approve_base()
            return
        if path == "/avatar/job:approve-set":
            self._do_avatar_approve_set()
            return
        if path == "/fillers:add":
            self._do_fillers_add()
            return
        if path == "/fillers:generate":
            self._do_fillers_generate()
            return
        if path == "/fillers:text":
            self._do_filler_text()
            return
        if path == "/fillers:regenerate":
            self._do_filler_regenerate()
            return
        if path == "/push":
            self._do_push()
            return
        if external_sessions_enabled() and path == "/external/takeover":
            self._do_external_takeover()
            return
        if path not in ("/talk", "/talk2", "/see"):
            if path.startswith("/history/") and path.endswith("/archive"):
                raw_id = path[len("/history/"):-len("/archive")]
                self._do_history_archive(raw_id)
                return
            self._send(404, b'{"ok":false}')
            return
        if not self._require_auth():
            return
        if path == "/talk2":
            self._do_talk2()
            return
        if path == "/see":
            self._do_see()
            return
        t0 = time.time()
        request_id = uuid.uuid4().hex[:12]
        session_id = self._session_id()
        if session_id:
            record_voice_session(session_id)
        raw = self._read_body_limited(AUDIO_BODY_LIMIT, "no audio")
        if raw is None:
            return
        src = _temp_path_with_bytes(raw, ".m4a")
        log(
            f"request_id={request_id} stage=receive status=ok "
            f"audio_bytes={len(raw)} route=talk"
        )
        try:
            wav = to_wav16k(src)
            text = stt(wav)
            log_conversation_content(request_id, "stt", text)
            if not text:
                self._send(204, b"", extra={"X-Transcript": ""})
                return
            reply = brain(text, session_id)
            if reply == "":
                self._send(204, b"", extra={"X-Transcript": text})
                return
            log_conversation_content(request_id, "reply", reply)
            mp3 = tts(reply)
            with open(mp3, "rb") as f:
                audio = f.read()
            log(
                f"request_id={request_id} stage=reply_audio status=ok "
                f"audio_bytes={len(audio)} latency_s={time.time()-t0:.1f}"
            )
            self._send(200, audio, ctype="audio/mpeg",
                       extra={"X-Transcript": text, "X-Reply": reply})
        except subprocess.TimeoutExpired as e:
            log_failure(request_id, "talk", e, status="timeout")
            self._send(504, json.dumps({"ok": False, "error": "timeout"}).encode())
        except Exception as e:
            log_failure(request_id, "talk", e)
            self._send(500, json.dumps({"ok": False, "error": str(e)}).encode())
        finally:
            for p in (src,):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def do_PUT(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/config":
            self._do_config_put()
            return
        if path == "/fillers":
            self._do_fillers_put()
            return
        if path == "/fillers:texts":
            self._do_filler_texts_put()
            return
        self._send(404, b'{"ok":false}')

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/fillers:texts":
            self._do_filler_texts_delete()
            return
        if path.startswith("/fillers/"):
            self._do_filler_delete(path[len("/fillers/"):])
            return
        self._send(404, b'{"ok":false}')

    def _do_see(self):
        """音声+画面フレームのstreaming入口。STT transcriptはそのまま返す。"""
        t0 = time.time()
        request_id = uuid.uuid4().hex[:12]
        session_id = self._session_id()
        if session_id:
            record_voice_session(session_id)
        raw = self._read_body_limited(AUDIO_BODY_LIMIT, "no multipart body")
        if raw is None:
            return
        src = None
        wav = None
        claimed_path = None
        cleanup_handed_off = False
        pending_delivery_paths = set()
        job = None
        worker_started = False
        try:
            parts = parse_multipart_form(self.headers, raw)
            audio_bytes = parts.get("audio") or b""
            image_bytes = parts.get("image") or b""
            if not audio_bytes:
                self._send(400, json.dumps({"ok": False, "error": "no audio"}).encode())
                return
            if not image_bytes:
                self._send(400, json.dumps({"ok": False, "error": "no image"}).encode())
                return

            src = _temp_path_with_bytes(audio_bytes, ".m4a")
            log(
                f"request_id={request_id} stage=receive status=ok route=see "
                f"audio_bytes={len(audio_bytes)} image_bytes={len(image_bytes)}"
            )

            wav = to_wav16k(src)
            text = stt(wav)
            log_conversation_content(request_id, "stt", text)
            if not text:
                self._send(204, b"")
                return

            sniffed_mime = share_store.sniff_attachment_mime(image_bytes)
            filename = (
                "screen.png"
                if sniffed_mime == share_store.ATTACHMENT_MIME_PNG
                else "screen.jpg"
            )
            taken_frame = _get_share_store().stage_claimed_bytes(
                image_bytes, filename, "image"
            )
            if isinstance(taken_frame, share_store.ClaimedFile):
                claimed_path = taken_frame.path
                plan = _prepare_binary_attachment(
                    taken_frame, text, request_id, source="screen"
                )
                pending_delivery_paths = {
                    attachment.path
                    for transport in ("generate", "stream")
                    for attachment in (
                        plan[transport].attachments
                        if isinstance(plan[transport], Delivery)
                        else ()
                    )
                    if attachment.path != claimed_path
                }
            else:
                plan = _rejected_attachment_plan(
                    taken_frame, text, source="screen"
                )
                log(
                    f"request_id={request_id} stage=attachment "
                    f"kind={taken_frame.declared_kind} "
                    "mime=application/octet-stream "
                    f"size={taken_frame.size} "
                    f"generate_reason={taken_frame.reason} "
                    f"stream_reason={taken_frame.reason}"
                )

            _purge_jobs()
            job = Job(text, session_id)
            job_id = request_id
            job.request_id = request_id
            job.binary_attachment_present = claimed_path is not None
            presence_state.set_job_id(job, job_id)
            if claimed_path is not None:
                job.add_cleanup(
                    lambda: _unlink_attachment_path(claimed_path)
                )
                cleanup_handed_off = True
            for path in pending_delivery_paths:
                job.add_cleanup(
                    lambda cleanup_path=path: _unlink_attachment_path(
                        cleanup_path
                    )
                )
            pending_delivery_paths.clear()
            with JOBS_LOCK:
                JOBS[job_id] = job
            worker = threading.Thread(
                target=stream_pipeline,
                args=(job, text, t0),
                kwargs={"plan": plan},
                daemon=True,
            )
            try:
                worker.start()
                worker_started = True
            except Exception as error:
                job.finish(error=str(error))
                with JOBS_LOCK:
                    JOBS.pop(job_id, None)
                log_failure(request_id, "see_worker_start", error)
                self._send(
                    500,
                    json.dumps({"ok": False, "error": str(error)}).encode(),
                )
                return
            self._send(200, json.dumps({"id": job_id, "transcript": text}, ensure_ascii=False).encode(),
                       extra={"X-Transcript": text})
        except subprocess.TimeoutExpired as e:
            if worker_started:
                log_failure(request_id, "respond", e)
                return
            log_failure(request_id, "see", e, status="timeout")
            self._send(504, json.dumps({"ok": False, "error": "timeout"}).encode())
        except Exception as e:
            if job is not None and not job.done and not worker_started:
                job.finish(error=str(e))
            if worker_started:
                log_failure(request_id, "respond", e)
                return
            log_failure(request_id, "see", e)
            self._send(500, json.dumps({"ok": False, "error": str(e)}).encode())
        finally:
            if claimed_path is not None and not cleanup_handed_off:
                _unlink_attachment_path(claimed_path)
            for path in pending_delivery_paths:
                _unlink_attachment_path(path)
            for p in (src, wav):
                if not p:
                    continue
                try:
                    os.remove(p)
                except OSError:
                    pass

    def _do_talk2(self):
        """streaming方式の入口。STTまで同期→残りはバックグラウンドで/stream/<id>へ。"""
        t0 = time.time()
        request_id = uuid.uuid4().hex[:12]
        session_id = self._session_id()
        if session_id:
            record_voice_session(session_id)
        # 端末STT(オンデバイス文字化)経路 (#74): 空ボディ + X-Caty-Text(percent-encoded)。
        # この版の CatyPhone は PTT/live とも端末STTで文字を送るため、テキスト経路対応が必須。
        text_hdr = self.headers.get("X-Caty-Text", "")
        share_id_hdr = self.headers.get("X-Caty-Share-Id")
        share_id = share_id_hdr.strip() if share_id_hdr is not None else ""
        if share_id_hdr is not None and not share_store.SHARE_ID_RE.fullmatch(share_id):
            self._send_json(404, {"ok": False, "error": "share not found"})
            return
        if text_hdr or share_id_hdr is not None:
            from urllib.parse import unquote
            text = unquote(text_hdr).strip()
            try:
                blen = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send(400, json.dumps({"ok": False, "error": "invalid content length"}).encode())
                return
            if blen > AUDIO_BODY_LIMIT:
                self._send(413, json.dumps({"ok": False, "error": "payload too large"}).encode())
                return
            if blen > 0:
                try:
                    self.rfile.read(blen)
                except Exception:
                    pass
            taken_share = None
            if share_id_hdr is not None:
                if not session_id:
                    self._send_json(
                        409, {"ok": False, "error": "share session mismatch"}
                    )
                    return
                try:
                    taken_share = _get_share_store().take(
                        share_id, session_id
                    )
                except ValueError:
                    self._send_json(
                        409, {"ok": False, "error": "share session mismatch"}
                    )
                    return
                except (share_store.InvalidShareId, share_store.ShareNotFound):
                    self._send_json(
                        404, {"ok": False, "error": "share not found"}
                    )
                    return
                except share_store.SessionMismatch:
                    self._send_json(
                        409, {"ok": False, "error": "share session mismatch"}
                    )
                    return
                except share_store.ShareExpired:
                    self._send_json(
                        410, {"ok": False, "error": "share expired"}
                    )
                    return
                except Exception as error:
                    log(
                        f"request_id={request_id} stage=share status=failed "
                        f"share_id={share_id} error_type={type(error).__name__}"
                    )
                    self._send_json(
                        500, {"ok": False, "error": "share consume failed"}
                    )
                    return
            if not text and taken_share is None:
                self._send(204, b"")
                return
            claimed_path = None
            cleanup_handed_off = False
            job = None
            worker_started = False

            def run_share_pipeline():
                try:
                    brain_text, plan = (
                        _compose_share_turn(taken_share, text, request_id)
                        if taken_share is not None
                        else (text, None)
                    )
                    if plan is not None:
                        paths = {
                            attachment.path
                            for transport in ("generate", "stream")
                            for attachment in (
                                plan[transport].attachments
                                if isinstance(plan[transport], Delivery)
                                else ()
                            )
                            if attachment.path != claimed_path
                        }
                        registered_paths = set()
                        try:
                            for path in paths:
                                job.add_cleanup(
                                    lambda cleanup_path=path: _unlink_attachment_path(
                                        cleanup_path
                                    )
                                )
                                registered_paths.add(path)
                        except Exception:
                            for path in paths - registered_paths:
                                _unlink_attachment_path(path)
                            raise
                except Exception as error:
                    log_failure(job.request_id, "stream", error)
                    job.finish(error=str(error))
                    return
                stream_pipeline(job, brain_text, t0, job.route, plan=plan)

            try:
                claimed_path = (
                    taken_share.path
                    if isinstance(taken_share, share_store.ClaimedFile)
                    else None
                )
                if taken_share is not None:
                    log(
                        f"request_id={request_id} stage=share status=consumed "
                        f"share_id={share_id} "
                        f"kind={getattr(taken_share, 'declared_kind', 'file')} "
                        f"bytes={taken_share.size}"
                    )
                log_conversation_content(request_id, "device_stt", text)
                route = self.headers.get("X-Caty-Route", "").strip().lower()
                is_extended = route in ("ptt", "live")
                _purge_jobs()
                job = Job(text, session_id)
                job.request_id = request_id
                job.binary_attachment_present = claimed_path is not None
                if is_extended:
                    job.route = route
                    job.ttl   = CATY_PTT_JOB_TTL
                job_id = request_id
                presence_state.set_job_id(job, job_id)
                if claimed_path is not None:
                    job.add_cleanup(
                        lambda: _unlink_attachment_path(claimed_path)
                    )
                    cleanup_handed_off = True
                with JOBS_LOCK:
                    JOBS[job_id] = job
                worker = threading.Thread(
                    target=run_share_pipeline,
                    daemon=True,
                )
                worker.start()
                worker_started = True
                self._send(200, json.dumps({"id": job_id, "transcript": text}, ensure_ascii=False).encode(),
                           extra={"X-Transcript": text})
            except Exception as error:
                if not worker_started:
                    if claimed_path is not None and not cleanup_handed_off:
                        _unlink_attachment_path(claimed_path)
                    if job is not None:
                        job.finish(error=str(error))
                    with JOBS_LOCK:
                        JOBS.pop(request_id, None)
                    log_failure(request_id, "share_worker_start", error)
                    self._send_json(
                        500, {"ok": False, "error": "share worker start failed"}
                    )
                else:
                    log_failure(request_id, "respond", error)
                return
            return
        raw = self._read_body_limited(AUDIO_BODY_LIMIT, "no audio")
        if raw is None:
            return
        src = _temp_path_with_bytes(raw, ".m4a")
        log(
            f"request_id={request_id} stage=receive status=ok "
            f"audio_bytes={len(raw)} route=talk2"
        )
        try:
            wav = to_wav16k(src)
            stt_t0 = time.time()
            text = stt(wav)
            stt_s = time.time() - stt_t0
            log_conversation_content(request_id, "stt", text)
            if not text:
                self._send(204, b"")
                return
            route = self.headers.get("X-Caty-Route", "").strip().lower()
            is_extended = route in ("ptt", "live")
            _purge_jobs()
            job = Job(text, session_id)
            job.request_id = request_id
            job.stt_s = stt_s
            if is_extended:
                job.route = route
                job.ttl   = CATY_PTT_JOB_TTL
            job_id = request_id
            presence_state.set_job_id(job, job_id)
            with JOBS_LOCK:
                JOBS[job_id] = job
            threading.Thread(target=stream_pipeline, args=(job, text, t0, job.route), daemon=True).start()
            self._send(200, json.dumps({"id": job_id, "transcript": text}, ensure_ascii=False).encode(),
                       extra={"X-Transcript": text})
        except subprocess.TimeoutExpired as e:
            log_failure(request_id, "talk2", e, status="timeout")
            self._send(504, json.dumps({"ok": False, "error": "timeout"}).encode())
        except Exception as e:
            log_failure(request_id, "talk2", e)
            self._send(500, json.dumps({"ok": False, "error": str(e)}).encode())
        finally:
            try:
                os.remove(src)
            except OSError:
                pass


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print("usage: caty-gateway [--help] [qr [--qr-delivery MODE] [--wait-visible-seconds SECONDS]]")
        print("Run the gateway in the foreground, or create a pairing QR code.")
        return
    qr_args = None
    if len(sys.argv) > 1 and sys.argv[1] == "qr":
        qr_args = _qr_cli_args(sys.argv[2:])
        try:
            url_delivery = _qr_delivery_mode(qr_args.qr_delivery) == "url"
        except ValueError:
            # Let the single print_qr() dispatch below emit the actionable error.
            url_delivery = True
        if url_delivery:
            result = print_qr(
                delivery=qr_args.qr_delivery,
                wait_visible_seconds=qr_args.wait_visible_seconds,
            )
            raise SystemExit(0 if result else 1)
    try:
        pairing_config = _get_pairing_config()
    except ValueError as error:
        print(f"pairing configuration invalid: {error}", file=sys.stderr)
        raise SystemExit(2)
    if pairing_config.allow_nontailnet:
        log(
            "WARN pairing source-address gate disabled by "
            "CATY_PAIRING_ALLOW_NONTAILNET=1"
        )
    if CATY_TOKEN and not _pairing_token_configured():
        log("WARN pairing disabled because CATY_TOKEN is whitespace only")
    elif not CATY_TOKEN:
        if not CATY_ADMIN_TOKEN and not require_auth_enabled():
            log(
                "WARN gateway is unauthenticated; pairing disabled because "
                "CATY_TOKEN is required"
            )
        else:
            log("WARN pairing disabled because a non-empty CATY_TOKEN is required")
    if qr_args is not None:
        # S3 の無人オーケストレーションが失敗を成功と誤認しないよう exit code で伝える
        result = print_qr(
            delivery=qr_args.qr_delivery,
            wait_visible_seconds=qr_args.wait_visible_seconds,
        )
        raise SystemExit(0 if result else 1)
    report_content_logging_mode()
    load_fillers()
    # Claimed files belong to process-local Jobs. Before opening the listening
    # socket, remove every regular orphan left by an earlier process.
    share_store.cleanup_claimed_orphans(share_store.default_share_root())
    try:
        _get_pairing_store().start_sweeper()
    except pairing_store.PairingStoreError as error:
        # 原則②: an unusable pairing store must not take voice/TTS/talk down with
        # it.  §6-2 already routes store unavailability to 503 pairing_disabled,
        # which the handlers do on their own once this stays non-fatal.
        log(f"WARN pairing store unavailable, pairing disabled: {error}")
    srv = ThreadingHTTPServer((BIND_HOST, PORT), Handler)
    print("=" * 56)
    print("  Caty gateway")
    print(f"  agent = {AGENT}   STT言語 = {LANG}")
    connection_url = _redact_log_text(_connection_payload()["url"])
    print(f"  ▶ 時計アプリに設定するURL:  {connection_url}")
    print(f"  ▶ 動作確認:  curl {connection_url}/health")
    print("  停止: Ctrl+C")
    print("=" * 56, flush=True)
    try:
        _get_neutral_voice_readiness().start()
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n停止しました")


if __name__ == "__main__":
    main()
