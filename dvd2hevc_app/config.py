"""Small user configuration and named conversion presets."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .pipeline import PipelineError


BUILTIN_PRESETS: dict[str, dict[str, Any]] = {
    "balanced": {
        "quality": "target-bitrate",
        "target_bitrate_multiplier": 1.0,
        "bitrate_mode": "vbr",
        "encoder": "hevc_nvenc",
        "encoder_preset": "p6",
        "deinterlace": "auto",
        "audio_mode": "passthrough",
        "description": "VBR at 25% of measured MPEG-2 bitrate, with useful HEVC savings.",
    },
    "compact": {
        "quality": "cq:27",
        "encoder": "hevc_nvenc",
        "encoder_preset": "p6",
        "deinterlace": "auto",
        "audio_mode": "passthrough",
        "description": "Smaller library copy, matching the Phase 7 compatibility fixtures.",
    },
    "high-quality": {
        "quality": "cq:20",
        "encoder": "hevc_nvenc",
        "encoder_preset": "p6",
        "deinterlace": "auto",
        "audio_mode": "passthrough",
        "description": "HandBrake-style CQ 20 for users who prefer a larger quality margin.",
    },
    "compact-stereo": {
        "quality": "target-bitrate",
        "target_bitrate_multiplier": 1.0,
        "bitrate_mode": "vbr",
        "encoder": "hevc_nvenc",
        "encoder_preset": "p6",
        "deinterlace": "auto",
        "audio_mode": "compact-stereo",
        "stereo_audio_bitrate": "256k",
        "mono_audio_bitrate": "128k",
        "audio_workers": 2,
        "pipeline_depth": 2,
        "description": "Balanced video plus DVD-compatible AC-3 stereo downmixes for smaller discs.",
    },
}


def config_root() -> Path:
    override = os.environ.get("DVD2HEVC_CONFIG_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "DVD2HEVC"
    return Path.home() / ".config" / "dvd2hevc"


def preset_path() -> Path:
    return config_root() / "presets.json"


def load_user_presets() -> dict[str, dict[str, Any]]:
    path = preset_path()
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"Could not read DVD2HEVC presets: {path}") from exc
    presets = value.get("presets") if isinstance(value, dict) else None
    if not isinstance(presets, dict):
        raise PipelineError(f"Invalid DVD2HEVC preset file: {path}")
    return {
        str(name): dict(settings)
        for name, settings in presets.items()
        if isinstance(name, str) and isinstance(settings, dict)
    }


def save_user_presets(presets: dict[str, dict[str, Any]]) -> Path:
    path = preset_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps({"schema": "dvd2hevc-presets-v1", "presets": presets}, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def all_presets() -> dict[str, dict[str, Any]]:
    presets: dict[str, dict[str, Any]] = {
        name: {**settings, "builtin": True}
        for name, settings in BUILTIN_PRESETS.items()
    }
    for name, settings in load_user_presets().items():
        migrated = dict(settings)
        if str(migrated.get("quality", "")).lower() in {"auto", "auto-cq", "compact-auto"}:
            migrated["quality"] = "target-bitrate"
        if "target_bitrate_multiplier" not in migrated and "auto_cq_multiplier" in migrated:
            migrated["target_bitrate_multiplier"] = migrated.pop("auto_cq_multiplier")
        if migrated.get("quality") == "target-bitrate":
            migrated.setdefault("bitrate_mode", "vbr")
        presets[name] = {**migrated, "builtin": False}
    return presets


def resolve_preset(name: str | None) -> dict[str, Any]:
    selected = name or "balanced"
    presets = all_presets()
    if selected not in presets:
        available = ", ".join(sorted(presets))
        raise PipelineError(f"Unknown preset {selected!r}. Available presets: {available}")
    return {"name": selected, **presets[selected]}


def save_named_preset(
    name: str,
    *,
    quality: str,
    encoder: str = "hevc_nvenc",
    encoder_preset: str,
    deinterlace: str,
    audio_mode: str = "passthrough",
    stereo_audio_bitrate: str | int = "256k",
    mono_audio_bitrate: str | int = "128k",
    audio_workers: int = 2,
    pipeline_depth: int = 2,
    target_bitrate_multiplier: float = 1.0,
    bitrate_mode: str = "vbr",
    main_title_quality: str | None = None,
    top_n_quality: str | None = None,
    top_n_count: int = 0,
    audio_language_overrides: dict[str, str] | None = None,
    add_filename_tags: bool = True,
    output_format: str = "uhd-bd",
) -> Path:
    normalized = name.strip()
    if not normalized or any(character in normalized for character in "\\/:*?\"<>|"):
        raise PipelineError("Preset names must be non-empty and may not contain path characters")
    if normalized in BUILTIN_PRESETS:
        raise PipelineError(f"{normalized!r} is a built-in preset and cannot be overwritten")
    presets = load_user_presets()
    presets[normalized] = {
        "quality": quality,
        "encoder": encoder,
        "encoder_preset": encoder_preset,
        "deinterlace": deinterlace,
        "audio_mode": audio_mode,
        "stereo_audio_bitrate": stereo_audio_bitrate,
        "mono_audio_bitrate": mono_audio_bitrate,
        "audio_workers": audio_workers,
        "pipeline_depth": pipeline_depth,
        "target_bitrate_multiplier": target_bitrate_multiplier,
        "bitrate_mode": bitrate_mode,
        "main_title_quality": main_title_quality,
        "top_n_quality": top_n_quality,
        "top_n_count": top_n_count,
        "audio_language_overrides": dict(audio_language_overrides or {}),
        "add_filename_tags": add_filename_tags,
        "output_format": output_format,
    }
    return save_user_presets(presets)


def remove_named_preset(name: str) -> Path:
    if name in BUILTIN_PRESETS:
        raise PipelineError(f"{name!r} is a built-in preset and cannot be removed")
    presets = load_user_presets()
    if name not in presets:
        raise PipelineError(f"Named preset not found: {name}")
    del presets[name]
    return save_user_presets(presets)
