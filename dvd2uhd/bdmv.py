"""Author BD-J bootstrap records and join tsMuxeR-authored clip metadata."""
import struct
from pathlib import Path

u16 = lambda n: struct.pack(">H", n)
u32 = lambda n: struct.pack(">I", n)


def section(b):
    return u32(len(b)) + b


def app_string(s):
    b = s.encode("ascii")
    return bytes([len(b)]) + b + (b"\0" if len(b) % 2 == 0 else b"")


def bdjo(args=()):
    """One persistent autostart Xlet; 1080 graphics; public profile 1 APIs."""
    terminal = section(b"00000" + b"\x10" + b"\0"*4)
    cache = section(b"\x01\0" + b"\x01" + b"00000" + b"eng" + b"\0"*3)
    playlists = section(u32(1 << 20))  # accessible all, no automatic media playback
    name = b"eng" + bytes([len(b"DVD Navigation")]) + b"DVD Navigation"
    # Not service-bound: the same VM must survive public TitleContext switches
    # between the interactive top menu and movie title used for VLC progress.
    descriptor = b"\x10\0" + u16(1) + bytes([1,0,0,0]) + b"\x00\x40"
    descriptor += u16(len(name)) + name + (b"\0" if len(name)%2 else b"")
    descriptor += app_string("") + u16(0) + app_string("00000")
    params=b"".join(bytes([len(a.encode('ascii'))])+a.encode('ascii') for a in args)
    if len(params)>255:raise ValueError("BD-J application parameters exceed 255 bytes")
    descriptor += app_string("") + app_string("org.dvd2uhd.DvdXlet") + bytes([len(params)]) + params
    if len(params)%2==0:descriptor+=b"\0"
    app = b"\x01\x10" + u32(0x44564432) + u16(1) + b"\0\0" + u32(len(descriptor)) + b"\0"*4 + descriptor
    management = section(b"\x01\0" + app)
    sections = [terminal, cache, playlists, management, u32(0xFFE00000), u16(0)]
    cursor, addresses = 48, []
    for s in sections:
        addresses.append(cursor)
        cursor += len(s)
    return b"BDJO0200" + b"".join(u32(a) for a in addresses) + b"\0"*16 + b"".join(sections)


def index_from_mux(template):
    """Keep tsMuxeR's UHD application info/extension, replace navigation objects."""
    data = bytearray(template)
    start, extension = struct.unpack_from(">II",data,8)
    if extension:
        count=data[extension+11]
        for i in range(count):
            a,b,off,size=struct.unpack_from(">HHII",data,extension+12+i*12)
            if (a,b)==(3,1):
                # Source DVDs are SDR. tsMuxeR's default UHD record claims HDR.
                data[extension+off+6] &= 0xC0
    old_end = start + 4 + int.from_bytes(data[start:start+4],"big")
    interactive = u32(2 << 30) + u16(3 << 14) + b"00000" + b"\0"
    movie = u32(2 << 30) + u16(2 << 14) + b"00000" + b"\0"
    index = section(interactive + interactive + u16(1) + movie)
    delta = len(index)-(old_end-start)
    header = bytearray(data[:start])
    if extension:
        struct.pack_into(">I",header,12,extension+delta)
    return bytes(header) + index + bytes(data[old_end:])


def rename_playlist(data, clip_id):
    data=bytearray(data)
    start=int.from_bytes(data[8:12],"big")
    # MPLS playlist section: length, reserved, item count, subpath count.
    count=int.from_bytes(data[start+6:start+8],"big")
    if count!=1:
        raise ValueError("Expected one tsMuxeR play item per cell")
    pos=start+10
    data[pos+2:pos+7]=f"{clip_id:05d}".encode("ascii")
    return bytes(data)


def playlist_times(data):
    start=int.from_bytes(data[8:12],"big")
    pos=start+10+2
    return struct.unpack_from(">II",data,pos+12)


