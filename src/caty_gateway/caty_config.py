import json
import os
import sys
import tempfile
import threading
import urllib.parse


WRITABLE_CONFIG_KEYS = {
    "name", "accent_color", "voice_id", "voice_hint", "stream_tts",
    "attachment_passthrough",
}
CONFIG_VALUE_CHOICES = {
    "stream_tts": {"on", "off", ""},
    "attachment_passthrough": {"on", "off", ""},
}
VERSION_KEYS = {"config_version", "assets_version", "fillers_version"}
VOICE_POINTER_KEYS = {
    "voice_catalog_id", "voice_preset_id", "voice_reference_id",
    "voice_provider", "voice_display_metadata", "voice_availability",
    "voice_checked_at", "active_pack_id", "voice_management_state",
    "filler_effective_status",
    "lkg_voice_catalog_id", "lkg_voice_preset_id", "lkg_voice_reference_id",
    "lkg_voice_provider", "lkg_voice_display_metadata",
    "lkg_voice_availability", "lkg_voice_checked_at", "lkg_pack_id",
    "lkg_voice_management_state", "lkg_filler_effective_status",
}
VOICE_TRANSACTION_KEYS = VOICE_POINTER_KEYS | {"voice_id"}
PERSISTED_KEYS = WRITABLE_CONFIG_KEYS | VERSION_KEYS | VOICE_POINTER_KEYS
_DEFAULT_CONFIG_DIR_WARNED = False
_DEFAULT_CONFIG_DIR_WARN_LOCK = threading.Lock()

# Security boundary for gateway-delivered avatar session bearer tokens.  This
# is intentionally not environment-overridable: production Caty Cloud is the
# only origin allowed to receive managed avatar photos and session credentials.
CATY_CLOUD_ORIGIN = "https://api.caty.talk"
AVATAR_VENDOR_HOST_ALLOWLIST = ("poyo.ai", "renoise.ai")


def avatar_vendor_host_allowlist():
    """Return the runtime-configured vendor URL suffix allowlist."""
    configured = os.environ.get("CATY_AVATAR_VENDOR_HOST_ALLOWLIST", "")
    if not configured.strip():
        return AVATAR_VENDOR_HOST_ALLOWLIST
    hosts = tuple(
        host.strip().lower().rstrip(".")
        for host in configured.split(",")
        if host.strip().rstrip(".")
    )
    return hosts or AVATAR_VENDOR_HOST_ALLOWLIST


def normalize_caty_cloud_origin(value):
    """Return the pinned Caty Cloud origin or raise InvalidConfig.

    A single trailing slash is the only spelling variation accepted.  URL
    components that can change request routing or credential scope are rejected
    before the normalized origin is compared with the pinned production value.
    """
    if not isinstance(value, str) or not value:
        raise InvalidConfig("cloud_session.base_url must be a non-empty string")
    if value != value.strip():
        raise InvalidConfig("cloud_session.base_url must be the official Caty Cloud HTTPS origin")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise InvalidConfig("invalid cloud_session.base_url") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise InvalidConfig("cloud_session.base_url must be the official Caty Cloud HTTPS origin")
    normalized = f"https://{parsed.hostname.lower()}"
    if normalized != CATY_CLOUD_ORIGIN:
        raise InvalidConfig("cloud_session.base_url must be the official Caty Cloud HTTPS origin")
    return normalized


class ConfigError(Exception):
    pass


class InvalidConfig(ConfigError):
    def __init__(self, message, invalid_keys=None):
        super().__init__(message)
        self.invalid_keys = sorted(invalid_keys or [])


class VersionConflict(ConfigError):
    def __init__(self, current_version):
        super().__init__("version_conflict")
        self.current_version = current_version


