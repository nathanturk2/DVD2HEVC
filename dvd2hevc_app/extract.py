"""Read-only extraction of physical DVD video-domain sector ranges."""

from __future__ import annotations

import io
import os
import re
from contextlib import ExitStack
from pathlib import Path
from typing import Any, BinaryIO

from .iso import DiscScanError, _entry_name, close_iso_image, open_iso_image
from .vob import DVD_SECTOR_SIZE, VobSectorScanner


class SectorRangeSink:
    """Consume a concatenated title domain while writing only one sector range."""

    def __init__(self, output: BinaryIO, first_sector: int, last_sector: int) -> None:
        self.output = output
        self.start = first_sector * DVD_SECTOR_SIZE
        self.end = (last_sector + 1) * DVD_SECTOR_SIZE
        self.position = 0
        self.written = 0

    def write(self, data: bytes) -> int:
        chunk_start = self.position
        chunk_end = chunk_start + len(data)
        overlap_start = max(chunk_start, self.start)
        overlap_end = min(chunk_end, self.end)
        if overlap_start < overlap_end:
            local_start = overlap_start - chunk_start
            local_end = overlap_end - chunk_start
            self.output.write(data[local_start:local_end])
            self.written += local_end - local_start
        self.position = chunk_end
        return len(data)

    def tell(self) -> int:
        return self.position

    def seek(self, *_args: object) -> int:
        return self.position

    def advance(self, byte_count: int) -> None:
        """Account for a whole preceding VOB without reading it from the ISO."""
        if byte_count < 0:
            raise ValueError("Cannot advance by a negative byte count")
        self.position += byte_count


class SectorRangesSink:
    """Consume a domain once while concatenating selected physical ranges."""

    def __init__(
        self, output: BinaryIO, ranges: list[tuple[int, int]]
    ) -> None:
        if not ranges:
            raise ValueError("At least one sector range is required")
        previous_end = -1
        self.ranges: list[tuple[int, int]] = []
        for first_sector, last_sector in ranges:
            if first_sector < 0 or last_sector < first_sector:
                raise ValueError("Invalid sector range")
            if first_sector <= previous_end:
                raise ValueError("Sector ranges must be sorted and non-overlapping")
            self.ranges.append(
                (first_sector * DVD_SECTOR_SIZE, (last_sector + 1) * DVD_SECTOR_SIZE)
            )
            previous_end = last_sector
        self.output = output
        self.position = 0
        self.written = 0
        self.range_index = 0

    @property
    def start(self) -> int:
        return self.ranges[0][0]

    @property
    def end(self) -> int:
        return self.ranges[-1][1]

    def write(self, data: bytes) -> int:
        chunk_start = self.position
        chunk_end = chunk_start + len(data)
        while (
            self.range_index < len(self.ranges)
            and self.ranges[self.range_index][1] <= chunk_start
        ):
            self.range_index += 1
        index = self.range_index
        while index < len(self.ranges):
            range_start, range_end = self.ranges[index]
            if range_start >= chunk_end:
                break
            overlap_start = max(chunk_start, range_start)
            overlap_end = min(chunk_end, range_end)
            if overlap_start < overlap_end:
                local_start = overlap_start - chunk_start
                local_end = overlap_end - chunk_start
                self.output.write(data[local_start:local_end])
                self.written += local_end - local_start
            if range_end <= chunk_end:
                index += 1
            else:
                break
        self.range_index = index
        self.position = chunk_end
        return len(data)

    def tell(self) -> int:
        return self.position

    def seek(self, *_args: object) -> int:
        return self.position

    def advance(self, byte_count: int) -> None:
        if byte_count < 0:
            raise ValueError("Cannot advance by a negative byte count")
        self.position += byte_count