def joined_playlist(sources,programs,connections=None,durations=None):
    """Reuse cell clips in logical PGC order, with original chapter marks."""
    items=[]
    times=[]
    for i,data in enumerate(sources):
        start=int.from_bytes(data[8:12],"big")
        if int.from_bytes(data[start+6:start+8],"big")!=1:raise ValueError("Expected cell playlist")
        pos=start+10
        size=int.from_bytes(data[pos:pos+2],"big")+2
        item=bytearray(data[pos:pos+size])
        # Condition 5 joins separately clocked, compatible sequences seamlessly.
        # Condition 1 preserves intentional discontinuities/format changes.
        condition=connections[i] if connections is not None else 1
        if condition not in (1,5):raise ValueError('Unsupported play-item connection')
        item[12]=(item[12]&0xF0)|condition
        begin,end=playlist_times(data)
        if durations is not None:
            if durations[i]<=0:raise ValueError('Invalid DVD presentation duration')
            end=begin+round(durations[i]/2)
            struct.pack_into('>I',item,18,end)
        items.append(bytes(item))
        times.append((begin,end))
    playlist=section(b"\0\0"+u16(len(items))+u16(0)+b"".join(items))
    marks=[]
    for n in programs:
        if not 1<=n<=len(items):raise ValueError("Invalid chapter cell")
        marks.append(b"\0\x01"+u16(n-1)+u32(times[n-1][0])+u16(0xFFFF)+u32(0))
    marks=section(u16(len(marks))+b"".join(marks))
    # Header + AppInfoPlayList are preserved; disc-title playback managed by Xlet.
    template=sources[0]
    start=int.from_bytes(template[8:12],"big")
    header=bytearray(template[:start])
    struct.pack_into(">II",header,12,start+len(playlist),0)
    return bytes(header)+playlist+marks,times


def join_titles(root,graph,report):
    joined=[]
    next_id=len(report["cells"])
    for p in graph["pgcs"]:
        all_cells=p["cells"]
        # Rebuilds must reconsider eligibility from physical clips rather than
        # retaining stale offsets from a previous joined playlist.
        for c in all_cells:
            if 'clip' in c:c.update(playlist=c['clip'],start_ms=0,end_ms=0)
        if p["domain"]!="title" or p["still"]:continue
        # Cell commands execute at the END of a cell. Include that cell in the
        # preceding run, then split before the next cell so native seeking cannot
        # jump past its command. True still/control cells stay independent.
        runs=[];current=[]
        for c in all_cells:
            clip=c.get('clip',c.get('playlist',-1))
            ordinary=clip>=0 and not (c['still'] or c['duration']<=0)
            if ordinary:
                begin,end=report['cells'][clip]['playlist_times']
                ordinary=abs(c['duration']-(end-begin)*2)<=90000
            if ordinary:current.append(c)
            if not ordinary or c['command']:
                if len(current)>1:runs.append(current)
                current=[]
        if len(current)>1:runs.append(current)
        for cells in runs:
            next_id=_join_run(root,p,cells,report,joined,next_id)
    report["joined_title_playlists"]=joined
    return joined


def primary_streams(data):
    """Bounded read of authored single-angle MPLS primary stream tables.

    Wire fields researched from libbluray's mpls_parse.c; implementation is
    independent. Returns each play item's video/audio/PG entries in order.
    """
    def get(pos,size,end=None):
        if pos<0 or pos+size>min(len(data),len(data) if end is None else end):
            raise ValueError('Truncated MPLS stream table')
        return data[pos:pos+size]
    def number(pos,size,end=None):return int.from_bytes(get(pos,size,end),'big')
    if get(0,4)!=b'MPLS':raise ValueError('Expected MPLS playlist')
    start=number(8,4);limit=start+4+number(start,4)
    get(start,10,limit);count=number(start+6,2,limit);pos=start+10;result=[]
    for _ in range(count):
        end=pos+2+number(pos,2,limit)
        if end>limit:raise ValueError('MPLS play item exceeds playlist')
        get(pos,34,end)
        if number(pos+12,1,end)&16:raise ValueError('Multi-angle stream audit unsupported')
        stn=pos+34;stn_end=stn+2+number(stn,2,end)
        if stn_end>end:raise ValueError('MPLS stream table exceeds play item')
        get(stn,16,stn_end)
        counts=get(stn+4,3,stn_end);cursor=stn+16;item={}
        for kind,n in zip(('video','audio','subtitle'),counts):
            entries=[]
            for _ in range(n):
                size=number(cursor,1,stn_end);entry=get(cursor+1,size,stn_end);cursor+=1+size
                size=number(cursor,1,stn_end);attr=get(cursor+1,size,stn_end);cursor+=1+size
                if len(entry)<3 or entry[0]!=1 or not attr:raise ValueError('Unexpected authored MPLS stream entry')
                codec=attr[0];language=None
                if kind=='audio':language=attr[2:5]
                if kind=='subtitle':
                    if codec!=0x90:raise ValueError('Expected PGS selection channel')
                    language=attr[1:4]
                if language is not None and len(language)!=3:raise ValueError('Truncated MPLS language')
                entries.append(dict(pid=int.from_bytes(entry[1:3],'big'),codec=codec,
                                    language=None if language is None else language.decode('ascii')))
            item[kind]=entries
        result.append(item);pos=end
    return result


