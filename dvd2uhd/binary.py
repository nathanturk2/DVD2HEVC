"""Versioned, dependency-free data interchange with the BD-J application."""
import gzip
import io
import struct
import zlib
from pathlib import Path


class Writer:
    def __init__(self):
        self.f = io.BytesIO()

    def i(self, n):
        self.f.write(struct.pack(">I", n & 0xFFFFFFFF))

    def q(self, n):
        self.f.write(struct.pack(">Q", n & 0xFFFFFFFFFFFFFFFF))

    def boolean(self, v):
        self.f.write(bytes([bool(v)]))

    def text(self, s):
        # Java DataInput.readUTF uses modified UTF-8, including C0 80 for NUL
        # and separately encoded UTF-16 surrogates. Undefined language bytes
        # in original IFO keys must survive the interchange unchanged.
        b = bytearray()
        units = s.encode('utf-16-be', 'surrogatepass')
        for i in range(0,len(units),2):
            c = int.from_bytes(units[i:i+2], 'big')
            if 0 < c < 128:b.append(c)
            elif c < 2048:b.extend((0xc0 | c >> 6,0x80 | c & 63))
            else:b.extend((0xe0 | c >> 12,0x80 | c >> 6 & 63,0x80 | c & 63))
        if len(b) > 65535:
            raise ValueError("Resource name too long")
        self.f.write(struct.pack(">H", len(b)))
        self.f.write(b)

    def ints(self, items):
        self.i(len(items))
        for n in items:
            self.i(n)

    def commands(self, items):
        self.i(len(items))
        for s in items:
            self.q(int(s, 16))

    def save(self, p):
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        Path(p).write_bytes(self.f.getvalue())


def model(graph, target):
    w = Writer()
    w.i(0x4456444A)
    w.i(6)
    w.i(len(graph["pgcs"]))
    for p in graph["pgcs"]:
        w.text(p["key"])
        w.i(p["vts"])
        w.boolean(p["domain"] == "title")
        for n in ("entry", "next", "prev", "up", "still", "playback_mode"):
            w.i(p[n])
        attr = graph["domains"][("T:" if p["domain"] == "title" else "M:") + str(p["vts"])]
        w.i(attr["width"])
        w.i(attr["height"])
        w.boolean(attr["aspect"] == "16:9")
        w.i(p.get('uops',0))
        for n in ("audio", "subpicture", "programs", "palette"):
            w.ints(p[n])
        for n in ("pre", "post", "cell_commands"):
            w.commands(p[n])
        w.i(len(p["cells"]))
        for c in p["cells"]:
            w.i(c["flags"])
            w.i(c["still"])
            w.i(c["command"])
            w.q(c["duration"] // 90)
            w.i(c.get("playlist", -1))
            w.text(c.get("graphics", ""))
            w.ints(c.get("audio_streams", []))
            w.q(c.get("start_ms",0))
            w.q(c.get("end_ms",0))
            w.q(c.get('presentation_ms',c['duration']//90))
            # Model 5's original boolean byte extends to compatible flags.
            # An older reader still sees True; new readers require a certified
            # silent tail before native keepalive can repeat any video.
            repeated=c.get('still_repeated',False)
            w.f.write(bytes([(1 if repeated else 0)|(2 if repeated and c.get('still_keepalive',False) else 0)]))
            w.boolean(c.get('native_subtitle_controls',False))
    w.i(len(graph["titles"]))
    for t in graph["titles"]:
        for n in ("number", "vts", "vts_title"):
            w.i(t[n])
        w.ints([x["pgc"] for x in t.get("parts", [])])
        w.ints([x["program"] for x in t.get("parts", [])])
    w.save(target)


def graphics(menus, pictures, origin, target, resets=None, uops=None):
    w = Writer()
    w.i(0x47584634)
    def millis(stamp):
        # DVD timestamps wrap at 2^32 in PCI, 2^33 in PES.
        return round((((stamp - origin) + 2**31) % 2**32 - 2**31) / 90)
    w.i(len(menus))
    for m in menus:
        w.q(millis(m["pts"]))
        w.q(0x7FFFFFFFFFFFFFFF if m["end"] == 0xFFFFFFFF else millis(m["end"]))
        for n in ("count", "groups", "forced_select", "forced_activate"):
            w.i(m[n])
        select_end=m.get('select_end',m['end'])
        w.q(0x7FFFFFFFFFFFFFFF if select_end==0xFFFFFFFF else millis(select_end))
        w.ints(m["display_types"])
        w.ints(m["colours"])
        for b in m["buttons"]:
            for n in ("colour", "x", "y", "x2", "y2", "auto", "up", "down", "left", "right"):
                w.i(b[n])
            w.q(int(b["command"], 16))
    w.i(len(pictures))
    for sid, stamp, pic in pictures:
        w.i(sid)
        w.q(millis(stamp) + pic.start // 90)
        w.q(millis(stamp) + pic.end // 90)
        w.boolean(pic.forced)
        for n in ("x", "y", "width", "height"):
            w.i(getattr(pic, n))
        w.ints(pic.colours)
        w.ints(pic.alpha)
        packed=zlib.compress(pic.pixels)
        w.i(len(packed))
        w.f.write(packed)
        w.i(len(pic.colcon))
        for region in pic.colcon:
            for value in region:w.i(value)
    w.i(len(resets or []))
    for sid,stamp in resets or []:w.i(sid);w.q(millis(stamp))
    w.i(len(uops or []))
    for stamp,mask in uops or []:w.q(millis(stamp));w.i(mask)
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    Path(target).write_bytes(gzip.compress(w.f.getvalue(), mtime=0))
