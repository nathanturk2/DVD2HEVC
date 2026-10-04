# SPDX-License-Identifier: GPL-3.0-only
"""Read-only UDF file access adapted from BD2HEVC udf_repair.py."""
from pathlib import Path
from .udf_validation import BLOCK,UdfValidationError,checked_tag,inspect_udf,number

class UdfImage:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.size = self.path.stat().st_size
        self.volume = inspect_udf(self.path)
        descriptors = self.volume["descriptors"]
        partition = next(bytes.fromhex(d["data"]) for d in descriptors if d["tag"] == 5)
        logical = next(bytes.fromhex(d["data"]) for d in descriptors if d["tag"] == 6)
        if number(logical, 264) != 6 or number(logical, 268) != 1 or logical[440:442] != b"\x01\x06":
            raise UdfValidationError("Repair supports a single physical UDF partition only")
        if number(logical, 444, 2) != number(partition, 22, 2):
            raise UdfValidationError("UDF partition map mismatch")
        self.partition = number(partition, 188) * BLOCK
        self.partition_size = number(partition, 192) * BLOCK
        fsd = self.read_at(self.partition + number(logical, 252) * BLOCK, BLOCK)
        if checked_tag(fsd, number(logical, 252)) != 256:
            raise UdfValidationError("Invalid file set descriptor")
        if number(logical, 256, 2) or number(fsd, 408, 2):
            raise UdfValidationError("Unsupported file set partition reference")
        self.files: dict[str, dict] = {}
        self._seen: set[int] = set()
        self._walk(number(fsd, 404), "", 0)

    def read_at(self, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0 or offset + length > self.size:
            raise UdfValidationError("Read outside ISO")
        with self.path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read(length)
        if len(data) != length:
            raise UdfValidationError("Short ISO read")
        return data

    def _entry(self, block: int) -> dict:
        offset = self.partition + block * BLOCK
        data = self.read_at(offset, BLOCK)
        tag = checked_tag(data, block)
        if tag not in (261, 266):
            raise UdfValidationError("Not a UDF file entry")
        ext = tag == 266
        size = number(data, 56, 8)
        ea = number(data, 208 if ext else 168)
        alen = number(data, 212 if ext else 172)
        start = (216 if ext else 176) + ea
        mode = number(data, 34, 2) & 7
        if start + alen > BLOCK:
            raise UdfValidationError("Allocation table extends outside file-entry block")
        extents = []
        if mode == 3:
            if size > alen:
                raise UdfValidationError("Truncated embedded file")
            extents = [(offset + start, size)]
        elif mode in (0, 1):
            step = 8 if mode == 0 else 16
            if alen % step:
                raise UdfValidationError("Invalid allocation table length")
            for p in range(start, start + alen, step):
                length = number(data, p)
                if length >> 30:
                    raise UdfValidationError("Sparse/continued UDF allocation is not supported for repair")
                if mode == 1 and number(data, p + 8, 2):
                    raise UdfValidationError("Unsupported allocation partition")
                relative = number(data, p + 4) * BLOCK
                if relative + length > self.partition_size:
                    raise UdfValidationError("Allocation extends outside partition")
                extents.append((self.partition + relative, length))
            if sum(length for _, length in extents) < size:
                raise UdfValidationError("File allocation is shorter than declared content")
        else:
            raise UdfValidationError("Unsupported UDF allocation type")
        return {"size": size, "extents": extents, "directory": data[27] == 4, "embedded": mode == 3}

    def _walk(self, block: int, parent: str, depth: int) -> None:
        if depth > 32 or block in self._seen:
            raise UdfValidationError("Cyclic or excessively nested UDF directory")
        self._seen.add(block)
        entry = self._entry(block)
        if not entry["directory"] or entry["size"] > 16 * 1024 * 1024:
            raise UdfValidationError("Invalid UDF directory")
        data = self.read_entry(entry)
        pos = 0
        while pos < len(data):
            if not any(data[pos:pos + 16]):
                pos = (pos // BLOCK + 1) * BLOCK
                continue
            if checked_tag(data[pos:]) != 257 or pos + 38 > len(data):
                raise UdfValidationError("Invalid UDF directory record")
            flags, nlen = data[pos + 18:pos + 20]
            impl = number(data, pos + 36, 2)
            end = pos + ((38 + impl + nlen + 3) & ~3)
            if end > len(data) or number(data, pos + 28, 2):
                raise UdfValidationError("Invalid directory record bounds/partition")
            if not flags & (8 | 4):
                encoded = data[pos + 38 + impl:pos + 38 + impl + nlen]
                if not encoded or encoded[0] not in (8, 16):
                    raise UdfValidationError("Unsupported UDF filename encoding")
                name = encoded[1:].decode("latin1" if encoded[0] == 8 else "utf-16-be")
                if name in (".", "..") or any(c in name for c in "/\\\0"):
                    raise UdfValidationError("Unsafe UDF filename")
                path = parent + "/" + name if parent else name
                child = number(data, pos + 24)
                if flags & 2:
                    self._walk(child, path, depth + 1)
                else:
                    if path in self.files:
                        raise UdfValidationError("Duplicate UDF file path")
                    self.files[path] = self._entry(child)
            pos = end

    def read_entry(self, entry: dict, limit: int | None = None) -> bytes:
        remaining = entry["size"] if limit is None else min(entry["size"], limit)
        chunks = []
        for offset, length in entry["extents"]:
            take = min(remaining, length)
            if take:
                chunks.append(self.read_at(offset, take))
                remaining -= take
            if not remaining:
                break
        return b"".join(chunks)

    def read(self, path: str, limit: int | None = None) -> bytes:
        entry = self.files[path]
        if limit is None and entry["size"] > 32 * 1024 * 1024:
            raise UdfValidationError("Specify a bounded read for large media files")
        return self.read_entry(entry, limit)

