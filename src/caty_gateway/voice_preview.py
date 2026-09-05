"""Fixed-script voice previews with scoped caching and cost guards."""

import collections
import hashlib
import math
import os
import subprocess
import tempfile
import threading
import time

from caty_gateway import voice_catalog


SCRIPT_ID = "voice-picker-ja-v1"
SCRIPT_TEXT = "こんにちは。今日はどんな一日でしたか？よかったら、少しお話ししましょう。"


class PreviewError(Exception):
    def __init__(self, code, status=400, retry_after=None, retryable=None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.retry_after = retry_after
        self.retryable = (
            status in {429, 502, 503, 504} if retryable is None else bool(retryable)
        )


class PreviewRateLimited(PreviewError):
    def __init__(self, retry_after):
        super().__init__("preview_rate_limited", 429, retry_after)


def _env_number(name, default, minimum, maximum, integer=False):
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    value = max(minimum, min(maximum, value))
    return int(value) if integer else value


def request_principal(credential):
    return hashlib.sha256((credential or "anonymous").encode("utf-8")).hexdigest()


def offline_enabled():
    return os.environ.get("CATY_OFFLINE", "").strip().lower() in {"1", "true", "yes", "on"}


def probe_mp3_duration(data):
    """Return duration using ffprobe, or None when probing is unavailable."""
    ffmpeg = os.environ.get("FFMPEG_BIN", "ffmpeg")
    ffprobe = os.environ.get("FFPROBE_BIN", "") or (
        ffmpeg[:-6] + "ffprobe" if ffmpeg.endswith("ffmpeg") else "ffprobe"
    )
    descriptor, path = tempfile.mkstemp(prefix=".voice-preview-", suffix=".mp3")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        completed = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
            text=True,
        )
        if completed.returncode != 0:
            return None
        duration = float(completed.stdout.strip())
        if not math.isfinite(duration) or duration <= 0:
            return None
        return duration
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


class _CacheEntry:
    def __init__(self, audio, duration, created_at, expires_at):
        self.audio = audio
        self.duration = duration
        self.created_at = created_at
        self.expires_at = expires_at


class _Flight:
    def __init__(self):
        self.event = threading.Event()
        self.error = None


class _NegativeCacheEntry:
    def __init__(self, error, expires_at):
        self.code = error.code
        self.status = error.status
        self.retry_after = error.retry_after
        self.retryable = error.retryable
        self.expires_at = expires_at


class _RateLimiter:
    def __init__(self, limit, window=60.0, clock=None, max_principals=1024):
        self._limit = max(1, int(limit))
        self._window = float(window)
        self._clock = clock or time.time
        self._max_principals = max(1, int(max_principals))
        self._lock = threading.Lock()
        self._events = collections.OrderedDict()

    def check(self, principal):
        now = self._clock()
        cutoff = now - self._window
        with self._lock:
            # Prune every principal, not only the current one, so one-shot
            # scoped credentials cannot leave permanent process state.
            for key in list(self._events):
                recorded = self._events[key]
                while recorded and recorded[0] <= cutoff:
                    recorded.popleft()
                if not recorded:
                    self._events.pop(key, None)
            events = self._events.get(principal)
            if events is None:
                while len(self._events) >= self._max_principals:
                    self._events.popitem(last=False)
                events = collections.deque()
                self._events[principal] = events
            else:
                self._events.move_to_end(principal)
            if len(events) >= self._limit:
                return max(1, int(math.ceil(events[0] + self._window - now)))
            events.append(now)
        return None


