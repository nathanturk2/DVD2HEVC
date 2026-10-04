"""Parse DVD program chains without flattening logical navigation into titles.

On-disc offsets follow libdvdread's ifo_types.h/ifo_read.c. Every read is
bounds checked; an invalid pointer is a hard failure, not an empty table.
"""
from __future__ import annotations

from .source import Disc, FormatError


class Reader:
    def __init__(self, data: bytes):
        self.data = data

    def get(self, off, size):
        if off < 0 or off + size > len(self.data):
            raise FormatError(f"IFO read out of bounds at {off:#x} ({size} bytes)")
        return self.data[off:off + size]

    def n(self, off, size=2):
        return int.from_bytes(self.get(off, size), "big")

    def sector(self, off):
        return self.n(off, 4) * 2048


def dvd_time(raw):
    def bcd(n):
        if (n & 15) > 9 or (n >> 4) > 9:
            raise FormatError("Invalid DVD BCD time")
        return (n >> 4) * 10 + (n & 15)
    secs = bcd(raw[0]) * 3600 + bcd(raw[1]) * 60 + bcd(raw[2])
    fps = {1: 25, 3: 30000 / 1001}.get(raw[3] >> 6)
    return round((secs + (bcd(raw[3] & 63) / fps if fps else 0)) * 90000)


def pgc(r: Reader, base, key, entry=0):
    r.get(base, 236)
    programs, cells = r.n(base + 2, 1), r.n(base + 3, 1)
    result = dict(key=key, entry=entry, duration=dvd_time(r.get(base + 4, 4)),
                  uops=r.n(base + 8, 4),
                  audio=[r.n(base + 12 + i * 2) for i in range(8)],
                  subpicture=[r.n(base + 28 + i * 4, 4) for i in range(32)],
                  next=r.n(base + 156), prev=r.n(base + 158), up=r.n(base + 160),
                  playback_mode=r.n(base + 162, 1), still=r.n(base + 163, 1),
                  palette=[r.n(base + 164 + i * 4, 4) for i in range(16)],
                  pre=[], post=[], cell_commands=[], programs=[], cells=[])
    cmd, pm, cp, pos = [r.n(base + 228 + i * 2) for i in range(4)]
    if cmd:
        cbase = base + cmd
        counts = [r.n(cbase + i * 2) for i in range(3)]
        if sum(counts) > 255:
            raise FormatError("IFO command count exceeds DVD limits")
        cursor = cbase + 8
        for name, count in zip(("pre", "post", "cell_commands"), counts):
            result[name] = [r.get(cursor + i * 8, 8).hex() for i in range(count)]
            cursor += count * 8
    if programs:
        if not pm:
            raise FormatError("PGC program map is absent")
        result["programs"] = list(r.get(base + pm, programs))
    if cells and (not cp or not pos):
        raise FormatError("PGC cell table is absent")
    for i in range(cells):
        off = base + cp + i * 24
        flags = r.n(off, 1)
        cell = dict(number=i + 1, flags=flags, block_mode=flags >> 6,
                    block_type=(flags >> 4) & 3, interleaved=bool(flags & 4),
                    still=r.n(off + 2, 1), command=r.n(off + 3, 1),
                    duration=dvd_time(r.get(off + 4, 4)),
                    first=r.n(off + 8, 4), last=r.n(off + 20, 4),
                    vob_id=r.n(base + pos + i * 4), cell_id=r.n(base + pos + i * 4 + 3, 1))
        if cell["first"] > cell["last"]:
            raise FormatError("Reversed cell extent")
        result["cells"].append(cell)
    if any(not 1 <= n <= cells for n in result["programs"]):
        raise FormatError("Invalid program-to-cell mapping")
    return result


def pgcit(r, base, prefix):
    if not base:
        return []
    count, size = r.n(base), r.n(base + 4, 4) + 1
    r.get(base, size)
    if count > 999:
        raise FormatError("Too many PGCs")
    result = []
    for i in range(count):
        off = base + 8 + 8 * i
        rel = r.n(off + 4, 4)
        if rel < 8 + 8 * count or rel + 236 > size:
            raise FormatError("Invalid PGC pointer")
        result.append(pgc(r, base + rel, f"{prefix}:{i+1}", r.n(off, 1)))
    return result


