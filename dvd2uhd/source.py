"""Read-only, bounded access to VIDEO_TS folders and UDF/ISO backups."""
from __future__ import annotations

import io
from contextlib import contextmanager
from pathlib import Path


class FormatError(ValueError):
    pass


class Disc:
    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        self.image = None
        self.root = None
        if self.path.is_dir():
            self.root = self.path / "VIDEO_TS" if (self.path / "VIDEO_TS").is_dir() else self.path
            self.names = {p.name.upper(): p.name for p in self.root.iterdir() if p.is_file()}
        else:
            import pycdlib
            self.image = pycdlib.PyCdlib()
            self.image.open(str(self.path))
            self.udf = self.image.has_udf()
            entries = self.image.list_children(**self._path(""))
            self.names = {}
            for p in entries:
                if p is not None and not p.is_dir():
                    n = p.file_identifier().decode("utf-8").split(";")[0]
                    self.names[n.upper()] = n
        if "VIDEO_TS.IFO" not in self.names:
            self.close()
            raise FormatError("Source has no VIDEO_TS.IFO")

    def _path(self, name):
        return {"udf_path": "/VIDEO_TS/" + name} if self.udf else {
            "iso_path": "/VIDEO_TS" + ("/" + name + ";1" if name else "")}

    @contextmanager
    def open(self, name):
        actual = self.names.get(name.upper())
        if actual is None:
            raise FormatError("Missing DVD file: " + name)
        if self.root:
            with (self.root / actual).open("rb") as f:
                yield f
        else:
            with self.image.open_file_from_iso(**self._path(actual)) as f:
                yield f

    def read(self, name):
        with self.open(name) as f:
            return f.read()

    def domain_files(self, vts: int, menu: bool):
        if vts == 0:
            return ["VIDEO_TS.VOB"] if "VIDEO_TS.VOB" in self.names else []
        prefix = f"VTS_{vts:02d}_"
        return sorted(n for n in self.names if n.startswith(prefix) and n.endswith(".VOB")
                      and (n == prefix + "0.VOB") == menu)

    def sectors(self, vts: int, menu: bool, first: int, last: int):
        if first < 0 or last < first:
            raise FormatError("Invalid cell sector range")
        start, remaining = first * 2048, (last - first + 1) * 2048
        for n in self.domain_files(vts, menu):
            with self.open(n) as f:
                f.seek(0, io.SEEK_END)
                size = f.tell()
                if size % 2048:
                    raise FormatError("VOB is not sector aligned: " + n)
                if start >= size:
                    start -= size
                    continue
                f.seek(start)
                count = min(remaining, size - start)
                while count:
                    block = f.read(min(count, 1024 * 1024))
                    if not block:
                        raise FormatError("Truncated VOB: " + n)
                    count -= len(block)
                    remaining -= len(block)
                    yield block
                start = 0
                if remaining == 0:
                    return
        if remaining:
            raise FormatError(f"Cell extends past VOB domain by {remaining} bytes")

    def close(self):
        if self.image:
            self.image.close()
            self.image = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
