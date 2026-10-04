"""Compatibility repairs for DVD2HEVC prototype images."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .repack import DVD_SECTOR_SIZE, _program_stream_packets, build_hevc_psm


def _css_safe_sector(sector: bytes) -> tuple[bytes, dict[str, int]]:
    repaired = bytearray(sector)
    hevc_maps = 0
    maps_rewritten = 0
    fixed_offset_maps = 0
    fixed_offset_css_safe = 0
    for stream_id, start, end in _program_stream_packets(sector):
        if stream_id != 0xBC:
            continue
        packet = sector[start:end]
        if (
            len(packet) != 20
            or packet[10:12] != b"\x00\x04"
            or packet[12] != 0x24
            or packet[14:16] != b"\x00\x00"
        ):
            continue
        hevc_maps += 1
        replacement = build_hevc_psm(packet[13])
        if len(replacement) != len(packet):
            raise AssertionError("CSS-safe PSM changed packet length")
        if packet != replacement:
            repaired[start:end] = replacement
            maps_rewritten += 1
        if start == 14:
            fixed_offset_maps += 1
            if replacement[6] & 0x30 == 0:
                fixed_offset_css_safe += 1
    return bytes(repaired), {
        "hevc_maps": hevc_maps,
        "maps_rewritten": maps_rewritten,
        "fixed_offset_maps": fixed_offset_maps,
        "fixed_offset_css_safe": fixed_offset_css_safe,
    }


def verify_css_safe_hevc_psms(source: Path, destination: Path) -> dict[str, Any]:
    """Prove that two images differ only by the deterministic PSM repair."""

    source = source.resolve()
    destination = destination.resolve()
    if not source.is_file() or not destination.is_file():
        raise ValueError("Both source and repaired images must exist")
    if source.stat().st_size != destination.stat().st_size:
        raise ValueError("Source and repaired image sizes differ")
    if source.stat().st_size % DVD_SECTOR_SIZE:
        raise ValueError("Image size is not aligned to 2,048-byte DVD sectors")

    sectors = source.stat().st_size // DVD_SECTOR_SIZE
    changed_sectors = 0
    differing_bytes = 0
    hevc_maps = 0
    maps_rewritten = 0
    fixed_offset_maps = 0
    fixed_offset_css_safe = 0
    with source.open("rb", buffering=1024 * 1024) as source_handle, destination.open(
        "rb", buffering=1024 * 1024
    ) as destination_handle:
        for sector_index in range(sectors):
            original = source_handle.read(DVD_SECTOR_SIZE)
            actual = destination_handle.read(DVD_SECTOR_SIZE)
            expected, stats = _css_safe_sector(original)
            hevc_maps += stats["hevc_maps"]
            maps_rewritten += stats["maps_rewritten"]
            fixed_offset_maps += stats["fixed_offset_maps"]
            fixed_offset_css_safe += stats["fixed_offset_css_safe"]
            if actual != original:
                changed_sectors += 1
                differing_bytes += sum(a != b for a, b in zip(original, actual))
            if actual != expected:
                raise ValueError(
                    f"Repaired image has a non-PSM difference at sector {sector_index}"
                )

    if hevc_maps == 0:
        raise ValueError("No DVD2HEVC program-stream maps were found")
    if fixed_offset_css_safe != fixed_offset_maps:
        raise ValueError("One or more fixed-offset HEVC maps remain CSS-ambiguous")
    return {
        "schema": "dvd2hevc-css-safe-psm-verification-v0",
        "status": "passed",
        "source": str(source),
        "destination": str(destination),
        "size": destination.stat().st_size,
        "sectors_compared": sectors,
        "changed_sectors": changed_sectors,
        "differing_bytes": differing_bytes,
        "hevc_program_stream_maps": hevc_maps,
        "maps_rewritten": maps_rewritten,
        "fixed_offset_maps": fixed_offset_maps,
        "fixed_offset_css_safe": fixed_offset_css_safe,
        "only_program_stream_maps_changed": True,
        "layout_unchanged": True,
    }


def audit_css_safe_hevc_psms(image: Path) -> dict[str, Any]:
    """Inspect a DVD2HEVC image without modifying it."""

    image = image.resolve()
    if not image.is_file():
        raise ValueError(f"Image was not found: {image}")
    if image.stat().st_size % DVD_SECTOR_SIZE:
        raise ValueError("Image size is not aligned to 2,048-byte DVD sectors")
    sectors = image.stat().st_size // DVD_SECTOR_SIZE
    hevc_maps = 0
    legacy_maps = 0
    fixed_offset_maps = 0
    fixed_offset_css_safe = 0
    with image.open("rb", buffering=1024 * 1024) as handle:
        for _sector_index in range(sectors):
            sector = handle.read(DVD_SECTOR_SIZE)
            _repaired, stats = _css_safe_sector(sector)
            hevc_maps += stats["hevc_maps"]
            legacy_maps += stats["maps_rewritten"]
            fixed_offset_maps += stats["fixed_offset_maps"]
            fixed_offset_css_safe += stats["fixed_offset_css_safe"]
    passed = (
        hevc_maps > 0
        and legacy_maps == 0
        and fixed_offset_css_safe == fixed_offset_maps
    )
    return {
        "schema": "dvd2hevc-css-safe-psm-audit-v0",
        "status": "passed" if passed else "failed",
        "passed": passed,
        "image": str(image),
        "size": image.stat().st_size,
        "sectors_scanned": sectors,
        "hevc_program_stream_maps": hevc_maps,
        "legacy_css_ambiguous_maps": legacy_maps,
        "fixed_offset_maps": fixed_offset_maps,
        "fixed_offset_css_safe": fixed_offset_css_safe,
    }


def repair_css_safe_hevc_psms(source: Path, destination: Path) -> dict[str, Any]:
    """Copy a sector image while replacing legacy HEVC PSM headers in place.

    Early DVD2HEVC images used the standards-reserved 0xe0 PSM control byte.
    When the PSM immediately followed a DVD pack header, that byte occupied
    sector offset 0x14 and looked like PES scrambling control to libdvdcss.
    The CSS-safe PSM is the same length, so no UDF, IFO, NAV, or compact-sector
    offsets change.
    """

    source = source.resolve()
    destination = destination.resolve()
    if not source.is_file():
        raise ValueError(f"Source image was not found: {source}")
    if source == destination:
        raise ValueError("CSS-safe repair requires a separate destination")
    if destination.exists():
        raise ValueError(f"Destination already exists: {destination}")
    if source.stat().st_size % DVD_SECTOR_SIZE:
        raise ValueError("Source size is not aligned to 2,048-byte DVD sectors")

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)

    sectors = source.stat().st_size // DVD_SECTOR_SIZE
    hevc_maps = 0
    maps_rewritten = 0
    fixed_offset_maps = 0
    fixed_offset_css_safe = 0
    with destination.open("r+b", buffering=1024 * 1024) as handle:
        for sector_index in range(sectors):
            sector_offset = sector_index * DVD_SECTOR_SIZE
            sector = handle.read(DVD_SECTOR_SIZE)
            if len(sector) != DVD_SECTOR_SIZE:
                raise OSError(f"Short read at image sector {sector_index}")
            repaired, stats = _css_safe_sector(sector)
            hevc_maps += stats["hevc_maps"]
            maps_rewritten += stats["maps_rewritten"]
            fixed_offset_maps += stats["fixed_offset_maps"]
            fixed_offset_css_safe += stats["fixed_offset_css_safe"]
            if repaired != sector:
                handle.seek(sector_offset)
                handle.write(repaired)
                handle.seek(sector_offset + DVD_SECTOR_SIZE)

    if hevc_maps == 0:
        destination.unlink(missing_ok=True)
        raise ValueError("No DVD2HEVC program-stream maps were found")
    if fixed_offset_css_safe != fixed_offset_maps:
        destination.unlink(missing_ok=True)
        raise AssertionError("One or more fixed-offset HEVC maps still resemble CSS scrambling")
    if destination.stat().st_size != source.stat().st_size:
        destination.unlink(missing_ok=True)
        raise AssertionError("CSS-safe PSM repair changed image size")

    verification = verify_css_safe_hevc_psms(source, destination)
    return {
        "schema": "dvd2hevc-css-safe-psm-repair-v0",
        "status": "passed",
        "source": str(source),
        "destination": str(destination),
        "size": destination.stat().st_size,
        "sectors_scanned": sectors,
        "hevc_program_stream_maps": hevc_maps,
        "maps_rewritten": maps_rewritten,
        "fixed_offset_maps": fixed_offset_maps,
        "fixed_offset_css_safe": fixed_offset_css_safe,
        "verification": verification,
        "layout_unchanged": True,
    }