def _join_run(root,p,cells,report,joined,next_id):
    ids=[c.get("clip",c["playlist"]) for c in cells]
    rows=[report["cells"][n] for n in ids]
    # DVD cell timing is authoritative. A muxer's last-frame convention
    # can shorten cells by 80 ms and accumulate seconds of chapter drift.
    durations=[round(c['duration']/2)*2 for c in cells]
    if next_id>=2000:raise ValueError("Too many BD-J playlists")
    sources=[(root/"BDMV/PLAYLIST"/f"{n:05d}.mpls").read_bytes() for n in ids]
    connections=[1]
    def format_key(row):
        # VLC handles source cadence changes (e.g. bobbed 50p -> 25p)
        # through each play item's own clock/SPS. Keeping condition 5
        # avoids dropping reference pictures at such DVD seamless joins.
        return tuple(row.get(k) for k in ('video_codec','width','height','audio_count'))+(tuple(row.get('audio_streams',[])),repr(row.get('audio_formats')),repr(row.get('subtitle_controls',[])))
    for i in range(1,len(cells)):
        compatible=format_key(rows[i-1])==format_key(rows[i])
        terminated=rows[i-1].get('sequence_end',False) and rows[i].get('sequence_end',False)
        dvd_seamless=bool(cells[i].get('source_flags',cells[i]['flags'])&8)
        connections.append(5 if compatible and terminated and dvd_seamless else 1)
    first=cells[0]['number'];last=cells[-1]['number']
    chapters=[n for n in p['programs'] if first<=n<=last]
    data,times=joined_playlist(sources,[n-first+1 for n in chapters],connections,durations)
    for prefix in ("","BACKUP/"):
        target=root/"BDMV"/(prefix+"PLAYLIST")/f"{next_id:05d}.mpls"
        target.write_bytes(data)
    cursor=0
    for c,clip,n in zip(cells,ids,durations):
        c.update(clip=clip,playlist=next_id,start_ms=round(cursor/90),end_ms=round((cursor+n)/90))
        cursor+=n
    joined.append(dict(pgc=p["key"],cells=[c['number'] for c in cells],playlist=next_id,clips=ids,connections=connections,chapter_cells=chapters,duration_ticks=cursor))
    return next_id+1

def bootstrap(root: Path, template, args=()):
    bdmv=root/"BDMV"
    empty_objects=b"MOBJ0200"+u32(0)+b"\0"*28+section(b"\0"*6)
    for relative,data in [("index.bdmv",index_from_mux(template)),("MovieObject.bdmv",empty_objects),("BDJO/00000.bdjo",bdjo(args))]:
        for prefix in ("", "BACKUP/"):
            p=bdmv/(prefix+relative)
            p.parent.mkdir(parents=True,exist_ok=True)
            p.write_bytes(data)
    (root/"CERTIFICATE/BACKUP").mkdir(parents=True,exist_ok=True)
    import hashlib
    identity=hashlib.sha256((root/"navigation.json").read_bytes()).digest()[:16]
    certificate=b"BDID0200"+u32(40)+u32(0)+b"\0"*24+u32(0x44564432)+identity
    for prefix in ("", "BACKUP/"):(root/"CERTIFICATE"/(prefix+"id.bdmv")).write_bytes(certificate)
