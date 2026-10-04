"""Read DVD PES sectors, keeping original PTS and physical stream identities."""
from .source import FormatError


def pts(b):
    if len(b) < 5 or not (b[0] & b[2] & b[4] & 1):
        raise FormatError("Invalid PES timestamp")
    return ((b[0] >> 1) & 7) << 30 | b[1] << 22 | (b[2] >> 1) << 15 | b[3] << 7 | b[4] >> 1


def packets(data):
    """DVD packs are sector sized; yield PES payload and PTS without guessing codecs."""
    if len(data) % 2048:
        raise FormatError("Unaligned VOB sector input")
    for base in range(0, len(data), 2048):
        sector = data[base:base + 2048]
        if sector[:4] != b"\x00\x00\x01\xba":
            raise FormatError(f"Missing MPEG pack header at {base}")
        cursor = 14 + (sector[13] & 7)
        while cursor + 6 <= 2048 and sector[cursor:cursor+3] == b"\x00\x00\x01":
            sid = sector[cursor+3]
            size = int.from_bytes(sector[cursor+4:cursor+6], "big")
            end = cursor + 6 + size
            if end > 2048:
                raise FormatError("PES packet extends outside DVD sector")
            body = sector[cursor+6:end]
            if sid == 0xBF:
                yield sid, body, None
            elif sid == 0xBD or 0xC0 <= sid <= 0xEF:
                if len(body) < 3 or body[0] & 0xC0 != 0x80:
                    raise FormatError("Unsupported MPEG-1 PES header")
                if body[0] & 0x30:
                    raise FormatError("Encrypted PES payload")
                hlen = body[2]
                stamp = pts(body[3:8]) if body[1] & 0x80 else None
                yield sid, body[3+hlen:], stamp
            cursor = end


def pci(payload):
    """Decode original menu button groups, commands and colour/alpha tables."""
    if len(payload) != 980 or payload[0] != 0:
        return None
    p = payload[1:]
    num = p[113] & 63
    if num > 36:
        raise FormatError("Invalid PCI button count")
    if not int.from_bytes(p[96:98], "big") & 3:
        return None
    groups = (p[110] >> 4) & 3
    colours = [int.from_bytes(p[118+i*4:122+i*4], "big") for i in range(6)]
    buttons = []
    for i in range(36):
        b = p[142 + i*18:160 + i*18]
        a, y = int.from_bytes(b[:3], "big"), int.from_bytes(b[3:6], "big")
        buttons.append(dict(number=i+1, colour=a >> 22, x=(a >> 12) & 1023,
                            x2=a & 1023, auto=y >> 22, y=(y >> 12) & 1023, y2=y & 1023,
                            up=b[6]&63, down=b[7]&63, left=b[8]&63, right=b[9]&63,
                            command=b[10:18].hex()))
    return dict(pts=int.from_bytes(p[98:102], "big"), end=int.from_bytes(p[102:106], "big"),
                select_end=int.from_bytes(p[106:110],"big"),
                count=num, groups=groups, display_types=[p[110]&7, (p[111]>>4)&7, p[111]&7],
                forced_select=p[116]&63, forced_activate=p[117]&63,
                colours=colours, buttons=buttons)


def pci_uops(payload):
    """VOBU restrictions change independently of whether highlight data exists."""
    if len(payload)!=980 or payload[0]!=0:return None
    p=payload[1:]
    return int.from_bytes(p[12:16],'big'),int.from_bytes(p[8:12],'big')


def cell_blocks(blocks, identity):
    """Select complete VOBUs by DSI identity inside an interleaved cell extent.

    Copying a branching cell's whole extent would mix footage from two cuts.
    DSI vobu_ea bounds every unit; incomplete or inconsistent units fail.
    """
    remaining=0
    keep=False
    selected=0
    buffered=bytearray()
    for block in blocks:
        for offset in range(0,len(block),2048):
            sector=block[offset:offset+2048]
            dsi=None
            for sid,body,stamp in packets(sector):
                if sid==0xbf and len(body)==1018 and body[0]==1:
                    p=body[1:]
                    dsi=(int.from_bytes(p[24:26],"big"),p[27],int.from_bytes(p[8:12],"big"))
            if dsi is not None:
                if remaining:raise FormatError("DSI VOBU length overlaps next NAV pack")
                keep=dsi[:2]==identity
                remaining=dsi[2]+1
                if remaining>10000:raise FormatError("Invalid DSI VOBU size")
            elif remaining==0:
                raise FormatError("Cell extraction does not begin at a complete NAV VOBU")
            if keep:
                buffered.extend(sector);selected+=1
                if len(buffered)>=1024*1024:
                    yield bytes(buffered);buffered.clear()
            remaining-=1
    if remaining:raise FormatError("Cell extent ends inside a VOBU")
    if not selected:raise FormatError("DSI identity was not found in cell extent")
    if buffered:yield bytes(buffered)
