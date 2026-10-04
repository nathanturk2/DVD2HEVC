"""Read-only DVD ISO/UDF discovery."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .vob import VobSectorScanner


class DiscScanError(RuntimeError):
    pass


_PADDING_VIEWS: dict[int, _IsoPaddingOverlay] = {}


class _IsoPaddingOverlay:
    """Read-only view that zeroes only malformed ISO-9660 directory padding.

    Some otherwise valid DVD UDF images omit or misuse bytes after the final
    ISO-9660 directory record.  libdvdread and players accept them, while
    pycdlib intentionally rejects them.  The UDF and file payload bytes are
    untouched; this view normalizes only the compatibility-directory padding.
    """

    def __init__(
        self,
        path: Path,
        overlays: list[tuple[int, int]],
        padded_reads: dict[tuple[int, int], int],
    ) -> None:
        self._handle = path.open("rb")
        self._overlays = overlays
        self._padded_reads = padded_reads

    def read(self, size: int = -1) -> bytes:
        start = self._handle.tell()
        data = bytearray(self._handle.read(size))
        padded_size = self._padded_reads.get((start, size))
        if padded_size and len(data) < padded_size:
            data.extend(b"\x00" * (padded_size - len(data)))
        end = start + len(data)
        for first, last in self._overlays:
            left, right = max(start, first), min(end, last)
            if left < right:
                data[left - start : right - start] = b"\x00" * (right - left)
        return bytes(data)

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._handle.seek(offset, whence)

    def tell(self) -> int:
        return self._handle.tell()

    def close(self) -> None:
        self._handle.close()


def _iso9660_padding_fixes(
    path: Path,
) -> tuple[list[tuple[int, int]], dict[tuple[int, int], int]]:
    overlays: list[tuple[int, int]] = []
    padded_reads: dict[tuple[int, int], int] = {}
    with path.open("rb") as handle:
        queue: list[tuple[int, int, int]] = []
        for sector in range(16, 256):
            handle.seek(sector * 2048)
            descriptor = handle.read(2048)
            if len(descriptor) != 2048 or descriptor[1:6] != b"CD001":
                break
            if descriptor[0] in {1, 2}:
                block = int.from_bytes(descriptor[128:130], "little") or 2048
                root = descriptor[156 : 156 + int(descriptor[156])]
                if len(root) >= 34:
                    queue.append((
                        int.from_bytes(root[2:6], "little"),
                        int.from_bytes(root[10:14], "little"),
                        block,
                    ))
            if descriptor[0] == 255:
                break
        seen: set[tuple[int, int]] = set()
        while queue:
            extent, length, block = queue.pop(0)
            if (extent, length) in seen or extent <= 0 or length <= 0:
                continue
            seen.add((extent, length))
            absolute = extent * block
            if length % block:
                padded_reads[(absolute, length)] = length + block - (length % block)
            handle.seek(absolute)
            data = handle.read(length)
            offset = 0
            while offset < len(data):
                record_length = data[offset]
                if record_length == 0:
                    padding = block - (offset % block)
                    finish = min(len(data), offset + padding)
                    if any(data[offset:finish]):
                        overlays.append((absolute + offset, absolute + finish))
                    offset = finish
                    continue
                record = data[offset : offset + record_length]
                if len(record) < 34:
                    break
                identifier_length = record[32]
                identifier = record[33 : 33 + identifier_length]
                if record[25] & 0x02 and identifier not in {b"\x00", b"\x01"}:
                    queue.append((
                        int.from_bytes(record[2:6], "little"),
                        int.from_bytes(record[10:14], "little"),
                        block,
                    ))
                offset += record_length
    return overlays, padded_reads


def _iso9660_padding_overlays(path: Path) -> list[tuple[int, int]]:
    return _iso9660_padding_fixes(path)[0]


def open_iso_image(path: Path) -> Any:
    """Open with pycdlib, tolerating only the known non-zero padding defect."""
    source = path.resolve()
    pycdlib = _pycdlib()
    image = pycdlib.PyCdlib()
    try:
        image.open(str(source))
        return image
    except Exception as exc:
        if "Invalid padding on ISO" not in str(exc):
            raise
        try:
            image.close()
        except Exception:
            # A partially opened PyCdlib instance is not always closeable.
            pass
    overlays, padded_reads = _iso9660_padding_fixes(source)
    if not overlays and not padded_reads:
        raise DiscScanError(f"ISO padding was rejected but no safe overlay was found: {source}")
    view = _IsoPaddingOverlay(source, overlays, padded_reads)
    image = pycdlib.PyCdlib()
    try:
        image.open_fp(view)
    except BaseException:
        view.close()
        raise
    _PADDING_VIEWS[id(image)] = view
    return image


def close_iso_image(image: Any) -> None:
    view = _PADDING_VIEWS.pop(id(image), None)
    try:
        image.close()
    finally:
        if view is not None:
            view.close()


def _pycdlib() -> Any:
    try:
        import pycdlib
    except ImportError as exc:
        raise DiscScanError("pycdlib is required to scan ISO images") from exc
    return pycdlib


def _entry_name(entry: Any) -> str:
    value = entry.file_identifier() or b""
    return value.decode("utf-8", "replace")


def scan_iso(path: Path, *, inspect_vobs: bool = True) -> dict[str, Any]:
    source = path.resolve()
    if not source.is_file():
        raise DiscScanError(f"ISO not found: {source}")

    image = open_iso_image(source)
    try:
        if not image.has_udf():
            raise DiscScanError(f"ISO has no UDF filesystem: {source}")
        try:
            entries = [entry for entry in image.list_children(udf_path="/VIDEO_TS") if entry is not None]
        except Exception as exc:
            raise DiscScanError(f"ISO has no readable /VIDEO_TS directory: {source}") from exc

        files: list[dict[str, Any]] = []
        vobs: list[dict[str, Any]] = []
        totals = {
            "vob_bytes": 0,
            "vob_sectors": 0,
            "video_payload_bytes": 0,
            "video_pes_packets": 0,
            "nav_packs": 0,
            "scrambled_pes_packets": 0,
            "invalid_sectors": 0,
        }
        for entry in entries:
            if entry.is_dot() or entry.is_dotdot() or not entry.is_file():
                continue
            name = _entry_name(entry)
            size = int(entry.get_data_length())
            item = {"name": name, "size": size}
            files.append(item)
            if not name.upper().endswith(".VOB"):
                continue
            vob: dict[str, Any] = {
                "name": name,
                "size": size,
                "domain": "menu" if name.upper() == "VIDEO_TS.VOB" or name.upper().endswith("_0.VOB") else "title",
            }
            if inspect_vobs:
                sink = VobSectorScanner()
                image.get_file_from_iso_fp(sink, udf_path=f"/VIDEO_TS/{name}")
                stats = sink.finish().to_dict()
                vob["stats"] = stats
                totals["vob_bytes"] += stats["bytes"]
                totals["vob_sectors"] += stats["sectors"]
                totals["video_payload_bytes"] += stats["video_payload_bytes"]
                totals["video_pes_packets"] += stats["video_pes_packets"]
                totals["nav_packs"] += stats["nav_packs"]
                totals["scrambled_pes_packets"] += stats["scrambled_pes_packets"]
                totals["invalid_sectors"] += stats["invalid_sectors"]
            vobs.append(vob)

        upper_names = {item["name"].upper() for item in files}
        vts_ifos = sorted(name for name in upper_names if name.startswith("VTS_") and name.endswith("_0.IFO"))
        return {
            "source": str(source),
            "label": source.stem,
            "source_size": source.stat().st_size,
            "udf": True,
            "video_ts": {
                "file_count": len(files),
                "vts_count": len(vts_ifos),
                "ifo_present": "VIDEO_TS.IFO" in upper_names,
                "bup_present": "VIDEO_TS.BUP" in upper_names,
                "files": sorted(files, key=lambda item: item["name"].upper()),
                "vobs": sorted(vobs, key=lambda item: item["name"].upper()),
                "totals": totals,
            },
            "content_decrypted": totals["scrambled_pes_packets"] == 0 if inspect_vobs else None,
            "scan_depth": "full" if inspect_vobs else "quick",
        }
    finally:
        close_iso_image(image)
