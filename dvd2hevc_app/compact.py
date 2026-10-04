"""Phase 6 compact-layout planning without per-VOBU quality fallback."""

from __future__ import annotations

import json
import hashlib
import mmap
import os
from bisect import bisect_right
from copy import deepcopy
from collections import Counter
from pathlib import Path
from typing import Any

from .atomic import write_json_atomic
from .encoders import parse_rate_control

from .audio import (
    _merge_track_rate_variants,
    build_dvd_ac3_pes_packets,
    count_ac3_access_units,
    dvd_audio_packet_identity,
    parse_audio_bitrate,
    parse_stream_id,
)
from .pipeline import PipelineError
from .progress import progress_event
from .extract import inspect_vob_file
from .repack import (
    SCR_TICKS_PER_SECOND,
    VideoSlot,
    _pack_packets_into_slots,
    _program_stream_packets,
    build_mpeg2_pack_header,
    padding_packet,
    parse_mpeg2_pack_header,
    rewrite_mpeg2_pack_header,
    video_slot,
)
from .transport import (
    VideoPes,
    parse_audio_pes,
    prepare_hevc_dvd_menu,
    read_audio_pes,
    read_video_pes,
)
from .vob import DVD_SECTOR_SIZE


GENERIC_VIDEO_PREFIX = bytes(14)  # Capacity model for a rewritten MPEG-2 pack header.
COMPACT_AUDIO_LAYOUT_POLICY = "source-local-vobu-sector-map-v2"


def _audio_identity_key(packet_stream_id: int, substream_id: int | None) -> str:
    return (
        f"0x{packet_stream_id:02x}:0x{substream_id:02x}"
        if substream_id is not None else f"0x{packet_stream_id:02x}"
    )


def _dvd_audio_inventory(
    sector: bytes, sector_index: int,
) -> tuple[list[tuple[int, int | None]], set[tuple[int, int | None]]]:
    """Inventory DVD audio in a sector already resident during video planning.

    Keeping this tiny index in the compact layout avoids rereading every source
    VOB sector when compact-stereo later replaces the authored audio packets.
    The second result records identities whose packet is followed by non-padding
    data; the audio layout retains the same safety rejection as a live rescan.
    """
    packets = _program_stream_packets(sector)
    matches: list[tuple[tuple[int, int | None], int]] = []
    for stream_id, start, end in packets:
        identity: tuple[int, int | None] | None = None
        if 0xC0 <= stream_id <= 0xC7:
            identity = (stream_id, None)
        elif stream_id == 0xBD and start + 9 <= end:
            payload_start = start + 9 + sector[start + 8]
            if payload_start < end:
                substream_id = sector[payload_start]
                if 0x80 <= substream_id <= 0x8F or 0xA0 <= substream_id <= 0xA7:
                    identity = (stream_id, substream_id)
        if identity is not None:
            matches.append((identity, start))
    identities = [identity for identity, _start in matches]
    unsafe = {
        identity
        for identity, first in matches
        if any(start > first and stream_id != 0xBE for stream_id, start, _end in packets)
    }
    return identities, unsafe


def _dvd_audio_slot(sector: bytes, sector_index: int, substream_id: int) -> VideoSlot | None:
    return _dvd_audio_slot_identity(sector, sector_index, 0xBD, substream_id)


def _dvd_audio_slot_identity(
    sector: bytes,
    sector_index: int,
    packet_stream_id: int,
    substream_id: int | None,
) -> VideoSlot | None:
    matches: list[int] = []
    packets = _program_stream_packets(sector)
    for stream_id, start, end in packets:
        if stream_id != packet_stream_id or start + 9 > end:
            continue
        if substream_id is None:
            matches.append(start)
        else:
            payload_start = start + 9 + sector[start + 8]
            if payload_start < end and sector[payload_start] == substream_id:
                matches.append(start)
    if not matches:
        return None
    if len(matches) != 1:
        raise PipelineError(f"Sector {sector_index} contains multiple packets for one DVD audio stream")
    first = matches[0]
    for stream_id, start, _end in packets:
        if start > first and stream_id != 0xBE:
            raise PipelineError(f"Sector {sector_index} mixes DVD audio with stream 0x{stream_id:02x}")
    return VideoSlot(sector_index=sector_index, prefix=sector[:first])


def _dvd_audio_slot_any(
    sector: bytes,
    sector_index: int,
    substream_ids: set[int],
    packet_stream_ids: set[int] | None = None,
) -> VideoSlot | None:
    matches: list[int] = []
    packet_stream_ids = packet_stream_ids or set()
    packets = _program_stream_packets(sector)
    for stream_id, start, end in packets:
        if stream_id in packet_stream_ids:
            matches.append(start)
            continue
        if stream_id != 0xBD or start + 9 > end:
            continue
        payload_start = start + 9 + sector[start + 8]
        if payload_start < end and sector[payload_start] in substream_ids:
            matches.append(start)
    if not matches:
        return None
    if len(matches) != 1:
        raise PipelineError(f"Sector {sector_index} contains multiple selected DVD audio packets")
    first = matches[0]
    for stream_id, start, _end in packets:
        if start > first and stream_id != 0xBE:
            raise PipelineError(f"Sector {sector_index} mixes DVD audio with stream 0x{stream_id:02x}")
    return VideoSlot(sector_index=sector_index, prefix=sector[:first])


def _compact_audio_groups(
    layout: dict[str, Any],
    row: dict[str, Any],
    audio_report: dict[str, Any],
) -> tuple[dict[int, list[bytes]], dict[str, Any]]:
    groups_with_order: dict[int, list[tuple[int, int, int, bytes]]] = {
        int(vobu["original_first_sector"]): []
        for cell in row["cells"] for vobu in cell["vobus"]
    }
    input_report = json.loads(Path(row["input_report"]).read_text(encoding="utf-8"))
    source_reports = {item["name"]: item for item in input_report["cells"]}
    if audio_report.get("schema") == "dvd2hevc-compact-audio-domain-v1":
        reports_by_cell = {
            str(item["name"]): json.loads(Path(item["report"]).read_text(encoding="utf-8"))
            for item in audio_report.get("cells") or []
        }
    elif audio_report.get("schema") == "dvd2hevc-compact-audio-prototype-v0" and len(row["cells"]) == 1:
        reports_by_cell = {str(row["cells"][0]["name"]): audio_report}
    else:
        raise PipelineError("Compact audio report does not cover this domain's physical cells")
    tracks_by_ordinal: dict[int, dict[str, Any]] = {}
    source_private_substream_ids: set[int] = set()
    source_packet_stream_ids: set[int] = set()
    source_audio_identities: set[tuple[int, int | None]] = set()
    access_units = 0
    packet_count = 0
    boundary_preroll_packets = 0
    boundary_postroll_packets = 0
    for cell in row["cells"]:
        name = str(cell["name"])
        cell_audio = reports_by_cell.get(name)
        if cell_audio is None:
            raise PipelineError(f"Compact audio report has no physical cell {name}")
        source_report = source_reports.get(name)
        if source_report is None:
            raise PipelineError(f"Input report has no physical cell {name}")
        attempt = exact_quality_attempt(
            source_report, row.get("quality", layout["quality_policy"]["quality"])
        )
        budgets = {int(item["index"]): item for item in attempt["budget"]["vobus"]}
        ranges: list[tuple[int, int, int]] = []
        for vobu in cell["vobus"]:
            budget = budgets[int(vobu["index"])]
            ranges.append((
                int(vobu["original_first_sector"]),
                int(budget["start_ptm"]),
                int(budget["end_ptm"]),
            ))
        if not ranges:
            raise PipelineError(f"Physical cell {name} has no VOBU timestamp ranges")
        range_starts = [start for _first, start, _end in ranges]
        for track in cell_audio.get("tracks") or []:
            ordinal = int(track["ordinal"])
            if not 0 <= ordinal <= 7:
                raise PipelineError(f"Invalid DVD audio stream ordinal: {ordinal}")
            source_codec = str(track.get("source_codec") or "").lower()
            source_probe_id = parse_stream_id(track.get("source_stream_id"))
            source_packet_id, source_substream_id = dvd_audio_packet_identity(source_codec, ordinal)
            expected_probe_id = source_substream_id if source_substream_id is not None else source_packet_id
            target_substream_id = 0x80 + ordinal
            if source_probe_id is None or (source_probe_id & 0xFF) != expected_probe_id:
                raise PipelineError(
                    f"{source_codec.upper()} stream {ordinal} has unexpected source id "
                    f"{track.get('source_stream_id')}"
                )
            source_audio_identities.add((source_packet_id, source_substream_id))
            if source_substream_id is None:
                source_packet_stream_ids.add(source_packet_id)
            else:
                source_private_substream_ids.add(source_substream_id)
            stable = {
                "ordinal": ordinal,
                "source_codec": source_codec,
                "source_ifo_codec": track.get("source_ifo_codec") or source_codec,
                "source_channels": int(track["source_channels"]),
                "target_channels": int(track["target"]["channels"]),
                "target_bitrate": int(track["target"]["bitrate"]),
                "target_bitrates": [int(track["target"]["bitrate"])],
                "source_bitrate": int(track["source_bitrate"]) if track.get("source_bitrate") else None,
                "source_bitrates": (
                    [int(track["source_bitrate"])] if track.get("source_bitrate") else []
                ),
                "processing": str(track.get("processing") or "downmix"),
                "elementary_identity": str(track.get("elementary_identity") or "reencoded"),
                "source_packet_id": source_packet_id,
                "source_substream_id": source_substream_id,
                "target_substream_id": target_substream_id,
                "language": track.get("language"),
            }
            previous = tracks_by_ordinal.get(ordinal)
            if previous is not None and any(
                previous[key] != stable[key]
                for key in (
                    "source_codec", "source_ifo_codec", "source_channels", "target_channels",
                    "processing", "elementary_identity", "source_packet_id",
                    "source_substream_id",
                )
            ):
                raise PipelineError(f"Audio stream {ordinal} changes format between physical cells")
            if previous is not None:
                stable = _merge_track_rate_variants(previous, stable)
            tracks_by_ordinal[ordinal] = stable
            transport_packets = read_audio_pes(Path(track["timestamped_output"]))
            dvd_packets = build_dvd_ac3_pes_packets(
                transport_packets,
                substream_id=target_substream_id,
            )
            access_units += count_ac3_access_units(transport_packets)
            packet_count += len(dvd_packets)
            last_pts = ranges[0][1]
            for sequence, pes in enumerate(dvd_packets):
                pts = parse_audio_pes(pes).pts
                if pts is None:
                    match = ranges[-1][0]
                    effective_pts = last_pts + 1
                else:
                    match, boundary = _match_audio_vobu(ranges, pts, range_starts)
                    if match is None:
                        raise PipelineError(
                            f"Compact AC-3 PTS {pts} in {name} stream {ordinal} "
                            "is outside the cell's bounded boundary preroll/postroll"
                        )
                    boundary_preroll_packets += boundary == "preroll"
                    boundary_postroll_packets += boundary == "postroll"
                    effective_pts = pts
                    last_pts = pts
                groups_with_order[match].append(
                    (effective_pts, target_substream_id, sequence, pes)
                )
    groups = {
        first: [item[3] for item in sorted(items, key=lambda item: item[:3])]
        for first, items in groups_with_order.items()
    }
    # Authored-but-unused descriptors make sparse physical stream IDs legal.
    # Preserve those IDs instead of renumbering navigation-visible audio.
    ordinals = sorted(tracks_by_ordinal)
    return groups, {
        "tracks": [tracks_by_ordinal[ordinal] for ordinal in ordinals],
        "processing_policy": audio_report.get("processing_policy"),
        "source_private_substream_ids": sorted(source_private_substream_ids),
        "source_packet_stream_ids": sorted(source_packet_stream_ids),
        "source_audio_identities": [
            {"packet_stream_id": packet_id, "substream_id": substream_id}
            for packet_id, substream_id in sorted(
                source_audio_identities,
                key=lambda item: (item[0], -1 if item[1] is None else item[1]),
            )
        ],
        "access_units": access_units,
        "dvd_pes_packets": packet_count,
        "boundary_preroll_packets": boundary_preroll_packets,
        "boundary_postroll_packets": boundary_postroll_packets,
    }


