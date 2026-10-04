"""DVD SPU reassembly and lossless two-bit mask decoding for BD-J graphics."""
from __future__ import annotations
from dataclasses import dataclass, field
from .source import FormatError


@dataclass
class Picture:
    start: int
    end: int
    forced: bool
    x: int
    y: int
    width: int
    height: int
    pixels: bytes
    colours: list[int]
    alpha: list[int]
    # Absolute inclusive rectangles, with a SET_COLOR/SET_CONTR combined word.
    # Keep the original two-bit mask: PCI button highlights override these.
    colcon: list[tuple[int, int, int, int, int]] = field(default_factory=list)


def colour_changes(data: bytes):
    """Decode CHG_COLCON's bounded LN_CTLI/PX_CTLI parameter area.

    The two-byte size includes itself. Each vertical band contains ascending
    start columns; a change lasts until the next column or the display edge.
    Format references are recorded in docs/ARCHITECTURE.md.
    """
    if len(data) < 6 or int.from_bytes(data[:2], 'big') != len(data):
        raise FormatError('Invalid CHG_COLCON parameter length')
    cursor, regions = 2, []
    while cursor + 4 <= len(data):
        header = int.from_bytes(data[cursor:cursor+4], 'big')
        cursor += 4
        if header == 0x0fffffff:
            if cursor != len(data):
                raise FormatError('Data after CHG_COLCON terminator')
            return regions
        top, count, bottom = (header >> 16) & 4095, (header >> 12) & 15, header & 4095
        if header >> 28 or count == 0 or bottom < top or cursor + count*6 > len(data):
            raise FormatError('Invalid CHG_COLCON line control')
        changes = []
        for _ in range(count):
            column = int.from_bytes(data[cursor:cursor+2], 'big')
            word = int.from_bytes(data[cursor+2:cursor+6], 'big')
            cursor += 6
            if column > 4095 or changes and column <= changes[-1][0]:
                raise FormatError('Invalid CHG_COLCON column order')
            changes.append((column, word))
        for i, (column, word) in enumerate(changes):
            right = changes[i+1][0]-1 if i+1 < len(changes) else 4095
            regions.append((column, top, right, bottom, word))
    raise FormatError('Missing CHG_COLCON terminator')


class Assembler:
    def __init__(self):
        self.pending = {}
        self.stamps = {}

    def feed(self, body, pts):
        if not body or not 0x20 <= body[0] <= 0x3F:
            return []
        sid = body[0] - 0x20
        if pts is not None:
            if self.pending.get(sid):
                raise FormatError("Incomplete SPU packet before next timestamp")
            self.stamps[sid] = pts
        buf = self.pending.setdefault(sid, bytearray())
        buf.extend(body[1:])
        result = []
        while len(buf) >= 2:
            size = int.from_bytes(buf[:2], "big")
            if size < 4:
                raise FormatError("Invalid SPU packet size")
            if len(buf) < size:
                break
            result.append((sid, self.stamps.get(sid), bytes(buf[:size])))
            del buf[:size]
        return result


