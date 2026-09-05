"""Pure face-frame generation logic shared by the CLI and avatar engine."""

from __future__ import annotations

import json
import math
import os
import shutil
import statistics
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable

if TYPE_CHECKING:
    from PIL import Image


class PillowRequiredError(RuntimeError):
    """Raised when avatar image processing is used without Pillow installed."""


def _require_pillow() -> tuple[Any, Any]:
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise PillowRequiredError("Pillow not installed; install gateway[avatar]") from exc
    return Image, ImageDraw


GENERATION_MODEL = "nano-banana-pro-edit"
FORBIDDEN_MODEL = "nano-banana-2"
GATEWAY_SLOTS = ("icon", "idle", "talk1", "talk2", "talk3", "blink", "talk_blink", "listen")
EXPRESSION_SLOTS = ("idle", "talk1", "talk2", "talk3", "blink", "talk_blink", "listen")
DIRECT_FRAMES = ("idle", "talk1", "blink", "listen")
CHAINED_FRAMES = {"talk2": "talk1", "talk3": "talk1", "talk_blink": "blink"}

DEFAULT_SIZE_FLOOR_BYTES = 4_000_000
DEFAULT_DEVIATION_PCT = 8.0
DEFAULT_RETRY_COUNT = 3
DEFAULT_FLOODFILL_THRESHOLD = 40
SENTINEL = (255, 0, 255)

DEFAULT_KEEP_PROMPT = (
    "Keep the exact same framing, pose, art style, proportions, colors, lighting, "
    "and background 100% IDENTICAL to the reference. Do NOT redraw or restyle the "
    "character. Do not add outlines or change the background."
)

DEFAULT_PROMPTS = {
    "idle": (
        "Edit ONLY the mouth of this {character_description} "
        "Make the mouth gently closed with a calm, composed soft expression. Eyes stay open."
    ),
    "talk1": (
        "Edit ONLY the mouth of this {character_description} "
        "Make the mouth slightly half-open as if softly saying 'a', a small relaxed opening. Eyes stay open."
    ),
    "talk2": (
        "Edit ONLY the mouth of this {character_description} "
        "Open the mouth a little wider than in the reference: fully open and rounded as if mid-speech saying 'o'. "
        "Eyes stay open. {keep_prompt}"
    ),
    "talk3": (
        "Edit ONLY the mouth of this {character_description} "
        "Change the mouth to a wide cheerful open smile, open horizontally as if happily saying 'eh'. "
        "Eyes stay open. {keep_prompt}"
    ),
    "blink": (
        "Edit ONLY the eyes of this {character_description} "
        "Close both eyes gently as if blinking (soft curved closed eyes). Mouth stays closed and calm."
    ),
    "talk_blink": (
        "Edit ONLY the mouth of this {character_description} "
        "Open the mouth wide and rounded as if mid-speech. Keep both eyes gently closed exactly as in the reference. "
        "{keep_prompt}"
    ),
    "listen": (
        "Edit ONLY one arm of this {character_description} "
        "Raise one hand up beside the ear, palm gently cupped behind the ear "
        "in a 'listening intently' gesture. Eyes stay open, mouth stays closed and calm."
    ),
}

PROMPT_VARIANT_SUFFIXES = (
    " Preserve every non-target pixel as closely as possible.",
    " This is an in-place edit, not a redraw; keep the original character identity locked.",
    " Keep the same canvas, watercolor texture, line softness, and background layout.",
)


class ConfigError(ValueError):
    """Raised when a member config cannot be loaded or validated."""


@dataclass(frozen=True)
class Thresholds:
    size_floor_bytes: int = DEFAULT_SIZE_FLOOR_BYTES
    deviation_pct: float = DEFAULT_DEVIATION_PCT
    retry_count: int = DEFAULT_RETRY_COUNT
    floodfill_threshold: int = DEFAULT_FLOODFILL_THRESHOLD


@dataclass(frozen=True)
class FrameConfig:
    prompt: str
    prompt_variants: tuple[str, ...] = ()


@dataclass(frozen=True)
class MemberConfig:
    name: str
    base_image: Path
    character_description: str
    keep_prompt: str = DEFAULT_KEEP_PROMPT
    frames: dict[str, FrameConfig] = field(default_factory=dict)
    thresholds: Thresholds = field(default_factory=Thresholds)

    @property
    def slug(self) -> str:
        return "".join(ch.lower() if ch.isalnum() else "-" for ch in self.name).strip("-") or "member"