def _match_audio_vobu(
    ranges: list[tuple[int, int, int]],
    pts: int,
    starts: list[int] | None = None,
) -> tuple[int | None, str | None]:
    """Map a packet PTS, allowing bounded authored cell-boundary overlap."""
    range_starts = starts if starts is not None else [start for _first, start, _end in ranges]
    index = bisect_right(range_starts, pts) - 1
    if index >= 0:
        first, start, end = ranges[index]
        if start <= pts < end:
            return first, None
    first_sector, first_start, first_end = ranges[0]
    last_sector, last_start, last_end = ranges[-1]
    # Seamless authored cells may carry audio across two video VOBU windows.
    # Keep that bounded preroll/postroll in the physical cell that supplied it.
    # MI2 is a real example: its maximum preroll is 55,429 ticks while the
    # first video VOBU is only 39,600 ticks.
    if pts < first_start and first_start - pts <= 2 * (first_end - first_start):
        return first_sector, "preroll"
    if pts >= last_end and pts - last_end <= 2 * (last_end - last_start):
        return last_sector, "postroll"
    return None, None


def plan_compact_audio_layout(
    layout_path: Path,
    audio_report_path: Path,
    destination: Path,
    *,
    domain: str,
    vts: int,
) -> dict[str, Any]:
    """Apply compact AC-3 sector counts to an existing exact-CQ layout."""
    layout_path = layout_path.resolve()
    base = json.loads(layout_path.read_text(encoding="utf-8"))
    result = deepcopy(base)
    row = next(
        (item for item in result["domains"] if item["domain"] == domain and int(item["vts"]) == vts),
        None,
    )
    if row is None:
        raise PipelineError(f"Compact layout has no {domain} VTS {vts}")
    audio_report_path = audio_report_path.resolve()
    audio_report = json.loads(audio_report_path.read_text(encoding="utf-8"))
    if audio_report.get("status") != "passed" or audio_report.get("audio_mode") != "compact-stereo":
        raise PipelineError("Compact-audio report has not passed")
    groups, metadata = _compact_audio_groups(result, row, audio_report)
    source_private_substream_ids = {
        int(value) for value in metadata["source_private_substream_ids"]
    }
    source_packet_stream_ids = {
        int(value) for value in metadata["source_packet_stream_ids"]
    }
    source_audio_identities = [
        (int(item["packet_stream_id"]), (
            int(item["substream_id"]) if item.get("substream_id") is not None else None
        ))
        for item in metadata["source_audio_identities"]
    ]
    original_audio = 0
    compact_audio = 0
    for cell in row["cells"]:
        source = Path(cell["source_cell"])
        inventory_ready = all(
            isinstance(vobu.get("source_audio_sectors_by_identity"), dict)
            for vobu in cell["vobus"]
        )
        source_handle = None
        source_map = None
        try:
            if not inventory_ready:
                source_handle = source.open("rb")
                source_map = mmap.mmap(source_handle.fileno(), 0, access=mmap.ACCESS_READ)
            for vobu in cell["vobus"]:
                source_audio_by_stream = {identity: 0 for identity in source_audio_identities}
                if inventory_ready:
                    if int(vobu.get("source_audio_multi_identity_sectors") or 0):
                        raise PipelineError(
                            f"VOBU {vobu['index']} in {cell['name']} mixes DVD audio identities"
                        )
                    inventory = vobu["source_audio_sectors_by_identity"]
                    unsafe = vobu.get("source_audio_unsafe_sectors_by_identity") or {}
                    for identity in source_audio_identities:
                        key = _audio_identity_key(*identity)
                        if int(unsafe.get(key) or 0):
                            raise PipelineError(
                                f"VOBU {vobu['index']} in {cell['name']} mixes selected DVD audio "
                                "with a later non-padding packet"
                            )
                        source_audio_by_stream[identity] = int(inventory.get(key) or 0)
                    source_audio = sum(source_audio_by_stream.values())
                else:
                    source_audio = 0
                    assert source_map is not None
                    for relative in _source_local_vobu_sector_range(cell, vobu):
                        sector = bytes(
                            source_map[
                                relative * DVD_SECTOR_SIZE : (relative + 1) * DVD_SECTOR_SIZE
                            ]
                        )
                        if _dvd_audio_slot_any(
                            sector, relative, source_private_substream_ids,
                            source_packet_stream_ids,
                        ) is not None:
                            source_audio += 1
                            for identity in source_audio_identities:
                                if _dvd_audio_slot_identity(sector, relative, *identity) is not None:
                                    source_audio_by_stream[identity] += 1
                                    break
                new_audio = len(groups[int(vobu["original_first_sector"])])
                vobu["original_audio_sectors"] = source_audio
                vobu["original_audio_sectors_by_stream"] = {
                    _audio_identity_key(packet_id, substream_id): count
                    for (packet_id, substream_id), count in sorted(
                        source_audio_by_stream.items(), key=lambda item: item[0][0]
                    )
                }
                vobu["compact_audio_sectors"] = new_audio
                vobu["compact_sectors"] = int(vobu["compact_sectors"]) - source_audio + new_audio
                vobu["sector_delta"] = int(vobu["compact_sectors"]) - int(vobu["original_sectors"])
                original_audio += source_audio
                compact_audio += new_audio
        finally:
            if source_map is not None:
                source_map.close()
            if source_handle is not None:
                source_handle.close()

    # Reallocate in original physical order. This preserves one shared copy of
    # interleaved branch extents after their audio sector counts change.
    physical_vobus = sorted(
        (
            (int(vobu["original_first_sector"]), cell, vobu)
            for cell in row["cells"] for vobu in cell["vobus"]
        ),
        key=lambda item: item[0],
    )
    domain_cursor = 0
    old_cursor = 0
    for old_first, _cell, vobu in physical_vobus:
        old_last = int(vobu["original_last_sector"])
        if old_first != old_cursor or old_last < old_first:
            raise PipelineError(
                f"Compact-audio physical VOBU coverage is not contiguous at {old_cursor}: "
                f"found {old_first}-{old_last}"
            )
        vobu["compact_domain_sector"] = domain_cursor
        domain_cursor += int(vobu["compact_sectors"])
        old_cursor = old_last + 1

    for cell in row["cells"]:
        cell_vobus = sorted(cell["vobus"], key=lambda item: int(item["index"]))
        cell_cursor = 0
        for vobu in cell_vobus:
            vobu["compact_cell_sector"] = cell_cursor
            cell_cursor += int(vobu["compact_sectors"])
        cell["compact_first_sector"] = int(cell_vobus[0]["compact_domain_sector"])
        cell["compact_last_sector"] = (
            int(cell_vobus[-1]["compact_domain_sector"])
            + int(cell_vobus[-1]["compact_sectors"]) - 1
        )
        cell["compact_sectors"] = cell_cursor
        cell["sector_delta"] = cell_cursor - int(cell["original_sectors"])
        compact_segments: list[dict[str, int]] = []
        for segment_index, source_segment in enumerate(cell["physical_segments"]):
            segment_vobus = [
                vobu for vobu in cell_vobus
                if int(vobu["physical_segment"]) == segment_index
            ]
            if not segment_vobus:
                raise PipelineError(
                    f"Physical segment {segment_index} in {cell['name']} contains no VOBUs"
                )
            first_vobu = segment_vobus[0]
            last_vobu = segment_vobus[-1]
            compact_segments.append({
                "original_start_sector": int(source_segment["start_sector"]),
                "original_last_sector": int(source_segment["last_sector"]),
                "compact_start_sector": int(first_vobu["compact_domain_sector"]),
                "compact_last_sector": (
                    int(last_vobu["compact_domain_sector"])
                    + int(last_vobu["compact_sectors"]) - 1
                ),
                "vobus": len(segment_vobus),
            })
        cell["compact_segments"] = compact_segments
    old_domain_compact = int(row["compact_sectors"])
    row["compact_sectors"] = domain_cursor
    row["sector_delta"] = domain_cursor - int(row["original_sectors"])
    row["compact_audio"] = {**metadata, "audio_report": str(audio_report_path)}

    total_delta = domain_cursor - old_domain_compact
    summary = result["summary"]
    summary["compact_sectors"] = int(summary["compact_sectors"]) + total_delta
    summary["compact_vob_bytes"] = int(summary["compact_sectors"]) * DVD_SECTOR_SIZE
    summary["saved_bytes"] = int(summary["original_bytes"]) - int(summary["compact_vob_bytes"])
    summary["saved_percent"] = round(100 * int(summary["saved_bytes"]) / int(summary["original_bytes"]), 3)
    summary["audio_savings_included"] = True
    summary["original_audio_sectors"] = int(summary.get("original_audio_sectors") or 0) + original_audio
    summary["compact_audio_sectors"] = int(summary.get("compact_audio_sectors") or 0) + compact_audio
    summary["saved_audio_sectors"] = (
        int(summary["original_audio_sectors"]) - int(summary["compact_audio_sectors"])
    )
    summary["vobus_expanding"] = sum(
        1 for item in result["domains"] for cell in item["cells"] for vobu in cell["vobus"]
        if int(vobu["sector_delta"]) > 0
    )
    summary["vobus_shrinking"] = sum(
        1 for item in result["domains"] for cell in item["cells"] for vobu in cell["vobus"]
        if int(vobu["sector_delta"]) < 0
    )
    result["schema"] = "dvd2hevc-compact-layout-v1"
    result["base_layout"] = str(layout_path)
    result["base_layout_sha256"] = hashlib.sha256(layout_path.read_bytes()).hexdigest()
    result["audio_policy"] = {
        "mode": "compact-stereo",
        "codec": "ac3",
        "sample_rate": 48_000,
        "stereo_bitrate": int(audio_report["stereo_bitrate"]),
        "mono_bitrate": int(audio_report["mono_bitrate"]),
        "sector_savings_included": True,
        "implementation": "dvd-private-stream-pes-exact-readback",
        "layout_policy": COMPACT_AUDIO_LAYOUT_POLICY,
    }
    _atomic_json(destination, result)
    return result


