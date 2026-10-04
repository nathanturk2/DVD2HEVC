"""Focused workflow component; explicit services preserve the public facade."""
from __future__ import annotations
import argparse
from pathlib import Path
from typing import Any, Callable

def runner_command(job: dict[str, Any], *, services) -> list[str]:
    settings = job["settings"]
    quality = services.canonical_quality(settings.get("quality", "target-bitrate"))
    multiplier = settings.get(
        "target_bitrate_multiplier", settings.get("auto_cq_multiplier", 1.0)
    )
    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(services.GENERAL_RUNNER),
        "-Plan",
        str(job["plan"]),
        "-WorkRoot",
        str(job["work_root"]),
        "-OutputIso",
        str(job["output"]),
        "-QualityValues",
        quality,
        "-QualityMode",
        quality,
        "-TargetBitrateMultiplier",
        str(multiplier),
        "-BitrateMode",
        str(settings.get("bitrate_mode", "vbr")),
        "-CompactQuality",
        quality,
        "-Encoder",
        str(settings.get("encoder", "hevc_nvenc")),
        "-Preset",
        str(settings["encoder_preset"]),
        "-Cadence",
        str(settings["cadence"]),
        "-AmbiguousCadence",
        str(settings["ambiguous_cadence"]),
        "-AudioMode",
        str(settings.get("audio_mode", "passthrough")),
        "-AudioLanguageOverridesJson",
        services.json.dumps(settings.get("audio_language_overrides") or {}, separators=(",", ":")),
        "-StereoAudioBitrate",
        str(settings.get("stereo_audio_bitrate", 256_000)),
        "-MonoAudioBitrate",
        str(settings.get("mono_audio_bitrate", 128_000)),
        "-AudioWorkers",
        str(settings.get("audio_workers", 2)),
        "-PipelineDepth",
        str(settings.get("pipeline_depth", 2)),
        "-Label",
        str(job["label"]),
        "-VlcRoot",
        str(job["vlc_root"]),
        "-Python",
        services.sys.executable,
    ]
    if settings.get("main_title_quality"):
        command.extend(["-MainTitleQuality", str(settings["main_title_quality"])])
    command.extend(["-OutputFormat", settings.get("output_format", "dvd-hevc")])
    for key, flag in (("tsmuxer", "-Tsmuxer"), ("udf_tool", "-UdfTool"), ("java_home", "-JavaHome")):
        if settings.get(key): command.extend([flag, str(settings[key])])
    if settings.get("top_n_quality"):
        command.extend([
            "-TopNQuality", str(settings["top_n_quality"]),
            "-TopNCount", str(settings.get("top_n_count") or 0),
        ])
    return command
