"""Preserve complete source AC-3 frames that a CRC-checking muxer discards.

Only the disposable mux input receives corrected CRC2 words. After muxing,
restore the original words at their independently tracked elementary offsets.
The normal full audio audit must still match every original frame byte.
"""
import hashlib
import mmap
import uuid
from pathlib import Path
from .audio_frames import frame_size


def _table():
    values=[]
    for value in range(256):
        value<<=8
        for _ in range(8):value=((value<<1)^0x8005 if value&0x8000 else value<<1)&65535
        values.append(value)
    return values


_CRC=_table()


def crc2(frame):
    value=0
    for byte in memoryview(frame)[2:-2]:value=((value<<8)&65535)^_CRC[(value>>8)^byte]
    return value.to_bytes(2,'big')


def prepare(path):
    """Retain originals, record exact repairs, and prepare a mux-only copy."""
    path=Path(path);output=path.with_name(path.stem+'.crc-'+uuid.uuid4().hex[:8]+path.suffix)
    changes=[];offset=0
    with path.open('rb') as source,output.open('xb') as destination:
        while header:=source.read(6):
            size=frame_size(header)
            if size<6:raise ValueError('Audio frame is smaller than its header')
            frame=header+source.read(size-6)
            if len(frame)!=size:raise ValueError('Incomplete frame in CRC preservation input')
            corrected=crc2(frame)
            if corrected!=frame[-2:]:
                changes.append(dict(offset=offset+size-2,original=frame[-2:].hex(),
                                    temporary=corrected.hex(),frame_sha256=hashlib.sha256(frame).hexdigest()))
                frame=frame[:-2]+corrected
            destination.write(frame);offset+=size
    if not changes:raise ValueError('Muxer reported bad AC-3 frames but no CRC2 mismatch was found')
    return output,changes


def restore(path, repairs):
    """Restore CRC words through M2TS/PES boundaries without changing packetization."""
    path=Path(path)
    states={}
    for pid,changes in repairs.items():
        replacements={}
        for row in changes:
            original=bytes.fromhex(row['original']);temporary=bytes.fromhex(row['temporary'])
            if len(original)!=2 or len(temporary)!=2:raise ValueError('Invalid CRC preservation record')
            for n in range(2):
                position=row['offset']+n
                if position in replacements:raise ValueError('Overlapping CRC preservation records')
                replacements[position]=(temporary[n],original[n])
        states[pid]=dict(offset=0,remaining=0,continuity=None,replacements=replacements,
                         positions=sorted(replacements),cursor=0)
    if path.stat().st_size%192:raise ValueError('Unaligned M2TS preservation input')
    with path.open('r+b') as stream,mmap.mmap(stream.fileno(),0) as data:
        # Validate every patch before writing any of them.
        patches=[]
        for base in range(0,len(data),192):
            p=base+4
            if data[p]!=0x47:raise ValueError('Missing M2TS sync byte')
            pid=((data[p+1]&31)<<8)|data[p+2]
            if pid not in states:continue
            state=states[pid];control=(data[p+3]>>4)&3
            if data[p+1]&0x80 or control==0 or data[p+3]&0xc0:raise ValueError('Invalid audio transport packet')
            if not control&1:continue
            cc=data[p+3]&15
            if state['continuity'] is not None and cc!=(state['continuity']+1)&15:
                raise ValueError('Audio transport continuity changed')
            state['continuity']=cc
            start=p+4
            if control&2:start+=1+data[start]
            end=base+192
            if start>end:raise ValueError('Invalid audio adaptation field')
            if data[p+1]&0x40:
                if state['remaining']:raise ValueError('Incomplete audio PES')
                if start+9>end or data[start:start+3]!=b'\x00\x00\x01':raise ValueError('Invalid audio PES header')
                size=int.from_bytes(data[start+4:start+6],'big');header=3+data[start+8]
                if size<header or start+6+header>end:raise ValueError('Unbounded or split audio PES header')
                state['remaining']=size-header;start+=6+header
            count=min(end-start,state['remaining'])
            first=state['offset'];last=first+count
            positions=state['positions']
            while state['cursor']<len(positions) and positions[state['cursor']]<last:
                position=positions[state['cursor']]
                if position<first:raise ValueError('Missed audio restoration offset')
                expected,original=state['replacements'][position];absolute=start+position-first
                if data[absolute]!=expected:raise ValueError('Muxed audio differs before CRC restoration')
                patches.append((absolute,original));state['cursor']+=1
            state['offset']=last;state['remaining']-=count
        for state in states.values():
            if state['remaining'] or state['cursor']!=len(state['positions']):
                raise ValueError('Muxer omitted bytes required for CRC restoration')
        for position,value in patches:data[position]=value
        data.flush()
    return sum(len(changes) for changes in repairs.values())