def _canonical_quality(value: int | float | str) -> str:
    return str(parse_rate_control(value)["canonical"])


def exact_quality_attempt(cell: dict[str, Any], quality: int | float | str) -> dict[str, Any]:
    """Select an exact encode-quality attempt; never fall back to a worse value."""
    matches = [
        attempt for attempt in cell.get("attempts", [])
        if _canonical_quality(attempt.get("encode", {}).get("quality", -1))
        == _canonical_quality(quality)
    ]
    if len(matches) != 1:
        raise PipelineError(
            f"{cell.get('name', 'cell')} has {len(matches)} attempts at exact quality {quality}"
        )
    return matches[0]


def minimum_video_slots(
    mandatory_prefixes: list[bytes],
    packets: list[VideoPes],
    *,
    pts_offset: int,
    start_ptm: int,
    end_ptm: int,
    add_psm: bool = True,
) -> int:
    """Find the smallest rewritten video-sector count that losslessly holds the PES data."""
    minimum = len(mandatory_prefixes)
    if packets:
        minimum = max(minimum, 1)
    def fits(count: int) -> bool:
        prefixes = mandatory_prefixes + [GENERIC_VIDEO_PREFIX] * (count - len(mandatory_prefixes))
        slots = [VideoSlot(index, prefix) for index, prefix in enumerate(prefixes)]
        try:
            _pack_packets_into_slots(
                slots,
                packets,
                pts_offset=pts_offset,
                start_ptm=start_ptm,
                end_ptm=end_ptm,
                add_psm=add_psm and bool(packets),
            )
            return True
        except ValueError:
            return False

    if fits(minimum):
        return minimum
    failed = minimum
    upper = max(failed + 1, failed * 2)
    while upper < 65536 and not fits(upper):
        failed = upper
        upper = min(65536, upper * 2)
    if upper >= 65536 and not fits(upper):
        raise PipelineError("Compact VOBU needs an unreasonable number of video sectors")
    lower = failed + 1
    while lower < upper:
        middle = (lower + upper) // 2
        if fits(middle):
            upper = middle
        else:
            lower = middle + 1
    return lower


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    write_json_atomic(path, value)


def _physical_segments(cell: dict[str, Any]) -> list[dict[str, int]]:
    """Return the non-overlapping physical ranges represented by one logical cell file."""
    source = cell.get("physical_segments") or [cell["cell"]]
    result: list[dict[str, int]] = []
    previous_last = -1
    for item in source:
        first = int(item.get("start_sector", item.get("first_sector")))
        last = int(item["last_sector"])
        if last < first or first <= previous_last:
            raise PipelineError(f"{cell.get('name', 'cell')} has invalid physical segments")
        result.append({"start_sector": first, "last_sector": last})
        previous_last = last
    return result


def _logical_sector_to_physical(
    logical_sector: int, segments: list[dict[str, int]]
) -> tuple[int, int]:
    """Map a sector in a concatenated extraction to (domain sector, segment index)."""
    cursor = 0
    for index, segment in enumerate(segments):
        count = int(segment["last_sector"]) - int(segment["start_sector"]) + 1
        if cursor <= logical_sector < cursor + count:
            return int(segment["start_sector"]) + logical_sector - cursor, index
        cursor += count
    raise PipelineError(f"Logical sector {logical_sector} is outside the segmented extraction")


def _plan_cell(
    cell: dict[str, Any], quality: int | float | str, *, psm_policy: str,
    menu_domain: bool = False,
) -> dict[str, Any]:
    if psm_policy not in {"dvd-cell-entry-psm-v1", "dvd-vobu-psm-v1"}:
        raise PipelineError(f"Unsupported compact PSM policy: {psm_policy}")
    attempt = exact_quality_attempt(cell, quality)
    source = Path(cell["source_cell"])
    if not source.is_file():
        raise PipelineError(f"Source cell is missing: {source}")
    encoded = Path(attempt["encoded"])
    if not encoded.is_file():
        raise PipelineError(f"Exact-quality encode is missing: {encoded}")
    packets = read_video_pes(encoded)
    if menu_domain:
        packets = prepare_hevc_dvd_menu(packets)
    budget = attempt["budget"]
    pts_offset = int(budget["pts_offset"])
    segments = _physical_segments(cell)
    original_cell_sectors = sum(
        int(segment["last_sector"]) - int(segment["start_sector"]) + 1
        for segment in segments
    )
    if source.stat().st_size != original_cell_sectors * DVD_SECTOR_SIZE:
        raise PipelineError(f"Source size does not match the physical segments for {cell['name']}")
    budget_vobus = list(budget["vobus"])
    if not budget_vobus:
        raise PipelineError(f"{cell['name']} has no planned VOBUs")
    # Ordinary Phase 6 reports retain domain-relative sector numbers.  The
    # segmented Phase 7 extractor numbers its concatenated logical file from
    # zero.  Normalize both forms before reading the source cell.
    first_budget_sector = int(budget_vobus[0]["first_sector"])
    last_budget_sector = int(budget_vobus[-1]["last_sector"])
    first_physical_sector = int(segments[0]["start_sector"])
    if (
        len(segments) == 1
        and first_budget_sector >= first_physical_sector
        and last_budget_sector <= int(segments[0]["last_sector"])
    ):
        budget_sector_base = first_physical_sector
    elif first_budget_sector >= 0 and last_budget_sector < original_cell_sectors:
        budget_sector_base = 0
    else:
        raise PipelineError(f"Cannot normalize VOBU sectors for {cell['name']}")
    rows: list[dict[str, Any]] = []
    compact_cursor = 0

    with source.open("rb") as handle:
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
            for vobu in budget_vobus:
                local_first = int(vobu["first_sector"]) - budget_sector_base
                local_last = int(vobu["last_sector"]) - budget_sector_base
                absolute_first, first_segment = _logical_sector_to_physical(local_first, segments)
                absolute_last, last_segment = _logical_sector_to_physical(local_last, segments)
                if first_segment != last_segment:
                    raise PipelineError(
                        f"VOBU {vobu['index']} in {cell['name']} crosses a physical segment boundary"
                    )
                source_slots: list[VideoSlot] = []
                source_audio_counts: Counter[str] = Counter()
                source_audio_unsafe: Counter[str] = Counter()
                source_audio_multi_identity_sectors = 0
                for local_sector in range(local_first, local_last + 1):
                    offset = local_sector * DVD_SECTOR_SIZE
                    sector = data[offset : offset + DVD_SECTOR_SIZE]
                    slot = video_slot(sector, local_sector)
                    if slot is not None:
                        source_slots.append(slot)
                    identities, unsafe_identities = _dvd_audio_inventory(sector, local_sector)
                    if len(identities) > 1:
                        source_audio_multi_identity_sectors += 1
                    for identity in identities:
                        source_audio_counts[_audio_identity_key(*identity)] += 1
                    for identity in unsafe_identities:
                        source_audio_unsafe[_audio_identity_key(*identity)] += 1
                # Prefixes longer than a bare 14-byte pack header carry system/audio/etc.
                # They must survive compaction even when their old video payload does not.
                mandatory = [slot.prefix for slot in source_slots if len(slot.prefix) > 14]
                vobu_packets = [
                    packet for packet in packets
                    if packet.pts is not None
                    and int(vobu["start_ptm"]) <= int(packet.pts) + pts_offset < int(vobu["end_ptm"])
                ]
                video_sectors = minimum_video_slots(
                    mandatory,
                    vobu_packets,
                    pts_offset=pts_offset,
                    start_ptm=int(vobu["start_ptm"]),
                    end_ptm=int(vobu["end_ptm"]),
                    add_psm=(
                        bool(vobu_packets)
                        and (
                            psm_policy == "dvd-vobu-psm-v1"
                            or int(vobu["index"]) == 0
                        )
                    ),
                )
                original_sectors = absolute_last - absolute_first + 1
                nonvideo_sectors = original_sectors - len(source_slots)
                compact_sectors = nonvideo_sectors + video_sectors
                rows.append({
                    "index": int(vobu["index"]),
                    "original_first_sector": absolute_first,
                    "original_last_sector": absolute_last,
                    "source_local_first_sector": local_first,
                    "source_local_last_sector": local_last,
                    "physical_segment": first_segment,
                    "original_sectors": original_sectors,
                    "original_video_sectors": len(source_slots),
                    "source_audio_inventory_policy": "dvd-sector-identity-count-v1",
                    "source_audio_sectors_by_identity": dict(source_audio_counts),
                    "source_audio_unsafe_sectors_by_identity": dict(source_audio_unsafe),
                    "source_audio_multi_identity_sectors": source_audio_multi_identity_sectors,
                    "mandatory_prefix_sectors": len(mandatory),
                    "compact_cell_sector": compact_cursor,
                    "compact_sectors": compact_sectors,
                    "compact_video_sectors": video_sectors,
                    "encoded_bytes": int(vobu["encoded_bytes"]),
                    "packet_count": len(vobu_packets),
                    "sector_delta": compact_sectors - original_sectors,
                })
                compact_cursor += compact_sectors

    if len(rows) != int(cell["vobu_count"]):
        raise PipelineError(f"VOBU count mismatch while planning {cell['name']}")
    return {
        "name": cell["name"],
        "cell": cell["cell"],
        "source_cell": str(source.resolve()),
        "encoded": str(encoded.resolve()),
        "encoder": attempt["encode"]["encoder"],
        "preset": attempt["encode"]["preset"],
        "quality": attempt["encode"]["quality"],
        "program_stream_map_policy": psm_policy,
        "original_sectors": original_cell_sectors,
        "compact_sectors": compact_cursor,
        "sector_delta": compact_cursor - original_cell_sectors,
        "physical_segments": segments,
        "vobus": rows,
    }


