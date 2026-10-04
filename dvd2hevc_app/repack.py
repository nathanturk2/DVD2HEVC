"""Experimental sector-preserving HEVC repacketizer for a single DVD cell."""

from __future__ import annotations

import os
import mmap
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .phase2 import analyze_vobu_budgets
from .transport import VideoPes
from .vob import DVD_SECTOR_SIZE


PTS_MASK = (1 << 33) - 1
SCR_TICKS_PER_SECOND = 27_000_000


@dataclass
class VideoSlot:
    sector_index: int
    prefix: bytes

    @property
    def content_limit(self) -> int:
        # Always reserve six bytes for a valid padding-stream header.
        return DVD_SECTOR_SIZE - len(self.prefix) - 6


@dataclass(frozen=True)
class Mpeg2PackHeader:
    scr_27mhz: int
    program_mux_rate: int
    stuffing_length: int


def parse_mpeg2_pack_header(data: bytes) -> Mpeg2PackHeader:
    """Parse the MPEG-2 pack fields used to time DVD sectors."""
    if len(data) < 14 or data[:4] != b"\x00\x00\x01\xba" or data[4] & 0xC0 != 0x40:
        raise ValueError("Not an MPEG-2 pack header")
    stuffing_length = data[13] & 0x07
    if len(data) < 14 + stuffing_length:
        raise ValueError("Truncated MPEG-2 pack stuffing")
    if not (data[4] & 0x04 and data[6] & 0x04 and data[8] & 0x04 and data[9] & 0x01):
        raise ValueError("Invalid MPEG-2 SCR marker bits")
    if data[12] & 0x03 != 0x03:
        raise ValueError("Invalid MPEG-2 mux-rate marker bits")
    base = (
        ((data[4] & 0x38) << 27)
        | ((data[4] & 0x03) << 28)
        | (data[5] << 20)
        | ((data[6] & 0xF8) << 12)
        | ((data[6] & 0x03) << 13)
        | (data[7] << 5)
        | (data[8] >> 3)
    )
    extension = ((data[8] & 0x03) << 7) | (data[9] >> 1)
    program_mux_rate = (data[10] << 14) | (data[11] << 6) | (data[12] >> 2)
    return Mpeg2PackHeader(
        scr_27mhz=base * 300 + extension,
        program_mux_rate=program_mux_rate,
        stuffing_length=stuffing_length,
    )


def build_mpeg2_pack_header(
    scr_27mhz: int,
    *,
    program_mux_rate: int = 25_200,
    stuffing: bytes = b"",
) -> bytes:
    """Build a standards-shaped MPEG-2 pack header for a DVD-sized pack."""
    if not 0 <= scr_27mhz < (1 << 33) * 300:
        raise ValueError("SCR is outside the MPEG-2 33-bit base range")
    if not 1 <= program_mux_rate < (1 << 22):
        raise ValueError("Program mux rate is outside its 22-bit range")
    if len(stuffing) > 7:
        raise ValueError("MPEG-2 pack stuffing cannot exceed seven bytes")
    base, extension = divmod(scr_27mhz, 300)
    return bytes([
        0x00, 0x00, 0x01, 0xBA,
        0x40 | ((base >> 27) & 0x38) | 0x04 | ((base >> 28) & 0x03),
        (base >> 20) & 0xFF,
        ((base >> 12) & 0xF8) | 0x04 | ((base >> 13) & 0x03),
        (base >> 5) & 0xFF,
        ((base & 0x1F) << 3) | 0x04 | ((extension >> 7) & 0x03),
        ((extension & 0x7F) << 1) | 0x01,
        (program_mux_rate >> 14) & 0xFF,
        (program_mux_rate >> 6) & 0xFF,
        ((program_mux_rate & 0x3F) << 2) | 0x03,
        0xF8 | len(stuffing),
    ]) + stuffing


def rewrite_mpeg2_pack_header(
    data: bytes,
    *,
    scr_27mhz: int,
    program_mux_rate: int | None = None,
) -> bytes:
    """Replace SCR/mux-rate fields while preserving the pack's stuffing bytes."""
    parsed = parse_mpeg2_pack_header(data)
    header_size = 14 + parsed.stuffing_length
    header = build_mpeg2_pack_header(
        scr_27mhz,
        program_mux_rate=program_mux_rate or parsed.program_mux_rate,
        stuffing=data[14:header_size],
    )
    return header + data[header_size:]


def encode_pes_timestamp(value: int, prefix: int) -> bytes:
    value &= PTS_MASK
    return bytes(
        [
            (prefix << 4) | (((value >> 30) & 0x07) << 1) | 1,
            (value >> 22) & 0xFF,
            (((value >> 15) & 0x7F) << 1) | 1,
            (value >> 7) & 0xFF,
            ((value & 0x7F) << 1) | 1,
        ]
    )