class OverlayConfig:
    """Thread-safe overlay config backed by CATY_CONFIG_DIR/member_config.json."""

    def __init__(self, defaults, *, fault_injector=None):
        self._defaults = defaults
        self._lock = threading.Lock()
        self._fault_injector = fault_injector

    def path(self):
        # 空文字も未設定扱い（cwd 相対 = チェックアウト内共有の footgun を残さない）
        config_dir = os.environ.get("CATY_CONFIG_DIR")
        if not config_dir:
            member_id = os.environ.get("CATY_ID", "caty")
            config_dir = os.path.expanduser(f"~/.config/caty-gateway/{member_id}-config")
            global _DEFAULT_CONFIG_DIR_WARNED
            with _DEFAULT_CONFIG_DIR_WARN_LOCK:
                if not _DEFAULT_CONFIG_DIR_WARNED:
                    print(
                        "WARN: CATY_CONFIG_DIR is unset; using default "
                        f"{config_dir}. Set CATY_CONFIG_DIR explicitly for production.",
                        file=sys.stderr,
                        flush=True,
                    )
                    _DEFAULT_CONFIG_DIR_WARNED = True
        return os.path.join(config_dir, "member_config.json")

    def get(self):
        with self._lock:
            return self._merged_unlocked()

    def update(self, fields, if_match):
        if not isinstance(fields, dict):
            raise InvalidConfig("json object required")
        invalid = set(fields) - WRITABLE_CONFIG_KEYS
        if invalid:
            raise InvalidConfig("unknown config field", invalid)
        if if_match is None:
            raise VersionConflict(self.get()["config_version"])
        try:
            expected = int(str(if_match).strip())
        except (TypeError, ValueError):
            # 数値でない If-Match（W/"1" 等）は前提条件の書式エラー＝400。
            # ヘッダ欠落(None)は上の分岐で 409（現在版を返して再送させる）。
            raise InvalidConfig("invalid if-match header")

        with self._lock:
            current = self._merged_unlocked()
            if expected != current["config_version"]:
                raise VersionConflict(current["config_version"])
            updated = dict(current)
            for key, value in fields.items():
                if not isinstance(value, str):
                    raise InvalidConfig("config field must be string", [key])
                choices = CONFIG_VALUE_CHOICES.get(key)
                if choices is not None and value not in choices:
                    raise InvalidConfig("invalid config value", [key])
                updated[key] = value
            default_voice_id = self._defaults_dict().get("voice_id", "")
            resets_legacy = bool(
                "voice_id" in fields
                and default_voice_id
                and fields["voice_id"] == default_voice_id
                and current.get("voice_management_state") != "legacy"
            )
            if "voice_id" in fields and (
                fields["voice_id"] != current.get("voice_id", "") or resets_legacy
            ):
                # The diagnostic raw route cannot prove that an existing pack
                # matches the new voice. Invalidate the pointer in this same
                # atomic replace so playback fails closed without a mismatch
                # window.
                if current.get("voice_management_state") == "managed":
                    updated.update({
                        "lkg_voice_catalog_id": current.get("voice_catalog_id", ""),
                        "lkg_voice_preset_id": current.get("voice_preset_id", ""),
                        "lkg_voice_reference_id": current.get("voice_reference_id", ""),
                        "lkg_voice_provider": current.get("voice_provider", ""),
                        "lkg_voice_display_metadata": current.get("voice_display_metadata", {}),
                        "lkg_voice_availability": current.get("voice_availability", "unknown"),
                        "lkg_voice_checked_at": current.get("voice_checked_at", ""),
                        "lkg_pack_id": current.get("active_pack_id", ""),
                        "lkg_voice_management_state": "managed",
                        "lkg_filler_effective_status": current.get(
                            "filler_effective_status", "unavailable"
                        ),
                    })
                restores_legacy = bool(
                    default_voice_id and fields["voice_id"] == default_voice_id
                )
                updated.update({
                    "voice_catalog_id": "",
                    "voice_preset_id": "",
                    "voice_reference_id": fields["voice_id"],
                    "voice_provider": "fish" if fields["voice_id"] else "",
                    "voice_display_metadata": {},
                    "voice_availability": "unknown",
                    "voice_checked_at": "",
                    "active_pack_id": "",
                    "voice_management_state": "legacy" if restores_legacy else "raw",
                    "filler_effective_status": (
                        "legacy-unknown" if restores_legacy
                        else "stale" if fields["voice_id"]
                        else "unavailable"
                    ),
                })
                updated["fillers_version"] = int(current.get("fillers_version", 1)) + 1
            updated["config_version"] = current["config_version"] + 1
            self._write_unlocked(updated)
            return self._merged_unlocked()

    def commit_voice_pointers(self, fields, if_match):
        """CAS and atomically replace one complete active/LKG pointer set."""
        if not isinstance(fields, dict) or set(fields) != VOICE_TRANSACTION_KEYS:
            raise InvalidConfig("invalid voice pointer transaction")
        with self._lock:
            current = self._merged_unlocked()
            if if_match is None:
                raise VersionConflict(current["config_version"])
            expected = self._parse_if_match(if_match)
            if expected != current["config_version"]:
                raise VersionConflict(current["config_version"])
            updated = dict(current)
            updated.update(fields)
            updated["config_version"] = current["config_version"] + 1
            filler_cache_keys = (
                "active_pack_id", "voice_id", "voice_reference_id",
                "voice_management_state", "filler_effective_status",
            )
            if any(fields.get(key) != current.get(key, "") for key in filler_cache_keys):
                updated["fillers_version"] = int(current.get("fillers_version", 1)) + 1
            self._write_unlocked(updated)
            return self._merged_unlocked()

    @staticmethod
    def _parse_if_match(if_match):
        try:
            return int(str(if_match).strip())
        except (TypeError, ValueError):
            raise InvalidConfig("invalid if-match header") from None

    def bump(self, key):
        if key not in ("assets_version", "fillers_version"):
            raise ValueError(f"unsupported version key: {key}")
        with self._lock:
            current = self._merged_unlocked()
            updated = dict(current)
            updated[key] = int(current.get(key, 1)) + 1
            self._write_unlocked(updated)
            return updated[key]

    def _defaults_dict(self):
        data = self._defaults() if callable(self._defaults) else dict(self._defaults)
        data = dict(data)
        data.setdefault("config_version", 1)
        data.setdefault("assets_version", 1)
        data.setdefault("fillers_version", 1)
        data.setdefault("voice_catalog_id", "")
        data.setdefault("voice_preset_id", "")
        data.setdefault("voice_reference_id", data.get("voice_id", ""))
        data.setdefault("voice_provider", "fish" if data.get("voice_id") else "")
        data.setdefault("voice_display_metadata", {})
        data.setdefault("voice_availability", "unknown")
        data.setdefault("voice_checked_at", "")
        data.setdefault("active_pack_id", "")
        data.setdefault("voice_management_state", "legacy")
        data.setdefault("filler_effective_status", "legacy-unknown")
        for key in (
            "lkg_voice_catalog_id", "lkg_voice_preset_id",
            "lkg_voice_reference_id", "lkg_voice_provider",
            "lkg_voice_availability", "lkg_voice_checked_at", "lkg_pack_id",
        ):
            data.setdefault(key, "")
        data.setdefault("lkg_voice_display_metadata", {})
        data.setdefault("lkg_voice_management_state", "legacy")
        data.setdefault("lkg_filler_effective_status", "legacy-unknown")
        for key in VERSION_KEYS:
            try:
                data[key] = int(data.get(key, 1))
            except (TypeError, ValueError):
                data[key] = 1
        return data

    def _read_overlay_unlocked(self):
        path = self.path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if k in PERSISTED_KEYS}

    def _merged_unlocked(self):
        merged = self._defaults_dict()
        overlay = self._read_overlay_unlocked()
        merged.update(overlay)
        # Pre-transaction overlays persisted voice_id without a pointer set.
        # The effective overlay voice is authoritative during this migration;
        # never retain a defaults/env pointer for a different voice.
        if (
            "voice_reference_id" not in overlay
            and merged.get("voice_management_state", "legacy") in {"legacy", "raw"}
        ):
            merged["voice_reference_id"] = merged.get("voice_id", "")
            merged["voice_provider"] = "fish" if merged["voice_reference_id"] else ""
        if "lkg_voice_management_state" not in overlay:
            merged["lkg_voice_management_state"] = (
                "managed"
                if merged.get("lkg_pack_id") or merged.get("lkg_voice_catalog_id")
                else "legacy"
            )
        if "lkg_filler_effective_status" not in overlay:
            if merged.get("lkg_pack_id"):
                merged["lkg_filler_effective_status"] = "ready"
            elif merged["lkg_voice_management_state"] == "managed":
                merged["lkg_filler_effective_status"] = (
                    "stale" if merged.get("lkg_voice_reference_id") else "unavailable"
                )
            else:
                merged["lkg_filler_effective_status"] = "legacy-unknown"
        for key in VERSION_KEYS:
            try:
                merged[key] = int(merged.get(key, 1))
            except (TypeError, ValueError):
                merged[key] = 1
        return merged

    def _write_unlocked(self, full_config):
        path = self.path()
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        payload = {k: full_config[k] for k in PERSISTED_KEYS if k in full_config}
        fd, tmp = tempfile.mkstemp(prefix=".member_config-", suffix=".json", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            if self._fault_injector is not None:
                self._fault_injector("config_after_tmp_write_before_replace", {"path": path})
            os.replace(tmp, path)
            try:
                directory_fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