@dataclass(frozen=True)
class FramePlan:
    slot: str
    source_kind: str
    source: str
    prompt: str
    raw_output: Path
    final_output: Path


GenerateCandidate = Callable[[str, str, Path, int, int], None]
DecisionCallback = Callable[[dict[str, Any]], None]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"malformed JSON config {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"config {path} must contain a JSON object")
    return data


def load_member_config(path: str | Path) -> MemberConfig:
    config_path = Path(path)
    data = _read_json(config_path)
    missing = [key for key in ("name", "base_image", "character_description") if not data.get(key)]
    if missing:
        raise ConfigError(f"missing required config field(s): {', '.join(missing)}")

    raw_thresholds = data.get("thresholds", {})
    if raw_thresholds is None:
        raw_thresholds = {}
    if not isinstance(raw_thresholds, dict):
        raise ConfigError("thresholds must be an object when present")
    thresholds = Thresholds(
        size_floor_bytes=int(raw_thresholds.get("size_floor_bytes", DEFAULT_SIZE_FLOOR_BYTES)),
        deviation_pct=float(raw_thresholds.get("deviation_pct", DEFAULT_DEVIATION_PCT)),
        retry_count=int(raw_thresholds.get("retry_count", DEFAULT_RETRY_COUNT)),
        floodfill_threshold=int(raw_thresholds.get("floodfill_threshold", DEFAULT_FLOODFILL_THRESHOLD)),
    )

    raw_frames = data.get("frames", {})
    if raw_frames is None:
        raw_frames = {}
    if not isinstance(raw_frames, dict):
        raise ConfigError("frames must be an object when present")

    frames: dict[str, FrameConfig] = {}
    for slot in EXPRESSION_SLOTS:
        raw = raw_frames.get(slot, {})
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ConfigError(f"frames.{slot} must be an object")
        prompt = str(raw.get("prompt", DEFAULT_PROMPTS[slot]))
        variants = raw.get("prompt_variants", ())
        if variants is None:
            variants = ()
        if not isinstance(variants, (list, tuple)):
            raise ConfigError(f"frames.{slot}.prompt_variants must be a list")
        try:
            prompt.format(character_description="x", keep_prompt="x", member_name="x", slot="x")
        except (KeyError, ValueError, IndexError) as exc:
            raise ConfigError(f"invalid prompt template for frames.{slot}: {exc}") from exc
        frames[slot] = FrameConfig(prompt=prompt, prompt_variants=tuple(str(item) for item in variants))

    return MemberConfig(
        name=str(data["name"]),
        base_image=Path(os.path.expanduser(str(data["base_image"]))),
        character_description=str(data["character_description"]),
        keep_prompt=str(data.get("keep_prompt", DEFAULT_KEEP_PROMPT)),
        frames=frames,
        thresholds=thresholds,
    )


def resolve_prompt(template: str, config: MemberConfig, slot: str, attempt: int, candidate: int) -> str:
    prompt = template.format(
        character_description=config.character_description,
        keep_prompt=config.keep_prompt,
        member_name=config.name,
        slot=slot,
    )
    variants = config.frames[slot].prompt_variants
    if attempt > 1:
        if variants:
            prompt = f"{prompt} {variants[(attempt + candidate - 3) % len(variants)]}"
        else:
            prompt = f"{prompt}{PROMPT_VARIANT_SUFFIXES[(attempt + candidate - 3) % len(PROMPT_VARIANT_SUFFIXES)]}"
    return prompt


def default_output_dir(config: MemberConfig) -> Path:
    return Path.cwd() / "caty-avatar-output" / config.slug


def build_plan(config: MemberConfig, output_dir: Path) -> list[FramePlan]:
    raw_dir = output_dir / "raw"
    plans: list[FramePlan] = []
    for slot in EXPRESSION_SLOTS:
        source = CHAINED_FRAMES.get(slot, "base_image")
        plans.append(
            FramePlan(
                slot=slot,
                source_kind="frame" if slot in CHAINED_FRAMES else "base",
                source=source,
                prompt=resolve_prompt(config.frames[slot].prompt, config, slot, attempt=1, candidate=1),
                raw_output=raw_dir / f"{slot}.2k.png",
                final_output=output_dir / f"{slot}.png",
            )
        )
    return plans