def build_video_pes(payload: bytes, *, pts: int | None = None, dts: int | None = None) -> bytes:
    if pts is None:
        flags = 0
        timestamps = b""
    elif dts is None or dts == pts:
        flags = 0x80
        timestamps = encode_pes_timestamp(pts, 0x2)
    else:
        flags = 0xC0
        timestamps = encode_pes_timestamp(pts, 0x3) + encode_pes_timestamp(dts, 0x1)
    body = bytes([0x80, flags, len(timestamps)]) + timestamps + payload
    if len(body) > 0xFFFF:
        raise ValueError("PES fragment is too large")
    return b"\x00\x00\x01\xe0" + len(body).to_bytes(2, "big") + body


def mpeg2_crc32(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc


def build_hevc_psm(stream_id: int = 0xE0) -> bytes:
    # current_next=1, reserved=2, version=0; marker/reserved byte; no program
    # descriptors.  The low reserved bit is deliberately clear.  DVD CSS
    # readers inspect fixed sector byte 0x14 as though every video sector
    # began with a PES header.  A standards-reserved value of 3 here would
    # place 0xe0 at byte 0x14 whenever this PSM follows the pack header and
    # make libdvdcss falsely decrypt an already-clear replacement sector.
    # 0xc0 is accepted by MPEG-PS demuxers and keeps the custom DVD-HEVC
    # sector unambiguously clear without consuming any additional capacity.
    body_without_crc = bytes([0xC0, 0xFF, 0x00, 0x00, 0x00, 0x04, 0x24, stream_id, 0x00, 0x00])
    prefix = b"\x00\x00\x01\xbc" + (len(body_without_crc) + 4).to_bytes(2, "big") + body_without_crc
    return prefix + mpeg2_crc32(prefix).to_bytes(4, "big")


def _program_stream_packets(sector: bytes) -> list[tuple[int, int, int]]:
    if len(sector) != DVD_SECTOR_SIZE or sector[:4] != b"\x00\x00\x01\xba" or (sector[4] & 0xC0) != 0x40:
        return []
    offset = 14 + (sector[13] & 0x07)
    packets: list[tuple[int, int, int]] = []
    while offset + 6 <= DVD_SECTOR_SIZE and sector[offset : offset + 3] == b"\x00\x00\x01":
        stream_id = sector[offset + 3]
        packet_end = offset + 6 + int.from_bytes(sector[offset + 4 : offset + 6], "big")
        if packet_end > DVD_SECTOR_SIZE:
            return []
        packets.append((stream_id, offset, packet_end))
        offset = packet_end
    return packets


def video_slot(sector: bytes, sector_index: int) -> VideoSlot | None:
    packets = _program_stream_packets(sector)
    video_offsets = [start for stream_id, start, _end in packets if 0xE0 <= stream_id <= 0xEF]
    if not video_offsets:
        return None
    first_video = video_offsets[0]
    for stream_id, start, _end in packets:
        if start >= first_video and not (0xE0 <= stream_id <= 0xEF or stream_id == 0xBE):
            raise ValueError(f"Sector {sector_index} mixes video with stream 0x{stream_id:02x}")
    return VideoSlot(sector_index=sector_index, prefix=sector[:first_video])


def padding_packet(total_size: int) -> bytes:
    if total_size < 6:
        raise ValueError("A program-stream padding packet needs at least six bytes")
    return b"\x00\x00\x01\xbe" + (total_size - 6).to_bytes(2, "big") + bytes(total_size - 6)


def _pack_packets_into_slots(
    slots: list[VideoSlot],
    packets: list[VideoPes],
    *,
    pts_offset: int,
    start_ptm: int,
    end_ptm: int,
    add_psm: bool,
) -> list[bytes]:
    if not slots:
        if packets:
            raise ValueError("VOBU has encoded video but no original video slots")
        return []
    contents = [bytearray() for _slot in slots]
    if add_psm:
        contents[0].extend(build_hevc_psm())
    cursor = 0

    for packet in packets:
        adjusted_pts = ((packet.pts or 0) + pts_offset) & PTS_MASK if packet.pts is not None else None
        adjusted_dts = ((packet.dts or packet.pts or 0) + pts_offset) & PTS_MASK if packet.dts is not None or packet.pts is not None else None
        remaining = memoryview(packet.payload)
        first_fragment = True
        while remaining:
            while cursor < len(slots):
                header_size = 19 if first_fragment and adjusted_pts is not None and adjusted_dts != adjusted_pts else 14 if first_fragment and adjusted_pts is not None else 9
                available = slots[cursor].content_limit - len(contents[cursor])
                if available > header_size:
                    break
                cursor += 1
            if cursor >= len(slots):
                raise ValueError("Encoded HEVC exceeds the available VOBU video slots")
            header_size = 19 if first_fragment and adjusted_pts is not None and adjusted_dts != adjusted_pts else 14 if first_fragment and adjusted_pts is not None else 9
            take = min(len(remaining), slots[cursor].content_limit - len(contents[cursor]) - header_size)
            fragment = bytes(remaining[:take])
            contents[cursor].extend(
                build_video_pes(
                    fragment,
                    pts=adjusted_pts if first_fragment else None,
                    dts=adjusted_dts if first_fragment else None,
                )
            )
            remaining = remaining[take:]
            first_fragment = False
            if remaining:
                cursor += 1

    sectors: list[bytes] = []
    for slot, content in zip(slots, contents):
        remaining = DVD_SECTOR_SIZE - len(slot.prefix) - len(content)
        sector = slot.prefix + bytes(content) + padding_packet(remaining)
        if len(sector) != DVD_SECTOR_SIZE:
            raise AssertionError("Repacked sector changed size")
        sectors.append(sector)
    return sectors


def repack_cell_hevc(
    source_cell: Path,
    destination: Path,
    *,
    cell_first_sector: int,
    cell_last_sector: int,
    vobus: list[dict[str, Any]],
    encoded_pes: list[VideoPes],
    psm_policy: str = "cell-entry",
) -> dict[str, Any]:
    if psm_policy not in {"cell-entry", "every-video-vobu"}:
        raise ValueError(f"Unknown HEVC program-stream-map policy: {psm_policy}")
    expected = (cell_last_sector - cell_first_sector + 1) * DVD_SECTOR_SIZE
    source_size = source_cell.stat().st_size
    if source_size != expected:
        raise ValueError(f"Cell VOB size mismatch: expected {expected}, found {source_size}")
    budget = analyze_vobu_budgets(
        source_cell,
        cell_first_sector=cell_first_sector,
        cell_last_sector=cell_last_sector,
        vobus=vobus,
        encoded_pes=encoded_pes,
    )
    if not budget["all_vobus_fit"] or budget["unassigned_pes_count"]:
        raise ValueError(f"HEVC budget failed: {budget['over_budget_vobus']}")

    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.part")
    changed_sector_indexes: set[int] = set()
    relevant = [vobu for vobu in vobus if cell_first_sector <= int(vobu["sector"]) <= cell_last_sector]
    program_stream_maps = 0
    try:
        shutil.copyfile(source_cell, temporary)
        with source_cell.open("rb") as source_handle, temporary.open("r+b") as output_handle:
            with mmap.mmap(source_handle.fileno(), 0, access=mmap.ACCESS_READ) as source:
                with mmap.mmap(output_handle.fileno(), 0, access=mmap.ACCESS_WRITE) as output:
                    for index, vobu in enumerate(relevant):
                        absolute_start = int(vobu["sector"])
                        absolute_end = int(relevant[index + 1]["sector"]) - 1 if index + 1 < len(relevant) else cell_last_sector
                        start_ptm = int(vobu["start_ptm"])
                        end_ptm = int(vobu["end_ptm"])
                        local_first = absolute_start - cell_first_sector
                        local_last = absolute_end - cell_first_sector
                        slots: list[VideoSlot] = []
                        for sector_index in range(local_first, local_last + 1):
                            start = sector_index * DVD_SECTOR_SIZE
                            slot = video_slot(source[start : start + DVD_SECTOR_SIZE], sector_index)
                            if slot:
                                slots.append(slot)
                        packets = [
                            packet for packet in encoded_pes
                            if packet.pts is not None and start_ptm <= int(packet.pts) + budget["pts_offset"] < end_ptm
                        ]
                        add_psm = bool(packets) and (
                            psm_policy == "every-video-vobu" or program_stream_maps == 0
                        )
                        repacked = _pack_packets_into_slots(
                            slots,
                            packets,
                            pts_offset=budget["pts_offset"],
                            start_ptm=start_ptm,
                            end_ptm=end_ptm,
                            add_psm=add_psm,
                        )
                        if add_psm:
                            program_stream_maps += 1
                        for slot, sector in zip(slots, repacked):
                            start = slot.sector_index * DVD_SECTOR_SIZE
                            output[start : start + DVD_SECTOR_SIZE] = sector
                            changed_sector_indexes.add(slot.sector_index)

                    for sector_index in range(source_size // DVD_SECTOR_SIZE):
                        if sector_index in changed_sector_indexes:
                            continue
                        start = sector_index * DVD_SECTOR_SIZE
                        if output[start : start + DVD_SECTOR_SIZE] != source[start : start + DVD_SECTOR_SIZE]:
                            raise AssertionError(f"Non-video sector {sector_index} changed")
                    output.flush()
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "source": str(source_cell.resolve()),
        "destination": str(destination),
        "size": source_size,
        "sector_count": source_size // DVD_SECTOR_SIZE,
        "changed_video_sectors": len(changed_sector_indexes),
        "unchanged_sectors": source_size // DVD_SECTOR_SIZE - len(changed_sector_indexes),
        "program_stream_maps": program_stream_maps,
        "program_stream_map_policy": (
            "dvd-vobu-psm-v1" if psm_policy == "every-video-vobu"
            else "dvd-cell-entry-psm-v1"
        ),
        "budget": budget,
    }