def plan_compact_layout(
    report_paths: list[Path],
    destination: Path,
    *,
    quality: int | float | str = 20,
    audio_mode: str = "passthrough",
    stereo_audio_bitrate: int = 256_000,
    mono_audio_bitrate: int = 128_000,
    quality_preset: dict[str, Any] | None = None,
    quality_by_vts: dict[int | str, int | float | str] | None = None,
) -> dict[str, Any]:
    if not report_paths:
        raise PipelineError("At least one complete domain report is required")
    if audio_mode not in {"passthrough", "compact-stereo"}:
        raise PipelineError(f"Unsupported compact audio mode: {audio_mode}")
    stereo_audio_bitrate = parse_audio_bitrate(stereo_audio_bitrate)
    mono_audio_bitrate = parse_audio_bitrate(mono_audio_bitrate)
    reports = [json.loads(path.resolve().read_text(encoding="utf-8")) for path in report_paths]
    if any(report.get("status") != "passed" for report in reports):
        raise PipelineError("Every compact-layout input report must have passed")
    sources = {str(Path(report["source"]).resolve()) for report in reports}
    if len(sources) != 1:
        raise PipelineError("Compact-layout reports must use the same source ISO")
    encoders = {
        str((report.get("settings") or {}).get("encoder") or "hevc_nvenc")
        for report in reports
    }
    if len(encoders) != 1:
        raise PipelineError(
            "Compact-layout reports must use one HEVC encoder throughout the disc: "
            + ", ".join(sorted(encoders))
        )
    selected_encoder = next(iter(encoders))
    domains = [(str(report.get("domain") or "title"), int(report["vts"])) for report in reports]
    if len(set(domains)) != len(domains):
        raise PipelineError("Compact-layout reports contain duplicate domain/VTS coverage")

    domain_rows: list[dict[str, Any]] = []
    quality_counts: Counter[int | float | str] = Counter()
    for report_path, report, (domain, vts) in zip(report_paths, reports, domains):
        domain_quality = quality
        if domain == "title" and quality_by_vts:
            selected = quality_by_vts.get(vts, quality_by_vts.get(str(vts)))
            if selected is not None:
                domain_quality = selected
        cells = sorted(report["cells"], key=lambda cell: int(cell["cell"]["first_sector"]))
        planned_cells: list[dict[str, Any]] = []
        psm_policy = str(
            (report.get("settings") or {}).get(
                "program_stream_map_policy", "dvd-cell-entry-psm-v1"
            )
        )
        for cell in cells:
            planned = _plan_cell(
                cell, domain_quality, psm_policy=psm_policy,
                menu_domain=domain in {"vmg_menu", "vts_menu"},
            )
            quality_counts[planned["quality"]] += len(planned["vobus"])
            planned_cells.append(planned)

        # A branching cell is logically contiguous but physically alternates
        # with another cut.  Allocate compact sectors in original *physical*
        # VOBU order so every shared clip remains single-copy and the authored
        # disc retains its interleave topology.
        physical_vobus = sorted(
            (
                (int(vobu["original_first_sector"]), cell, vobu)
                for cell in planned_cells for vobu in cell["vobus"]
            ),
            key=lambda item: item[0],
        )
        if not physical_vobus:
            raise PipelineError(f"Compact layout has no VOBUs for {domain} VTS {vts}")
        domain_cursor = 0
        old_cursor = 0
        for old_first, _cell, vobu in physical_vobus:
            old_last = int(vobu["original_last_sector"])
            if old_first != old_cursor or old_last < old_first:
                raise PipelineError(
                    f"Physical VOBU coverage is not contiguous at sector {old_cursor}: "
                    f"found {old_first}-{old_last}"
                )
            vobu["compact_domain_sector"] = domain_cursor
            domain_cursor += int(vobu["compact_sectors"])
            old_cursor = old_last + 1

        for cell in planned_cells:
            cell_vobus = sorted(cell["vobus"], key=lambda item: int(item["index"]))
            cell["compact_first_sector"] = int(cell_vobus[0]["compact_domain_sector"])
            cell["compact_last_sector"] = (
                int(cell_vobus[-1]["compact_domain_sector"])
                + int(cell_vobus[-1]["compact_sectors"]) - 1
            )
            compact_segments: list[dict[str, int]] = []
            for segment_index, source_segment in enumerate(cell["physical_segments"]):
                segment_vobus = [
                    vobu for vobu in cell_vobus
                    if int(vobu["physical_segment"]) == segment_index
                ]
                if not segment_vobus:
                    raise PipelineError(
                        f"Physical segment {segment_index} in {cell['name']} contains no VOBUs"
                    )
                first_vobu = segment_vobus[0]
                last_vobu = segment_vobus[-1]
                compact_segments.append({
                    "original_start_sector": int(source_segment["start_sector"]),
                    "original_last_sector": int(source_segment["last_sector"]),
                    "compact_start_sector": int(first_vobu["compact_domain_sector"]),
                    "compact_last_sector": (
                        int(last_vobu["compact_domain_sector"])
                        + int(last_vobu["compact_sectors"]) - 1
                    ),
                    "vobus": len(segment_vobus),
                })
            cell["compact_segments"] = compact_segments

        original = old_cursor
        domain_rows.append({
            "domain": domain,
            "vts": vts,
            "quality": domain_quality,
            "input_report": str(report_path.resolve()),
            "original_sectors": original,
            "compact_sectors": domain_cursor,
            "sector_delta": domain_cursor - original,
            "physical_order": True,
            "program_stream_map_policy": psm_policy,
            "interleaved_cells": sum(
                bool(cell["cell"].get("interleaved")) for cell in planned_cells
            ),
            "cells": planned_cells,
        })

    original_sectors = sum(int(row["original_sectors"]) for row in domain_rows)
    compact_sectors = sum(int(row["compact_sectors"]) for row in domain_rows)
    result = {
        "schema": "dvd2hevc-compact-layout-v0",
        "status": "planned",
        "source": next(iter(sources)),
        "quality_policy": {
            "mode": "exact-no-fallback",
            "encoder": selected_encoder,
            "quality": quality,
            "quality_by_vts": {
                str(key): value for key, value in (quality_by_vts or {}).items()
            },
            "preset": quality_preset or {"name": "manual", "resolved_quality": quality},
            "vobu_quality_counts": dict(sorted(quality_counts.items(), key=lambda item: str(item[0]))),
        },
        "audio_policy": {
            "mode": audio_mode,
            "codec": "source" if audio_mode == "passthrough" else "ac3",
            "sample_rate": None if audio_mode == "passthrough" else 48_000,
            "stereo_bitrate": None if audio_mode == "passthrough" else stereo_audio_bitrate,
            "mono_bitrate": None if audio_mode == "passthrough" else mono_audio_bitrate,
            "sector_savings_included": False,
            "implementation": (
                "byte-exact-source-packets"
                if audio_mode == "passthrough"
                else "elementary-prototype-passed-pes-packetization-pending"
            ),
        },
        "domains": domain_rows,
        "summary": {
            "domains": len(domain_rows),
            "cells": sum(len(row["cells"]) for row in domain_rows),
            "vobus": sum(len(cell["vobus"]) for row in domain_rows for cell in row["cells"]),
            "original_sectors": original_sectors,
            "compact_sectors": compact_sectors,
            "original_bytes": original_sectors * DVD_SECTOR_SIZE,
            "compact_vob_bytes": compact_sectors * DVD_SECTOR_SIZE,
            "saved_bytes": (original_sectors - compact_sectors) * DVD_SECTOR_SIZE,
            "saved_percent": round(100 * (original_sectors - compact_sectors) / original_sectors, 3),
            "audio_savings_included": False,
            "vobus_expanding": sum(
                1 for row in domain_rows for cell in row["cells"] for vobu in cell["vobus"]
                if int(vobu["sector_delta"]) > 0
            ),
            "vobus_shrinking": sum(
                1 for row in domain_rows for cell in row["cells"] for vobu in cell["vobus"]
                if int(vobu["sector_delta"]) < 0
            ),
        },
        "rewrite_requirements": [
            "Rebuild compact VOBU sectors and MPEG pack SCR values",
            "Rewrite NAV PCI/DSI LBN, VOBU end, and forward/backward search pointers",
            "Rewrite VMG/VTS C_ADT and VOBU_ADMAP tables",
            "Rewrite every PGC cell first/last/last-VOBU sector field",
            "Recalculate VTS menu/title starts, VOB file splits, and authored UDF extents",
        ],
    }
    expected_controls = {_canonical_quality(quality)}
    expected_controls.update(_canonical_quality(value) for value in (quality_by_vts or {}).values())
    actual_controls = {_canonical_quality(value) for value in quality_counts}
    if not actual_controls <= expected_controls:
        raise PipelineError(f"Compact plan violated exact quality policy: {quality_counts}")
    _atomic_json(destination, result)
    return result