def evaluate_drift(
    file_size_bytes: int,
    clear_rate_pct: float,
    accepted_clear_rates: Iterable[float],
    size_floor_bytes: int = DEFAULT_SIZE_FLOOR_BYTES,
    deviation_pct: float = DEFAULT_DEVIATION_PCT,
) -> tuple[bool, str]:
    """Return whether an edit output is suspect and a human-readable reason."""
    if file_size_bytes < size_floor_bytes:
        return True, f"file size {file_size_bytes} below floor {size_floor_bytes}"

    accepted = list(accepted_clear_rates)
    if accepted:
        median = statistics.median(accepted)
        delta = abs(clear_rate_pct - median)
        if delta > deviation_pct:
            return True, f"clear rate {clear_rate_pct:.2f}% deviates {delta:.2f}pp from median {median:.2f}%"
    return False, "accepted"


def _floodfill_sentinel(image: Image.Image, threshold: int = DEFAULT_FLOODFILL_THRESHOLD) -> tuple[Image.Image, int, float]:
    _, ImageDraw = _require_pillow()
    im = image.convert("RGB")
    width, height = im.size
    for corner in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)):
        if im.getpixel(corner) != SENTINEL:
            ImageDraw.floodfill(im, corner, SENTINEL, thresh=threshold)

    rgba = im.convert("RGBA")
    px = rgba.load()
    cleared = 0
    for y in range(height):
        for x in range(width):
            if px[x, y][:3] == SENTINEL:
                px[x, y] = (0, 0, 0, 0)
                cleared += 1
    clear_rate = 100.0 * cleared / float(width * height)
    return rgba, cleared, clear_rate


def measure_clear_rate(image_path: str | Path, threshold: int = DEFAULT_FLOODFILL_THRESHOLD) -> float:
    Image, _ = _require_pillow()
    with Image.open(image_path) as image:
        _, _, clear_rate = _floodfill_sentinel(image, threshold)
    return clear_rate


def postprocess_expression(
    src: str | Path,
    dst: str | Path,
    threshold: int = DEFAULT_FLOODFILL_THRESHOLD,
    size: int = 512,
) -> dict[str, Any]:
    Image, _ = _require_pillow()
    with Image.open(src) as image:
        rgba, cleared, clear_rate = _floodfill_sentinel(image, threshold)
    out = rgba.resize((size, size), Image.LANCZOS)
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    out.save(dst)
    return {
        "cleared_pixels": cleared,
        "clear_rate_pct": clear_rate,
        "alpha_bbox": alpha_bbox(out),
        "size": out.size,
    }


def make_icon(src: str | Path, dst: str | Path, size: int = 512) -> dict[str, Any]:
    Image, _ = _require_pillow()
    with Image.open(src) as image:
        im = image.convert("RGBA")
        width, height = im.size
        side = min(width, height)
        left = (width - side) // 2
        top = (height - side) // 2
        cropped = im.crop((left, top, left + side, top + side)).resize((size, size), Image.LANCZOS)
        opaque = Image.new("RGBA", (size, size), (255, 255, 255, 255))
        opaque.alpha_composite(cropped)
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    opaque.save(dst)
    return {"crop_box": (left, top, left + side, top + side), "size": opaque.size}


def alpha_bbox(image: Image.Image) -> tuple[int, int, int, int] | None:
    return image.convert("RGBA").getchannel("A").getbbox()


def attempt_paths(output_dir: Path, slot: str, attempt: int, candidate_count: int) -> list[Path]:
    raw_dir = output_dir / "raw"
    if attempt == 1 and candidate_count == 1:
        return [raw_dir / f"{slot}.attempt1.png"]
    return [raw_dir / f"{slot}.attempt{attempt}.candidate{idx}.png" for idx in range(1, candidate_count + 1)]