class SectorBatchSink:
    """Consume a DVD domain once while writing independent cell files.

    Ranges may overlap because authored logical cells can share physical DVD
    sectors. Each destination still receives exactly its requested contiguous
    byte range, but the underlying UDF payload is read only once.
    """

    def __init__(
        self,
        outputs: list[tuple[BinaryIO, int, int]],
    ) -> None:
        if not outputs:
            raise ValueError("At least one output sector range is required")
        self.ranges: list[dict[str, Any]] = []
        for index, (output, first_sector, last_sector) in enumerate(outputs):
            if first_sector < 0 or last_sector < first_sector:
                raise ValueError("Invalid sector range")
            self.ranges.append({
                "output": output,
                "start": first_sector * DVD_SECTOR_SIZE,
                "end": (last_sector + 1) * DVD_SECTOR_SIZE,
                "written": 0,
                "index": index,
            })
        self.ranges.sort(key=lambda row: (int(row["start"]), int(row["end"])))
        self._starts = [int(row["start"]) for row in self.ranges]
        self._ends = [int(row["end"]) for row in self.ranges]
        self._next_index = 0
        self._active: list[dict[str, Any]] = []
        self.position = 0
        self.consumed = 0

    @property
    def start(self) -> int:
        return self._starts[0]

    @property
    def end(self) -> int:
        return max(self._ends)

    @property
    def written(self) -> list[int]:
        values = [0] * len(self.ranges)
        for row in self.ranges:
            values[int(row["index"])] = int(row["written"])
        return values

    def intersects(self, start: int, end: int) -> bool:
        """Whether an absolute byte interval intersects any requested range."""
        if end <= start:
            return False
        # This is called once per VOB file (normally no more than nine), so a
        # linear check is both cheap and correct for nested/overlapping ranges.
        return any(
            int(row["start"]) < end and int(row["end"]) > start
            for row in self.ranges
        )

    def write(self, data: bytes) -> int:
        chunk_start = self.position
        chunk_end = chunk_start + len(data)
        self._active = [
            row for row in self._active if int(row["end"]) > chunk_start
        ]
        while (
            self._next_index < len(self.ranges)
            and int(self.ranges[self._next_index]["start"]) < chunk_end
        ):
            row = self.ranges[self._next_index]
            if int(row["end"]) > chunk_start:
                self._active.append(row)
            self._next_index += 1
        for row in self._active:
            range_start = int(row["start"])
            range_end = int(row["end"])
            overlap_start = max(chunk_start, range_start)
            overlap_end = min(chunk_end, range_end)
            if overlap_start < overlap_end:
                local_start = overlap_start - chunk_start
                local_end = overlap_end - chunk_start
                row["output"].write(data[local_start:local_end])
                row["written"] = int(row["written"]) + local_end - local_start
        self.position = chunk_end
        self.consumed += len(data)
        return len(data)

    def tell(self) -> int:
        return self.position

    def seek(self, *_args: object) -> int:
        return self.position

    def advance(self, byte_count: int) -> None:
        if byte_count < 0:
            raise ValueError("Cannot advance by a negative byte count")
        self.position += byte_count


def title_vob_entries(image: Any, vts: int) -> list[Any]:
    pattern = re.compile(rf"^VTS_{vts:02d}_([1-9][0-9]*)\.VOB$", re.IGNORECASE)
    rows: list[tuple[int, Any]] = []
    for entry in image.list_children(udf_path="/VIDEO_TS"):
        if entry is None or entry.is_dot() or entry.is_dotdot() or not entry.is_file():
            continue
        match = pattern.match(_entry_name(entry))
        if match:
            rows.append((int(match.group(1)), entry))
    return [entry for _number, entry in sorted(rows, key=lambda row: row[0])]


def domain_vob_entries(image: Any, *, domain: str, vts: int) -> list[Any]:
    """Return VOB files in sector-address order for one DVD video domain."""
    if domain == "title":
        return title_vob_entries(image, vts)
    if domain not in {"vts_menu", "vmg_menu"}:
        raise DiscScanError(f"Unknown DVD video domain: {domain}")
    wanted = "VIDEO_TS.VOB" if domain == "vmg_menu" else f"VTS_{vts:02d}_0.VOB"
    for entry in image.list_children(udf_path="/VIDEO_TS"):
        if entry is None or entry.is_dot() or entry.is_dotdot() or not entry.is_file():
            continue
        if _entry_name(entry).upper() == wanted:
            return [entry]
    return []