def _put_u32(data: bytearray, offset: int, value: int) -> None:
    data[offset : offset + 4] = int(value & 0xFFFFFFFF).to_bytes(4, "big")


def _put_u16(data: bytearray, offset: int, value: int) -> None:
    if not 0 <= value <= 0xFFFF:
        raise PipelineError(f"Compact NAV 16-bit value is out of range: {value}")
    data[offset : offset + 2] = int(value).to_bytes(2, "big")


def _map_relative_sector(value: int, old_to_new: dict[int, int], kept: list[int]) -> int:
    if value in old_to_new:
        return old_to_new[value]
    if not kept:
        return 0
    nearest = min(kept, key=lambda old: abs(old - value))
    return old_to_new[nearest]


def _patch_nav_sector(
    sector: bytes,
    *,
    old_vobu_start: int,
    new_vobu_start: int,
    compact_sectors: int,
    old_vobu_to_new: dict[int, int],
    old_vobu_end_to_new: dict[int, int],
    old_relative_to_new: dict[int, int],
    kept_video_relative: list[int],
    audio_sync_relative: dict[int, int] | None = None,
) -> bytes:
    """Rewrite baseline non-angle PCI/DSI sector references in one NAV pack."""
    output = bytearray(sector)
    pci_offset: int | None = None
    dsi_offset: int | None = None
    for stream_id, start, _end in _program_stream_packets(sector):
        if stream_id != 0xBF or start + 7 > len(sector):
            continue
        if sector[start + 6] == 0:
            pci_offset = start + 7
        elif sector[start + 6] == 1:
            dsi_offset = start + 7
    if pci_offset is None or dsi_offset is None:
        raise PipelineError("Compact VOBU does not begin with a complete PCI/DSI NAV pack")
    _put_u32(output, pci_offset, new_vobu_start)
    nonseamless_angles = [
        int.from_bytes(sector[pci_offset + 60 + index * 4 : pci_offset + 64 + index * 4], "big")
        for index in range(9)
    ]
    if any(nonseamless_angles):
        raise PipelineError(
            "Compact relocation does not yet support PCI non-seamless angle destinations"
        )
    _put_u32(output, dsi_offset + 4, new_vobu_start)
    _put_u32(output, dsi_offset + 8, compact_sectors - 1)

    # MPEG-2 reference-image offsets are irrelevant to HEVC decoding, but keep
    # them inside the rewritten VOBU for readers that sanity-check the fields.
    for offset in (12, 16, 20):
        old = int.from_bytes(sector[dsi_offset + offset : dsi_offset + offset + 4], "big")
        _put_u32(
            output,
            dsi_offset + offset,
            _map_relative_sector(old, old_relative_to_new, kept_video_relative),
        )

    def rewrite_sri(offset: int, forward: bool) -> None:
        old = int.from_bytes(sector[dsi_offset + offset : dsi_offset + offset + 4], "big")
        flags = old & 0xC0000000
        distance = old & 0x3FFFFFFF
        if distance == 0x3FFFFFFF:
            return
        target = old_vobu_start + distance if forward else old_vobu_start - distance
        new_target = old_vobu_to_new.get(target)
        if new_target is None:
            raise PipelineError(
                f"NAV search pointer from {old_vobu_start} targets unknown VOBU {target}"
            )
        new_distance = new_target - new_vobu_start if forward else new_vobu_start - new_target
        _put_u32(output, dsi_offset + offset, flags | new_distance)

    for offset in [234, *[238 + index * 4 for index in range(19)], 314]:
        rewrite_sri(offset, True)
    for offset in [318, *[322 + index * 4 for index in range(19)], 398]:
        rewrite_sri(offset, False)

    # Seamless branching stores the end of the current ILVU, the beginning
    # of the next same-cut ILVU, and that next unit's size in SML_PBI.  These
    # are physical-sector relationships, so they must be relocated along with
    # the ordinary VOBU search pointers.  Taken 2/3 use this two-cut form and
    # leave the nine seamless-angle addresses zero.
    old_ilvu_ea = int.from_bytes(sector[dsi_offset + 34 : dsi_offset + 38], "big")
    old_ilvu_sa = int.from_bytes(sector[dsi_offset + 38 : dsi_offset + 42], "big")
    old_next_size = int.from_bytes(sector[dsi_offset + 42 : dsi_offset + 44], "big")
    if old_ilvu_ea:
        old_end = old_vobu_start + old_ilvu_ea
        new_end = old_vobu_end_to_new.get(old_end)
        if new_end is None:
            raise PipelineError(
                f"ILVU end from {old_vobu_start} targets unknown sector {old_end}"
            )
        _put_u32(output, dsi_offset + 34, new_end - new_vobu_start)
    if old_ilvu_sa == 0xFFFFFFFF:
        if old_next_size != 0xFFFF:
            raise PipelineError("Terminal ILVU sentinel has an unexpected size")
        # 0xffffffff/0xffff is the authored no-next-ILVU marker.
    elif old_ilvu_sa:
        old_next = old_vobu_start + old_ilvu_sa
        new_next = old_vobu_to_new.get(old_next)
        if new_next is None:
            raise PipelineError(
                f"Next ILVU from {old_vobu_start} targets unknown VOBU {old_next}"
            )
        _put_u32(output, dsi_offset + 38, new_next - new_vobu_start)
        if old_next_size:
            old_next_end = old_next + old_next_size - 1
            new_next_end = old_vobu_end_to_new.get(old_next_end)
            if new_next_end is None:
                raise PipelineError(
                    f"Next ILVU size from {old_vobu_start} ends at unknown sector {old_next_end}"
                )
            _put_u16(output, dsi_offset + 42, new_next_end - new_next + 1)
    elif old_next_size:
        raise PipelineError("SML_PBI has an ILVU size without a next-ILVU address")

    angle_addresses = [
        int.from_bytes(sector[dsi_offset + 180 + index * 6 : dsi_offset + 184 + index * 6], "big")
        for index in range(9)
    ]
    if any(angle_addresses):
        raise PipelineError(
            "Compact relocation supports seamless branching but not multi-angle SML_AGLI addresses"
        )

    # Audio and subpicture synchronization addresses are VOBU-relative.
    for index in range(8):
        offset = dsi_offset + 402 + index * 2
        old = int.from_bytes(sector[offset : offset + 2], "big")
        if audio_sync_relative is not None and index in audio_sync_relative:
            output[offset : offset + 2] = (
                (old & 0xC000) | int(audio_sync_relative[index])
            ).to_bytes(2, "big")
            continue
        distance = old & 0x3FFF
        if distance and distance != 0x3FFF:
            mapped = _map_relative_sector(distance, old_relative_to_new, list(old_relative_to_new))
            output[offset : offset + 2] = ((old & 0xC000) | mapped).to_bytes(2, "big")
    for index in range(32):
        offset = dsi_offset + 418 + index * 4
        old = int.from_bytes(sector[offset : offset + 4], "big")
        distance = old & 0x3FFFFFFF
        if distance:
            mapped = _map_relative_sector(distance, old_relative_to_new, list(old_relative_to_new))
            _put_u32(output, offset, (old & 0xC0000000) | mapped)
    return bytes(output)


def _evenly_selected(values: list[VideoSlot], count: int) -> list[VideoSlot]:
    if count <= 0:
        return []
    if count >= len(values):
        return list(values)
    if count == 1:
        return [values[0]]
    indexes = [round(index * (len(values) - 1) / (count - 1)) for index in range(count)]
    return [values[index] for index in indexes]


def _pack_dvd_audio_sector(slot: VideoSlot, pes: bytes) -> bytes:
    remaining = DVD_SECTOR_SIZE - len(slot.prefix) - len(pes)
    if remaining < 0:
        raise PipelineError(
            f"Compact AC-3 PES exceeds source pack capacity at sector {slot.sector_index}"
        )
    if remaining == 0:
        return slot.prefix + pes
    if remaining < 6:
        raise PipelineError("Compact AC-3 pack leaves an invalid short padding remainder")
    return slot.prefix + pes + padding_packet(remaining)


