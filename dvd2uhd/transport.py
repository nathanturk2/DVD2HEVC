# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded, allocation-preserving HEVC termination for tiny DVD pictures."""
from pathlib import Path
import re

EOS=bytes.fromhex('000000014801')
AUD=bytes.fromhex('00000001460150')
MAX_SHORT_CLIP=64*1024**2


def terminal_delimiter_edits(data,pid=0x1011):
    """Return packet edits retaining every original picture and audio byte.

    VLC's Annex-B parser needs a following start code to consume the final EOS
    NAL and drain delayed frames while a menu's audio continues. Reuse the final AUD, or adaptation stuffing, keeping all original packet
    addresses, clocks and allocations.
    Only a caller-confirmed tiny video asset should use this operation.
    """
    if not data or len(data)>MAX_SHORT_CLIP or len(data)%192:
        raise ValueError('Invalid bounded M2TS still asset')
    last=None;head=None;payload_size=0;end_bytes=b'';pes_packets=[]
    for at in range(0,len(data),192):
        packet=data[at:at+192]
        if packet[4]!=0x47:raise ValueError('M2TS packet synchronization lost')
        packet_pid=((packet[5]&31)<<8)|packet[6]
        if packet_pid!=pid:continue
        if packet[5]&0x80 or packet[7]&0xc0:raise ValueError('Damaged or scrambled video transport')
        mode=(packet[7]>>4)&3
        if mode not in (1,2,3):raise ValueError('Invalid transport adaptation mode')
        if mode==2:continue
        pos=9+packet[8] if mode==3 else 8
        if pos>192:raise ValueError('Invalid transport adaptation length')
        if packet[5]&0x40:head=(at,pos);payload_size=0;end_bytes=b'';pes_packets=[]
        pes_packets.append((at,pos,packet))
        end_bytes=(end_bytes+packet[pos:])[-20:]
        if head is None:raise ValueError('Video payload has no PES header')
        payload_size+=192-pos;last=(at,pos,packet)
    if last is None:raise ValueError('Video transport is absent')
    at,pos,packet=last;tail=packet[pos:]
    endings=(EOS,EOS[1:],EOS+b'\x80',EOS[1:]+b'\x80')
    if any(end_bytes.endswith(e+a) for e in endings for a in (AUD,AUD[1:])):return []
    # Reuse the final picture's existing AUD when possible. Its first-slice
    # flag and prefix SEI already delimit that picture. Moving this non-VCL
    # delimiter after EOS gives the parser its next start code, with no new
    # bytes, packet movement, timestamp changes or stuffing requirements.
    pes=b''.join(p[pos:] for _,pos,p in pes_packets)
    if len(pes)<9 or pes[:3]!=b'\0\0\1' or not 0xe0<=pes[3]<=0xef:
        raise ValueError('Invalid terminal video PES header')
    length=int.from_bytes(pes[4:6],'big')
    if length and length!=len(pes)-6:raise ValueError('Terminal video PES length does not match its packets')
    header=9+pes[8];video=pes[header:]
    delimiter=re.match(b'\x00\x00(?:\x00)?\x01\x46\x01[\x10\x30\x50]',video)
    if delimiter and any(video.endswith(e) for e in endings):
        aud=delimiter.group();replacement=pes[:header]+video[len(aud):]+aud
        edits=[];consumed=0
        for offset,pos,p in pes_packets:
            count=192-pos;changed=p[:pos]+replacement[consumed:consumed+count];consumed+=count
            if changed!=p:edits.append((offset,changed))
        return edits
    ending=next((e for e in (EOS+b'\x80',EOS,EOS[1:]+b'\x80',EOS[1:]) if tail.endswith(e)),b'')
    prefix=tail[:-len(ending)] if ending else tail
    replacement=prefix+EOS+AUD
    # Three-byte Annex-B prefixes are equally valid. Reclaim one delimiter
    # zero when a tightly packed final packet has only five stuffing bytes.
    if ((packet[7]>>4)&3)==3 and packet[8]<len(replacement)-len(tail)+1:
        replacement=prefix+EOS[1:]+AUD[1:]
    added=len(replacement)-len(tail)
    # Accept simple adaptation stuffing only; do not risk PCR/OPCR, private
    # adaptation data, splice fields or extensions that happen to contain FF.
    safe=((packet[7]>>4)&3)==3 and packet[8]>=added+1 and not packet[9]&0x1f and packet[pos-added:pos]==b'\xff'*added
    if safe:
        edits={at:bytearray(packet)};edits[at][8]-=added
        edits[at][pos-added:]=replacement
    else:
        raise ValueError('Terminal video lacks a reusable AUD or safe adaptation stuffing')
    start,header=head;first=data[start:start+192]
    if first[header:header+3]!=b'\0\0\1' or not 0xe0<=first[header+3]<=0xef:
        raise ValueError('Invalid terminal video PES header')
    length=int.from_bytes(first[header+4:header+6],'big')
    if length and (length!=payload_size-6 or length+added>65535):
        raise ValueError('Terminal video PES length does not match its packets')
    if length:
        field=header-added if safe and start==at else header
        edits.setdefault(start,bytearray(first))[field+4:field+6]=(length+added).to_bytes(2,'big')
    return [(offset,bytes(changed)) for offset,changed in sorted(edits.items())]


def terminate_short_clip(path):
    path=Path(path)
    if path.stat().st_size>MAX_SHORT_CLIP:raise ValueError('Short clip exceeds bounded repair limit')
    edits=terminal_delimiter_edits(path.read_bytes())
    with path.open('r+b') as stream:
        for offset,packet in edits:stream.seek(offset);stream.write(packet)
    return [offset for offset,_ in edits]
