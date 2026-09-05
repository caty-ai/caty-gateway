"""Fish voice catalog normalization, filtering, pagination, and last-good cache.

This module deliberately owns the catalog policy.  ``caty_gateway`` only maps
HTTP inputs and outputs, while ``tts_fish`` remains a small HTTP transport.
"""

import base64
import collections
import hashlib
import hmac
import json
import math
import os
import secrets
import threading
import time
import urllib.parse

from caty_gateway import voice_presets


PROVIDER = "fish"
CATALOG_ID_PREFIX = "vc1."
VALID_SCOPES = frozenset(("recommended", "all", "self"))
VALID_AVAILABILITY = frozenset(("available", "hidden", "unavailable", "unknown"))
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50
DEFAULT_LANGUAGE = "ja"
DEFAULT_EDITORIAL_PRIORITIES = (
    ("gentle", "やさしく落ち着いた印象の声です。"),
    ("warm", "あたたかく親しみやすい印象の声です。"),
    ("clear", "明瞭で聞き取りやすい印象の声です。"),
    ("calm", "穏やかで会話向きの印象の声です。"),
    ("natural", "自然な会話に合う印象の声です。"),
)
_CURSOR_SECRET = secrets.token_bytes(32)


class CatalogError(Exception):
    def __init__(
        self, code, status=400, retry_after=None, allow_stale=False, retryable=None
    ):
        super().__init__(code)
        self.code = code
        self.status = status
        self.retry_after = retry_after
        self.allow_stale = bool(allow_stale)
        self.retryable = (
            status in {429, 502, 503, 504} if retryable is None else bool(retryable)
        )


class CatalogUpstreamError(CatalogError):
    pass


class CatalogVoiceUnavailable(CatalogError):
    def __init__(self, code="voice_unavailable", status=404, reference_id=None):
        super().__init__(code, status=status)
        self.reference_id = reference_id


def _env_number(name, default, minimum, maximum, integer=False):
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    value = max(minimum, min(maximum, value))
    return int(value) if integer else value


def _clean_text(value, limit=256):
    if value is None:
        return ""
    if isinstance(value, dict):
        for key in ("name", "title", "nickname", "username"):
            if value.get(key):
                value = value[key]
                break
        else:
            return ""
    if not isinstance(value, (str, int, float)):
        return ""
    cleaned = " ".join(str(value).split())
    raw_credential = os.environ.get("FISH_API_KEY", "")
    for credential in (raw_credential, raw_credential.strip()):
        if credential:
            cleaned = cleaned.replace(credential, "[REDACTED]")
    return cleaned[:limit]


def _first(mapping, names, default=None):
    for name in names:
        value = mapping.get(name)
        if value is not None:
            return value
    return default


def _string_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    result = []
    for entry in value:
        cleaned = _clean_text(entry, 64).lower()
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result


def _normalize_language(value):
    value = _clean_text(value, 32).lower().replace("_", "-")
    aliases = {
        "japanese": "ja",
        "jp": "ja",
        "ja-jp": "ja",
        "english": "en",
        "en-us": "en",
        "en-gb": "en",
        "chinese": "zh",
        "zh-cn": "zh",
        "korean": "ko",
        "ko-kr": "ko",
    }
    return aliases.get(value, value.split("-", 1)[0])


def normalize_availability(raw, self_scope=False):
    """Map Fish state/visibility/DMCA fields to the four-value API enum."""
    if not isinstance(raw, dict):
        return "unknown"
    if _dmca_taken_down(raw):
        return "unavailable"
    state = _clean_text(_first(raw, ("state", "status", "training_state"))).lower()
    visibility = _clean_text(_first(raw, ("visibility", "privacy"))).lower()
    if state in {"deleted", "disabled", "failed", "rejected", "unavailable"}:
        return "unavailable"
    if not self_scope and visibility in {"private", "self", "owner"}:
        return "unavailable"
    if visibility in {"hidden", "unlisted"}:
        return "hidden"
    if state in {"trained", "ready", "succeeded", "available"}:
        if visibility in {"public", "", "private", "self", "owner"}:
            return "available"
    if state in {"training", "pending", "processing", "queued"}:
        return "unavailable"
    return "unknown"


def _reference_id(raw):
    return _clean_text(_first(raw, ("reference_id", "_id", "id", "model_id")), 160)


def _dmca_taken_down(raw):
    return isinstance(raw, dict) and (
        raw.get("dmca_taken_down") is True or raw.get("dmca") is True
    )