def menus(r, base, vts):
    if not base:
        return []
    r.get(base, r.n(base + 4, 4) + 1)
    result = []
    for i in range(r.n(base)):
        off = base + 8 + i * 8
        # Undefined DVD language codes can contain 0xff. Preserve both bytes;
        # replacement characters would collapse distinct language units/keys.
        lang = r.get(off, 2).decode("latin-1")
        for p in pgcit(r, base + r.n(off + 4, 4), f"M:{vts}:{lang}"):
            p.update(domain="menu", vts=vts, language=lang)
            result.append(p)
    return result


def attributes(r, base):
    video = r.get(base, 2)
    acount = r.n(base + 3, 1)
    scount = r.n(base + 85, 1)
    if acount > 8 or scount > 32:
        raise FormatError("Invalid DVD stream count")
    return dict(width=[720, 704, 352, 352][(video[1] >> 3) & 3],
                height=576 if (video[0] >> 4) & 3 == 1 else 480,
                aspect="16:9" if (video[0] >> 2) & 3 == 3 else "4:3",
                audio=[dict(ordinal=i, format=r.n(base + 4 + i * 8, 1) >> 5,
                            language=r.get(base + 6 + i * 8, 2).decode("latin-1"))
                       for i in range(acount)],
                subtitles=[dict(ordinal=i, language=r.get(base + 88 + i * 6, 2).decode("latin-1"))
                           for i in range(scount)])


def inspect(disc: Disc):
    r = Reader(disc.read("VIDEO_TS.IFO"))
    if r.get(0, 12) != b"DVDVIDEO-VMG":
        raise FormatError("Invalid VMGI signature")
    first = r.n(0x84, 4)
    pgcs = menus(r, r.sector(0xC8), 0)
    if first:
        p = pgc(r, first, "F:0:1")
        p.update(domain="first", vts=0, language="en")
        pgcs.insert(0, p)
    domains = {"M:0": attributes(r, 0x100)}
    titles = []
    tb = r.sector(0xC4)
    if tb:
        r.get(tb, r.n(tb + 4, 4) + 1)
        for i in range(r.n(tb)):
            off = tb + 8 + i * 12
            titles.append(dict(number=i + 1, angles=r.n(off + 1, 1),
                               chapters=r.n(off + 2), vts=r.n(off + 6, 1),
                               vts_title=r.n(off + 7, 1)))
    for vts in range(1, r.n(0x3E) + 1):
        vr = Reader(disc.read(f"VTS_{vts:02d}_0.IFO"))
        if vr.get(0, 12) != b"DVDVIDEO-VTS":
            raise FormatError("Invalid VTSI signature")
        tp = pgcit(vr, vr.sector(0xCC), f"T:{vts}")
        for p in tp:
            p.update(domain="title", vts=vts, language="")
        pgcs.extend(tp)
        pgcs.extend(menus(vr, vr.sector(0xD0), vts))
        domains[f"T:{vts}"] = attributes(vr, 0x200)
        domains[f"M:{vts}"] = attributes(vr, 0x100)
        pt = vr.sector(0xC8)
        if pt:
            count, end = vr.n(pt), vr.n(pt + 4, 4) + 1
            offsets = [vr.n(pt + 8 + j * 4, 4) for j in range(count)] + [end]
            for t in [t for t in titles if t["vts"] == vts]:
                n = t["vts_title"] - 1
                if not 0 <= n < count:
                    raise FormatError("Global title references missing VTS title")
                a, b = offsets[n:n+2]
                if a < 8 + 4 * count or b < a or (b-a) % 4:
                    raise FormatError("Invalid part-of-title offsets")
                t["parts"] = [dict(pgc=vr.n(pt+j), program=vr.n(pt+j+2)) for j in range(a,b,4)]
    return dict(schema="dvd2uhd-navigation-v1", pgcs=pgcs, titles=titles, domains=domains)