def _retime_expanded_vobu(
    sectors: list[bytes],
    source_sectors: list[bytes],
) -> tuple[list[bytes], int]:
    """Fit a larger pack count inside the source VOBU's SCR interval."""
    if len(sectors) <= len(source_sectors):
        return sectors, 0
    if len(sectors) < 2 or len(source_sectors) < 2:
        raise PipelineError("Cannot retime an expanded VOBU without an SCR interval")
    source_headers = [parse_mpeg2_pack_header(sector) for sector in source_sectors]
    start_scr = source_headers[0].scr_27mhz
    end_scr = source_headers[-1].scr_27mhz
    if end_scr <= start_scr:
        raise PipelineError("Expanded VOBU has a non-increasing source SCR interval")
    span = end_scr - start_scr
    intervals = len(sectors) - 1
    minimum_step = span // intervals
    if minimum_step <= 0:
        raise PipelineError("Expanded VOBU has insufficient SCR resolution")
    # program_mux_rate is in units of 50 bytes/second. Round upward so the
    # declared delivery rate is never below the densest generated pack pair.
    required_rate = (
        DVD_SECTOR_SIZE * SCR_TICKS_PER_SECOND + minimum_step * 50 - 1
    ) // (minimum_step * 50)
    mux_rate = max(required_rate, *(header.program_mux_rate for header in source_headers))
    rewritten = [
        rewrite_mpeg2_pack_header(
            sector,
            scr_27mhz=start_scr + round(index * span / intervals),
            program_mux_rate=mux_rate,
        )
        for index, sector in enumerate(sectors)
    ]
    if parse_mpeg2_pack_header(rewritten[-1]).scr_27mhz != end_scr:
        raise AssertionError("Expanded VOBU retiming did not preserve its end SCR")
    return rewritten, mux_rate


def _validate_compact_nav_file(path: Path, row: dict[str, Any]) -> dict[str, int]:
    """Prove that every relocated NAV address closes over the compact VOBU graph."""
    planned = sorted(
        (
            int(vobu["compact_domain_sector"]),
            int(vobu["compact_sectors"]),
        )
        for cell in row["cells"] for vobu in cell["vobus"]
    )
    starts = {first for first, _count in planned}
    ends = {first + count - 1 for first, count in planned}
    sri_targets = 0
    ilvu_targets = 0
    with path.open("rb") as handle:
        for first, count in planned:
            handle.seek(first * DVD_SECTOR_SIZE)
            sector = handle.read(DVD_SECTOR_SIZE)
            if len(sector) != DVD_SECTOR_SIZE:
                raise PipelineError(f"Short compact NAV read at sector {first}")
            pci_offset: int | None = None
            dsi_offset: int | None = None
            for stream_id, start, _end in _program_stream_packets(sector):
                if stream_id != 0xBF or start + 7 > len(sector):
                    continue
                if sector[start + 6] == 0:
                    pci_offset = start + 7
                elif sector[start + 6] == 1:
                    dsi_offset = start + 7
            if pci_offset is None or dsi_offset is None:
                raise PipelineError(f"Compact VOBU {first} has no complete NAV pack")
            if int.from_bytes(sector[pci_offset : pci_offset + 4], "big") != first:
                raise PipelineError(f"PCI LBN differs at compact VOBU {first}")
            if int.from_bytes(sector[dsi_offset + 4 : dsi_offset + 8], "big") != first:
                raise PipelineError(f"DSI LBN differs at compact VOBU {first}")
            if int.from_bytes(sector[dsi_offset + 8 : dsi_offset + 12], "big") != count - 1:
                raise PipelineError(f"DSI VOBU end differs at compact VOBU {first}")

            def check_sri(offset: int, forward: bool) -> None:
                nonlocal sri_targets
                raw = int.from_bytes(sector[dsi_offset + offset : dsi_offset + offset + 4], "big")
                distance = raw & 0x3FFFFFFF
                if distance == 0x3FFFFFFF:
                    return
                target = first + distance if forward else first - distance
                if target not in starts:
                    raise PipelineError(
                        f"Compact SRI at {first} targets non-VOBU sector {target}"
                    )
                sri_targets += 1

            for offset in [234, *[238 + index * 4 for index in range(19)], 314]:
                check_sri(offset, True)
            for offset in [318, *[322 + index * 4 for index in range(19)], 398]:
                check_sri(offset, False)

            ilvu_ea = int.from_bytes(sector[dsi_offset + 34 : dsi_offset + 38], "big")
            ilvu_sa = int.from_bytes(sector[dsi_offset + 38 : dsi_offset + 42], "big")
            ilvu_size = int.from_bytes(sector[dsi_offset + 42 : dsi_offset + 44], "big")
            if ilvu_ea:
                if first + ilvu_ea not in ends:
                    raise PipelineError(f"Compact ILVU at {first} has an invalid end")
                ilvu_targets += 1
            if ilvu_sa == 0xFFFFFFFF:
                if ilvu_size != 0xFFFF:
                    raise PipelineError(f"Compact ILVU at {first} has a malformed terminal sentinel")
            elif ilvu_sa:
                next_start = first + ilvu_sa
                if next_start not in starts:
                    raise PipelineError(f"Compact ILVU at {first} has an invalid next start")
                if ilvu_size and next_start + ilvu_size - 1 not in ends:
                    raise PipelineError(f"Compact ILVU at {first} has an invalid next size")
                ilvu_targets += 1
    return {
        "vobus": len(planned),
        "sri_targets": sri_targets,
        "ilvu_targets": ilvu_targets,
    }


def _validate_compact_audio_file(
    path: Path,
    row: dict[str, Any],
    expected_groups: dict[int, list[bytes]],
) -> dict[str, Any]:
    """Prove every generated DVD AC-3 PES survived compact VOB placement exactly."""
    packets_by_stream: Counter[int] = Counter()
    checked_vobus = 0
    with path.open("rb") as handle:
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
            planned = sorted(
                (
                    int(vobu["compact_domain_sector"]),
                    int(vobu["compact_sectors"]),
                    int(vobu["original_first_sector"]),
                )
                for cell in row["cells"] for vobu in cell["vobus"]
            )
            for compact_first, compact_sectors, original_first in planned:
                actual: list[bytes] = []
                for sector_index in range(compact_first, compact_first + compact_sectors):
                    sector = bytes(
                        data[sector_index * DVD_SECTOR_SIZE : (sector_index + 1) * DVD_SECTOR_SIZE]
                    )
                    for stream_id, start, end in _program_stream_packets(sector):
                        if stream_id != 0xBD or start + 9 > end:
                            continue
                        payload_start = start + 9 + sector[start + 8]
                        if payload_start < end and 0x80 <= sector[payload_start] <= 0x87:
                            packet = sector[start:end]
                            actual.append(packet)
                            packets_by_stream[int(sector[payload_start])] += 1
                expected = expected_groups.get(original_first, [])
                if actual != expected:
                    raise PipelineError(
                        f"Compact audio PES readback changed VOBU {original_first}: "
                        f"expected={len(expected)}, actual={len(actual)}"
                    )
                checked_vobus += 1
    return {
        "vobus": checked_vobus,
        "packets": sum(packets_by_stream.values()),
        "packets_by_stream": {
            f"0x{stream_id:02x}": count
            for stream_id, count in sorted(packets_by_stream.items())
        },
        "packet_readback": "exact",
    }


def _compact_row_proves_no_audio(row: dict[str, Any]) -> bool:
    """Require complete per-cell scan evidence before treating a VTS as silent."""
    cells = row.get("cells") or []
    if not cells:
        return False

    # Compact-first planning inventories every sector while it is already in
    # memory.  This is the strongest and cheapest proof for current reports,
    # whose compact-input validation intentionally has no repacked
    # ``validation.structure`` object.
    inventory_vobus = [
        vobu
        for cell in cells
        for vobu in (cell.get("vobus") or [])
    ]
    inventory_complete = (
        bool(inventory_vobus)
        and all(cell.get("vobus") for cell in cells)
        and all(
            vobu.get("source_audio_inventory_policy")
            == "dvd-sector-identity-count-v1"
            and isinstance(vobu.get("source_audio_sectors_by_identity"), dict)
            and not vobu["source_audio_sectors_by_identity"]
            and not (vobu.get("source_audio_unsafe_sectors_by_identity") or {})
            and int(vobu.get("source_audio_multi_identity_sectors") or 0) == 0
            for vobu in inventory_vobus
        )
    )
    if inventory_complete:
        return True

    try:
        report = json.loads(Path(row["input_report"]).read_text(encoding="utf-8"))
        by_name = {str(item["name"]): item for item in report["cells"]}
        planned_names = [str(cell["name"]) for cell in cells]

        def scanned_audio_packets(name: str) -> int:
            source = by_name[name]
            structure = (source.get("validation") or {}).get("structure")
            if isinstance(structure, dict) and "audio_pes_packets" in structure:
                return int(structure["audio_pes_packets"])
            # Compact-input reports retain the extractor's complete VOB scan
            # rather than manufacturing a sector-preserving validation object.
            extraction = source.get("extraction") or {}
            vob_stats = extraction.get("vob_stats") or {}
            if "audio_pes_packets" in vob_stats:
                return int(vob_stats["audio_pes_packets"])
            raise KeyError("complete audio scan evidence")

        return bool(planned_names) and all(
            scanned_audio_packets(name) == 0
            for name in planned_names
        )
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
        return False


def _source_local_vobu_sector_range(
    cell: dict[str, Any], vobu: dict[str, Any]
) -> range:
    """Map a physical VOBU to its extracted cell file, including ILVU gaps."""
    original_first = int(vobu["original_first_sector"])
    original_last = int(vobu["original_last_sector"])
    cell_first = int(cell["cell"]["first_sector"])
    local_first = int(vobu.get("source_local_first_sector", original_first - cell_first))
    local_last = int(vobu.get("source_local_last_sector", original_last - cell_first))
    if local_first < 0 or local_last - local_first != original_last - original_first:
        raise PipelineError(
            f"VOBU {original_first}-{original_last} has an invalid extracted-sector mapping "
            f"{local_first}-{local_last}"
        )
    return range(local_first, local_last + 1)