def generate_frame_with_retries(
    config: MemberConfig,
    slot: str,
    output_dir: Path,
    accepted_clear_rates: list[float],
    generate_candidate: GenerateCandidate,
    decision_callback: DecisionCallback | None = None,
) -> dict[str, Any]:
    best_candidate: dict[str, Any] | None = None
    last_transport_error: BaseException | None = None
    total_rounds = config.thresholds.retry_count + 1
    for attempt in range(1, total_rounds + 1):
        candidate_count = 1 if attempt == 1 else 2
        generated: list[dict[str, Any]] = []
        for candidate, raw_path in enumerate(attempt_paths(output_dir, slot, attempt, candidate_count), start=1):
            prompt = resolve_prompt(config.frames[slot].prompt, config, slot, attempt, candidate)
            try:
                generate_candidate(slot, prompt, raw_path, attempt, candidate)
            except Exception as exc:
                if isinstance(exc, urllib.error.HTTPError) or not isinstance(
                    exc, (urllib.error.URLError, TimeoutError, ConnectionError)
                ):
                    raise
                last_transport_error = exc
                rejected = {
                    "slot": slot,
                    "attempt": attempt,
                    "candidate": candidate,
                    "path": raw_path,
                    "file_size_bytes": 0,
                    "clear_rate_pct": None,
                    "accepted": False,
                    "fallback": False,
                    "reason": f"transport error: {type(exc).__name__}",
                }
                if decision_callback:
                    decision_callback(rejected)
                continue
            size = raw_path.stat().st_size
            clear_rate = measure_clear_rate(raw_path, config.thresholds.floodfill_threshold)
            is_drift, reason = evaluate_drift(
                size,
                clear_rate,
                accepted_clear_rates,
                config.thresholds.size_floor_bytes,
                config.thresholds.deviation_pct,
            )
            record = {
                "slot": slot,
                "attempt": attempt,
                "candidate": candidate,
                "path": raw_path,
                "file_size_bytes": size,
                "clear_rate_pct": clear_rate,
                "accepted": not is_drift,
                "fallback": False,
                "reason": reason,
            }
            if decision_callback:
                decision_callback(record)
            generated.append(record)
        accepted = [record for record in generated if record["accepted"]]
        if accepted:
            chosen = max(accepted, key=lambda record: int(record["file_size_bytes"]))
            final_raw = output_dir / "raw" / f"{slot}.2k.png"
            shutil.copyfile(chosen["path"], final_raw)
            chosen["accepted_raw"] = final_raw
            return chosen
        if not generated:
            continue
        largest = max(generated, key=lambda record: int(record["file_size_bytes"]))
        if best_candidate is None or largest["file_size_bytes"] > best_candidate["file_size_bytes"]:
            best_candidate = largest

    if best_candidate is None:
        raise RuntimeError("all avatar candidates failed with transport errors") from last_transport_error
    final_raw = output_dir / "raw" / f"{slot}.2k.png"
    shutil.copyfile(best_candidate["path"], final_raw)
    best_candidate["accepted_raw"] = final_raw
    best_candidate["accepted"] = True
    best_candidate["fallback"] = True
    best_candidate["fallback_reason"] = best_candidate["reason"]
    best_candidate["reason"] = f"accepted largest candidate after {config.thresholds.retry_count} retries"
    if decision_callback:
        decision_callback(best_candidate)
    return best_candidate


def bbox_warnings(report: dict[str, Any]) -> list[str]:
    boxes = {
        slot: tuple(info["alpha_bbox"]) if info.get("alpha_bbox") else None
        for slot, info in report.get("frames", {}).items()
    }
    present = [box for box in boxes.values() if box is not None]
    if not present:
        return ["no expression frame alpha bounding boxes found"]
    baseline = present[0]
    warnings = []
    for slot, box in boxes.items():
        if box != baseline:
            warnings.append(f"{slot} alpha bbox {box} differs from baseline {baseline}")
    return warnings


def make_contact_sheet(output_dir: Path, dst: Path) -> None:
    Image, ImageDraw = _require_pillow()
    tile = 512
    label_h = 36
    cols = 4
    rows = math.ceil(len(GATEWAY_SLOTS) / cols)
    sheet = Image.new("RGBA", (cols * tile, rows * (tile + label_h)), (255, 255, 255, 255))
    draw = ImageDraw.Draw(sheet)
    for idx, slot in enumerate(GATEWAY_SLOTS):
        x = (idx % cols) * tile
        y = (idx // cols) * (tile + label_h)
        with Image.open(output_dir / f"{slot}.png") as image:
            sheet.alpha_composite(image.convert("RGBA").resize((tile, tile)), (x, y))
        draw.text((x + 12, y + tile + 10), slot, fill=(0, 0, 0, 255))
    dst.parent.mkdir(parents=True, exist_ok=True)
    sheet.convert("RGB").save(dst)


def json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    return value


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.write_text(json.dumps(json_safe(report), indent=2, ensure_ascii=False), encoding="utf-8")
