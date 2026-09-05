"""Transactional Voice Picker activation and readiness state.

The immutable pack registry stages independently.  Only OverlayConfig owns the
active voice/pack pointer, and commits the complete pointer set with one CAS
protected atomic replace.
"""

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import tempfile
import threading

from caty_gateway import caty_config
from caty_gateway import filler_pack
from caty_gateway import filler_texts
from caty_gateway import voice_catalog
from caty_gateway import voice_presets


LOGGER = logging.getLogger(__name__)


FILLER_POLICY_REQUIRE_MATCHING = "require_matching"
FILLER_POLICY_VOICE_ONLY = "voice_only"
SUPPORTED_SCOPES = ("recommended", "all", "self")
def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _fsync_directory(path):
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


class ActivationError(Exception):
    def __init__(
        self, code, status=400, *, retryable=False, retry_after=None, details=None
    ):
        super().__init__(code)
        self.code = code
        self.status = status
        self.retryable = bool(retryable)
        self.retry_after = retry_after
        self.details = dict(details or {})


class VoiceActivationService:
    def __init__(
        self,
        config,
        catalog,
        registry,
        *,
        member_id,
        synthesizer,
        inference_contract_version,
        engine_truth,
        clock=None,
        fault_injector=None,
    ):
        self.config = config
        self.catalog = catalog
        self.registry = registry
        self.member_id = member_id
        registry_root = getattr(registry, "root", None)
        if registry_root is None:
            raise ValueError("registry root required")
        self.data_root = Path(registry_root).parent.parent
        self.synthesizer = synthesizer
        self.inference_contract_version = inference_contract_version
        self.engine_truth = engine_truth
        self.clock = clock or _now
        self.fault_injector = fault_injector
        self._operation_lock = threading.Lock()
        self._runtime_lock = threading.Lock()
        self._generating = None
        self._last_filler_error = None
        self.journal_path = Path(config.path()).with_name("voice-activation-journal.json")
        self.recover()

    def _fault(self, point, **context):
        if self.fault_injector is not None:
            self.fault_injector(point, context)

    def _write_journal(self, payload):
        path = self.journal_path
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=".voice-activation-", suffix=".json", dir=path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            _fsync_directory(path.parent)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def _clear_journal(self):
        try:
            self.journal_path.unlink()
            _fsync_directory(self.journal_path.parent)
        except OSError:
            pass

    def recover(self):
        """Discard the advisory journal and recover storage, never pointers.

        The config pointer set is the only activation commit record. Recovery
        never reads the journal to select or promote an active pack.
        """
        try:
            self.registry.recover_staging()
        finally:
            # The config pointer is the only commit record. A surviving marker
            # therefore describes an abandoned or already-committed operation;
            # neither case may select a staged pack during recovery.
            self._clear_journal()

    @staticmethod
    def _safe_display(resolved):
        display = resolved.get("display_metadata")
        if not isinstance(display, dict):
            display = {}
        result = {}
        for key in ("title", "author", "source", "summary"):
            value = display.get(key)
            if isinstance(value, str) and value.strip():
                result[key] = " ".join(value.split())[:280]
        return result

    def _validate_target(self, *, catalog_id=None, reference_id=None):
        try:
            resolved = self.catalog.resolve_preview(
                catalog_id=catalog_id, reference_id=reference_id
            )
        except voice_catalog.CatalogVoiceUnavailable as error:
            raise ActivationError(
                error.code,
                404,
                details={"availability": "unavailable", "recovery_candidates": self._recovery_candidates()},
            ) from None
        except voice_catalog.CatalogError as error:
            raise ActivationError(
                error.code,
                error.status,
                retryable=error.retryable,
                retry_after=error.retry_after,
                details={"availability": "unknown", "recovery_candidates": self._recovery_candidates()},
            ) from None
        except Exception:
            raise ActivationError(
                "voice_validation_unknown",
                503,
                retryable=True,
                details={"availability": "unknown", "recovery_candidates": self._recovery_candidates()},
            ) from None
        if resolved.get("availability") not in ("available", "hidden"):
            raise ActivationError(
                "voice_unavailable",
                404,
                details={"availability": "unavailable", "recovery_candidates": self._recovery_candidates()},
            )
        display_metadata = self._safe_display(resolved)
        preset_id = resolved.get("preset_id", "")
        if preset_id:
            preset = voice_presets.get_preset(preset_id)
            title = (preset or {}).get("display_name_ja")
            if isinstance(title, str) and title.strip() and "title" not in display_metadata:
                display_metadata["title"] = " ".join(title.split())[:280]
        return {
            "catalog_id": catalog_id or "",
            "preset_id": "",
            "reference_id": resolved["reference_id"],
            "provider": resolved.get("provider") or "fish",
            # resolve_preview is the live validation boundary and intentionally
            # returns no catalog display record. Keep the saved snapshot empty
            # instead of walking catalog pages while activation is serialized.
            "display_metadata": display_metadata,
            "availability": resolved["availability"],
            "checked_at": self.clock(),
        }

    @staticmethod
    def _voice_from_config(config, prefix=""):
        key = lambda name: f"{prefix}{name}"
        display = config.get(key("voice_display_metadata"), {})
        display = display if isinstance(display, dict) else {}
        return {
            "catalog_id": config.get(key("voice_catalog_id"), ""),
            "preset_id": config.get(key("voice_preset_id"), ""),
            "reference_id": config.get(key("voice_reference_id"), ""),
            "provider": config.get(key("voice_provider"), ""),
            "display_metadata": {
                name: " ".join(value.split())[:280]
                for name, value in display.items()
                if name in ("title", "author", "source", "summary")
                and isinstance(value, str) and value.strip()
            },
            "availability": config.get(key("voice_availability"), "unknown"),
            "checked_at": config.get(key("voice_checked_at"), ""),
        }

    @staticmethod
    def _active_fields(
        target, pack_id, status, current, *, management_state="managed"
    ):
        fields = {
            "voice_id": target["reference_id"],
            "voice_catalog_id": target["catalog_id"],
            "voice_preset_id": target["preset_id"],
            "voice_reference_id": target["reference_id"],
            "voice_provider": target["provider"],
            "voice_display_metadata": target["display_metadata"],
            "voice_availability": target["availability"],
            "voice_checked_at": target["checked_at"],
            "active_pack_id": pack_id or "",
            "voice_management_state": management_state,
            "filler_effective_status": status,
        }
        old_voice = VoiceActivationService._voice_from_config(current)
        pointer_mismatch = current.get("voice_id") != old_voice["reference_id"]
        if pointer_mismatch:
            old_voice.update({
                "catalog_id": "",
                "preset_id": "",
                "reference_id": current.get("voice_id", ""),
                "provider": "fish" if current.get("voice_id") else "",
                "display_metadata": {},
                "availability": "unknown",
                "checked_at": "",
            })
        fields.update({
            "lkg_voice_catalog_id": old_voice["catalog_id"],
            "lkg_voice_preset_id": old_voice["preset_id"],
            "lkg_voice_reference_id": old_voice["reference_id"],
            "lkg_voice_provider": old_voice["provider"],
            "lkg_voice_display_metadata": old_voice["display_metadata"],
            "lkg_voice_availability": old_voice["availability"],
            "lkg_voice_checked_at": old_voice["checked_at"],
            "lkg_pack_id": "" if pointer_mismatch else current.get("active_pack_id", ""),
            "lkg_voice_management_state": current.get(
                "voice_management_state", "legacy"
            ),
            "lkg_filler_effective_status": current.get(
                "filler_effective_status", "legacy-unknown"
            ),
        })
        return fields

    def _effective_texts(self):
        return filler_texts.effective(self.member_id, self.data_root)

    def _pack_text_matches(self, pack_id, effective_snapshot, inspected=None):
        inspected = inspected or self.registry.inspect(pack_id)
        active_version = inspected.get("filler_text_version")
        if active_version == effective_snapshot.version:
            return True, active_version
        if active_version != filler_texts.LEGACY_TEXT_VERSION:
            return False, active_version
        try:
            sidecar = self.registry.read_texts(pack_id)
            matches = (
                filler_texts.normalize(sidecar.get("texts"))
                == filler_texts.normalize(effective_snapshot.texts)
            )
        except (filler_pack.FillerPackError, filler_texts.ValidationError):
            matches = False
        return matches, active_version

    def _matching_pack(self, target, effective_snapshot=None):
        effective_snapshot = effective_snapshot or self._effective_texts()
        for item in self.registry.list_packs():
            if item.get("status") != "ready":
                continue
            generated_for = item.get("generated_for") or {}
            inspected = self.registry.inspect(item["pack_id"])
            preset_matches = (
                not target["preset_id"]
                or inspected.get("preset_id") == target["preset_id"]
            )
            text_matches, _active_version = self._pack_text_matches(
                item["pack_id"], effective_snapshot, inspected
            )
            if (
                generated_for.get("provider") == target["provider"]
                and generated_for.get("reference_id") == target["reference_id"]
                and preset_matches
                and text_matches
            ):
                resolved = self.registry.resolve(
                    item["pack_id"],
                    active_provider=target["provider"],
                    active_reference_id=target["reference_id"],
                )
                if resolved.get("status") == "ready":
                    return item["pack_id"]
        return None

    def _stage_matching_pack(self, target, effective_snapshot=None):
        effective_snapshot = effective_snapshot or self._effective_texts()
        try:
            manifest = self.registry.stage_pack(
                generated_for_provider=target["provider"],
                generated_for_reference_id=target["reference_id"],
                preset_id=target["preset_id"] or None,
                filler_text_version=effective_snapshot.version,
                texts=effective_snapshot.texts,
                synthesizer=self.synthesizer,
                inference_contract_version=self.inference_contract_version(),
                provenance={"generator": "caty-gateway"},
                license_metadata={},
            )
        except filler_pack.FillerPackError as error:
            # The filler transport deliberately sanitizes synthesis failures,
            # so classify against the same live voice boundary used before
            # staging. A voice can become permanently unavailable between
            # those two calls; do not report that case as a generic retry.
            try:
                self._validate_target(
                    catalog_id=target["catalog_id"] or None,
                    reference_id=(
                        None if target["catalog_id"] else target["reference_id"]
                    ),
                )
            except ActivationError as validation_error:
                validation_error.details["state_unchanged"] = True
                raise validation_error from None
            status = getattr(error, "status", None)
            retry_after = getattr(error, "retry_after", None)
            if isinstance(status, int) and 400 <= status < 500 and status != 429:
                raise ActivationError(
                    getattr(error, "code", None) or "filler_generation_rejected",
                    status,
                    details={
                        "availability": "unavailable",
                        "recovery_candidates": self._recovery_candidates(),
                        "state_unchanged": True,
                    },
                ) from None
            raise ActivationError(
                getattr(error, "code", None) or "filler_generation_unknown",
                status if status in (429, 502, 503, 504) else 503,
                retryable=True,
                retry_after=retry_after,
                details={
                    "availability": "unknown",
                    "recovery_candidates": self._recovery_candidates(),
                    "state_unchanged": True,
                },
            ) from None
        return manifest["pack_id"]

    def activate(self, request, if_match):
        if not isinstance(request, dict):
            raise ActivationError("json_object_required")
        action = request.get("action", "activate")
        if action == "rollback":
            if set(request) != {"action"}:
                raise ActivationError("invalid_rollback_fields")
            return self.rollback(if_match)
        if action != "activate":
            raise ActivationError("invalid_activation_action")
        allowed = {"action", "catalog_id", "reference_id", "preset_id", "filler_policy"}
        if set(request) - allowed:
            raise ActivationError("invalid_activation_fields")
        catalog_id = request.get("catalog_id")
        reference_id = request.get("reference_id")
        if bool(catalog_id) == bool(reference_id) or any(
            value is not None and not isinstance(value, str)
            for value in (catalog_id, reference_id)
        ):
            raise ActivationError("exactly_one_voice_identifier_required")
        policy = request.get("filler_policy", FILLER_POLICY_REQUIRE_MATCHING)
        if policy not in (FILLER_POLICY_REQUIRE_MATCHING, FILLER_POLICY_VOICE_ONLY):
            raise ActivationError("invalid_filler_policy")
        preset_id = request.get("preset_id", "")
        if not isinstance(preset_id, str):
            raise ActivationError("invalid_preset_id")

        with self._operation_lock:
            current = None
            try:
                self._require_supported_engine()
                current = self.config.get()
                # Early check avoids paid staging for an already stale request;
                # commit_voice_pointers repeats it while holding the config lock.
                self._check_version(if_match, current)
                target = self._validate_target(
                    catalog_id=catalog_id, reference_id=reference_id
                )
                target["preset_id"] = preset_id.strip()
                effective_snapshot = self._effective_texts()
                self._write_journal({
                    "operation": "activate", "phase": "validated",
                    "target_reference_id": target["reference_id"], "started_at": self.clock(),
                })
                pack_id = self._matching_pack(target, effective_snapshot)
                if policy == FILLER_POLICY_REQUIRE_MATCHING and pack_id is None:
                    with self._runtime_lock:
                        self._generating = target["reference_id"]
                    try:
                        # The transport clamps timeout and attempts via
                        # CATY_VOICE_FILLER_TTS_*; text count and kinds are
                        # bounded before the serial activation stage begins.
                        pack_id = self._stage_matching_pack(target, effective_snapshot)
                    finally:
                        with self._runtime_lock:
                            self._generating = None
                self._write_journal({
                    "operation": "activate", "phase": "staged",
                    "target_reference_id": target["reference_id"],
                    "pack_id": pack_id or "", "started_at": self.clock(),
                })
                self._fault("activation_after_stage_before_commit", pack_id=pack_id)
                if pack_id:
                    status = "ready"
                elif current.get("voice_id"):
                    status = "stale"
                else:
                    status = "unavailable"
                fields = self._active_fields(target, pack_id, status, current)
                self._require_supported_engine()
                self._fault("activation_before_config_replace")
                try:
                    updated = self.config.commit_voice_pointers(fields, if_match)
                except caty_config.VersionConflict as error:
                    raise ActivationError(
                        "version_conflict", 409,
                        details={"config_version": error.current_version},
                    ) from None
                except caty_config.InvalidConfig as error:
                    raise ActivationError(str(error), 400) from None
                self._fault("activation_after_config_replace_before_gc")
                self._post_commit_retention(updated)
                return {"ok": True, "action": "activated", "state": self.state()}
            except ActivationError:
                raise
            except Exception:
                raise self._unexpected_operation_error(current) from None
            finally:
                self._clear_journal()

    def regenerate(self, *, force=False):
        """Regenerate managed fillers for the current voice without silencing it."""
        with self._operation_lock:
            current = None
            try:
                self._require_supported_engine()
                current = self.config.get()
                target = self._voice_from_config(current)
                if not target["reference_id"]:
                    raise ActivationError("voice_unavailable", 409)
                effective_snapshot = self._effective_texts()
                if (
                    effective_snapshot.override_status.startswith("invalid:")
                    and not force
                ):
                    raise ActivationError("override_invalid", 409)
                self._write_journal({
                    "operation": "regenerate", "phase": "validated",
                    "target_reference_id": target["reference_id"],
                    "started_at": self.clock(),
                })
                pack_id = self._matching_pack(target, effective_snapshot)
                if pack_id is None:
                    with self._runtime_lock:
                        self._generating = target["reference_id"]
                    try:
                        pack_id = self._stage_matching_pack(target, effective_snapshot)
                    finally:
                        with self._runtime_lock:
                            self._generating = None
                self._write_journal({
                    "operation": "regenerate", "phase": "staged",
                    "target_reference_id": target["reference_id"],
                    "pack_id": pack_id, "started_at": self.clock(),
                })
                if current.get("active_pack_id") != pack_id:
                    fields = self._active_fields(target, pack_id, "ready", current)
                    try:
                        updated = self.config.commit_voice_pointers(
                            fields, str(current["config_version"])
                        )
                    except caty_config.VersionConflict as error:
                        raise ActivationError(
                            "version_conflict", 409,
                            details={"config_version": error.current_version},
                        ) from None
                    except caty_config.InvalidConfig as error:
                        raise ActivationError(str(error), 400) from None
                    self._post_commit_retention(updated)
                self._last_filler_error = None
                return {"ok": True, "action": "regenerated", "state": self.state()}
            except ActivationError as error:
                self._last_filler_error = error.code
                raise
            except Exception:
                error = self._unexpected_operation_error(current)
                self._last_filler_error = error.code
                raise error from None
            finally:
                self._clear_journal()

    def _check_version(self, if_match, current):
        if if_match is None:
            raise ActivationError(
                "version_conflict", 409,
                details={"config_version": current["config_version"]},
            )
        try:
            expected = int(str(if_match).strip())
        except (TypeError, ValueError):
            raise ActivationError("invalid_if_match_header", 400) from None
        if expected != current["config_version"]:
            raise ActivationError(
                "version_conflict", 409,
                details={"config_version": current["config_version"]},
            )

    def _require_supported_engine(self):
        if self.engine_truth() != "fish":
            raise ActivationError("voice_activation_unsupported", 409)

    def _unexpected_operation_error(self, predecessor):
        details = {
            "availability": "unknown",
            "recovery_candidates": self._recovery_candidates(),
        }
        if isinstance(predecessor, dict):
            try:
                details["state_unchanged"] = (
                    self.config.get().get("config_version")
                    == predecessor.get("config_version")
                )
            except Exception:
                pass
        return ActivationError(
            "voice_activation_unknown", 503, retryable=True, details=details
        )

    def rollback(self, if_match):
        with self._operation_lock:
            current = None
            try:
                self._require_supported_engine()
                current = self.config.get()
                self._check_version(if_match, current)
                target = self._voice_from_config(current, "lkg_")
                if not target["reference_id"]:
                    raise ActivationError("lkg_unavailable", 409)
                try:
                    validated = self._validate_target(
                        catalog_id=target["catalog_id"] or None,
                        reference_id=(
                            None if target["catalog_id"] else target["reference_id"]
                        ),
                    )
                except ActivationError as error:
                    if error.details.get("availability") != "unknown":
                        raise
                    # A saved LKG is the recovery authority when live Fish
                    # validation is transiently unknown. Definite permanent
                    # unavailability is still rejected above.
                    validated = dict(target)
                    validated["availability"] = "unknown"
                validated["catalog_id"] = target["catalog_id"]
                validated["preset_id"] = target["preset_id"]
                if target["display_metadata"]:
                    validated["display_metadata"] = target["display_metadata"]
                pack_id = current.get("lkg_pack_id", "")
                status = current.get(
                    "lkg_filler_effective_status", "unavailable"
                )
                management_state = current.get(
                    "lkg_voice_management_state", "legacy"
                )
                if pack_id:
                    resolved = self.registry.resolve(
                        pack_id,
                        active_provider=validated["provider"],
                        active_reference_id=validated["reference_id"],
                    )
                    if resolved.get("status") not in ("ready", "legacy-unknown"):
                        raise ActivationError("lkg_unavailable", 409)
                    status = resolved["status"]
                self._write_journal({
                    "operation": "rollback", "phase": "ready", "pack_id": pack_id,
                    "target_reference_id": validated["reference_id"], "started_at": self.clock(),
                })
                fields = self._active_fields(
                    validated, pack_id, status, current,
                    management_state=management_state,
                )
                self._require_supported_engine()
                self._fault("rollback_before_config_replace")
                try:
                    updated = self.config.commit_voice_pointers(fields, if_match)
                except caty_config.VersionConflict as error:
                    raise ActivationError(
                        "version_conflict", 409,
                        details={"config_version": error.current_version},
                    ) from None
                self._post_commit_retention(updated)
                return {"ok": True, "action": "rolled_back", "state": self.state()}
            except ActivationError:
                raise
            except Exception:
                raise self._unexpected_operation_error(current) from None
            finally:
                self._clear_journal()

    def _post_commit_retention(self, config):
        active = config.get("active_pack_id", "")
        lkg = config.get("lkg_pack_id", "")
        try:
            if lkg:
                self.registry.pin_lkg(lkg)
            protected = {pack_id for pack_id in (active, lkg) if pack_id}
            candidates = [
                item["pack_id"] for item in self.registry.list_packs()
                if item.get("pack_id") not in protected
            ]
            self.registry.garbage_collect(
                candidates, protected_pack_ids=protected
            )
        except Exception:
            # Pointer commit already succeeded. Retention is deliberately
            # best-effort and a failure here must not misreport activation as
            # failed or attempt a second compensating config write.
            LOGGER.exception("voice activation retention failed after pointer commit")

    def _recovery_candidates(self):
        config = self.config.get()
        candidates = ["reselect", "neutral"]
        if config.get("lkg_voice_reference_id"):
            candidates.append("last-known-good")
        return candidates

    def state(self):
        config = self.config.get()
        effective_snapshot = self._effective_texts()
        engine = self.engine_truth()
        active = self._voice_from_config(config)
        with self._runtime_lock:
            generating = self._generating
        filler_status = config.get("filler_effective_status", "legacy-unknown")
        pack_id = config.get("active_pack_id", "")
        active_text_version = None
        text_stale = False
        if pack_id:
            resolved = self.registry.resolve(
                pack_id,
                active_provider=active["provider"],
                active_reference_id=active["reference_id"],
            )
            resolved_status = resolved.get("status", "unavailable")
            text_matches, active_text_version = self._pack_text_matches(
                pack_id, effective_snapshot
            )
            text_stale = active_text_version is not None and not text_matches
        if generating:
            filler_status = "generating"
        elif pack_id:
            filler_status = resolved_status
        elif config.get("voice_management_state") == "legacy":
            filler_status = "legacy-unknown"
        lkg_voice = self._voice_from_config(config, "lkg_")
        lkg_pack = config.get("lkg_pack_id", "")
        lkg_available = bool(lkg_voice["reference_id"])
        if lkg_available and lkg_pack:
            lkg_available = self.registry.resolve(
                lkg_pack,
                active_provider=lkg_voice["provider"],
                active_reference_id=lkg_voice["reference_id"],
            ).get("status") in ("ready", "legacy-unknown")
        return {
            "active": active,
            "availability": active["availability"],
            "checked_at": active["checked_at"] or None,
            "voice_ready": bool(active["reference_id"] and active["availability"] in ("available", "hidden")),
            "config_version": config["config_version"],
            "fillers_version": config["fillers_version"],
            "filler": {
                "effective_status": filler_status,
                "pack_id": pack_id or None,
                "desired_text_version": effective_snapshot.version,
                "active_text_version": active_text_version,
                "text_stale": text_stale,
                "fallback_reason": (
                    "override_invalid"
                    if effective_snapshot.override_status.startswith("invalid:")
                    else None
                ),
                "last_error": self._last_filler_error,
            },
            "last_known_good": {
                "available": lkg_available,
                "voice_available": bool(lkg_voice["reference_id"]),
                "pack_available": bool(lkg_pack and lkg_available),
            },
            "picker": {
                "capable": engine == "fish",
                "activation_action": "activate",
                "rollback_action": "rollback",
                "supported_scopes": list(SUPPORTED_SCOPES),
            },
            "engine": engine,
            "inference_contract_version": self.inference_contract_version(),
        }

    def filler_audio(self, config=None):
        config = self.config.get() if config is None else config
        if config.get("voice_management_state") == "legacy":
            return None
        pack_id = config.get("active_pack_id", "")
        if not pack_id:
            return {"status": config.get("filler_effective_status", "unavailable"), "audio": None}
        return self.registry.read_audio(
            pack_id,
            active_provider=config.get("voice_provider", ""),
            active_reference_id=config.get("voice_reference_id", ""),
        )
