"""Validation helpers for the patched VLC DVDNAV proof."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import re


VLC_DVDHEVC_MARKERS = {
    "dvdnav_opened": 'using access_demux module "dvdnav"',
    "cell_navigation": "DVDNAV_CELL_CHANGE",
    "hevc_psm_accepted": "DVD-HEVC program stream map accepted",
    "hevc_packetizer": 'using packetizer module "hevc"',
    "hevc_decoder_started": "codec (hevc) started",
    "first_picture": "Received first picture",
}

VLC_HEVC_CORRUPTION_MARKERS = {
    "no_pps_id_errors": "PPS id out of range",
    "no_nal_parse_errors": "Error parsing NAL",
    "no_oversized_sps": "Truncating likely oversized SPS",
}

# VLC's video-output layer does not always emit ``Received first picture``
# when a dummy video output is used for the automated DVD-menu smoke test.
# Its bundled FFmpeg HEVC decoder does, however, log every frame it actually
# outputs.  That is equally strong evidence that a first picture was decoded;
# merely opening the decoder is deliberately not sufficient.
VLC_HEVC_OUTPUT_FRAME_RE = re.compile(r"\[hevc @ [^\]]+\]\s+Output frame with POC\b")

COMPACT_RELOCATED_DVDNAV_FIELDS = {"cell_length", "pg_length", "cell_start", "pg_start"}


def validate_vlc_dvdhevc_text(text: str, *, minimum_cell_changes: int = 1) -> dict[str, Any]:
    """Return stable pass/fail checks from a verbose patched-VLC log."""
    checks = {name: marker in text for name, marker in VLC_DVDHEVC_MARKERS.items()}
    vlc_first_picture_count = text.count(VLC_DVDHEVC_MARKERS["first_picture"])
    hevc_output_frame_count = len(VLC_HEVC_OUTPUT_FRAME_RE.findall(text))
    checks["first_picture"] = bool(vlc_first_picture_count or hevc_output_frame_count)
    corruption_counts = {
        name: text.count(marker) for name, marker in VLC_HEVC_CORRUPTION_MARKERS.items()
    }
    checks.update({name: count == 0 for name, count in corruption_counts.items()})
    cell_change_count = text.count("DVDNAV_CELL_CHANGE")
    checks["cell_navigation"] = cell_change_count >= minimum_cell_changes
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "missing": [name for name, passed in checks.items() if not passed],
        "observations": {
            "cell_change_count": cell_change_count,
            "hevc_psm_accept_count": text.count("DVD-HEVC program stream map accepted"),
            "first_picture_count": vlc_first_picture_count + hevc_output_frame_count,
            "vlc_first_picture_count": vlc_first_picture_count,
            "hevc_output_frame_count": hevc_output_frame_count,
            "hevc_corruption_counts": corruption_counts,
            "ac3_packetizer": 'using packetizer module "a52"' in text,
            "mpeg2_packetizer": 'using packetizer module "mpegvideo"' in text,
        },
    }


def validate_vlc_dvdhevc_log(path: Path, *, minimum_cell_changes: int = 1) -> dict[str, Any]:
    return validate_vlc_dvdhevc_text(
        path.read_text(encoding="utf-8", errors="replace"),
        minimum_cell_changes=minimum_cell_changes,
    )


def validate_navigation_only_menu_text(
    source_text: str,
    output_text: str,
    *,
    minimum_cell_changes: int = 1,
) -> dict[str, Any]:
    """Accept a no-video menu probe only when it matches the source probe.

    Some authored first-play/menu command chains briefly enter a navigation-only
    cell and then end when driven by VLC's noninteractive dummy interface. This
    fallback is deliberately source-relative: a source probe that exposes MPEG-2
    video keeps the normal HEVC decoder gate mandatory.
    """
    source = validate_vlc_dvdhevc_text(
        source_text, minimum_cell_changes=minimum_cell_changes
    )
    output = validate_vlc_dvdhevc_text(
        output_text, minimum_cell_changes=minimum_cell_changes
    )
    navigation = compare_vlc_navigation_text(
        source_text, output_text, compact_relocation=True
    )
    source_observations = source["observations"]
    output_observations = output["observations"]
    source_video_evidence = bool(
        source_observations["mpeg2_packetizer"]
        or source_observations["hevc_psm_accept_count"]
        or source_observations["first_picture_count"]
        or 'using packetizer module "hevc"' in source_text
    )
    output_video_evidence = bool(
        output_observations["mpeg2_packetizer"]
        or output_observations["hevc_psm_accept_count"]
        or output_observations["first_picture_count"]
        or 'using packetizer module "hevc"' in output_text
        or "codec (hevc) started" in output_text
    )
    checks = {
        "source_dvdnav_opened": bool(source["checks"]["dvdnav_opened"]),
        "source_cell_navigation": bool(source["checks"]["cell_navigation"]),
        "source_navigation_only": not source_video_evidence,
        "output_dvdnav_opened": bool(output["checks"]["dvdnav_opened"]),
        "output_cell_navigation": bool(output["checks"]["cell_navigation"]),
        "output_navigation_only": not output_video_evidence,
        "navigation_equivalent": bool(navigation["passed"]),
        "no_hevc_corruption": all(
            output["checks"][name] for name in VLC_HEVC_CORRUPTION_MARKERS
        ),
    }
    return {
        "schema": "dvd2hevc-vlc-navigation-only-menu-equivalence-v0",
        "passed": all(checks.values()),
        "checks": checks,
        "missing": [name for name, passed in checks.items() if not passed],
        "source": source,
        "output": output,
        "navigation": navigation,
    }


def validate_navigation_only_menu_logs(
    source: Path,
    output: Path,
    *,
    minimum_cell_changes: int = 1,
) -> dict[str, Any]:
    return validate_navigation_only_menu_text(
        source.read_text(encoding="utf-8", errors="replace"),
        output.read_text(encoding="utf-8", errors="replace"),
        minimum_cell_changes=minimum_cell_changes,
    )


def extract_dvdnav_trace(text: str, *, ignore_fields: set[str] | None = None) -> list[str]:
    """Normalize the behavioral DVDNAV events and their reported fields."""
    trace: list[str] = []
    pattern = re.compile(r"dvdnav demux debug:\s+(.*)$")
    for line in text.splitlines():
        match = pattern.search(line)
        if not match:
            continue
        value = match.group(1).strip()
        if value.startswith("-") and ignore_fields:
            field = value[1:].strip().split("=", 1)[0]
            if field in ignore_fields:
                continue
        if value.startswith("DVDNAV_") or value.startswith("-"):
            trace.append(value)
    return trace


def compare_vlc_navigation_text(
    source_text: str,
    output_text: str,
    *,
    compact_relocation: bool = False,
) -> dict[str, Any]:
    ignored = COMPACT_RELOCATED_DVDNAV_FIELDS if compact_relocation else None
    source_trace = extract_dvdnav_trace(source_text, ignore_fields=ignored)
    output_trace = extract_dvdnav_trace(output_text, ignore_fields=ignored)
    first_mismatch: int | None = None
    for index, pair in enumerate(zip(source_trace, output_trace)):
        if pair[0] != pair[1]:
            first_mismatch = index
            break
    if first_mismatch is None and len(source_trace) != len(output_trace):
        first_mismatch = min(len(source_trace), len(output_trace))
    return {
        "passed": bool(source_trace) and source_trace == output_trace,
        "source_event_count": sum(value.startswith("DVDNAV_") for value in source_trace),
        "output_event_count": sum(value.startswith("DVDNAV_") for value in output_trace),
        "source_trace_count": len(source_trace),
        "output_trace_count": len(output_trace),
        "first_mismatch": first_mismatch,
        "ignored_fields": sorted(ignored or []),
        "source_trace": source_trace,
        "output_trace": output_trace,
    }


def compare_vlc_navigation_logs(
    source: Path,
    output: Path,
    *,
    compact_relocation: bool = False,
) -> dict[str, Any]:
    return compare_vlc_navigation_text(
        source.read_text(encoding="utf-8", errors="replace"),
        output.read_text(encoding="utf-8", errors="replace"),
        compact_relocation=compact_relocation,
    )