def inspect_vob_file(path: Path) -> dict[str, int]:
    scanner = VobSectorScanner()
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            scanner.write(chunk)
    return scanner.finish().to_dict()


def extract_title_sector_range(
    source: Path,
    *,
    vts: int,
    first_sector: int,
    last_sector: int,
    destination: Path,
) -> dict[str, Any]:
    return extract_domain_sector_range(
        source,
        domain="title",
        vts=vts,
        first_sector=first_sector,
        last_sector=last_sector,
        destination=destination,
    )


def extract_title_sector_segments(
    source: Path,
    *,
    vts: int,
    segments: list[dict[str, int]],
    destination: Path,
) -> dict[str, Any]:
    """Concatenate discontinuous title-domain extents in playback order."""
    if vts < 1:
        raise DiscScanError("VTS number must be at least 1")
    ranges = [
        (int(segment["start_sector"]), int(segment["last_sector"]))
        for segment in segments
    ]
    try:
        # Validate ordering before opening the image or destination.
        SectorRangesSink(io.BytesIO(), ranges)
    except ValueError as exc:
        raise DiscScanError(str(exc)) from exc

    source = source.resolve()
    destination = destination.resolve()
    if source == destination:
        raise DiscScanError("Extraction destination cannot be the source ISO")
    image = open_iso_image(source)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.part")
    try:
        entries = domain_vob_entries(image, domain="title", vts=vts)
        if not entries:
            raise DiscScanError(f"No VOB files found for title VTS {vts:02d}")
        files = [
            {"name": _entry_name(entry), "size": int(entry.get_data_length())}
            for entry in entries
        ]
        total_bytes = sum(item["size"] for item in files)
        if any(item["size"] % DVD_SECTOR_SIZE for item in files):
            raise DiscScanError("A domain VOB is not aligned to 2,048-byte sectors")
        total_sectors = total_bytes // DVD_SECTOR_SIZE
        if ranges[-1][1] >= total_sectors:
            raise DiscScanError(
                f"Sector {ranges[-1][1]} is outside title VTS {vts:02d} ({total_sectors} sectors)"
            )

        destination.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("wb") as output:
            sink = SectorRangesSink(output, ranges)
            for entry in entries:
                name = _entry_name(entry)
                size = int(entry.get_data_length())
                if sink.position + size <= sink.start:
                    sink.advance(size)
                    continue
                if sink.position >= sink.end:
                    break
                image.get_file_from_iso_fp(sink, udf_path=f"/VIDEO_TS/{name}")
        expected = sum((last - first + 1) * DVD_SECTOR_SIZE for first, last in ranges)
        if sink.written != expected or temporary.stat().st_size != expected:
            raise DiscScanError(f"Short extraction: expected {expected} bytes, wrote {sink.written}")
        os.replace(temporary, destination)
        output_cursor = 0
        mapping = []
        for first, last in ranges:
            count = last - first + 1
            mapping.append({
                "source_first_sector": first,
                "source_last_sector": last,
                "output_first_sector": output_cursor,
                "output_last_sector": output_cursor + count - 1,
                "sector_count": count,
            })
            output_cursor += count
        return {
            "source": str(source),
            "destination": str(destination),
            "vts": vts,
            "domain": "title",
            "segment_count": len(ranges),
            "sector_count": output_cursor,
            "size": expected,
            "segments": mapping,
            "domain_files": files,
            "title_domain_files": files,
            "vob_stats": inspect_vob_file(destination),
        }
    finally:
        close_iso_image(image)
        temporary.unlink(missing_ok=True)