def decode(raw: bytes, duration=10 * 90000, events=None):
    size = int.from_bytes(raw[:2], "big")
    ctrl = int.from_bytes(raw[2:4], "big")
    if size != len(raw) or not 4 <= ctrl < size:
        raise FormatError("Invalid SPU control offset")
    off, seen = ctrl, set()
    colours, alpha, coords, offsets = [0]*4, [0]*4, None, None
    colcon = []
    snapshots = []
    visible, forced = False, False
    stops=[]
    while off not in seen:
        if off < ctrl or off + 4 > size:
            raise FormatError("Invalid SPU control chain")
        seen.add(off)
        date = int.from_bytes(raw[off:off+2], "big") * 1024
        nxt = int.from_bytes(raw[off+2:off+4], "big")
        if nxt != off and (nxt <= off or nxt + 4 > size):
            raise FormatError('Invalid SPU control chain')
        limit = nxt if nxt != off else size
        if snapshots and date < snapshots[-1][0]:
            raise FormatError('SPU control dates run backwards')
        cursor = off + 4
        while cursor < limit:
            op = raw[cursor]
            cursor += 1
            lengths = {0:0, 1:0, 2:0, 3:2, 4:2, 5:6, 6:4, 7:2, 255:0}
            if op not in lengths:
                raise FormatError(f"Unsupported SPU control opcode {op:#x}")
            n = lengths[op]
            if cursor + n > limit:
                raise FormatError("Truncated SPU command")
            if op == 7:
                n = int.from_bytes(raw[cursor:cursor+2], 'big')
                if n < 6 or cursor + n > limit:
                    raise FormatError('Truncated CHG_COLCON command')
            b = raw[cursor:cursor+n]
            cursor += n
            if op == 255:
                break
            if op in (0, 1):
                visible = True
                forced = op == 0
            elif op == 2:
                visible = False
                stops.append(date)
            elif op in (3, 4):
                v = int.from_bytes(b, "big")
                values = [(v >> (i*4)) & 15 for i in range(4)]
                if op == 3:
                    colours = values
                else:
                    alpha = values
            elif op == 5:
                x, y = int.from_bytes(b[:3], "big"), int.from_bytes(b[3:], "big")
                coords = (x >> 12, y >> 12, (x & 4095) - (x >> 12) + 1, (y & 4095) - (y >> 12) + 1)
            elif op == 6:
                offsets = (int.from_bytes(b[:2], "big"), int.from_bytes(b[2:], "big"))
            elif op == 7:
                colcon = colour_changes(b)
        else:
            raise FormatError("Unterminated SPU commands")
        snapshots.append((date, visible, forced, coords, offsets, colours[:], alpha[:], colcon[:]))
        if nxt == off:
            break
        off = nxt
    result = []
    for i, (date, vis, forced, coords, offsets, col, al, changes) in enumerate(snapshots):
        if not vis:
            continue
        if coords is None or offsets is None:
            raise FormatError("Visible SPU has no bitmap geometry")
        x,y,w,h = coords
        if w <= 0 or h <= 0 or w > 720 or h > 576 or x+w > 720 or y+h > 576:
            raise FormatError("Invalid SPU geometry")
        pixels = bytearray(w*h)
        for field in range(2):
            nibble = offsets[field] * 2
            def read_nibble():
                nonlocal nibble
                if nibble >= ctrl*2:
                    raise FormatError("SPU RLE extends into control table")
                val = raw[nibble//2]
                val = val & 15 if nibble & 1 else val >> 4
                nibble += 1
                return val
            for row in range(field,h,2):
                column = 0
                while column < w:
                    code = read_nibble()
                    if code < 4:
                        code = code*16 + read_nibble()
                        if code < 16:
                            code = code*16 + read_nibble()
                            if code < 64:
                                code = code*16 + read_nibble()
                    run, colour = code >> 2, code & 3
                    if run == 0:
                        run = w-column
                    if run > w-column:
                        raise FormatError("SPU RLE run exceeds scanline")
                    pixels[row*w+column:row*w+column+run] = bytes([colour])*run
                    column += run
                nibble += nibble & 1
        end = snapshots[i+1][0] if i+1 < len(snapshots) else duration
        if end > date:
            result.append(Picture(date,end,forced,x,y,w,h,bytes(pixels),col,al,changes))
    if events is not None:
        # Replacing or clearing an SPU remains meaningful even if the packet
        # contains no visible bitmap (especially across a DVD cell boundary).
        events.append(min(p.start for p in result) if result else min(stops) if stops else snapshots[0][0])
    return result


def rgba(picture: Picture, palette, colour_word=None, crop=None):
    from PIL import Image
    col, alpha = picture.colours, picture.alpha
    if colour_word is not None:
        col = [(colour_word >> (16+i*4)) & 15 for i in range(4)]
        alpha = [(colour_word >> (i*4)) & 15 for i in range(4)]
    lut = []
    for i in range(4):
        val = palette[col[i]]
        y, cr, cb = (val >> 16) & 255, (val >> 8) & 255, val & 255
        def clamp(n):
            return max(0, min(255, round(n)))
        c,d,e = y-16,cb-128,cr-128
        lut.append((clamp(1.164*c+1.596*e),clamp(1.164*c-.392*d-.813*e),
                    clamp(1.164*c+2.017*d),alpha[i]*17))
    img = Image.new("RGBA", (picture.width,picture.height))
    img.putdata([lut[p] for p in picture.pixels])
    if colour_word is None:
        for left, top, right, bottom, word in picture.colcon:
            changed = rgba(Picture(picture.start,picture.end,picture.forced,picture.x,picture.y,
                                   picture.width,picture.height,picture.pixels,picture.colours,picture.alpha),
                           palette,word)
            box = (max(0,left-picture.x),max(0,top-picture.y),
                   min(picture.width,right-picture.x+1),min(picture.height,bottom-picture.y+1))
            if box[0] < box[2] and box[1] < box[3]:
                img.paste(changed.crop(box),box[:2])
    if crop:
        x,y,x2,y2 = crop
        img = img.crop((max(0,x-picture.x),max(0,y-picture.y),
                        min(picture.width,x2-picture.x+1),min(picture.height,y2-picture.y+1)))
    return img
