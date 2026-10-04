"""Native subtitle selection channels; original DVD SPUs still render in BD-J.

PGS display sets contain only clears, so the native and BD-J renderers never
draw duplicate captions. Each channel represents one original logical DVD
subtitle slot, independent of its PGC-specific physical stream and palette.
Wire layout was researched from FFmpeg's public pgssubdec.c; no code copied.
"""
import struct
from .languages import ISO3


def languages(graph,refs):
    pgc=refs[0][0]
    if pgc['domain']!='title':return []
    domain=graph['domains']['T:'+str(pgc['vts'])]
    tracks=domain['subtitles']
    if len(tracks)>32 or [t['ordinal'] for t in tracks]!=list(range(len(tracks))):
        raise ValueError('Invalid original logical subtitle slots')
    # Some converted DVDs declare 32 attribute slots but use only five.
    # Keep the same inventory across this VTS, including internal holes, so
    # channel ordinal remains the DVD logical slot at every cell boundary.
    used=[n for p in graph['pgcs'] if p['domain']=='title' and p['vts']==pgc['vts']
          for n,control in enumerate(p['subpicture']) if control&0x80000000]
    count=max(used,default=-1)+1
    if count>len(tracks):raise ValueError('DVD subtitle control exceeds original attributes')
    return [ISO3.get(t['language'],'und') for t in tracks[:count]]


def clear_stream(fps):
    rates={24:0x20,25:0x30,30:0x50,50:0x60,60:0x70}
    rate=0x10 if abs(fps-24000/1001)<.01 else 0x40 if abs(fps-30000/1001)<.01 else rates.get(round(fps))
    if rate is None:raise ValueError('Unsupported subtitle channel frame rate')
    def segment(stamp,kind,data):
        return b'PG'+struct.pack('>IIBH',stamp,stamp,kind,len(data))+data
    def clear(stamp,number):
        pcs=struct.pack('>HHBHBBBB',1920,1080,rate,number,0x80,0,0,0)
        return (segment(stamp,0x16,pcs)+segment(stamp,0x17,b'\0')+
                segment(stamp,0x14,b'\0\0\0\x10\x80\x80\0')+segment(stamp,0x80,b''))
    return clear(0,0)+clear(900,1)