def extract_domain_sector_range(
    source: Path,
    *,
    domain: str,
    vts: int,
    first_sector: int,
    last_sector: int,
    destination: Path,
) -> dict[str, Any]:
    if vts < 1:
        if domain != "vmg_menu" or vts != 0:
            raise DiscScanError("VTS number must be at least 1 outside the VMG menu domain")
    if first_sector < 0 or last_sector < first_sector:
        raise DiscScanError("Invalid sector range")
    source = source.resolve()
    destination = destination.resolve()
    if source == destination:
        raise DiscScanError("Extraction destination cannot be the source ISO")

    image = open_iso_image(source)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.part")
    try:
        entries = domain_vob_entries(image, domain=domain, vts=vts)
        if not entries:
            raise DiscScanError(f"No VOB files found for {domain} VTS {vts:02d}")
        files = [
            {"name": _entry_name(entry), "size": int(entry.get_data_length())}
            for entry in entries
        ]
        total_bytes = sum(item["size"] for item in files)
        if any(item["size"] % DVD_SECTOR_SIZE for item in files):
            raise DiscScanError("A domain VOB is not aligned to 2,048-byte sectors")
        total_sectors = total_bytes // DVD_SECTOR_SIZE
        if last_sector >= total_sectors:
            raise DiscScanError(
                f"Sector {last_sector} is outside {domain} VTS {vts:02d} ({total_sectors} sectors)"
            )

        destination.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("wb") as output:
            sink = SectorRangeSink(output, first_sector, last_sector)
            for entry in entries:
                name = _entry_name(entry)
                size = int(entry.get_data_length())
                if sink.position + size <= sink.start:
                    sink.advance(size)
                    continue
                if sink.position >= sink.end:
                    break
                image.get_file_from_iso_fp(sink, udf_path=f"/VIDEO_TS/{name}")
        expected = (last_sector - first_sector + 1) * DVD_SECTOR_SIZE
        if sink.written != expected or temporary.stat().st_size != expected:
            raise DiscScanError(f"Short extraction: expected {expected} bytes, wrote {sink.written}")
        os.replace(temporary, destination)
        return {
            "source": str(source),
            "destination": str(destination),
            "vts": vts,
            "domain": domain,
            "first_sector": first_sector,
            "last_sector": last_sector,
            "sector_count": last_sector - first_sector + 1,
            "size": expected,
            "domain_files": files,
            # Compatibility for Phase 2/3 report consumers.
            "title_domain_files": files if domain == "title" else [],
            "vob_stats": inspect_vob_file(destination),
        }
    finally:
        close_iso_image(image)
        temporary.unlink(missing_ok=True)