def _source_version(raw):
    explicit = _clean_text(
        _first(raw, ("source_version", "updated_at", "updatedAt", "version", "revision")),
        128,
    )
    if explicit and "://" not in explicit:
        return explicit
    stable = {
        "id": _reference_id(raw),
        "state": raw.get("state"),
        "visibility": raw.get("visibility"),
        "dmca": _dmca_taken_down(raw),
        "title": raw.get("title"),
    }
    encoded = json.dumps(stable, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()[:20]


def make_catalog_id(scope, reference_id, source_version):
    provider_scope = "self" if scope == "self" else "public"
    payload = json.dumps(
        {"p": PROVIDER, "s": provider_scope, "r": reference_id, "v": source_version},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return CATALOG_ID_PREFIX + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def parse_catalog_id(value):
    preset = voice_presets.get_preset(value) if isinstance(value, str) else None
    if preset is not None:
        return {
            "provider": preset.get("provider") or PROVIDER,
            "scope": "public",
            "reference_id": preset.get("reference_id", ""),
            "source_version": None,
            "preset_id": value,
        }
    if (
        not isinstance(value, str)
        or len(value) > 2048
        or not value.startswith(CATALOG_ID_PREFIX)
    ):
        raise CatalogError("invalid_catalog_id")
    encoded = value[len(CATALOG_ID_PREFIX):]
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise CatalogError("invalid_catalog_id") from None
    if (
        not isinstance(payload, dict)
        or payload.get("p") != PROVIDER
        or payload.get("s") not in {"public", "self"}
        or not isinstance(payload.get("r"), str)
        or not payload["r"].strip()
        or len(payload["r"]) > 160
        or not isinstance(payload.get("v"), str)
        or not payload["v"]
        or len(payload["v"]) > 160
    ):
        raise CatalogError("invalid_catalog_id")
    return {
        "provider": PROVIDER,
        "scope": payload["s"],
        "reference_id": payload["r"],
        "source_version": payload["v"],
    }


def _extract_taxonomy(raw, field, tag_prefix):
    values = _string_list(raw.get(field))
    for tag in _string_list(raw.get("tags")):
        if tag.startswith(tag_prefix):
            value = tag[len(tag_prefix):].strip()
            if value and value not in values:
                values.append(value)
    return values


def normalize_voice(raw, scope="all", editorial=None):
    if not isinstance(raw, dict):
        return None
    ref = _reference_id(raw)
    if not ref:
        return None
    self_scope = scope == "self"
    availability = normalize_availability(raw, self_scope=self_scope)
    language_values = _string_list(_first(raw, ("languages", "language", "locale")))
    for tag in _string_list(raw.get("tags")):
        if tag.startswith("language:"):
            language_values.append(tag[len("language:"):].strip())
    languages = [_normalize_language(v) for v in language_values]
    languages = list(dict.fromkeys(v for v in languages if v))
    title = _clean_text(_first(raw, ("title", "name")), 160) or "Untitled voice"
    author = _clean_text(_first(raw, ("author", "creator", "owner")), 160) or "Unknown creator"
    source = _clean_text(_first(raw, ("source", "source_name", "origin")), 160) or "Fish Audio"
    # URLs can be signed/private sample locations.  Attribution is textual only.
    if "://" in source:
        source = "Fish Audio"
    source_version = _source_version(raw)
    editorial = dict(editorial or {})
    summary = _clean_text(editorial.get("summary"), 280)
    item = {
        "catalog_id": make_catalog_id(scope, ref, source_version),
        "provider": PROVIDER,
        "title": title,
        "availability": availability if availability in VALID_AVAILABILITY else "unknown",
        "languages": languages,
        "directions": list(dict.fromkeys(
            _string_list(raw.get("direction"))
            + _extract_taxonomy(raw, "directions", "direction:")
        )),
        "impressions": list(dict.fromkeys(
            _string_list(raw.get("impression"))
            + _extract_taxonomy(raw, "impressions", "impression:")
        )),
        "attribution": {
            "title": title,
            "author": author,
            "source": source,
        },
        "caty_summary": summary or None,
        "source_version": source_version,
        "editorial": {
            "recommended": bool(editorial),
            "rank": editorial.get("rank") if isinstance(editorial.get("rank"), int) else None,
        },
        # Internal-only fields are removed before the HTTP response.
        "_reference_id": ref,
        "_visibility": _clean_text(raw.get("visibility"), 32).lower(),
        "_state": _clean_text(raw.get("state"), 32).lower(),
        "_dmca": _dmca_taken_down(raw),
    }
    return item


def _public_selectable(raw):
    if not isinstance(raw, dict):
        return False
    voice_type = _clean_text(_first(raw, ("type", "model_type"))).lower()
    return (
        voice_type == "tts"
        and _clean_text(raw.get("state")).lower() == "trained"
        and _clean_text(raw.get("visibility")).lower() == "public"
        and not _dmca_taken_down(raw)
    )


def _self_voice(raw):
    voice_type = _clean_text(_first(raw, ("type", "model_type"))).lower()
    return voice_type in {"", "tts"}


def _extract_items(payload):
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("items", "models", "results"):
        if isinstance(payload.get(key), list):
            return payload[key]
    data = payload.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("items", "models", "results"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def _has_more(payload, page_number, received, page_size):
    if not isinstance(payload, dict):
        return received >= page_size
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if isinstance(data.get("has_more"), bool):
        return data["has_more"]
    if isinstance(data.get("next_cursor"), str):
        return bool(data["next_cursor"])
    total = data.get("total")
    if isinstance(total, int):
        return page_number * page_size < total
    return received >= page_size


def _safe_retry_after(error, default=30):
    value = getattr(error, "retry_after", None)
    try:
        value = int(float(value))
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, 3600))


def _classify_upstream_error(error):
    if getattr(error, "configuration_error", False):
        return CatalogUpstreamError(
            "catalog_not_configured",
            status=503,
            allow_stale=False,
            retryable=False,
        )
    status = getattr(error, "status", None)
    if status in {401, 403}:
        return CatalogUpstreamError(
            "catalog_credentials_rejected",
            status=503,
            allow_stale=False,
            retryable=False,
        )
    if status == 429:
        return CatalogUpstreamError(
            "catalog_rate_limited",
            status=429,
            retry_after=_safe_retry_after(error),
            allow_stale=True,
        )
    if status is None or (isinstance(status, int) and 500 <= status <= 599):
        return CatalogUpstreamError(
            "catalog_temporarily_unavailable",
            status=503,
            retry_after=_safe_retry_after(error),
            allow_stale=True,
        )
    return CatalogUpstreamError(
        "catalog_request_rejected",
        status=502,
        allow_stale=False,
        retryable=False,
    )


def credential_partition(installation_id):
    key = os.environ.get("FISH_API_KEY", "")
    material = (str(installation_id) + "\0" + key).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


class _Snapshot:
    def __init__(self, items, fetched_at, expires_at):
        self.items = items
        self.fetched_at = fetched_at
        self.expires_at = expires_at


class _Flight:
    def __init__(self):
        self.event = threading.Event()
        self.result = None
        self.error = None


class VoiceCatalogService:
    def __init__(
        self,
        transport,
        installation_id,
        editorial_overlay=None,
        clock=None,
        cache_ttl=None,
        fetch_limit=None,
        max_snapshot_entries=8,
    ):
        self._transport = transport
        self._installation_id = installation_id
        self._editorial = dict(editorial_overlay or {})
        self._clock = clock or time.time
        self._cache_ttl = cache_ttl if cache_ttl is not None else _env_number(
            "CATY_VOICE_CATALOG_TTL_SECONDS", 300, 5, 86400
        )
        self._fetch_limit = fetch_limit if fetch_limit is not None else _env_number(
            "CATY_VOICE_CATALOG_FETCH_LIMIT", 300, 50, 1000, integer=True
        )
        self._lock = threading.Lock()
        self._snapshots = collections.OrderedDict()
        self._flights = {}
        self._max_snapshot_entries = max(1, int(max_snapshot_entries))

    def _cache_scope(self, scope):
        if scope == "self":
            return ("self", credential_partition(self._installation_id))
        return ("public", "shared")

    def _fetch(self, scope):
        self_scope = scope == "self"
        result = []
        page_number = 1
        upstream_page_size = min(100, self._fetch_limit)
        while len(result) < self._fetch_limit:
            params = {
                "page_size": upstream_page_size,
                "page_number": page_number,
                "type": "tts",
            }
            if self_scope:
                params["self"] = "true"
            else:
                params.update({"state": "trained", "visibility": "public"})
            payload = self._transport("/model", params)
            raw_items = _extract_items(payload)
            if not raw_items:
                break
            result.extend(raw_items[: self._fetch_limit - len(result)])
            if not _has_more(payload, page_number, len(raw_items), upstream_page_size):
                break
            page_number += 1
        normalized = []
        seen = set()
        for raw in result:
            if (self_scope and not _self_voice(raw)) or (not self_scope and not _public_selectable(raw)):
                continue
            ref = _reference_id(raw)
            if not ref or ref in seen:
                continue
            seen.add(ref)
            item = normalize_voice(raw, scope="self" if self_scope else "all")
            if item is not None:
                normalized.append(item)
        return normalized

    def _snapshot(self, scope):
        key = self._cache_scope(scope)
        now = self._clock()
        with self._lock:
            for cached_key, snapshot in list(self._snapshots.items()):
                if cached_key != key and snapshot.expires_at <= now:
                    self._snapshots.pop(cached_key, None)
            cached = self._snapshots.get(key)
            if cached is not None and cached.expires_at > now:
                self._snapshots.move_to_end(key)
                return cached, False
            flight = self._flights.get(key)
            leader = flight is None
            if leader:
                flight = _Flight()
                self._flights[key] = flight
        if not leader:
            flight.event.wait()
            if flight.error is not None:
                raise flight.error
            return flight.result
        try:
            try:
                items = self._fetch(scope)
            except Exception as error:
                classified = _classify_upstream_error(error)
                with self._lock:
                    cached = self._snapshots.get(key)
                    if cached is not None:
                        self._snapshots.move_to_end(key)
                if cached is not None and classified.allow_stale:
                    flight.result = (cached, True)
                    return flight.result
                flight.error = classified
                raise classified from None
            snapshot = _Snapshot(items, now, now + self._cache_ttl)
            with self._lock:
                self._snapshots[key] = snapshot
                self._snapshots.move_to_end(key)
                while len(self._snapshots) > self._max_snapshot_entries:
                    self._snapshots.popitem(last=False)
            flight.result = (snapshot, False)
            return flight.result
        finally:
            with self._lock:
                self._flights.pop(key, None)
                flight.event.set()

    def _editorial_for(self, item):
        explicit = self._editorial.get(item["_reference_id"])
        if explicit:
            return dict(explicit)
        taxonomy = set(item["directions"] + item["impressions"])
        for index, (label, summary) in enumerate(DEFAULT_EDITORIAL_PRIORITIES, start=1):
            if label in taxonomy:
                # Attribute-based editorial ranking keeps the whole public
                # catalog visible; it is intentionally not an ID allowlist.
                return {"rank": 100 + index, "summary": summary}
        return {}

    @staticmethod
    def _fingerprint(scope, language, query, direction, impression):
        value = json.dumps(
            [scope, language, query, direction, impression],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(value).hexdigest()[:24]

    @staticmethod
    def _cursor(offset, fingerprint):
        raw = json.dumps({"o": offset, "f": fingerprint}, separators=(",", ":")).encode("utf-8")
        signature = hmac.new(_CURSOR_SECRET, raw, hashlib.sha256).digest()[:12]
        return base64.urlsafe_b64encode(raw + signature).decode("ascii").rstrip("=")

    @staticmethod
    def _cursor_offset(cursor, fingerprint):
        if not cursor:
            return 0
        if not isinstance(cursor, str) or len(cursor) > 512:
            raise CatalogError("invalid_cursor")
        try:
            decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            raw, signature = decoded[:-12], decoded[-12:]
            expected = hmac.new(_CURSOR_SECRET, raw, hashlib.sha256).digest()[:12]
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            raise CatalogError("invalid_cursor") from None
        if (
            len(decoded) < 13
            or not hmac.compare_digest(signature, expected)
            or not isinstance(payload, dict)
            or payload.get("f") != fingerprint
            or not isinstance(payload.get("o"), int)
            or payload["o"] < 0
        ):
            raise CatalogError("invalid_cursor")
        return payload["o"]

    def list_voices(
        self,
        scope="recommended",
        language=None,
        query="",
        direction="",
        impression="",
        cursor=None,
        page_size=DEFAULT_PAGE_SIZE,
    ):
        if scope not in VALID_SCOPES:
            raise CatalogError("invalid_scope")
        try:
            page_size = int(page_size)
        except (TypeError, ValueError):
            raise CatalogError("invalid_page_size") from None
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise CatalogError("invalid_page_size")
        language = DEFAULT_LANGUAGE if language is None else _clean_text(language, 32).lower()
        if language in {"all", "*", "none"}:
            language = ""
        language = _normalize_language(language) if language else ""
        query = _clean_text(query, 160).casefold()
        direction = _clean_text(direction, 64).lower()
        impression = _clean_text(impression, 64).lower()
        fingerprint = self._fingerprint(scope, language, query, direction, impression)
        offset = self._cursor_offset(cursor, fingerprint)
        snapshot, stale = self._snapshot(scope)
        items = []
        for original in snapshot.items:
            item = dict(original)
            item["attribution"] = dict(original["attribution"])
            item["editorial"] = dict(original["editorial"])
            ref = item["_reference_id"]
            if scope == "recommended":
                editorial = self._editorial_for(item)
                item["caty_summary"] = _clean_text(editorial.get("summary"), 280) or None
                item["editorial"] = {
                    "recommended": bool(editorial),
                    "rank": editorial.get("rank") if isinstance(editorial.get("rank"), int) else None,
                }
                item["catalog_id"] = make_catalog_id("recommended", ref, item["source_version"])
            if language and language not in item["languages"]:
                continue
            if direction and direction not in item["directions"]:
                continue
            if impression and impression not in item["impressions"]:
                continue
            if query:
                searchable = " ".join((
                    item["title"], item["attribution"]["author"],
                    item.get("caty_summary") or "", *item["languages"],
                    *item["directions"], *item["impressions"],
                )).casefold()
                if query not in searchable:
                    continue
            items.append(item)
        if scope == "recommended":
            items.sort(key=lambda item: (
                not item["editorial"]["recommended"],
                item["editorial"]["rank"] if item["editorial"]["rank"] is not None else 10**9,
                item["title"].casefold(),
            ))
        page = items[offset:offset + page_size]
        public_page = []
        for item in page:
            public_page.append({key: value for key, value in item.items() if not key.startswith("_")})
        next_offset = offset + len(page)
        next_cursor = self._cursor(next_offset, fingerprint) if next_offset < len(items) else None
        return {
            "scope": scope,
            "items": public_page,
            "next_cursor": next_cursor,
            "page_size": page_size,
            "stale": stale,
            "filters": {
                "language": language or None,
                "query": query or None,
                "direction": direction or None,
                "impression": impression or None,
            },
        }

    def resolve_preview(self, catalog_id=None, reference_id=None):
        if bool(catalog_id) == bool(reference_id):
            raise CatalogError("exactly_one_voice_identifier_required")
        hint = parse_catalog_id(catalog_id) if catalog_id else {
            "provider": PROVIDER,
            "scope": "diagnostic",
            "reference_id": _clean_text(reference_id, 160),
            "source_version": None,
        }
        if not hint["reference_id"]:
            raise CatalogError("invalid_reference_id")
        try:
            safe_reference = urllib.parse.quote(hint["reference_id"], safe="")
            payload = self._transport("/model/" + safe_reference, None)
        except Exception as error:
            status = getattr(error, "status", None)
            if status == 404:
                raise CatalogVoiceUnavailable("voice_not_found", reference_id=hint["reference_id"]) from None
            raise _classify_upstream_error(error) from None
        raw_items = _extract_items(payload)
        raw = raw_items[0] if raw_items else payload
        if not isinstance(raw, dict) or _reference_id(raw) != hint["reference_id"]:
            raise CatalogVoiceUnavailable("voice_not_found", reference_id=hint["reference_id"])
        if _dmca_taken_down(raw):
            raise CatalogVoiceUnavailable("voice_unavailable", reference_id=hint["reference_id"])
        public = _public_selectable(raw)
        requested_self = hint["scope"] == "self"
        requested_diagnostic = hint["scope"] == "diagnostic"
        if hint["scope"] == "public" and not public:
            raise CatalogVoiceUnavailable("voice_unavailable", reference_id=hint["reference_id"])
        if requested_self and not _self_voice(raw):
            raise CatalogVoiceUnavailable("voice_unavailable", reference_id=hint["reference_id"])
        effective_scope = "self" if requested_self or requested_diagnostic or not public else "public"
        item = normalize_voice(raw, scope="self" if effective_scope == "self" else "all")
        if item is None or item["availability"] not in {"available", "hidden"}:
            raise CatalogVoiceUnavailable("voice_unavailable", reference_id=hint["reference_id"])
        return {
            "provider": PROVIDER,
            "scope": effective_scope,
            "reference_id": hint["reference_id"],
            "source_version": item["source_version"],
            "hint_source_version": hint.get("source_version"),
            "preset_id": hint.get("preset_id", ""),
            "cache_partition": (
                credential_partition(self._installation_id) if effective_scope == "self" else "shared"
            ),
            "availability": item["availability"],
        }
