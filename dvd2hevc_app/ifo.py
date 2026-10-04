"""Phase 6 VTS IFO relocation for compact title-domain VOBUs."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .atomic import replace_with_retry
from .pipeline import PipelineError
from .vob import DVD_SECTOR_SIZE


VTS_LAST_SECTOR_OFFSET = 0x0C
VTSI_LAST_SECTOR_OFFSET = 0x1C
VTSM_VOBS_OFFSET = 0xC0
VTSTT_VOBS_OFFSET = 0xC4
VTS_PGCIT_OFFSET = 0xCC
VTSM_PGCI_UT_OFFSET = 0xD0
VTS_TMAPT_OFFSET = 0xD4
VTSM_C_ADT_OFFSET = 0xD8
VTSM_VOBU_ADMAP_OFFSET = 0xDC
VTS_C_ADT_OFFSET = 0xE0
VTS_VOBU_ADMAP_OFFSET = 0xE4
VTS_AUDIO_COUNT_OFFSET = 0x203
VTS_AUDIO_ATTRS_OFFSET = 0x204
AUDIO_ATTR_SIZE = 8
VMG_LAST_SECTOR_OFFSET = 0x0C
VMGI_LAST_SECTOR_OFFSET = 0x1C
VMGI_LAST_BYTE_OFFSET = 0x80
VMG_FIRST_PLAY_PGC_OFFSET = 0x84
VMGM_VOBS_OFFSET = 0xC0
VMG_TT_SRPT_OFFSET = 0xC4
VMGM_PGCI_UT_OFFSET = 0xC8
VMGM_C_ADT_OFFSET = 0xD8
VMGM_VOBU_ADMAP_OFFSET = 0xDC
DVD_AUDIO_FORMATS = {"ac3": 0, "mp1": 2, "mp2": 3, "pcm_dvd": 4, "dts": 6}


def _validate_compact_audio_ordinals(audio_count: int, ordinals: list[int]) -> None:
    """Require unique observed tracks within, not equal to, the IFO declarations."""
    if len(ordinals) != len(set(ordinals)):
        raise PipelineError(f"Compact-stereo report has duplicate track ordinals: {ordinals}")
    outside = [ordinal for ordinal in ordinals if not 0 <= ordinal < int(audio_count)]
    if outside:
        raise PipelineError(
            f"Compact-stereo track ordinals are outside the VTS IFO range "
            f"0-{max(0, int(audio_count) - 1)}: {outside}"
        )


def _relocated_vts_extents(
    *,
    ifo_sectors: int,
    menu_start: int,
    title_start: int,
    original_menu_sectors: int,
    compact_menu_sectors: int,
    original_title_sectors: int,
    compact_title_sectors: int,
    old_last_sector: int,
) -> dict[str, int]:
    """Validate source VOB extents and describe a contiguous staged VTS.

    Padding sectors in an authored ISO are not part of any extracted VIDEO_TS
    file.  A stage made from those files is therefore contiguous even when the
    source IFO described alignment gaps.  The rewritten control fields must
    describe that staged layout; ``genisoimage -dvd-video`` adds the required
    ISO-level alignment again while authoring.
    """
    values = (
        ifo_sectors, menu_start, title_start, original_menu_sectors,
        compact_menu_sectors, original_title_sectors, compact_title_sectors,
        old_last_sector,
    )
    if any(int(value) < 0 for value in values) or ifo_sectors == 0:
        raise PipelineError("Invalid VTS menu/title VOB extents")
    if menu_start:
        if menu_start < ifo_sectors or title_start < menu_start + original_menu_sectors:
            raise PipelineError("Unexpected VTS menu/title VOB extents")
        prefix_padding = menu_start - ifo_sectors
        menu_title_padding = title_start - menu_start - original_menu_sectors
    else:
        if original_menu_sectors or compact_menu_sectors or title_start < ifo_sectors:
            raise PipelineError("Unexpected title-VOB start for a VTS without menus")
        prefix_padding = title_start - ifo_sectors
        menu_title_padding = 0
    tail_sectors = old_last_sector + 1 - title_start - original_title_sectors
    if tail_sectors < ifo_sectors:
        raise PipelineError("VTS extent does not retain a complete backup IFO")
    new_menu_start = ifo_sectors if compact_menu_sectors else 0
    new_title_start = ifo_sectors + compact_menu_sectors
    new_last_sector = (
        ifo_sectors * 2 + compact_menu_sectors + compact_title_sectors - 1
    )
    if new_title_start < 0 or new_last_sector < new_title_start:
        raise PipelineError("Relocated VTS extents are invalid")
    return {
        "new_menu_start": new_menu_start,
        "new_title_start": new_title_start,
        "new_last_sector": new_last_sector,
        "prefix_padding_sectors": prefix_padding,
        "menu_title_padding_sectors": menu_title_padding,
        "tail_sectors": tail_sectors,
    }


def _relocated_vmg_extents(
    *,
    ifo_sectors: int,
    menu_start: int,
    original_menu_sectors: int,
    compact_menu_sectors: int,
    old_last_sector: int,
) -> dict[str, int]:
    """Describe a compact contiguous VMGI, VMGM VOB, and backup IFO."""
    values = (
        ifo_sectors,
        menu_start,
        original_menu_sectors,
        compact_menu_sectors,
        old_last_sector,
    )
    if any(int(value) < 0 for value in values) or ifo_sectors == 0:
        raise PipelineError("Invalid VMG menu VOB extents")
    if not menu_start or menu_start < ifo_sectors:
        raise PipelineError("Unexpected VMG menu VOB start")
    tail_sectors = old_last_sector + 1 - menu_start - original_menu_sectors
    if tail_sectors < ifo_sectors:
        raise PipelineError("VMG extent does not retain a complete backup IFO")
    new_menu_start = ifo_sectors
    new_last_sector = ifo_sectors * 2 + compact_menu_sectors - 1
    return {
        "new_menu_start": new_menu_start,
        "new_last_sector": new_last_sector,
        "prefix_padding_sectors": menu_start - ifo_sectors,
        "tail_sectors": tail_sectors,
    }


def _normalize_staged_vts_extents(video_ts: Path, vts: int) -> bool:
    """Make one extracted/staged VTS IFO describe its contiguous files."""
    ifo_path = video_ts / f"VTS_{vts:02d}_0.IFO"
    bup_path = video_ts / f"VTS_{vts:02d}_0.BUP"
    if not ifo_path.is_file() or not bup_path.is_file():
        raise PipelineError(f"VTS {vts} IFO/BUP is missing from the stage")
    source = ifo_path.read_bytes()
    if source != bup_path.read_bytes():
        raise PipelineError(f"VTS {vts} IFO and BUP differ before extent normalization")
    if len(source) % DVD_SECTOR_SIZE or source[:12] != b"DVDVIDEO-VTS":
        raise PipelineError(f"Invalid staged VTS {vts} IFO")
    ifo_sectors = len(source) // DVD_SECTOR_SIZE
    if _u32(source, VTSI_LAST_SECTOR_OFFSET) + 1 != ifo_sectors:
        raise PipelineError(f"VTS {vts} IFO size field does not match its staged file")

    menu_path = video_ts / f"VTS_{vts:02d}_0.VOB"
    menu_sectors = 0
    if menu_path.is_file():
        if menu_path.stat().st_size % DVD_SECTOR_SIZE:
            raise PipelineError(f"VTS {vts} menu VOB is not sector aligned")
        menu_sectors = menu_path.stat().st_size // DVD_SECTOR_SIZE
    title_files = sorted(video_ts.glob(f"VTS_{vts:02d}_[1-9]*.VOB"))
    if not title_files or any(path.stat().st_size % DVD_SECTOR_SIZE for path in title_files):
        raise PipelineError(f"VTS {vts} title VOB files are missing or not sector aligned")
    title_sectors = sum(path.stat().st_size for path in title_files) // DVD_SECTOR_SIZE

    data = bytearray(source)
    _put_u32(data, VTSM_VOBS_OFFSET, ifo_sectors if menu_sectors else 0)
    _put_u32(data, VTSTT_VOBS_OFFSET, ifo_sectors + menu_sectors)
    _put_u32(
        data,
        VTS_LAST_SECTOR_OFFSET,
        ifo_sectors * 2 + menu_sectors + title_sectors - 1,
    )
    output = bytes(data)
    if output == source:
        return False
    _atomic_bytes(ifo_path, output)
    _atomic_bytes(bup_path, output)
    return True


def _u16(data: bytes | bytearray, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], "big")


def _u32(data: bytes | bytearray, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "big")


def _put_u32(data: bytearray, offset: int, value: int) -> None:
    if not 0 <= value <= 0xFFFFFFFF:
        raise PipelineError(f"IFO sector value is out of range: {value}")
    data[offset : offset + 4] = value.to_bytes(4, "big")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_bytes(path: Path, data: bytes) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.part")
    try:
        temporary.write_bytes(data)
        replace_with_retry(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _rewrite_compact_audio_attribute(
    data: bytearray,
    offset: int,
    *,
    source_codec: str,
    source_channels: int,
    target_channels: int,
    processing: str,
) -> str:
    """Validate one source descriptor and describe its compact AC-3 output."""
    source_codec = str(source_codec).lower()
    expected_format = DVD_AUDIO_FORMATS.get(source_codec)
    if expected_format is None or (data[offset] >> 5) != expected_format:
        raise PipelineError(
            f"VTS audio descriptor does not match source {source_codec or 'unknown'}"
        )
    descriptor_channels = (data[offset + 1] & 0x07) + 1
    if not 1 <= int(source_channels) <= 8:
        raise PipelineError(f"Invalid decoded source audio channel count: {source_channels}")
    if not 1 <= int(target_channels) <= 8:
        raise PipelineError(f"Invalid compact audio channel count: {target_channels}")
    if processing == "passthrough":
        if source_codec != "ac3" or int(source_channels) != int(target_channels):
            raise PipelineError("Only unchanged AC-3 can use compact-audio passthrough")
        return (
            "matched"
            if descriptor_channels == int(source_channels)
            else "authored-mismatch-preserved"
        )

    # The compact elementary stream is always 48 kHz AC-3. Clear the original
    # coding format and multichannel-extension flag while retaining language
    # type/application metadata, then clear source quantization/frequency bits.
    data[offset] &= 0x0F
    data[offset + 1] = (data[offset + 1] & 0x08) | (int(target_channels) - 1)
    data[offset + 7] = 0
    return (
        "matched"
        if descriptor_channels == int(source_channels)
        else "authored-mismatch-normalized"
    )


def relocated_title_set_starts(
    old_starts: dict[int, int],
    title_set_sectors: dict[int, int],
    *,
    first_start: int | None = None,
) -> dict[int, int]:
    """Pack title sets contiguously from an explicit or authored first start."""
    if not old_starts or set(old_starts) != set(title_set_sectors):
        raise PipelineError("VMGI title-set start/size coverage differs")
    result: dict[int, int] = {}
    cursor = old_starts[min(old_starts)] if first_start is None else int(first_start)
    if cursor < 0:
        raise PipelineError("VMGI first title-set sector is invalid")
    for vts in sorted(old_starts):
        result[vts] = cursor
        cursor += title_set_sectors[vts]
    return result


def rewrite_vmgi_title_set_sectors(
    video_ts: Path,
    *,
    first_title_set_sector: int | None = None,
) -> dict[str, Any]:
    """Normalize staged VTS extents and relocate VMGI title-set starts."""
    video_ts = video_ts.resolve()
    ifo_path = video_ts / "VIDEO_TS.IFO"
    bup_path = video_ts / "VIDEO_TS.BUP"
    source = ifo_path.read_bytes()
    if source != bup_path.read_bytes():
        raise PipelineError("VIDEO_TS IFO and BUP differ before VMGI relocation")
    if len(source) % DVD_SECTOR_SIZE or source[:12] != b"DVDVIDEO-VMG":
        raise PipelineError("Invalid VIDEO_TS.IFO while relocating title sets")
    data = bytearray(source)
    table = _u32(data, VMG_TT_SRPT_OFFSET) * DVD_SECTOR_SIZE
    if table <= 0 or table + 8 > len(data):
        raise PipelineError("VMGI TT_SRPT is outside VIDEO_TS.IFO")
    count = _u16(data, table)
    last_byte = _u32(data, table + 4)
    if table + last_byte >= len(data) or last_byte + 1 != 8 + count * 12:
        raise PipelineError("Invalid VMGI TT_SRPT length")
    entries: list[tuple[int, int]] = []
    old_starts: dict[int, int] = {}
    for index in range(count):
        entry = table + 8 + index * 12
        vts = data[entry + 6]
        old = _u32(data, entry + 8)
        if not vts:
            raise PipelineError("VMGI title points to VTS zero")
        if vts in old_starts and old_starts[vts] != old:
            raise PipelineError(f"VMGI titles disagree on the VTS {vts} start")
        old_starts[vts] = old
        entries.append((entry, vts))
    sizes: dict[int, int] = {}
    normalized_title_sets: list[int] = []
    for vts in old_starts:
        if _normalize_staged_vts_extents(video_ts, vts):
            normalized_title_sets.append(vts)
        files = list(video_ts.glob(f"VTS_{vts:02d}_*"))
        if not files or any(not path.is_file() or path.stat().st_size % DVD_SECTOR_SIZE for path in files):
            raise PipelineError(f"VTS {vts} files are missing or not sector aligned")
        sizes[vts] = sum(path.stat().st_size for path in files) // DVD_SECTOR_SIZE
        vts_ifo = (video_ts / f"VTS_{vts:02d}_0.IFO").read_bytes()
        if _u32(vts_ifo, VTS_LAST_SECTOR_OFFSET) + 1 != sizes[vts]:
            raise PipelineError(f"VTS {vts} IFO extent does not match its staged files")
    starts = relocated_title_set_starts(
        old_starts,
        sizes,
        first_start=first_title_set_sector,
    )
    for entry, vts in entries:
        _put_u32(data, entry + 8, starts[vts])
    output = bytes(data)
    _atomic_bytes(ifo_path, output)
    _atomic_bytes(bup_path, output)
    return {
        "titles": count,
        "old_starts": old_starts,
        "new_starts": starts,
        "first_title_set_sector": starts[min(starts)],
        "title_set_sectors": sizes,
        "changed_title_sets": sum(old_starts[vts] != starts[vts] for vts in starts),
        "normalized_title_sets": normalized_title_sets,
        "rewritten_control_files": 2 + len(normalized_title_sets) * 2,
    }


def _domain_row(layout: dict[str, Any], vts: int, domain: str = "title") -> dict[str, Any]:
    matches = [
        row for row in layout.get("domains", [])
        if row.get("domain") == domain and int(row.get("vts", -1)) == vts
    ]
    if len(matches) != 1:
        raise PipelineError(f"Compact layout has {len(matches)} {domain} rows for VTS {vts}")
    return matches[0]


def _address_maps(row: dict[str, Any]) -> tuple[dict[int, int], dict[int, int]]:
    """Build physical VOBU start/end relocation maps for ordinary or ILVU titles."""
    starts: dict[int, int] = {}
    ends: dict[int, int] = {}
    for cell in row.get("cells", []):
        planned_vobus = cell.get("vobus") or []
        if not planned_vobus:
            raise PipelineError(f"Compact cell {cell.get('name')} has no VOBUs")
        for vobu in planned_vobus:
            old_first = int(vobu["original_first_sector"])
            old_last = int(vobu["original_last_sector"])
            new_first = int(vobu["compact_domain_sector"])
            new_last = new_first + int(vobu["compact_sectors"]) - 1
            if old_first in starts or old_last in ends:
                raise PipelineError(f"Compact layout duplicates source VOBU {old_first}-{old_last}")
            starts[old_first] = new_first
            ends[old_last] = new_last
    starts = dict(sorted(starts.items()))
    ends = dict(sorted(ends.items()))
    if not starts or len(starts) != len(ends):
        raise PipelineError("Compact layout has incomplete VOBU address maps")
    return starts, ends


def _patch_pgcit_at(
    data: bytearray,
    table: int,
    starts: dict[int, int],
    ends: dict[int, int],
) -> dict[str, int]:
    count = _u16(data, table)
    last_byte = _u32(data, table + 4)
    if table + last_byte >= len(data):
        raise PipelineError("VTS PGCIT extends beyond the IFO")
    patched = 0
    pgcs_with_cells = 0
    for index in range(count):
        srp = table + 8 + index * 8
        pgc = table + _u32(data, srp + 4)
        result = _patch_pgc_at(
            data,
            pgc,
            limit=table + last_byte + 1,
            starts=starts,
            ends=ends,
            label="menu/title PGC",
        )
        if result["cells"]:
            pgcs_with_cells += 1
            patched += int(result["cells"])
    return {"pgcs": count, "pgcs_with_cells": pgcs_with_cells, "cells": patched}


def _patch_pgc_at(
    data: bytearray,
    pgc: int,
    *,
    limit: int,
    starts: dict[int, int],
    ends: dict[int, int],
    label: str,
) -> dict[str, int]:
    """Relocate the cell playback table of one PGC stored at a byte offset."""
    if pgc < 0 or pgc + 236 > limit or limit > len(data):
        raise PipelineError(f"{label} starts outside its containing IFO table")
    nr_cells = data[pgc + 3]
    playback_offset = _u16(data, pgc + 232)
    position_offset = _u16(data, pgc + 234)
    if nr_cells == 0:
        return {"cells": 0}
    if not playback_offset or not position_offset:
        raise PipelineError(f"{label} has cells but no playback/position table")
    if (
        pgc + playback_offset + nr_cells * 24 > limit
        or pgc + position_offset + nr_cells * 4 > limit
    ):
        raise PipelineError(f"{label} cell tables extend outside their containing IFO table")
    for cell_index in range(nr_cells):
        position = pgc + position_offset + cell_index * 4
        key = (_u16(data, position), data[position + 3])
        playback = pgc + playback_offset + cell_index * 24
        old_values = [_u32(data, playback + offset) for offset in (8, 12, 16, 20)]
        first, first_ilvu_end, last_vobu, last = old_values
        if first not in starts or last_vobu not in starts or last not in ends:
            raise PipelineError(
                f"{label} cell {key} references an unplanned VOBU boundary: {old_values}"
            )
        if first_ilvu_end and first_ilvu_end not in ends:
            raise PipelineError(
                f"{label} cell {key} first ILVU ends at an unplanned boundary "
                f"{first_ilvu_end}"
            )
        new_values = [
            starts[first],
            ends[first_ilvu_end] if first_ilvu_end else 0,
            starts[last_vobu],
            ends[last],
        ]
        for offset, value in zip((8, 12, 16, 20), new_values):
            _put_u32(data, playback + offset, value)
    return {"cells": nr_cells}


def _patch_pgcit(
    data: bytearray,
    sector: int,
    starts: dict[int, int],
    ends: dict[int, int],
) -> dict[str, int]:
    return _patch_pgcit_at(data, sector * DVD_SECTOR_SIZE, starts, ends)


def _patch_pgci_ut(
    data: bytearray,
    sector: int,
    starts: dict[int, int],
    ends: dict[int, int],
) -> dict[str, int]:
    """Patch every distinct menu-language PGCIT inside a PGCI_UT."""
    table = sector * DVD_SECTOR_SIZE
    languages = _u16(data, table)
    last_byte = _u32(data, table + 4)
    if not languages or table + last_byte >= len(data):
        raise PipelineError("VTSM PGCI_UT is empty or extends beyond the IFO")
    seen: set[int] = set()
    totals = {"languages": languages, "pgcs": 0, "pgcs_with_cells": 0, "cells": 0}
    for index in range(languages):
        unit = table + 8 + index * 8
        pgcit = table + _u32(data, unit + 4)
        if pgcit in seen:
            continue
        seen.add(pgcit)
        patched = _patch_pgcit_at(data, pgcit, starts, ends)
        for key in ("pgcs", "pgcs_with_cells", "cells"):
            totals[key] += int(patched[key])
    return totals


def _patch_c_adt(
    data: bytearray,
    sector: int,
    starts: dict[int, int],
    ends: dict[int, int],
) -> int:
    table = sector * DVD_SECTOR_SIZE
    last_byte = _u32(data, table + 4)
    if last_byte + 1 < 8 or (last_byte + 1 - 8) % 12:
        raise PipelineError("Invalid VTS C_ADT length")
    count = (last_byte + 1 - 8) // 12
    for index in range(count):
        entry = table + 8 + index * 12
        key = (_u16(data, entry), data[entry + 2])
        old = (_u32(data, entry + 4), _u32(data, entry + 8))
        if old[0] not in starts or old[1] not in ends:
            raise PipelineError(f"C_ADT cell {key} has an unplanned boundary: {old}")
        _put_u32(data, entry + 4, starts[old[0]])
        _put_u32(data, entry + 8, ends[old[1]])
    return count


def _patch_vobu_admap(data: bytearray, sector: int, vobus: dict[int, int]) -> int:
    table = sector * DVD_SECTOR_SIZE
    last_byte = _u32(data, table)
    if last_byte + 1 < 4 or (last_byte + 1 - 4) % 4:
        raise PipelineError("Invalid VTS VOBU_ADMAP length")
    count = (last_byte + 1 - 4) // 4
    old = [_u32(data, table + 4 + index * 4) for index in range(count)]
    if old != list(vobus):
        raise PipelineError("VTS VOBU_ADMAP does not match the compact layout source order")
    for index, value in enumerate(vobus.values()):
        _put_u32(data, table + 4 + index * 4, value)
    return count


def _patch_tmapt(data: bytearray, sector: int, vobus: dict[int, int]) -> dict[str, int]:
    if sector == 0:
        return {"maps": 0, "entries": 0}
    table = sector * DVD_SECTOR_SIZE
    count = _u16(data, table)
    last_byte = _u32(data, table + 4)
    if table + last_byte >= len(data):
        raise PipelineError("VTS_TMAPT extends beyond the IFO")
    entries = 0
    for index in range(count):
        tmap = table + _u32(data, table + 8 + index * 4)
        nr_entries = _u16(data, tmap + 2)
        for entry_index in range(nr_entries):
            offset = tmap + 4 + entry_index * 4
            raw = _u32(data, offset)
            flag = raw & 0x80000000
            old_sector = raw & 0x7FFFFFFF
            if old_sector not in vobus:
                raise PipelineError(f"VTS time-map sector {old_sector} is not a VOBU start")
            _put_u32(data, offset, flag | vobus[old_sector])
            entries += 1
    return {"maps": count, "entries": entries}


def rewrite_compact_vmgi(
    layout_path: Path,
    *,
    source_ifo: Path,
    destination_ifo: Path,
    destination_bup: Path | None = None,
) -> dict[str, Any]:
    """Rewrite VMGI/BUP addresses and extents for a compact VMG menu VOB."""
    layout_path = layout_path.resolve()
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    row = _domain_row(layout, 0, "vmg_menu")
    vobus, vobu_ends = _address_maps(row)
    source_ifo = source_ifo.resolve()
    source_bytes = source_ifo.read_bytes()
    if len(source_bytes) % DVD_SECTOR_SIZE or source_bytes[:12] != b"DVDVIDEO-VMG":
        raise PipelineError(f"Invalid VMGI IFO: {source_ifo}")
    data = bytearray(source_bytes)
    ifo_sectors = len(data) // DVD_SECTOR_SIZE
    if _u32(data, VMGI_LAST_SECTOR_OFFSET) + 1 != ifo_sectors:
        raise PipelineError("VMGI last-sector field does not match the IFO size")
    menu_start = _u32(data, VMGM_VOBS_OFFSET)
    menu_vob = source_ifo.with_name("VIDEO_TS.VOB")
    if not menu_vob.is_file() or menu_vob.stat().st_size % DVD_SECTOR_SIZE:
        raise PipelineError("VIDEO_TS.VOB is missing or not sector aligned")
    menu_sectors = menu_vob.stat().st_size // DVD_SECTOR_SIZE
    if int(row["original_sectors"]) != menu_sectors:
        raise PipelineError(
            f"VMG menu extent changed: {menu_sectors} != {int(row['original_sectors'])}"
        )
    compact_menu_sectors = int(row["compact_sectors"])
    old_last_sector = _u32(data, VMG_LAST_SECTOR_OFFSET)
    extents = _relocated_vmg_extents(
        ifo_sectors=ifo_sectors,
        menu_start=menu_start,
        original_menu_sectors=menu_sectors,
        compact_menu_sectors=compact_menu_sectors,
        old_last_sector=old_last_sector,
    )
    _put_u32(data, VMGM_VOBS_OFFSET, extents["new_menu_start"])
    _put_u32(data, VMG_LAST_SECTOR_OFFSET, extents["new_last_sector"])

    first_play_offset = _u32(data, VMG_FIRST_PLAY_PGC_OFFSET)
    if first_play_offset:
        first_play = _patch_pgc_at(
            data,
            first_play_offset,
            limit=_u32(data, VMGI_LAST_BYTE_OFFSET) + 1,
            starts=vobus,
            ends=vobu_ends,
            label="VMGI first-play PGC",
        )
    else:
        first_play = {"cells": 0}
    menu_tables = {
        "pgci_ut": _patch_pgci_ut(
            data, _u32(data, VMGM_PGCI_UT_OFFSET), vobus, vobu_ends
        ),
        "c_adt_cells": _patch_c_adt(
            data, _u32(data, VMGM_C_ADT_OFFSET), vobus, vobu_ends
        ),
        "vobu_admap_entries": _patch_vobu_admap(
            data, _u32(data, VMGM_VOBU_ADMAP_OFFSET), vobus
        ),
    }

    output = bytes(data)
    destination_ifo = destination_ifo.resolve()
    _atomic_bytes(destination_ifo, output)
    if destination_bup is not None:
        _atomic_bytes(destination_bup.resolve(), output)
    result = {
        "schema": "dvd2hevc-compact-vmgi-v0",
        "status": "passed",
        "layout": str(layout_path),
        "layout_sha256": _sha256(layout_path.read_bytes()),
        "source_ifo": str(source_ifo),
        "destination_ifo": str(destination_ifo),
        "destination_bup": str(destination_bup.resolve()) if destination_bup else None,
        "source_sha256": _sha256(source_bytes),
        "output_sha256": _sha256(output),
        "summary": {
            "ifo_sectors": ifo_sectors,
            "menu_vob_start": {"old": menu_start, "new": extents["new_menu_start"]},
            "menu_vob_sectors": {"old": menu_sectors, "new": compact_menu_sectors},
            "vmg_last_sector": {"old": old_last_sector, "new": extents["new_last_sector"]},
            "source_extent_padding": {
                "before_menu_vob": extents["prefix_padding_sectors"],
                "after_menu_including_bup": extents["tail_sectors"],
            },
            "first_play_pgc": first_play,
            "menu_tables": menu_tables,
        },
        "limits": [
            "VMG menu audio and subpictures remain byte-exact while video VOBUs are relocated",
            "Title-set starts are repacked by compact staging after every VTS extent is known",
        ],
    }
    report_path = destination_ifo.with_suffix(destination_ifo.suffix + ".json")
    report_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def rewrite_compact_vts_ifo(
    layout_path: Path,
    *,
    vts: int,
    source_ifo: Path,
    destination_ifo: Path,
    destination_bup: Path | None = None,
) -> dict[str, Any]:
    """Rewrite VTS IFO/BUP addresses for compact title and optional menu domains."""
    layout_path = layout_path.resolve()
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    row = _domain_row(layout, vts, "title")
    vobus, vobu_ends = _address_maps(row)
    menu_matches = [
        item for item in layout.get("domains", [])
        if item.get("domain") == "vts_menu" and int(item.get("vts", -1)) == vts
    ]
    if len(menu_matches) > 1:
        raise PipelineError(f"Compact layout duplicates the VTS {vts} menu domain")
    menu_row = menu_matches[0] if menu_matches else None
    menu_vobus, menu_vobu_ends = _address_maps(menu_row) if menu_row else ({}, {})
    source_ifo = source_ifo.resolve()
    source_bytes = source_ifo.read_bytes()
    if len(source_bytes) % DVD_SECTOR_SIZE or source_bytes[:12] != b"DVDVIDEO-VTS":
        raise PipelineError(f"Invalid VTS IFO: {source_ifo}")
    data = bytearray(source_bytes)
    ifo_sectors = len(data) // DVD_SECTOR_SIZE
    if _u32(data, VTSI_LAST_SECTOR_OFFSET) + 1 != ifo_sectors:
        raise PipelineError("VTSI last-sector field does not match the IFO size")
    menu_start = _u32(data, VTSM_VOBS_OFFSET)
    title_start = _u32(data, VTSTT_VOBS_OFFSET)
    menu_vob = source_ifo.with_name(f"VTS_{vts:02d}_0.VOB")
    if menu_start:
        if not menu_vob.is_file() or menu_vob.stat().st_size % DVD_SECTOR_SIZE:
            raise PipelineError(f"VTS {vts} menu VOB is missing or not sector aligned")
        menu_sectors = menu_vob.stat().st_size // DVD_SECTOR_SIZE
    else:
        menu_sectors = 0

    if menu_row is not None:
        if not menu_start:
            raise PipelineError(f"Compact layout has a VTS {vts} menu but the IFO has no menu VOB")
        if int(menu_row["original_sectors"]) != menu_sectors:
            raise PipelineError(
                f"VTS menu extent changed: {menu_sectors} != {int(menu_row['original_sectors'])}"
            )
        compact_menu_sectors = int(menu_row["compact_sectors"])
    else:
        compact_menu_sectors = menu_sectors

    old_last_sector = _u32(data, VTS_LAST_SECTOR_OFFSET)
    extents = _relocated_vts_extents(
        ifo_sectors=ifo_sectors,
        menu_start=menu_start,
        title_start=title_start,
        original_menu_sectors=menu_sectors,
        compact_menu_sectors=compact_menu_sectors,
        original_title_sectors=int(row["original_sectors"]),
        compact_title_sectors=int(row["compact_sectors"]),
        old_last_sector=old_last_sector,
    )
    _put_u32(data, VTSM_VOBS_OFFSET, extents["new_menu_start"])
    new_title_start = extents["new_title_start"]
    _put_u32(data, VTSTT_VOBS_OFFSET, new_title_start)
    new_last_sector = extents["new_last_sector"]
    _put_u32(data, VTS_LAST_SECTOR_OFFSET, new_last_sector)

    pgcit = _patch_pgcit(data, _u32(data, VTS_PGCIT_OFFSET), vobus, vobu_ends)
    c_adt_cells = _patch_c_adt(data, _u32(data, VTS_C_ADT_OFFSET), vobus, vobu_ends)
    vobu_count = _patch_vobu_admap(data, _u32(data, VTS_VOBU_ADMAP_OFFSET), vobus)
    tmaps = _patch_tmapt(data, _u32(data, VTS_TMAPT_OFFSET), vobus)
    menu_tables: dict[str, Any] | None = None
    if menu_row is not None:
        menu_tables = {
            "pgci_ut": _patch_pgci_ut(
                data, _u32(data, VTSM_PGCI_UT_OFFSET), menu_vobus, menu_vobu_ends
            ),
            "c_adt_cells": _patch_c_adt(
                data, _u32(data, VTSM_C_ADT_OFFSET), menu_vobus, menu_vobu_ends
            ),
            "vobu_admap_entries": _patch_vobu_admap(
                data, _u32(data, VTSM_VOBU_ADMAP_OFFSET), menu_vobus
            ),
        }
    audio_attributes: list[dict[str, int]] = []
    compact_audio = row.get("compact_audio")
    if compact_audio:
        audio_count = data[VTS_AUDIO_COUNT_OFFSET]
        tracks = sorted(
            (compact_audio.get("tracks") or []),
            key=lambda track: int(track["ordinal"]),
        )
        ordinals = [int(track["ordinal"]) for track in tracks]
        # Some commercial DVDs declare tracks that never occur physically, or
        # use only a high-numbered substream.  Rewrite observed descriptors in
        # place and leave inactive declarations byte-for-byte unchanged.
        _validate_compact_audio_ordinals(audio_count, ordinals)
        for track in tracks:
            ordinal = int(track["ordinal"])
            target_channels = int(track["target_channels"])
            attr = VTS_AUDIO_ATTRS_OFFSET + ordinal * 8
            source_channels = (data[attr + 1] & 0x07) + 1
            expected_source_channels = int(track["source_channels"])
            source_channel_alignment = _rewrite_compact_audio_attribute(
                data,
                attr,
                source_codec=str(
                    track.get("source_ifo_codec") or track.get("source_codec") or ""
                ),
                source_channels=expected_source_channels,
                target_channels=target_channels,
                processing=str(track.get("processing") or "downmix"),
            )
            audio_attributes.append({
                "stream": ordinal,
                "source_codec": str(track.get("source_codec") or ""),
                "source_ifo_codec": str(
                    track.get("source_ifo_codec") or track.get("source_codec") or ""
                ),
                "source_channels": source_channels,
                "decoded_source_channels": expected_source_channels,
                "source_channel_alignment": source_channel_alignment,
                "target_channels": target_channels,
                "target_bitrate": int(track["target_bitrate"]),
            })

    output = bytes(data)
    destination_ifo = destination_ifo.resolve()
    _atomic_bytes(destination_ifo, output)
    if destination_bup is not None:
        _atomic_bytes(destination_bup.resolve(), output)
    result = {
        "schema": "dvd2hevc-compact-vts-ifo-v0",
        "status": "passed",
        "layout": str(layout_path),
        "layout_sha256": _sha256(layout_path.read_bytes()),
        "vts": vts,
        "source_ifo": str(source_ifo),
        "destination_ifo": str(destination_ifo),
        "destination_bup": str(destination_bup.resolve()) if destination_bup else None,
        "source_sha256": _sha256(source_bytes),
        "output_sha256": _sha256(output),
        "summary": {
            "ifo_sectors": ifo_sectors,
            "preserved_menu_sectors": menu_sectors,
            "menu_vob_sectors": {
                "old": menu_sectors,
                "new": compact_menu_sectors,
            },
            "title_vob_start": {"old": title_start, "new": new_title_start},
            "vts_last_sector": {"old": old_last_sector, "new": new_last_sector},
            "source_extent_padding": {
                "before_first_vob": extents["prefix_padding_sectors"],
                "between_menu_and_title": extents["menu_title_padding_sectors"],
                "after_title_including_bup": extents["tail_sectors"],
            },
            "title_vob_sectors": {"old": int(row["original_sectors"]), "new": int(row["compact_sectors"])},
            "pgcit": pgcit,
            "c_adt_cells": c_adt_cells,
            "vobu_admap_entries": vobu_count,
            "time_maps": tmaps,
            "audio_attributes": audio_attributes,
            "menu_tables": menu_tables,
        },
        "limits": [
            (
                "VTS menu PGC/C_ADT/VOBU_ADMAP addresses are compact-relocated"
                if menu_row is not None
                else "VTS menu VOBs are preserved byte-for-byte"
            ),
            "Interleaved branching cells retain physical order and relocated ILVU boundaries",
            "Multi-angle SML_AGLI navigation remains outside this relocation profile",
            "VMGI title-set physical-sector fields are not rewritten by this VTS-local operation",
        ],
    }
    report_path = destination_ifo.with_suffix(destination_ifo.suffix + ".json")
    report_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result
