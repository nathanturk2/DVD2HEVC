"""Capacity analysis for the one-cell HEVC proof of concept."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .transport import VideoPes
from .vob import DVD_SECTOR_SIZE


def sector_video_payload_capacity(sector: bytes) -> int:
    if len(sector) != DVD_SECTOR_SIZE or sector[:4] != b"\x00\x00\x01\xba" or (sector[4] & 0xC0) != 0x40:
        return 0
    offset = 14 + (sector[13] & 0x07)
    capacity = 0
    while offset + 6 <= DVD_SECTOR_SIZE and sector[offset : offset + 3] == b"\x00\x00\x01":
        stream_id = sector[offset + 3]
        packet_end = offset + 6 + int.from_bytes(sector[offset + 4 : offset + 6], "big")
        if packet_end > DVD_SECTOR_SIZE:
            break
        if 0xE0 <= stream_id <= 0xEF and offset + 9 <= packet_end and (sector[offset + 6] & 0xC0) == 0x80:
            payload_start = offset + 9 + sector[offset + 8]
            if payload_start <= packet_end:
                capacity += packet_end - payload_start
        offset = packet_end
    return capacity


def analyze_vobu_budgets(
    cell_vob: Path,
    *,
    cell_first_sector: int,
    cell_last_sector: int,
    vobus: list[dict[str, Any]],
    encoded_pes: list[VideoPes],
) -> dict[str, Any]:
    expected = (cell_last_sector - cell_first_sector + 1) * DVD_SECTOR_SIZE
    actual_size = cell_vob.stat().st_size
    if actual_size != expected:
        raise ValueError(f"Cell VOB size mismatch: expected {expected}, found {actual_size}")
    relevant = [vobu for vobu in vobus if cell_first_sector <= int(vobu["sector"]) <= cell_last_sector]
    if not relevant:
        raise ValueError("No VOBUs overlap the selected cell")
    first_pts = next((packet.pts for packet in encoded_pes if packet.pts is not None), None)
    first_ptm = relevant[0].get("start_ptm")
    if first_pts is None or first_ptm is None:
        raise ValueError("VOBU and encoded PES timestamps are required")
    pts_offset = int(first_ptm) - int(first_pts)

    rows: list[dict[str, Any]] = []
    with cell_vob.open("rb") as handle:
        for index, vobu in enumerate(relevant):
            absolute_start = int(vobu["sector"])
            absolute_end = (
                int(relevant[index + 1]["sector"]) - 1
                if index + 1 < len(relevant)
                else cell_last_sector
            )
            local_start = absolute_start - cell_first_sector
            local_end = absolute_end - cell_first_sector
            capacity = 0
            handle.seek(local_start * DVD_SECTOR_SIZE)
            for _sector_index in range(local_start, local_end + 1):
                capacity += sector_video_payload_capacity(handle.read(DVD_SECTOR_SIZE))
            start_ptm = int(vobu.get("start_ptm") or 0)
            end_ptm = int(vobu.get("end_ptm") or start_ptm)
            packets = [
                packet
                for packet in encoded_pes
                if packet.pts is not None and start_ptm <= int(packet.pts) + pts_offset < end_ptm
            ]
            encoded_bytes = sum(len(packet.payload) for packet in packets)
            rows.append(
                {
                    "index": index,
                    "first_sector": absolute_start,
                    "last_sector": absolute_end,
                    "start_ptm": start_ptm,
                    "end_ptm": end_ptm,
                    "capacity": capacity,
                    "encoded_bytes": encoded_bytes,
                    "packet_count": len(packets),
                    "fits": encoded_bytes <= capacity,
                    "headroom": capacity - encoded_bytes,
                }
            )
    assigned = sum(row["packet_count"] for row in rows)
    return {
        "pts_offset": pts_offset,
        "vobu_count": len(rows),
        "encoded_pes_count": len(encoded_pes),
        "assigned_pes_count": assigned,
        "unassigned_pes_count": len(encoded_pes) - assigned,
        "total_capacity": sum(row["capacity"] for row in rows),
        "total_encoded_bytes": sum(row["encoded_bytes"] for row in rows),
        "all_vobus_fit": all(row["fits"] for row in rows),
        "over_budget_vobus": [row["index"] for row in rows if not row["fits"]],
        "vobus": rows,
    }