class VoicePreviewService:
    def __init__(
        self,
        catalog,
        synthesizer,
        inference_contract_version,
        installation_id,
        duration_probe=None,
        clock=None,
        cache_ttl=None,
        rate_per_minute=None,
        max_rate_principals=None,
        max_audio_size=None,
        max_duration=None,
        max_cache_entries=None,
        max_cache_bytes=None,
        negative_cache_ttl=None,
        single_flight_timeout=None,
    ):
        self._catalog = catalog
        self._synthesizer = synthesizer
        self._contract_version = inference_contract_version
        self._installation_id = installation_id
        self._duration_probe = duration_probe or probe_mp3_duration
        self._clock = clock or time.time
        self._cache_ttl = cache_ttl if cache_ttl is not None else _env_number(
            "CATY_VOICE_PREVIEW_TTL_SECONDS", 604800, 60, 2592000
        )
        rate = rate_per_minute if rate_per_minute is not None else _env_number(
            "CATY_VOICE_PREVIEW_RATE_PER_MINUTE", 10, 1, 600, integer=True
        )
        rate_principals = max_rate_principals if max_rate_principals is not None else _env_number(
            "CATY_VOICE_PREVIEW_RATE_PRINCIPALS", 1024, 16, 10000, integer=True
        )
        self._max_audio_size = max_audio_size if max_audio_size is not None else _env_number(
            "CATY_VOICE_PREVIEW_MAX_BYTES", 2 * 1024 * 1024, 1024, 20 * 1024 * 1024, integer=True
        )
        self._max_duration = max_duration if max_duration is not None else _env_number(
            "CATY_VOICE_PREVIEW_MAX_DURATION_SECONDS", 15, 1, 60
        )
        self._max_cache_entries = max_cache_entries if max_cache_entries is not None else _env_number(
            "CATY_VOICE_PREVIEW_CACHE_ENTRIES", 128, 1, 1000, integer=True
        )
        self._max_cache_bytes = max_cache_bytes if max_cache_bytes is not None else _env_number(
            "CATY_VOICE_PREVIEW_CACHE_BYTES", 32 * 1024 * 1024, 1024, 512 * 1024 * 1024,
            integer=True,
        )
        # Audio that cannot fit in the cache must fail at the existing bounded
        # read guard, so a successful paid response is always reusable.
        self._max_audio_size = min(self._max_audio_size, self._max_cache_bytes)
        self._negative_cache_ttl = (
            negative_cache_ttl if negative_cache_ttl is not None else _env_number(
                "CATY_VOICE_PREVIEW_NEGATIVE_TTL_SECONDS", 300, 5, 3600
            )
        )
        self._flight_timeout = single_flight_timeout if single_flight_timeout is not None else _env_number(
            "CATY_VOICE_PREVIEW_SINGLE_FLIGHT_TIMEOUT_SECONDS", 180, 5, 600
        )
        self._rate_limiter = _RateLimiter(
            rate, clock=self._clock, max_principals=rate_principals
        )
        self._lock = threading.Lock()
        self._cache = collections.OrderedDict()
        self._cache_bytes = 0
        self._negative_cache = collections.OrderedDict()
        self._flights = {}

    @staticmethod
    def _key(resolved, contract_version):
        partition = "shared" if resolved["scope"] == "public" else resolved["cache_partition"]
        return (
            partition,
            resolved["provider"],
            resolved["reference_id"],
            resolved["source_version"],
            SCRIPT_ID,
            contract_version,
        )

    def _lookup(self, key):
        now = self._clock()
        with self._lock:
            entry = self._cache.get(key)
            if entry is not None and entry.expires_at > now:
                self._cache.move_to_end(key)
                return entry
            if entry is not None:
                self._remove_cache_locked(key)
        return None

    def _remove_cache_locked(self, key):
        entry = self._cache.pop(key, None)
        if entry is not None:
            self._cache_bytes -= len(entry.audio)
        return entry

    def _store_cache_locked(self, key, entry):
        self._remove_cache_locked(key)
        self._cache[key] = entry
        self._cache_bytes += len(entry.audio)
        self._cache.move_to_end(key)
        while (
            len(self._cache) > self._max_cache_entries
            or self._cache_bytes > self._max_cache_bytes
        ):
            oldest = next(iter(self._cache))
            self._remove_cache_locked(oldest)

    def _negative_error(self, key):
        now = self._clock()
        with self._lock:
            entry = self._negative_cache.get(key)
            if entry is None:
                return None
            if entry.expires_at <= now:
                self._negative_cache.pop(key, None)
                return None
            self._negative_cache.move_to_end(key)
            return PreviewError(
                entry.code,
                entry.status,
                entry.retry_after,
                retryable=entry.retryable,
            )

    def _store_negative(self, key, error):
        if error.code not in {"preview_audio_too_large", "preview_audio_too_long"}:
            return
        with self._lock:
            self._negative_cache[key] = _NegativeCacheEntry(
                error, self._clock() + self._negative_cache_ttl
            )
            self._negative_cache.move_to_end(key)
            while len(self._negative_cache) > self._max_cache_entries:
                self._negative_cache.popitem(last=False)

    def _cached_for_hint(self, hint, contract_version):
        if hint is None:
            return None, None
        partition = (
            "shared" if hint["scope"] == "public"
            else voice_catalog.credential_partition(self._installation_id)
        )
        now = self._clock()
        with self._lock:
            candidates = []
            for key, entry in list(self._cache.items()):
                if entry.expires_at <= now:
                    self._remove_cache_locked(key)
                    continue
                if (
                    key[0] == partition
                    and key[1] == hint["provider"]
                    and key[2] == hint["reference_id"]
                    and key[4] == SCRIPT_ID
                    and key[5] == contract_version
                    and (not hint.get("source_version") or key[3] == hint["source_version"])
                ):
                    candidates.append((entry.created_at, key, entry))
            if not candidates:
                return None, None
            _, key, entry = max(candidates, key=lambda value: value[0])
            self._cache.move_to_end(key)
            return key, entry

    def _invalidate_reference(self, reference_id, include_private=True):
        with self._lock:
            for key in list(self._cache):
                if key[2] == reference_id and (include_private or key[0] == "shared"):
                    self._remove_cache_locked(key)
            for key in list(self._negative_cache):
                if key[2] == reference_id and (include_private or key[0] == "shared"):
                    self._negative_cache.pop(key, None)

    def _result(self, entry, contract_version, cache, stale=False):
        return {
            "audio": entry.audio,
            "content_type": "audio/mpeg",
            "script_id": SCRIPT_ID,
            "inference_contract_version": contract_version,
            "cache": cache,
            "stale": bool(stale),
            "duration_seconds": entry.duration,
        }

    def _safe_contract_version(self):
        try:
            version = self._contract_version()
        except Exception:
            raise PreviewError("preview_inference_unavailable", 503) from None
        if not isinstance(version, str) or not version or len(version) > 160:
            raise PreviewError("preview_inference_unavailable", 503)
        return version

    def _generate(self, reference_id):
        total = 0
        chunks = []
        try:
            stream = self._synthesizer(SCRIPT_TEXT, reference_id)
            for chunk in stream:
                if not isinstance(chunk, (bytes, bytearray)):
                    raise PreviewError("preview_generation_failed", 502)
                total += len(chunk)
                if total > self._max_audio_size:
                    close = getattr(stream, "close", None)
                    if callable(close):
                        close()
                    raise PreviewError("preview_audio_too_large", 502)
                chunks.append(bytes(chunk))
        except PreviewError:
            raise
        except Exception:
            raise PreviewError("preview_generation_failed", 502, retry_after=30) from None
        audio = b"".join(chunks)
        if not audio or not (audio.startswith(b"ID3") or audio.startswith(b"\xff")):
            raise PreviewError("preview_invalid_audio", 502)
        try:
            duration = self._duration_probe(audio)
        except Exception:
            duration = None
        if duration is not None and (
            not isinstance(duration, (int, float))
            or not math.isfinite(duration)
            or duration <= 0
        ):
            duration = None
        if duration is not None and duration > self._max_duration:
            raise PreviewError("preview_audio_too_long", 502)
        return audio, duration

    def preview(self, principal, catalog_id=None, reference_id=None, offline=None):
        if bool(catalog_id) == bool(reference_id):
            raise PreviewError("exactly_one_voice_identifier_required", 400)
        hint = None
        if catalog_id:
            try:
                hint = voice_catalog.parse_catalog_id(catalog_id)
            except voice_catalog.CatalogError as error:
                raise PreviewError(error.code, error.status) from None
        elif (
            isinstance(reference_id, str)
            and reference_id.strip()
            and len(reference_id.strip()) <= 160
        ):
            hint = {
                "provider": voice_catalog.PROVIDER,
                "scope": "self",
                "reference_id": reference_id.strip(),
                "source_version": None,
            }
        else:
            raise PreviewError("invalid_reference_id", 400)

        retry_after = self._rate_limiter.check(principal)
        if retry_after is not None:
            raise PreviewRateLimited(retry_after)
        contract_version = self._safe_contract_version()

        is_offline = offline_enabled() if offline is None else bool(offline)
        if is_offline:
            _, cached = self._cached_for_hint(hint, contract_version)
            if cached is None:
                raise PreviewError("preview_unavailable_offline", 503)
            return self._result(cached, contract_version, "hit", stale=True)

        try:
            resolved = self._catalog.resolve_preview(catalog_id=catalog_id, reference_id=reference_id)
        except voice_catalog.CatalogVoiceUnavailable as error:
            self._invalidate_reference(error.reference_id, include_private=True)
            raise PreviewError(error.code, error.status) from None
        except voice_catalog.CatalogUpstreamError as error:
            if error.allow_stale:
                _, cached = self._cached_for_hint(hint, contract_version)
                if cached is not None:
                    return self._result(cached, contract_version, "hit", stale=True)
            raise PreviewError(
                error.code, error.status, error.retry_after, retryable=error.retryable
            ) from None
        except voice_catalog.CatalogError as error:
            raise PreviewError(error.code, error.status, error.retry_after) from None

        key = self._key(resolved, contract_version)
        cached = self._lookup(key)
        if cached is not None:
            return self._result(cached, contract_version, "hit")
        negative_error = self._negative_error(key)
        if negative_error is not None:
            raise negative_error

        with self._lock:
            flight = self._flights.get(key)
            leader = flight is None
            if leader:
                flight = _Flight()
                self._flights[key] = flight
        if not leader:
            if not flight.event.wait(self._flight_timeout):
                raise PreviewError("preview_generation_busy", 503, retry_after=5)
            if flight.error is not None:
                raise PreviewError(
                    flight.error.code, flight.error.status, flight.error.retry_after
                )
            cached = self._lookup(key)
            if cached is None:
                raise PreviewError("preview_generation_failed", 502)
            return self._result(cached, contract_version, "coalesced")

        try:
            audio, duration = self._generate(resolved["reference_id"])
            now = self._clock()
            entry = _CacheEntry(audio, duration, now, now + self._cache_ttl)
            with self._lock:
                self._store_cache_locked(key, entry)
            return self._result(entry, contract_version, "miss")
        except PreviewError as error:
            self._store_negative(key, error)
            flight.error = error
            raise
        finally:
            with self._lock:
                self._flights.pop(key, None)
                flight.event.set()
