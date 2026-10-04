"""HEVC encoder selection and DVD2HEVC-safe FFmpeg option profiles."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any

from .subprocess_utils import hidden_subprocess_kwargs


HEVC_ENCODERS = ("hevc_nvenc", "hevc_qsv", "hevc_amf", "libx265")
HARDWARE_HEVC_ENCODERS = frozenset({"hevc_nvenc", "hevc_qsv", "hevc_amf"})
ABSTRACT_ENCODER_PRESETS = tuple(f"p{number}" for number in range(1, 8))

ENCODER_LABELS = {
    "hevc_nvenc": "NVIDIA NVENC",
    "hevc_qsv": "Intel Quick Sync",
    "hevc_amf": "AMD AMF",
    "libx265": "x265 software",
}

# p1 is fastest and p7 is slowest/highest-efficiency.  The public p1-p7
# vocabulary stays stable even though FFmpeg's native preset names differ.
_QSV_PRESETS = {
    "p1": "veryfast", "p2": "faster", "p3": "fast", "p4": "medium",
    "p5": "slow", "p6": "slower", "p7": "veryslow",
}
_AMF_PRESETS = {
    "p1": "speed", "p2": "speed", "p3": "balanced", "p4": "balanced",
    "p5": "balanced", "p6": "quality", "p7": "quality",
}
_X265_PRESETS = {
    "p1": "ultrafast", "p2": "superfast", "p3": "veryfast", "p4": "faster",
    "p5": "fast", "p6": "medium", "p7": "slow",
}


class EncoderConfigurationError(ValueError):
    """An encoder selection cannot satisfy the requested DVD2HEVC profile."""


def _hevc_nal_types(payload: bytes) -> list[int]:
    result: list[int] = []
    offset = 0
    while offset < len(payload) - 3:
        start = payload.find(b"\x00\x00\x01", offset)
        if start < 0:
            break
        header = start + 3
        if header < len(payload):
            result.append((payload[header] >> 1) & 0x3F)
        offset = header + 1
    return result


def encoder_is_hardware(encoder: str) -> bool:
    return encoder in HARDWARE_HEVC_ENCODERS


def encoder_label(encoder: str) -> str:
    return ENCODER_LABELS.get(encoder, encoder)


def resolve_encoder_preset(encoder: str, preset: str) -> str:
    """Resolve the common p1-p7 scale to an FFmpeg backend-native preset."""
    if encoder not in HEVC_ENCODERS:
        raise EncoderConfigurationError(f"Unsupported HEVC encoder: {encoder}")
    value = str(preset).strip().lower()
    if encoder == "hevc_nvenc":
        if value not in ABSTRACT_ENCODER_PRESETS:
            raise EncoderConfigurationError("NVENC preset must be p1 through p7")
        return value
    if encoder == "hevc_qsv":
        if value in _QSV_PRESETS:
            return _QSV_PRESETS[value]
        if value in _QSV_PRESETS.values():
            return value
        raise EncoderConfigurationError(
            "QSV preset must be p1-p7 or veryfast/faster/fast/medium/slow/slower/veryslow"
        )
    if encoder == "hevc_amf":
        if value in _AMF_PRESETS:
            return _AMF_PRESETS[value]
        if value in set(_AMF_PRESETS.values()):
            return value
        raise EncoderConfigurationError("AMF preset must be p1-p7, speed, balanced, or quality")
    if value in _X265_PRESETS:
        return _X265_PRESETS[value]
    if value in {
        "ultrafast", "superfast", "veryfast", "faster", "fast", "medium",
        "slow", "slower", "veryslow", "placebo",
    }:
        return value
    raise EncoderConfigurationError("x265 preset must be p1-p7 or a native x265 preset")


def parse_rate_control(value: int | float | str) -> dict[str, Any]:
    """Normalize a CQ or target-bitrate instruction used by every backend.

    Public target-bitrate tokens use ``vbr:BPS`` and ``cbr:BPS``.  Numeric
    values and ``cq:N`` retain the familiar HandBrake-style CQ interface.
    Keeping this translation in one place prevents a backend from silently
    turning target bitrate back into a constant-quality encode.
    """
    text = str(value).strip().lower()
    for mode in ("vbr", "cbr"):
        prefix = f"{mode}:"
        if text.startswith(prefix):
            bitrate_text = text[len(prefix):].strip()
            multiplier = 1
            if bitrate_text.endswith("k"):
                bitrate_text, multiplier = bitrate_text[:-1], 1_000
            elif bitrate_text.endswith("m"):
                bitrate_text, multiplier = bitrate_text[:-1], 1_000_000
            try:
                bitrate = int(round(float(bitrate_text) * multiplier))
            except ValueError as exc:
                raise EncoderConfigurationError(
                    f"Invalid {mode.upper()} target bitrate: {value!r}"
                ) from exc
            if not 16_000 <= bitrate <= 100_000_000:
                raise EncoderConfigurationError(
                    "Target video bitrate must be between 16 kbit/s and 100 Mbit/s"
                )
            return {
                "mode": mode,
                "target_bps": bitrate,
                "canonical": f"{mode}:{bitrate}",
            }
    if text.startswith("cq:"):
        text = text[3:]
    try:
        quality = float(text)
    except ValueError as exc:
        raise EncoderConfigurationError(
            f"Invalid video rate control {value!r}; use cq:N, vbr:BPS, or cbr:BPS"
        ) from exc
    if not 0 <= quality <= 51:
        raise EncoderConfigurationError("CQ quality must be between 0 and 51")
    rendered = str(int(quality)) if quality.is_integer() else f"{quality:g}"
    return {"mode": "cq", "quality": quality, "canonical": f"cq:{rendered}"}


def rate_control_slug(value: int | float | str) -> str:
    control = parse_rate_control(value)
    if control["mode"] == "cq":
        return str(control["canonical"]).replace(":", "-").replace(".", "_")
    return f"{control['mode']}-{int(control['target_bps'])}bps"


def rate_control_label(value: int | float | str) -> str:
    control = parse_rate_control(value)
    if control["mode"] == "cq":
        return str(control["canonical"]).upper()
    return f"{control['mode'].upper()} {int(control['target_bps']) / 1_000_000:.3f} Mbit/s"


def build_hevc_encoder_options(
    encoder: str,
    *,
    quality: int | float | str,
    preset: str,
    cadence_mode: str,
    threads: int = 0,
) -> tuple[list[str], str]:
    """Return a strict 8-bit HEVC profile suitable for DVD VOBU repacking.

    Forced keyframe timestamps are supplied by the caller.  Each backend is
    configured to turn those forced pictures into closed IDRs with AUDs and no
    B-frame reordering.  The post-encode VOBU validator remains authoritative:
    it requires VPS/SPS/PPS plus an IDR in every independently addressed VOBU.
    """
    if encoder not in HEVC_ENCODERS:
        raise EncoderConfigurationError(f"Unsupported HEVC encoder: {encoder}")
    if cadence_mode not in {"interlaced", "progressive", "deinterlace50"}:
        raise EncoderConfigurationError(f"Unsupported cadence mode: {cadence_mode}")
    if cadence_mode == "interlaced" and encoder != "libx265":
        raise EncoderConfigurationError(
            f"{encoder_label(encoder)} cannot create DVD2HEVC interlaced output; "
            "select progressive or deinterlace50"
        )
    control = parse_rate_control(quality)
    mode = str(control["mode"])
    value = float(control.get("quality", 0))
    target = int(control.get("target_bps", 0))
    peak = target * 2
    buffer_size = target * 2
    native_preset = resolve_encoder_preset(encoder, preset)
    gop = "24" if cadence_mode == "deinterlace50" else "12"

    if encoder == "hevc_nvenc":
        rate_options = (
            ["-rc", "vbr", "-cq", str(value), "-b:v", "0"]
            if mode == "cq"
            else [
                "-rc", "vbr", "-b:v", str(target),
                "-maxrate", str(peak), "-bufsize", str(buffer_size),
            ]
            if mode == "vbr"
            else [
                "-rc", "cbr", "-b:v", str(target), "-minrate", str(target),
                "-maxrate", str(target), "-bufsize", str(buffer_size),
            ]
        )
        return ([
            "-c:v", encoder,
            "-preset", native_preset,
            "-tune", "hq",
            "-profile:v", "main",
            "-tier", "main",
            *rate_options,
            "-multipass", "qres",
            "-g", gop,
            "-bf", "0",
            "-no-scenecut", "1",
            "-strict_gop", "1",
            "-forced-idr", "1",
            "-aud", "1",
            "-pix_fmt", "yuv420p",
        ], native_preset)

    if encoder == "hevc_qsv":
        # QSV calls this ICQ/global_quality rather than NVENC CQ.  The numeric
        # direction is the same, but cross-backend visual equivalence is not
        # claimed; the documentation and reports preserve the distinction.
        rate_options = (
            ["-global_quality", str(value)]
            if mode == "cq"
            else [
                "-b:v", str(target), "-maxrate", str(peak),
                "-bufsize", str(buffer_size),
            ]
            if mode == "vbr"
            else [
                "-b:v", str(target), "-minrate", str(target),
                "-maxrate", str(target), "-bufsize", str(buffer_size),
                "-low_delay_brc", "1", "-cbr_padding", "1",
            ]
        )
        return ([
            "-c:v", encoder,
            "-preset", native_preset,
            "-profile:v", "main",
            *rate_options,
            "-g", gop,
            "-bf", "0",
            "-idr_interval", "1",
            "-forced_idr", "1",
            "-look_ahead_depth", "0",
            "-aud", "1",
            "-pix_fmt", "nv12",
        ], native_preset)

    if encoder == "hevc_amf":
        rate_options = (
            ["-rc", "qvbr", "-qvbr_quality_level", str(value)]
            if mode == "cq"
            else [
                "-rc", "vbr_peak", "-b:v", str(target),
                "-maxrate", str(peak), "-bufsize", str(buffer_size),
            ]
            if mode == "vbr"
            else [
                "-rc", "cbr", "-b:v", str(target), "-minrate", str(target),
                "-maxrate", str(target), "-bufsize", str(buffer_size),
            ]
        )
        return ([
            "-c:v", encoder,
            "-quality", native_preset,
            "-profile:v", "main",
            "-profile_tier", "main",
            *rate_options,
            "-g", gop,
            "-bf", "0",
            "-gops_per_idr", "1",
            "-header_insertion_mode", "idr",
            "-forced_idr", "1",
            "-aud", "1",
            "-pix_fmt", "yuv420p",
        ], native_preset)

    parameters = [
        f"keyint={gop}",
        "min-keyint=1",
        "scenecut=0",
        "bframes=0",
        "rc-lookahead=0",
        "open-gop=0",
        "repeat-headers=1",
        "aud=1",
        f"pools={max(1, int(threads or 1))}",
        "frame-threads=2",
    ]
    if cadence_mode == "interlaced":
        parameters.insert(0, "interlace=tff")
    if mode == "cbr":
        parameters.extend(["nal-hrd=cbr", "force-cfr=1"])
    rate_options = (
        ["-crf", str(value)]
        if mode == "cq"
        else [
            "-b:v", str(target), "-maxrate", str(peak),
            "-bufsize", str(buffer_size),
        ]
        if mode == "vbr"
        else [
            "-b:v", str(target), "-minrate", str(target),
            "-maxrate", str(target), "-bufsize", str(buffer_size),
        ]
    )
    return ([
        "-c:v", encoder,
        "-preset", native_preset,
        *rate_options,
        "-profile:v", "main",
        "-forced-idr", "1",
        "-bf", "0",
        "-pix_fmt", "yuv420p",
        "-x265-params", ":".join(parameters),
    ], native_preset)


@lru_cache(maxsize=8)
def ffmpeg_encoder_names(ffmpeg: str) -> tuple[str, ...]:
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=15,
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ()
    if result.returncode != 0:
        return ()
    return tuple(sorted(set(re.findall(
        r"^\s*[A-Z.]{6}\s+([A-Za-z0-9_]+)\s", result.stdout, re.MULTILINE
    ))))


def available_hevc_encoders(ffmpeg: str) -> tuple[str, ...]:
    names = set(ffmpeg_encoder_names(ffmpeg))
    return tuple(encoder for encoder in HEVC_ENCODERS if encoder in names)


@lru_cache(maxsize=32)
def probe_hevc_encoder(
    ffmpeg: str,
    ffprobe: str,
    encoder: str,
    preset: str = "p6",
    quality: int | float | str = 27,
) -> dict[str, Any]:
    """Actually initialize one backend with the DVD2HEVC encode profile."""
    available = available_hevc_encoders(ffmpeg)
    if encoder not in HEVC_ENCODERS:
        return {"encoder": encoder, "compiled": False, "operational": False,
                "error": "not an allowed HEVC encoder"}
    if encoder not in available:
        return {"encoder": encoder, "compiled": False, "operational": False,
                "error": "FFmpeg does not list this encoder"}
    try:
        options, native_preset = build_hevc_encoder_options(
            encoder, quality=quality, preset=preset, cadence_mode="progressive", threads=2
        )
    except EncoderConfigurationError as exc:
        return {"encoder": encoder, "compiled": True, "operational": False,
                "error": str(exc)}
    with tempfile.TemporaryDirectory(prefix="dvd2hevc-encoder-probe-") as temporary:
        output = Path(temporary) / "probe.hevc"
        command = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=720x480:rate=30000/1001:duration=0.4",
            "-frames:v", "12", "-force_key_frames", "0,0.2002",
            *options, "-f", "hevc", str(output),
        ]
        try:
            encoded = subprocess.run(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, check=False, timeout=45,
                **hidden_subprocess_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"encoder": encoder, "compiled": True, "operational": False,
                    "native_preset": native_preset, "error": str(exc)}
        if encoded.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
            detail = "\n".join(encoded.stderr.strip().splitlines()[-8:])
            return {"encoder": encoder, "compiled": True, "operational": False,
                    "native_preset": native_preset,
                    "error": detail or f"FFmpeg exited {encoded.returncode}"}
        probed = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries",
             "stream=codec_name,profile,pix_fmt", "-of", "json", str(output)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, check=False, timeout=15,
            **hidden_subprocess_kwargs(),
        )
        try:
            streams = json.loads(probed.stdout).get("streams", [])
        except json.JSONDecodeError:
            streams = []
        stream = streams[0] if streams else {}
        nal_types = _hevc_nal_types(output.read_bytes())
        nal_counts = {
            str(nal_type): nal_types.count(nal_type)
            for nal_type in (32, 33, 34, 35, 19, 20)
        }
        random_access_sets = min(
            nal_counts["32"], nal_counts["33"], nal_counts["34"],
            nal_counts["19"] + nal_counts["20"],
        )
        operational = (
            probed.returncode == 0
            and stream.get("codec_name") == "hevc"
            and stream.get("pix_fmt") in {"yuv420p", "nv12"}
            and random_access_sets >= 2
            and nal_counts["35"] >= 2
        )
        return {
            "encoder": encoder,
            "compiled": True,
            "operational": operational,
            "native_preset": native_preset,
            "rate_control": parse_rate_control(quality),
            "stream": stream,
            "random_access_probe": {
                "forced_boundaries": 2,
                "independent_header_idr_sets": random_access_sets,
                "nal_counts": nal_counts,
            },
            "error": None if operational else (
                probed.stderr.strip()
                or "probe did not repeat VPS/SPS/PPS plus IDR at both forced boundaries"
            ),
        }