def extract_domain_sector_segment_sets(
    source: Path,
    *,
    domain: str,
    vts: int,
    requests: list[tuple[list[tuple[int, int]], Path]],
) -> list[dict[str, Any]]:
    """Extract independent contiguous or segmented outputs in one domain pass."""
    if not requests:
        return []
    if vts < 1 and (domain != "vmg_menu" or vts != 0):
        raise DiscScanError("VTS number must be at least 1 outside the VMG menu domain")
    source = source.resolve()
    normalized: list[tuple[list[tuple[int, int]], Path, Path]] = []
    destinations: set[Path] = set()
    for index, (segments_value, destination_value) in enumerate(requests):
        segments = [(int(first), int(last)) for first, last in segments_value]
        try:
            SectorRangesSink(io.BytesIO(), segments)
        except ValueError as exc:
            raise DiscScanError(str(exc)) from exc
        destination = destination_value.resolve()
        if destination == source:
            raise DiscScanError("Extraction destination cannot be the source ISO")
        if destination in destinations:
            raise DiscScanError(f"Duplicate batch extraction destination: {destination}")
        destinations.add(destination)
        temporary = destination.with_name(
            f".{destination.name}.{os.getpid()}.{index}.part"
        )
        normalized.append((segments, destination, temporary))

    image = open_iso_image(source)
    try:
        entries = domain_vob_entries(image, domain=domain, vts=vts)
        if not entries:
            raise DiscScanError(f"No VOB files found for {domain} VTS {vts:02d}")
        files = [
            {"name": _entry_name(entry), "size": int(entry.get_data_length())}
            for entry in entries
        ]
        total_bytes = sum(item["size"] for item in files)
        if any(item["size"] % DVD_SECTOR_SIZE for item in files):
            raise DiscScanError("A domain VOB is not aligned to 2,048-byte sectors")
        total_sectors = total_bytes // DVD_SECTOR_SIZE
        if max(
            last
            for segments, _destination, _temporary in normalized
            for _first, last in segments
        ) >= total_sectors:
            raise DiscScanError(
                f"A requested sector is outside {domain} VTS {vts:02d} "
                f"({total_sectors} sectors)"
            )

        for _segments, destination, _temporary in normalized:
            destination.parent.mkdir(parents=True, exist_ok=True)
        with ExitStack() as stack:
            handles = [
                stack.enter_context(temporary.open("wb"))
                for _segments, _destination, temporary in normalized
            ]
            flattened: list[tuple[BinaryIO, int, int]] = []
            output_range_indices: list[list[int]] = []
            for handle, (segments, _destination, _temporary) in zip(handles, normalized):
                indices: list[int] = []
                for first, last in segments:
                    indices.append(len(flattened))
                    flattened.append((handle, first, last))
                output_range_indices.append(indices)
            sink = SectorBatchSink(flattened)
            for entry, file_row in zip(entries, files):
                size = int(file_row["size"])
                entry_start = sink.position
                entry_end = entry_start + size
                if not sink.intersects(entry_start, entry_end):
                    sink.advance(size)
                    continue
                image.get_file_from_iso_fp(
                    sink, udf_path=f"/VIDEO_TS/{file_row['name']}"
                )

        reports: list[dict[str, Any]] = []
        written = sink.written
        for index, (segments, destination, temporary) in enumerate(normalized):
            expected = sum(
                (last - first + 1) * DVD_SECTOR_SIZE for first, last in segments
            )
            actual = sum(written[row] for row in output_range_indices[index])
            if actual != expected or temporary.stat().st_size != expected:
                raise DiscScanError(
                    f"Short batch extraction for {destination}: expected {expected} "
                    f"bytes, wrote {actual}"
                )
            output_cursor = 0
            mapping = []
            for first, last in segments:
                count = last - first + 1
                mapping.append({
                    "source_first_sector": first,
                    "source_last_sector": last,
                    "output_first_sector": output_cursor,
                    "output_last_sector": output_cursor + count - 1,
                    "sector_count": count,
                })
                output_cursor += count
            report = {
                "source": str(source),
                "destination": str(destination),
                "vts": vts,
                "domain": domain,
                "segment_count": len(segments),
                "sector_count": output_cursor,
                "size": expected,
                "segments": mapping,
                "domain_files": files,
                "title_domain_files": files if domain == "title" else [],
                "vob_stats": inspect_vob_file(temporary),
                "batch_extraction": {
                    "outputs": len(normalized),
                    "ranges": len(flattened),
                    "iso_opens": 1,
                    "domain_bytes_consumed": sink.consumed,
                    "domain_bytes": total_bytes,
                },
            }
            if len(segments) == 1:
                report["first_sector"] = segments[0][0]
                report["last_sector"] = segments[0][1]
            reports.append(report)
        for _segments, destination, temporary in normalized:
            os.replace(temporary, destination)
        return reports
    finally:
        close_iso_image(image)
        for _segments, _destination, temporary in normalized:
            temporary.unlink(missing_ok=True)


def extract_domain_sector_ranges(
    source: Path,
    *,
    domain: str,
    vts: int,
    ranges: list[tuple[int, int, Path]],
) -> list[dict[str, Any]]:
    """Extract independent contiguous ranges with one ISO/VOB pass."""
    return extract_domain_sector_segment_sets(
        source,
        domain=domain,
        vts=vts,
        requests=[([(first, last)], destination) for first, last, destination in ranges],
    )