def prototype_compact_domain(
    layout_path: Path,
    *,
    domain: str,
    vts: int,
    destination: Path,
    _loaded_layout: dict[str, Any] | None = None,
    _layout_sha256: str | None = None,
) -> dict[str, Any]:
    """Write a compact VOB domain in original physical VOBU order."""
    layout_path = layout_path.resolve()
    layout = (
        _loaded_layout
        if _loaded_layout is not None
        else json.loads(layout_path.read_text(encoding="utf-8"))
    )
    requested_audio_mode = (layout.get("audio_policy") or {}).get("mode", "passthrough")
    # Compact-stereo policy applies to title audio. Menu audio is deliberately
    # retained byte-exact while its video VOBUs and navigation are relocated.
    audio_mode = requested_audio_mode if domain == "title" else "passthrough"
    row = next(
        (item for item in layout["domains"] if item["domain"] == domain and int(item["vts"]) == vts),
        None,
    )
    if row is None:
        raise PipelineError(f"Compact layout has no {domain} VTS {vts}")
    audio_groups: dict[int, list[bytes]] = {}
    audio_private_substream_ids: set[int] = set()
    audio_packet_stream_ids: set[int] = set()
    silent_compact_audio = False
    if audio_mode == "compact-stereo":
        if not (layout.get("audio_policy") or {}).get("sector_savings_included"):
            raise PipelineError("Compact-stereo layout has not incorporated audio sector counts")
        metadata = row.get("compact_audio")
        if not metadata:
            if not _compact_row_proves_no_audio(row):
                raise PipelineError(f"Compact-stereo layout has no audio metadata for {domain} VTS {vts}")
            silent_compact_audio = True
        else:
            audio_report = json.loads(Path(metadata["audio_report"]).read_text(encoding="utf-8"))
            audio_groups, regenerated = _compact_audio_groups(layout, row, audio_report)
            audio_private_substream_ids = {
                int(value) for value in regenerated["source_private_substream_ids"]
            }
            audio_packet_stream_ids = {
                int(value) for value in regenerated["source_packet_stream_ids"]
            }
            if int(regenerated["dvd_pes_packets"]) != int(metadata["dvd_pes_packets"]):
                raise PipelineError("Compact audio packet count changed since layout planning")
    elif audio_mode != "passthrough":
        raise PipelineError(f"Unsupported compact-domain audio mode: {audio_mode}")
    old_vobu_to_new = {
        int(vobu["original_first_sector"]): int(vobu["compact_domain_sector"])
        for cell in row["cells"] for vobu in cell["vobus"]
    }
    old_vobu_end_to_new = {
        int(vobu["original_last_sector"]): (
            int(vobu["compact_domain_sector"]) + int(vobu["compact_sectors"]) - 1
        )
        for cell in row["cells"] for vobu in cell["vobus"]
    }
    new_intervals = sorted(
        (
            int(vobu["compact_domain_sector"]),
            int(vobu["compact_domain_sector"]) + int(vobu["compact_sectors"]) - 1,
        )
        for cell in row["cells"] for vobu in cell["vobus"]
    )
    cursor = 0
    for first, last in new_intervals:
        if first != cursor or last < first:
            raise PipelineError(f"Compact VOBU allocation is not contiguous at sector {cursor}")
        cursor = last + 1
    if cursor != int(row["compact_sectors"]):
        raise PipelineError("Compact VOBU allocation size differs from the domain plan")
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.part")
    output_cells: list[dict[str, Any]] = []
    synthetic_video_sectors = 0
    synthetic_audio_sectors = 0
    retimed_vobus = 0
    maximum_mux_rate = 0
    input_report = json.loads(Path(row["input_report"]).read_text(encoding="utf-8"))
    source_reports = {item["name"]: item for item in input_report["cells"]}
    try:
        with temporary.open("w+b") as output:
            output.truncate(int(row["compact_sectors"]) * DVD_SECTOR_SIZE)
            for cell_number, cell in enumerate(row["cells"], start=1):
                progress_event(
                    "mux", "progress", str(cell["name"]),
                    current=cell_number - 1, total=len(row["cells"]), vts=vts,
                )
                source = Path(cell["source_cell"])
                packets = read_video_pes(Path(cell["encoded"]))
                if str(row.get("domain") or "title") in {"vmg_menu", "vts_menu"}:
                    packets = prepare_hevc_dvd_menu(packets)
                # Reuse the exact-quality attempt's timestamp offset.
                source_report = source_reports.get(cell["name"])
                if source_report is None:
                    raise PipelineError(f"Input report has no source row for {cell['name']}")
                attempt = exact_quality_attempt(
                    source_report, row.get("quality", layout["quality_policy"]["quality"])
                )
                pts_offset = int(attempt["budget"]["pts_offset"])
                written_vobus = 0
                psm_written = False
                psm_policy = str(
                    row.get("program_stream_map_policy", "dvd-cell-entry-psm-v1")
                )
                with source.open("rb") as handle:
                    with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
                        for vobu in cell["vobus"]:
                            old_first = int(vobu["original_first_sector"])
                            old_last = int(vobu["original_last_sector"])
                            source_local_first = int(vobu.get(
                                "source_local_first_sector",
                                old_first - int(cell["cell"]["first_sector"]),
                            ))
                            source_local_last = int(vobu.get(
                                "source_local_last_sector",
                                old_last - int(cell["cell"]["first_sector"]),
                            ))
                            source_sectors = [
                                bytes(data[sector * DVD_SECTOR_SIZE : (sector + 1) * DVD_SECTOR_SIZE])
                                for sector in range(source_local_first, source_local_last + 1)
                            ]
                            slots: list[VideoSlot] = []
                            audio_slots: list[VideoSlot] = []
                            for relative, sector_data in enumerate(source_sectors):
                                slot = video_slot(sector_data, relative)
                                if slot is not None:
                                    slots.append(slot)
                                if audio_private_substream_ids or audio_packet_stream_ids:
                                    audio_slot = _dvd_audio_slot_any(
                                        sector_data, relative, audio_private_substream_ids,
                                        audio_packet_stream_ids,
                                    )
                                    if audio_slot is not None:
                                        audio_slots.append(audio_slot)
                            mandatory = [slot for slot in slots if len(slot.prefix) > 14]
                            pure = [slot for slot in slots if len(slot.prefix) == 14]
                            desired = int(vobu["compact_video_sectors"])
                            selected = sorted(
                                [*mandatory, *_evenly_selected(pure, desired - len(mandatory))],
                                key=lambda slot: slot.sector_index,
                            )
                            if desired > len(selected):
                                if not slots:
                                    raise PipelineError("Cannot synthesize video packs without a source template")
                                template = slots[-1].prefix
                                parsed_template = parse_mpeg2_pack_header(template)
                                additions = desired - len(selected)
                                selected.extend(
                                    VideoSlot(
                                        len(source_sectors) + index,
                                        build_mpeg2_pack_header(
                                            parsed_template.scr_27mhz,
                                            program_mux_rate=parsed_template.program_mux_rate,
                                        ),
                                    )
                                    for index in range(additions)
                                )
                                synthetic_video_sectors += additions
                            vobu_packets = [
                                packet for packet in packets
                                if packet.pts is not None
                                and int(attempt["budget"]["vobus"][int(vobu["index"])]["start_ptm"])
                                <= int(packet.pts) + pts_offset
                                < int(attempt["budget"]["vobus"][int(vobu["index"])]["end_ptm"])
                            ]
                            packed = _pack_packets_into_slots(
                                selected,
                                vobu_packets,
                                pts_offset=pts_offset,
                                start_ptm=int(attempt["budget"]["vobus"][int(vobu["index"])]["start_ptm"]),
                                end_ptm=int(attempt["budget"]["vobus"][int(vobu["index"])]["end_ptm"]),
                                add_psm=(
                                    bool(vobu_packets)
                                    and (
                                        psm_policy == "dvd-vobu-psm-v1"
                                        or not psm_written
                                    )
                                ),
                            )
                            if vobu_packets:
                                psm_written = True
                            replacements = {
                                slot.sector_index: sector_data for slot, sector_data in zip(selected, packed)
                            }
                            new_audio_packets = audio_groups.get(old_first, [])
                            if audio_mode == "compact-stereo":
                                missing_default = 0 if silent_compact_audio else -1
                                if len(new_audio_packets) != int(vobu.get("compact_audio_sectors", missing_default)):
                                    raise PipelineError("Compact audio VOBU count differs from the planned layout")
                            selected_audio = _evenly_selected(audio_slots, len(new_audio_packets))
                            if len(new_audio_packets) > len(selected_audio):
                                if not source_sectors:
                                    raise PipelineError("Cannot synthesize compact audio packs without a source template")
                                template = parse_mpeg2_pack_header(source_sectors[-1])
                                next_index = max(
                                    [len(source_sectors), *[slot.sector_index + 1 for slot in selected]]
                                )
                                additions = len(new_audio_packets) - len(selected_audio)
                                selected_audio.extend(
                                    VideoSlot(
                                        next_index + index,
                                        build_mpeg2_pack_header(
                                            template.scr_27mhz,
                                            program_mux_rate=template.program_mux_rate,
                                        ),
                                    )
                                    for index in range(additions)
                                )
                                synthetic_audio_sectors += additions
                            audio_replacements = {
                                slot.sector_index: _pack_dvd_audio_sector(slot, pes)
                                for slot, pes in zip(selected_audio, new_audio_packets)
                            }
                            kept: list[tuple[int, bytes]] = []
                            for relative, sector_data in enumerate(source_sectors):
                                slot = video_slot(sector_data, relative)
                                audio_slot = (
                                    _dvd_audio_slot_any(
                                        sector_data, relative, audio_private_substream_ids,
                                        audio_packet_stream_ids,
                                    )
                                    if audio_private_substream_ids or audio_packet_stream_ids else None
                                )
                                if audio_slot is not None:
                                    if relative in audio_replacements:
                                        kept.append((relative, audio_replacements[relative]))
                                elif slot is None:
                                    kept.append((relative, sector_data))
                                elif relative in replacements:
                                    kept.append((relative, replacements[relative]))
                            kept.extend(
                                (slot.sector_index, replacements[slot.sector_index])
                                for slot in selected
                                if slot.sector_index >= len(source_sectors)
                            )
                            kept.extend(
                                (slot.sector_index, audio_replacements[slot.sector_index])
                                for slot in selected_audio
                                if slot.sector_index >= len(source_sectors)
                            )
                            old_relative_to_new = {
                                old_relative: new_relative
                                for new_relative, (old_relative, _sector) in enumerate(kept)
                            }
                            if len(kept) != int(vobu["compact_sectors"]):
                                raise PipelineError(
                                    f"Prototype VOBU size differs from plan for {cell['name']} #{vobu['index']}"
                                )
                            nav_indexes = [
                                index for index, (_old, sector_data) in enumerate(kept)
                                if any(
                                    stream_id == 0xBF and sector_data[start + 6] == 0
                                    for stream_id, start, _end in _program_stream_packets(sector_data)
                                )
                            ]
                            if nav_indexes != [0]:
                                raise PipelineError(f"Expected one leading NAV sector, found {nav_indexes}")
                            new_start = int(vobu["compact_domain_sector"])
                            audio_sync_relative: dict[int, int] | None = None
                            if selected_audio:
                                audio_sync_relative = {}
                                for slot, pes in zip(selected_audio, new_audio_packets):
                                    parsed_audio = parse_audio_pes(pes)
                                    if not parsed_audio.payload:
                                        raise PipelineError("Compact audio PES has no DVD substream header")
                                    audio_index = int(parsed_audio.payload[0]) - 0x80
                                    if not 0 <= audio_index <= 7:
                                        raise PipelineError("Compact audio PES has an invalid DVD stream id")
                                    new_relative = old_relative_to_new[slot.sector_index]
                                    audio_sync_relative.setdefault(audio_index, new_relative)
                            kept[0] = (
                                kept[0][0],
                                _patch_nav_sector(
                                    kept[0][1],
                                    old_vobu_start=old_first,
                                    new_vobu_start=new_start,
                                    compact_sectors=len(kept),
                                    old_vobu_to_new=old_vobu_to_new,
                                    old_vobu_end_to_new=old_vobu_end_to_new,
                                    old_relative_to_new=old_relative_to_new,
                                    kept_video_relative=[slot.sector_index for slot in selected],
                                    audio_sync_relative=audio_sync_relative,
                                ),
                            )
                            if len(kept) > len(source_sectors):
                                retimed, mux_rate = _retime_expanded_vobu(
                                    [sector_data for _old, sector_data in kept],
                                    source_sectors,
                                )
                                kept = [
                                    (old_relative, sector_data)
                                    for (old_relative, _old_sector), sector_data in zip(kept, retimed)
                                ]
                                retimed_vobus += 1
                                maximum_mux_rate = max(maximum_mux_rate, mux_rate)
                            output.seek(new_start * DVD_SECTOR_SIZE)
                            for _old_relative, sector_data in kept:
                                output.write(sector_data)
                            written_vobus += 1
                output_cells.append({
                    "name": cell["name"],
                    "first_sector": int(cell["compact_first_sector"]),
                    "last_sector": int(cell["compact_last_sector"]),
                    "sectors": int(cell["compact_sectors"]),
                    "physical_segments": cell.get("compact_segments") or [{
                        "compact_start_sector": int(cell["compact_first_sector"]),
                        "compact_last_sector": int(cell["compact_last_sector"]),
                    }],
                    "vobus": written_vobus,
                })
                progress_event(
                    "mux", "progress", str(cell["name"]),
                    current=cell_number, total=len(row["cells"]), vts=vts,
                )
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    stats = inspect_vob_file(destination)
    if stats["invalid_sectors"] or stats["scrambled_pes_packets"]:
        raise PipelineError(f"Prototype compact VOB scan failed: {stats}")
    navigation = _validate_compact_nav_file(destination, row)
    audio_validation = (
        _validate_compact_audio_file(destination, row, audio_groups)
        if audio_mode == "compact-stereo"
        else None
    )
    psm_policy = str(row.get("program_stream_map_policy", "dvd-cell-entry-psm-v1"))
    expected_maps = (
        sum(
            int(vobu.get("packet_count", 0)) > 0
            for cell in row["cells"] for vobu in cell["vobus"]
        )
        if psm_policy == "dvd-vobu-psm-v1"
        else sum(1 for cell in output_cells if int(cell["vobus"]) > 0)
    )
    if int(stats["program_stream_maps"]) != expected_maps:
        raise PipelineError(
            f"Compact domain HEVC map count differs from {psm_policy}: "
            f"expected={expected_maps}, actual={stats['program_stream_maps']}"
        )
    result = {
        "schema": "dvd2hevc-compact-domain-prototype-v0",
        "status": "passed",
        "layout": str(layout_path),
        "layout_sha256": _layout_sha256 or hashlib.sha256(layout_path.read_bytes()).hexdigest(),
        "domain": domain,
        "vts": vts,
        "destination": str(destination),
        "quality": row.get("quality", layout["quality_policy"]["quality"]),
        "audio_mode": audio_mode,
        "silent_compact_audio": silent_compact_audio,
        "cells": output_cells,
        "summary": {
            "original_sectors": int(row["original_sectors"]),
            "compact_sectors": int(row["compact_sectors"]),
            "saved_percent": round(
                100 * (int(row["original_sectors"]) - int(row["compact_sectors"]))
                / int(row["original_sectors"]), 3
            ),
            "vobus": sum(int(cell["vobus"]) for cell in output_cells),
            "program_stream_maps": stats["program_stream_maps"],
            "program_stream_map_policy": psm_policy,
            "invalid_sectors": stats["invalid_sectors"],
            "compact_audio_packets": sum(
                len(packets) for packets in audio_groups.values()
            ),
            "synthetic_video_sectors": synthetic_video_sectors,
            "synthetic_audio_sectors": synthetic_audio_sectors,
            "retimed_expanding_vobus": retimed_vobus,
            "maximum_program_mux_rate": maximum_mux_rate or None,
            "navigation": navigation,
            "audio_validation": audio_validation,
        },
        "limits": [
            "This raw-domain command does not apply the separate compact VMGI/VTSI IFO/BUP rewrite",
            "Expanding VOBUs use synthesized pack headers and preserve their source SCR interval",
            "Stage only with matching rewritten compact control files",
        ],
    }
    _atomic_json(destination.with_suffix(destination.suffix + ".json"), result)
    return result


