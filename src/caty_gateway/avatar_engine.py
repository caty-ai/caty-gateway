"""Async avatar generation engine for gateway callers.

This module owns network access and job orchestration.  Deterministic image
processing and drift decisions live in face_core so the CLI and gateway engine
share the same behavior.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from caty_gateway import caty_config as gateway_config
from caty_gateway.face_core import (
    CHAINED_FRAMES,
    DEFAULT_PROMPTS,
    DIRECT_FRAMES,
    EXPRESSION_SLOTS,
    GATEWAY_SLOTS,
    GENERATION_MODEL,
    FrameConfig,
    MemberConfig,
    Thresholds,
    bbox_warnings,
    generate_frame_with_retries,
    json_safe,
    make_contact_sheet,
    make_icon,
    postprocess_expression,
    write_report,
)


DEFAULT_POYO_BASE = "https://api.poyo.ai"
DEFAULT_RENOISE_BASE_URL = "https://www.renoise.ai/api/public/v1"
POYO_SUBMIT_PATH = "/api/generate/submit"
POYO_STATUS_PATH = "/api/generate/status/{task_id}"
# The renoise WAF rejects urllib's default "Python-urllib/3.x" UA with 403
# A custom user agent avoids provider-side default-client filtering.
USER_AGENT = "caty-gateway/1.0"

# Every avatar-flow base is soft pixel art (STYLIZE.md canon). Faithful 2K
# pixel-art edits compress to ~1-3.5MB, so the CLI's watercolor default floor
# (4MB) falsely flags nearly every candidate as drift and burns the whole
# retry budget (#254, field-observed: ~35min per set). 400KB is the proven
# pixel-art calibration used by the gateway defaults.
PIXEL_ART_THRESHOLDS = Thresholds(size_floor_bytes=400_000)

MAX_DOWNLOAD_BYTES = 30 * 1024 * 1024
UNKNOWN_STATUS_LIMIT = 20
AVATAR_PASS_KINDS = frozenset({"stylize", "set"})
GENERIC_IDENTITY_DESCRIPTION = (
    "the same visible person or character details from image 1, including face, hair, outfit, "
    "expression, accessories, and any held items"
)
STYLIZE_PROMPT_TEMPLATE = (
    "Redraw the character from image 1 as a full-body chibi pixel art illustration\n"
    "in the same soft pixel art style as image 2, but with an even finer pixel\n"
    "grid: noticeably smaller pixels, higher pixel density, smoother anti-aliased\n"
    "edges and softer color transitions than image 2, while keeping the same\n"
    "gentle pastel mood. Keep the character's identity from image 1:\n"
    "{identity_description}. Standing pose facing the viewer. Plain white\n"
    "background with a few tiny pastel sparkles. No watercolor texture, no circuit\n"
    "motifs, no text."
)


class AvatarEngineDisabled(RuntimeError):
    """Raised when required avatar-generation credentials are not configured."""


class AvatarEngineBusy(RuntimeError):
    """Raised when another avatar job is still active in this process."""


class AvatarJobStateError(RuntimeError):
    """Raised when a job method is called at the wrong approval stage."""


class AvatarCredentialConflict(AvatarJobStateError):
    """Raised when a caller tries to replace credentials during an active pass."""


def _read_json_response(response: Any) -> dict[str, Any]:
    raw = response.read().decode("utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise RuntimeError("API response must be a JSON object")
    return payload


def _vendor_host_allowlist() -> tuple[str, ...]:
    return tuple(gateway_config.avatar_vendor_host_allowlist())


def _validate_vendor_download_url(url: str) -> None:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("avatar result URL is invalid") from exc
    host = (parsed.hostname or "").lower()
    allowed = any(host == suffix or host.endswith("." + suffix) for suffix in _vendor_host_allowlist())
    if (
        parsed.scheme.lower() != "https"
        or not allowed
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
    ):
        raise RuntimeError("avatar result URL must use HTTPS on an allow-listed vendor host")


class PoyoClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        urlopen: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        poll_interval: float = 5.0,
        timeout: float = 600.0,
        retry_attempts: int = 3,
        retry_backoff: float = 1.0,
        max_download_bytes: int = MAX_DOWNLOAD_BYTES,
        unknown_status_limit: int = UNKNOWN_STATUS_LIMIT,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get("POYO_API_KEY")
        if not self.api_key:
            raise AvatarEngineDisabled("POYO_API_KEY is required for avatar generation")
        self.base_url = (base_url or os.environ.get("POYO_BASE") or DEFAULT_POYO_BASE).rstrip("/")
        self.urlopen = urlopen or urllib.request.urlopen
        self.sleep = sleep
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.retry_attempts = max(1, int(retry_attempts))
        self.retry_backoff = retry_backoff
        self.max_download_bytes = max_download_bytes
        self.unknown_status_limit = max(1, int(unknown_status_limit))

    def submit(self, prompt: str, image_urls: list[str]) -> str:
        payload = {
            "model": GENERATION_MODEL,
            "input": {
                "prompt": prompt,
                "image_urls": list(image_urls),
                "size": "1:1",
                "resolution": "2K",
            },
        }
        def make_request() -> urllib.request.Request:
            return urllib.request.Request(
                self.base_url + POYO_SUBMIT_PATH,
                data=json.dumps(payload).encode("utf-8"),
                method="POST",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": USER_AGENT,
                },
            )

        # A submit POST is a paid, non-idempotent operation: a lost response may
        # mean the task was already queued, so a blind retry can double-bill.
        # Only 5xx (and one 429) are retried; URLError/timeout surface at once.
        data = self._request_json(make_request, timeout=60, retry_429_once=True, retry_url_errors=False)
        task_id = (data.get("data") or {}).get("task_id")
        if not task_id:
            raise RuntimeError(f"poyo submit response missing data.task_id: {data}")
        return str(task_id)

    def status(self, task_id: str) -> dict[str, Any]:
        def make_request() -> urllib.request.Request:
            return urllib.request.Request(
                self.base_url + POYO_STATUS_PATH.format(task_id=task_id),
                method="GET",
                headers={"Authorization": f"Bearer {self.api_key}", "User-Agent": USER_AGENT},
            )

        return self._request_json(make_request, timeout=60)

    def poll(self, task_id: str) -> list[str]:
        deadline = time.monotonic() + self.timeout
        unknown_count = 0
        while True:
            payload = self.status(task_id)
            data = payload.get("data") or {}
            if not isinstance(data, dict):
                raise RuntimeError(f"poyo status response data must be an object: {payload}")
            status = str(data.get("status", "")).lower()
            if status in {"finished", "succeeded", "success"}:
                urls = self._result_urls(data)
                if not urls:
                    raise RuntimeError(f"poyo task {task_id} finished without result file URLs")
                return urls
            if status in {"failed", "error", "cancelled", "canceled"}:
                message = data.get("error_message") or data.get("message") or f"poyo task {task_id} failed"
                raise RuntimeError(str(message))
            if status in {"pending", "queued", "running", "processing", "generating", "submitted", "in_progress"}:
                unknown_count = 0
            else:
                unknown_count += 1
                if unknown_count >= self.unknown_status_limit:
                    raise RuntimeError(
                        f"poyo task {task_id} returned unknown status {status!r} "
                        f"{unknown_count} consecutive times"
                    )
            if time.monotonic() >= deadline:
                raise TimeoutError(f"poyo task {task_id} did not finish within {self.timeout:.0f}s")
            self.sleep(self.poll_interval)

    def download(self, url: str, output_path: Path) -> None:
        _validate_vendor_download_url(url)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        def make_request() -> urllib.request.Request:
            return urllib.request.Request(url, method="GET", headers={"User-Agent": USER_AGENT})

        self._download_with_retries(make_request, output_path, timeout=120)

    def generate(self, prompt: str, image_urls: list[str], output_path: Path) -> str:
        task_id = self.submit(prompt, image_urls)
        urls = self.poll(task_id)
        self.download(urls[0], output_path)
        return urls[0]

    @staticmethod
    def _result_urls(data: dict[str, Any]) -> list[str]:
        urls: list[str] = []
        files = data.get("files") or []
        if not isinstance(files, list):
            return urls
        for item in files:
            if not isinstance(item, dict):
                continue
            url = item.get("file_url") or item.get("url") or item.get("image_url")
            if url:
                urls.append(str(url))
        return urls

    def _request_json(
        self,
        request_factory: Callable[[], urllib.request.Request],
        timeout: float,
        retry_429_once: bool = False,
        retry_url_errors: bool = True,
    ) -> dict[str, Any]:
        used_429_retry = False
        last_exc: BaseException | None = None
        for attempt in range(self.retry_attempts):
            try:
                with self.urlopen(request_factory(), timeout=timeout) as response:
                    return _read_json_response(response)
            except urllib.error.HTTPError as exc:
                last_exc = exc
                if retry_429_once and exc.code == 429 and not used_429_retry and attempt + 1 < self.retry_attempts:
                    used_429_retry = True
                    self.sleep(15)
                    continue
                if 500 <= exc.code <= 599 and attempt + 1 < self.retry_attempts:
                    self.sleep(self._retry_delay(attempt))
                    continue
                raise
            except urllib.error.URLError as exc:
                last_exc = exc
                if retry_url_errors and attempt + 1 < self.retry_attempts:
                    self.sleep(self._retry_delay(attempt))
                    continue
                raise
        assert last_exc is not None
        raise last_exc

    def _download_with_retries(
        self,
        request_factory: Callable[[], urllib.request.Request],
        output_path: Path,
        timeout: float,
    ) -> None:
        last_exc: BaseException | None = None
        for attempt in range(self.retry_attempts):
            try:
                total = 0
                with self.urlopen(request_factory(), timeout=timeout) as response, output_path.open("wb") as handle:
                    while True:
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            return
                        total += len(chunk)
                        if total > self.max_download_bytes:
                            raise RuntimeError(
                                f"poyo download exceeded maximum size of {self.max_download_bytes} bytes"
                            )
                        handle.write(chunk)
            except urllib.error.HTTPError as exc:
                last_exc = exc
                output_path.unlink(missing_ok=True)
                if 500 <= exc.code <= 599 and attempt + 1 < self.retry_attempts:
                    self.sleep(self._retry_delay(attempt))
                    continue
                raise
            except urllib.error.URLError as exc:
                last_exc = exc
                output_path.unlink(missing_ok=True)
                if attempt + 1 < self.retry_attempts:
                    self.sleep(self._retry_delay(attempt))
                    continue
                raise
            except Exception:
                output_path.unlink(missing_ok=True)
                raise
        assert last_exc is not None
        raise last_exc

    def _retry_delay(self, attempt: int) -> float:
        return float(self.retry_backoff) * (2**attempt)


class RenoiseClient:
    def __init__(
        self,
        api_key: str | None = None,
        auth_token: str | None = None,
        base_url: str | None = None,
        urlopen: Callable[..., Any] | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get("RENOISE_API_KEY")
        self.auth_token = auth_token if auth_token is not None else os.environ.get("RENOISE_AUTH_TOKEN")
        if not self.api_key and not self.auth_token:
            raise AvatarEngineDisabled("RENOISE_API_KEY or RENOISE_AUTH_TOKEN is required for avatar generation")
        self.base_url = (base_url or os.environ.get("RENOISE_BASE_URL") or DEFAULT_RENOISE_BASE_URL).rstrip("/")
        self.urlopen = urlopen or urllib.request.urlopen

    def upload(self, file_path: str | Path, material_type: str = "image") -> str:
        path = Path(file_path)
        return self.upload_bytes(path.read_bytes(), path.name, material_type)

    def upload_bytes(self, data: bytes, filename: str, material_type: str = "image") -> str:
        if material_type not in {"image", "video"}:
            raise ValueError("material_type must be one of: image, video")
        filename = _sanitize_multipart_filename(filename)
        content_type = _sniff_content_type(data)
        boundary = "----caty-avatar-" + uuid.uuid4().hex
        body = bytearray()
        body.extend(f"--{boundary}\r\n".encode("ascii"))
        body.extend(
            (
                f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8")
        )
        body.extend(data)
        body.extend(b"\r\n")
        body.extend(f"--{boundary}\r\n".encode("ascii"))
        body.extend(b'Content-Disposition: form-data; name="type"\r\n\r\n')
        body.extend(material_type.encode("utf-8"))
        body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode("ascii"))

        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": USER_AGENT,
        }
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        else:
            headers["X-API-Key"] = self.api_key
        request = urllib.request.Request(
            self.base_url + "/materials/upload",
            data=bytes(body),
            method="POST",
            headers=headers,
        )
        with self.urlopen(request, timeout=120) as response:
            payload = _read_json_response(response)
        url = payload.get("downloadUrl")
        if not url:
            raise RuntimeError(f"renoise upload response missing top-level downloadUrl: {payload}")
        return str(url)


def _sanitize_multipart_filename(filename: str) -> str:
    cleaned = "".join("_" if char in {'"', "\r", "\n"} else char for char in filename)
    return cleaned or "upload"


def _sniff_content_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    return "application/octet-stream"


@dataclass(frozen=True)
class AvatarPassClients:
    """Credential-bound clients for exactly one avatar generation pass."""

    kind: str
    poyo: Any = field(repr=False)
    renoise: Any = field(repr=False)
    credential_id: str = field(repr=False)
    source: str

    def __post_init__(self) -> None:
        if self.kind not in AVATAR_PASS_KINDS:
            raise ValueError(f"unsupported avatar pass kind: {self.kind}")

    @classmethod
    def from_environment(cls, kind: str) -> "AvatarPassClients":
        poyo = PoyoClient()
        renoise = RenoiseClient()
        return cls(
            kind=kind,
            poyo=poyo,
            renoise=renoise,
            credential_id=_credential_id(
                "byok",
                poyo.base_url,
                poyo.api_key,
                renoise.base_url,
                renoise.auth_token or "",
                renoise.api_key or "",
            ),
            source="byok",
        )

    @classmethod
    def from_cloud(cls, kind: str, cloud_origin: str, token: str) -> "AvatarPassClients":
        avatar_base = cloud_origin.rstrip("/") + "/v1/avatar"
        return cls(
            kind=kind,
            poyo=PoyoClient(base_url=avatar_base, api_key=token),
            renoise=RenoiseClient(base_url=avatar_base, auth_token=token),
            credential_id=_credential_id("cloud", cloud_origin, token),
            source="cloud",
        )


def _credential_id(*parts: Any) -> str:
    digest = hashlib.sha256()
    for part in parts:
        encoded = str(part).encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()


@dataclass
class AvatarJob:
    job_id: str
    work_dir: Path
    stage: str = "idle"
    error: str | None = None
    slots: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {slot: {"status": "pending", "attempts": 0} for slot in GATEWAY_SLOTS}
    )
    paths: dict[str, Any] = field(default_factory=lambda: {"final_pngs": {}})
    report: dict[str, Any] = field(default_factory=dict)
    identity_image_bytes: bytes = b""
    identity_description: str | None = None
    base_prompt_description: str = GENERIC_IDENTITY_DESCRIPTION
    pass_clients: AvatarPassClients | None = field(default=None, repr=False)
    pass_kind: str | None = field(default=None, repr=False)
    pass_credential_id: str | None = field(default=None, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)

    def start_thread(self, target: Callable[[], None]) -> None:
        thread = threading.Thread(target=target, name=f"caty-avatar-{self.job_id}", daemon=True)
        with self._lock:
            self._thread = thread
        thread.start()

    def wait(self, timeout: float | None = None) -> bool:
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json_safe(
                {
                    "job_id": self.job_id,
                    "stage": self.stage,
                    "error": self.error,
                    "slots": self.slots,
                    "paths": self.paths,
                }
            )


_active_lock = threading.Lock()
_active_job: AvatarJob | None = None


class AvatarEngine:
    def __init__(
        self,
        work_dir: str | Path | None = None,
        poyo_client: PoyoClient | Any | None = None,
        renoise_client: RenoiseClient | Any | None = None,
        pass_client_factory: Callable[[str], AvatarPassClients] | None = None,
        member_name: str | None = None,
        thresholds: Thresholds | None = None,
        style_ref_path: str | Path | None = None,
    ) -> None:
        raw_style_ref = (
            style_ref_path
            if style_ref_path is not None
            else os.environ.get("CATY_AVATAR_STYLE_REF") or None
        )
        self.work_root = Path(work_dir or os.environ.get("CATY_AVATAR_WORKDIR") or (Path(tempfile.gettempdir()) / "caty-avatar-engine"))
        self.work_root.mkdir(parents=True, exist_ok=True)
        if pass_client_factory is not None and (poyo_client is not None or renoise_client is not None):
            raise ValueError("pass_client_factory cannot be combined with legacy client injection")
        if pass_client_factory is not None:
            self._pass_client_factory = pass_client_factory
        elif poyo_client is not None or renoise_client is not None:
            def injected_client_factory(kind: str) -> AvatarPassClients:
                poyo = poyo_client if poyo_client is not None else PoyoClient()
                renoise = renoise_client if renoise_client is not None else RenoiseClient()
                return AvatarPassClients(
                    kind=kind,
                    poyo=poyo,
                    renoise=renoise,
                    # Test/embedding injection has no safe generic way to inspect
                    # arbitrary client credentials. Treat the legacy seam as one
                    # stable source; production BYOK/cloud bundles are fingerprinted.
                    credential_id=_credential_id("injected"),
                    source="injected",
                )

            self._pass_client_factory = injected_client_factory
        else:
            self._pass_client_factory = AvatarPassClients.from_environment
        self.member_name = member_name or os.environ.get("CATY_NAME") or "Member"
        self.thresholds = thresholds or PIXEL_ART_THRESHOLDS
        self.style_ref_path = Path(raw_style_ref).expanduser() if raw_style_ref else None
        self._job: AvatarJob | None = None

    def start_stylize(
        self,
        image_bytes: bytes,
        identity_description: str | None = None,
        pass_clients: AvatarPassClients | None = None,
    ) -> AvatarJob:
        if self.style_ref_path is None:
            raise AvatarEngineDisabled("CATY_AVATAR_STYLE_REF is required for avatar generation")
        if not self.style_ref_path.exists():
            raise AvatarEngineDisabled(f"avatar style reference image not found: {self.style_ref_path}")
        clients = self._resolve_pass_clients("stylize", pass_clients)
        job = AvatarJob(job_id=uuid.uuid4().hex, work_dir=self.work_root / uuid.uuid4().hex)
        job.identity_image_bytes = image_bytes
        job.base_prompt_description = identity_description or GENERIC_IDENTITY_DESCRIPTION
        self._bind_pass(job, clients)
        job.work_dir.mkdir(parents=True, exist_ok=True)
        self._claim_job(job, "stylizing")
        job.start_thread(lambda: self._run_stylize(job))
        return job

    def regenerate_base(self, pass_clients: AvatarPassClients | None = None) -> AvatarJob:
        job = self._require_job()
        clients = self._resolve_pass_clients("stylize", pass_clients)
        with job._lock:
            if self._job is not job:
                raise AvatarJobStateError("avatar job was cancelled")
            if job.stage != "awaiting_base_approval":
                self._reject_credential_swap(job, clients)
                raise AvatarJobStateError(f"cannot regenerate base while stage is {job.stage}")
            self._bind_pass_unlocked(job, clients)
            job.stage = "stylizing"
            job.error = None
            job.paths.pop("base_candidate_512", None)
            job.paths.pop("base_candidate_raw", None)
        job.start_thread(lambda: self._run_stylize(job))
        return job

    def approve_base(
        self,
        identity_description: str,
        pass_clients: AvatarPassClients | None = None,
    ) -> AvatarJob:
        job = self._require_job()
        clients = self._resolve_pass_clients("set", pass_clients)
        with job._lock:
            if self._job is not job:
                raise AvatarJobStateError("avatar job was cancelled")
            if job.stage != "awaiting_base_approval":
                self._reject_credential_swap(job, clients)
                raise AvatarJobStateError(f"cannot approve base while stage is {job.stage}")
            self._bind_pass_unlocked(job, clients)
            job.stage = "generating"
            job.identity_description = identity_description
            for slot in GATEWAY_SLOTS:
                job.slots[slot] = {"status": "pending", "attempts": 0}
            job.error = None
        job.start_thread(lambda: self._run_generate_set(job))
        return job

    def regenerate_set(self, pass_clients: AvatarPassClients | None = None) -> AvatarJob:
        job = self._require_job()
        clients = self._resolve_pass_clients("set", pass_clients)
        with job._lock:
            if self._job is not job:
                raise AvatarJobStateError("avatar job was cancelled")
            if job.stage != "awaiting_set_approval":
                self._reject_credential_swap(job, clients)
                raise AvatarJobStateError(f"cannot regenerate set while stage is {job.stage}")
            self._bind_pass_unlocked(job, clients)
            job.stage = "generating"
            for slot in GATEWAY_SLOTS:
                job.slots[slot] = {"status": "pending", "attempts": 0}
            job.error = None
        job.start_thread(lambda: self._run_generate_set(job))
        return job

    def approve_set(self) -> AvatarJob:
        job = self._require_job()
        with job._lock:
            if self._job is not job:
                raise AvatarJobStateError("avatar job was cancelled")
            if job.stage != "awaiting_set_approval":
                raise AvatarJobStateError(f"cannot approve set while stage is {job.stage}")
            job.stage = "done"
        self._release_job(job)
        return job

    def cancel(self) -> None:
        global _active_job
        with _active_lock:
            job = self._job
            if job is None:
                raise AvatarJobStateError("no avatar job has been started")
            with job._lock:
                if job.stage not in {"awaiting_base_approval", "awaiting_set_approval", "failed", "done"}:
                    raise AvatarJobStateError(f"cannot cancel while stage is {job.stage}")
                self._clear_pass_unlocked(job)
                if self._job is job:
                    self._job = None
                if _active_job is job:
                    _active_job = None

    def snapshot(self) -> dict[str, Any]:
        job = self._job
        if job is None:
            return {"stage": "idle", "slots": {}, "paths": {}}
        return job.snapshot()

    def assert_pass_credentials(self, clients: AvatarPassClients) -> None:
        job = self._require_job()
        with job._lock:
            self._reject_credential_swap(job, clients)

    def _run_stylize(self, job: AvatarJob) -> None:
        try:
            with job._lock:
                if job.stage != "stylizing":
                    raise AvatarJobStateError(f"cannot stylize while stage is {job.stage}")
                job.error = None
                clients = self._pass_clients_for_worker_unlocked(job, "stylize")
            source_path = job.work_dir / "source.png"
            source_path.write_bytes(job.identity_image_bytes)
            identity_url = clients.renoise.upload_bytes(job.identity_image_bytes, "identity.png")
            style_url = clients.renoise.upload(self.style_ref_path)
            prompt = STYLIZE_PROMPT_TEMPLATE.format(identity_description=job.base_prompt_description)
            base_raw = job.work_dir / "base-candidate.2k.png"
            clients.poyo.generate(prompt, [identity_url, style_url], base_raw)
            base_preview = job.work_dir / "base-candidate.png"
            make_icon(base_raw, base_preview)
            with job._lock:
                job.stage = "awaiting_base_approval"
                job.paths["source"] = source_path
                job.paths["base_candidate_raw"] = base_raw
                job.paths["base_candidate_512"] = base_preview
                self._clear_pass_unlocked(job)
        except Exception as exc:  # noqa: BLE001 - jobs report errors through snapshots.
            self._fail_job(job, exc)

    def _run_generate_set(self, job: AvatarJob) -> None:
        try:
            with job._lock:
                if job.stage != "generating":
                    raise AvatarJobStateError(f"cannot generate set while stage is {job.stage}")
                job.error = None
                clients = self._pass_clients_for_worker_unlocked(job, "set")
            base_raw = Path(job.paths["base_candidate_raw"])
            # Use a fresh directory per pass. Generation writes are not guarded by
            # job._lock, so reusing "set" can expose half-written files through
            # snapshot paths from a previous awaiting_set_approval pass.
            output_dir = job.work_dir / f"set-{uuid.uuid4().hex}"
            output_dir.mkdir(parents=True, exist_ok=True)
            config = MemberConfig(
                name=self.member_name,
                base_image=base_raw,
                character_description=job.identity_description or GENERIC_IDENTITY_DESCRIPTION,
                frames={slot: FrameConfig(prompt=DEFAULT_PROMPTS[slot]) for slot in EXPRESSION_SLOTS},
                thresholds=self.thresholds,
            )
            accepted_clear_rates: list[float] = []
            fallback_slots: list[str] = []
            report: dict[str, Any] = {"frames": {}, "icon": {}, "fallback_slots": fallback_slots}

            self._set_slot(job, "icon", "running", attempts=0)
            icon_path = output_dir / "icon.png"
            report["icon"] = make_icon(base_raw, icon_path)
            self._set_slot(job, "icon", "done", attempts=1, path=icon_path)

            base_ref = clients.renoise.upload(base_raw)
            refs = {"base_image": base_ref}
            chained_source_slots = set(CHAINED_FRAMES.values())

            for slot in DIRECT_FRAMES:
                record = self._generate_slot(
                    job, clients.poyo, config, slot, refs["base_image"], output_dir, accepted_clear_rates
                )
                raw = Path(record["accepted_raw"])
                stats = postprocess_expression(raw, output_dir / f"{slot}.png", config.thresholds.floodfill_threshold)
                if record.get("fallback"):
                    fallback_slots.append(slot)
                else:
                    accepted_clear_rates.append(float(record["clear_rate_pct"]))
                if slot in chained_source_slots:
                    refs[slot] = clients.renoise.upload(raw)
                report["frames"][slot] = {**record, **stats, "path": str(record["path"]), "accepted_raw": str(raw)}
                self._set_slot(job, slot, "done", attempts=int(record["attempt"]), path=output_dir / f"{slot}.png")

            for slot in ("talk2", "talk3"):
                record = self._generate_slot(
                    job, clients.poyo, config, slot, refs["talk1"], output_dir, accepted_clear_rates
                )
                raw = Path(record["accepted_raw"])
                stats = postprocess_expression(raw, output_dir / f"{slot}.png", config.thresholds.floodfill_threshold)
                if record.get("fallback"):
                    fallback_slots.append(slot)
                else:
                    accepted_clear_rates.append(float(record["clear_rate_pct"]))
                report["frames"][slot] = {**record, **stats, "path": str(record["path"]), "accepted_raw": str(raw)}
                self._set_slot(job, slot, "done", attempts=int(record["attempt"]), path=output_dir / f"{slot}.png")

            slot = "talk_blink"
            record = self._generate_slot(
                job, clients.poyo, config, slot, refs["blink"], output_dir, accepted_clear_rates
            )
            raw = Path(record["accepted_raw"])
            stats = postprocess_expression(raw, output_dir / f"{slot}.png", config.thresholds.floodfill_threshold)
            if record.get("fallback"):
                fallback_slots.append(slot)
            else:
                accepted_clear_rates.append(float(record["clear_rate_pct"]))
            report["frames"][slot] = {**record, **stats, "path": str(record["path"]), "accepted_raw": str(raw)}
            self._set_slot(job, slot, "done", attempts=int(record["attempt"]), path=output_dir / f"{slot}.png")

            contact_sheet = output_dir / "contact-sheet.png"
            make_contact_sheet(output_dir, contact_sheet)
            report["contact_sheet"] = str(contact_sheet)
            report["bbox_warnings"] = bbox_warnings(report)
            write_report(output_dir / "report.json", report)
            with job._lock:
                job.report = report
                job.paths["contact_sheet"] = contact_sheet
                job.paths["final_pngs"] = {slot: output_dir / f"{slot}.png" for slot in GATEWAY_SLOTS}
                job.stage = "awaiting_set_approval"
                self._clear_pass_unlocked(job)
        except Exception as exc:  # noqa: BLE001 - jobs report errors through snapshots.
            self._fail_job(job, exc)

    def _generate_slot(
        self,
        job: AvatarJob,
        poyo: Any,
        config: MemberConfig,
        slot: str,
        ref_url: str,
        output_dir: Path,
        accepted_clear_rates: list[float],
    ) -> dict[str, Any]:
        self._set_slot(job, slot, "running", attempts=0)

        def generate_candidate(slot: str, prompt: str, raw_path: Path, attempt: int, candidate: int) -> None:
            self._set_slot(job, slot, "running", attempts=attempt)
            poyo.generate(prompt, [ref_url], raw_path)

        def decision(record: dict[str, Any]) -> None:
            with job._lock:
                job.slots[slot].update(
                    {
                        "attempts": max(int(job.slots[slot].get("attempts", 0)), int(record["attempt"])),
                        "last_candidate": int(record["candidate"]),
                        "last_reason": record["reason"],
                    }
                )

        return generate_frame_with_retries(config, slot, output_dir, accepted_clear_rates, generate_candidate, decision)

    def _set_slot(self, job: AvatarJob, slot: str, status: str, attempts: int | None = None, path: Path | None = None) -> None:
        with job._lock:
            info = job.slots.setdefault(slot, {})
            info["status"] = status
            if attempts is not None:
                info["attempts"] = attempts
            if path is not None:
                info["path"] = path

    def _claim_job(self, job: AvatarJob, stage: str) -> None:
        global _active_job
        with _active_lock:
            with job._lock:
                if _active_job is not None and _active_job.stage not in {"done", "failed"}:
                    raise AvatarEngineBusy("another avatar generation job is already active")
                job.stage = stage
                job.error = None
                _active_job = job
                self._job = job

    def _release_job(self, job: AvatarJob) -> None:
        global _active_job
        with _active_lock:
            if _active_job is job:
                _active_job = None

    def _fail_job(self, job: AvatarJob, exc: Exception) -> None:
        with job._lock:
            job.stage = "failed"
            job.error = str(exc)
            self._clear_pass_unlocked(job)
        self._release_job(job)

    def _require_job(self) -> AvatarJob:
        if self._job is None:
            raise AvatarJobStateError("no avatar job has been started")
        return self._job

    def _resolve_pass_clients(
        self,
        kind: str,
        supplied: AvatarPassClients | None,
    ) -> AvatarPassClients:
        clients = supplied if supplied is not None else self._pass_client_factory(kind)
        if clients.kind != kind:
            raise AvatarJobStateError(
                f"avatar pass requires {kind} credentials, got {clients.kind}"
            )
        return clients

    @staticmethod
    def _bind_pass(job: AvatarJob, clients: AvatarPassClients) -> None:
        with job._lock:
            AvatarEngine._bind_pass_unlocked(job, clients)

    @staticmethod
    def _bind_pass_unlocked(job: AvatarJob, clients: AvatarPassClients) -> None:
        job.pass_clients = clients
        job.pass_kind = clients.kind
        job.pass_credential_id = clients.credential_id

    @staticmethod
    def _clear_pass_unlocked(job: AvatarJob) -> None:
        job.pass_clients = None
        job.pass_kind = None
        job.pass_credential_id = None

    @staticmethod
    def _pass_clients_for_worker_unlocked(job: AvatarJob, kind: str) -> AvatarPassClients:
        clients = job.pass_clients
        if clients is None or job.pass_kind != kind:
            raise AvatarJobStateError(f"avatar {kind} pass has no bound clients")
        return clients

    @staticmethod
    def _reject_credential_swap(job: AvatarJob, clients: AvatarPassClients | None) -> None:
        if clients is None or job.pass_credential_id is None:
            return
        if not hmac.compare_digest(job.pass_credential_id, clients.credential_id):
            raise AvatarCredentialConflict("avatar credentials cannot change while a pass is in flight")