def prototype_compact_domain_batch(
    layout_path: Path,
    tasks: list[dict[str, Any]],
    report_path: Path,
) -> dict[str, Any]:
    """Write several compact domains while parsing one large layout once.

    Every domain keeps its existing standalone report and atomic destination,
    so a failure or cancellation remains resumable at domain granularity.
    """
    layout_path = layout_path.resolve()
    layout_bytes = layout_path.read_bytes()
    try:
        layout = json.loads(layout_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PipelineError(f"Invalid compact layout JSON: {layout_path}") from exc
    if layout.get("status") != "planned" or not isinstance(layout.get("domains"), list):
        raise PipelineError("Compact-domain batch requires a passed planned layout")
    layout_sha256 = hashlib.sha256(layout_bytes).hexdigest()
    normalized: list[tuple[str, int, Path, int]] = []
    seen: set[tuple[str, int]] = set()
    for item in tasks:
        if not isinstance(item, dict):
            raise PipelineError("Compact-domain batch tasks must be JSON objects")
        domain = str(item.get("domain") or "")
        if domain not in {"title", "vts_menu", "vmg_menu"}:
            raise PipelineError(f"Unsupported compact-domain batch domain: {domain}")
        try:
            vts = int(item["vts"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PipelineError("Compact-domain batch task has no valid VTS number") from exc
        key = (domain, vts)
        if key in seen:
            raise PipelineError(f"Duplicate compact-domain batch task: {domain} VTS {vts}")
        seen.add(key)
        destination_value = str(item.get("destination") or "")
        if not destination_value:
            raise PipelineError(f"Compact-domain batch task has no destination: {domain} VTS {vts}")
        row = next(
            (
                value for value in layout["domains"]
                if str(value.get("domain")) == domain and int(value.get("vts")) == vts
            ),
            None,
        )
        if row is None:
            raise PipelineError(f"Compact layout has no {domain} VTS {vts}")
        normalized.append((domain, vts, Path(destination_value), int(row["compact_sectors"])))
    results: list[dict[str, Any]] = []
    all_compact_sectors = sum(int(row["compact_sectors"]) for row in layout["domains"])
    completed_compact_sectors = all_compact_sectors - sum(item[3] for item in normalized)
    progress_event(
        "mux", "start", "compact-domain-batch",
        current=completed_compact_sectors, total=all_compact_sectors,
        unit="sectors", domains_current=0, domains_total=len(normalized),
    )
    for index, (domain, vts, destination, compact_sectors) in enumerate(normalized, start=1):
        result = prototype_compact_domain(
            layout_path,
            domain=domain,
            vts=vts,
            destination=destination,
            _loaded_layout=layout,
            _layout_sha256=layout_sha256,
        )
        results.append({
            "domain": domain,
            "vts": vts,
            "destination": str(destination.resolve()),
            "report": str(destination.resolve().with_suffix(destination.suffix + ".json")),
            "compact_sectors": result["summary"]["compact_sectors"],
        })
        completed_compact_sectors += compact_sectors
        progress_event(
            "mux", "progress", "compact-domain-batch",
            current=completed_compact_sectors, total=all_compact_sectors,
            unit="sectors", domains_current=index, domains_total=len(normalized),
            domain=domain, vts=vts,
        )
    report = {
        "schema": "dvd2hevc-compact-domain-batch-v1",
        "status": "passed",
        "layout": str(layout_path),
        "layout_sha256": layout_sha256,
        "tasks": results,
        "summary": {
            "domains": len(results),
            "compact_sectors": sum(int(item["compact_sectors"]) for item in results),
            "layout_reads": 1,
        },
    }
    _atomic_json(report_path.resolve(), report)
    progress_event(
        "mux", "done", "compact-domain-batch",
        current=all_compact_sectors, total=all_compact_sectors,
        unit="sectors", domains_current=len(normalized), domains_total=len(normalized),
    )
    return report
