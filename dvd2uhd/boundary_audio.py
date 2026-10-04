"""Recover a source AC-3 frame split across unambiguous seamless DVD cells.

The completed frame belongs to the preceding cell. Shared clips are kept;
conflicting successors, format changes, stills and command boundaries are
left unresolved rather than choosing a branch's audio for another branch.
"""
import hashlib
from pathlib import Path
from .audio_frames import inventory,frame_size


def edge_fragments(path):
    """Bounded planning read; full-file verification remains the audit's job."""
    path=Path(path);size=path.stat().st_size;window=24576
    with path.open('rb') as f:
        head=f.read(window)
        info=inventory([head]);prefix=head[:info['prefix_bytes']]
        if size<=window:return prefix,head[-info['suffix_bytes']:] if info['suffix_bytes'] else b''
        f.seek(-window,2);tail=f.read(window)
    # Any valid chain of at least three complete tail frames must converge
    # on the same final fragment. Discard false sync words inside audio data.
    candidates=set();pos=0
    while True:
        pos=tail.find(b'\x0b\x77',pos)
        if pos<0:break
        try:
            info=inventory([tail[pos:]])
            if info['prefix_bytes']==0 and info['frames']>=3:
                candidates.add(tail[-info['suffix_bytes']:] if info['suffix_bytes'] else b'')
        except ValueError:pass
        pos+=1
    if len(candidates)!=1:
        # Repeated compressed payloads can themselves contain a repeating
        # false sync chain. Resolve that rare ambiguity from the real start,
        # using the full streaming parser rather than guessing an alignment.
        with path.open('rb') as f:info=inventory(iter(lambda:f.read(65536),b''))
        with path.open('rb') as f:
            prefix=f.read(info['prefix_bytes']);f.seek(-info['suffix_bytes'],2)
            suffix=f.read() if info['suffix_bytes'] else b''
        return prefix,suffix
    return prefix,next(iter(candidates))


def successors(graph,rows):
    following={}
    def clip(c):return c.get('clip',c.get('playlist',-1))
    # Require every occurrence to have a compatible seamless successor.
    for p in graph['pgcs']:
        for pos,c in enumerate(p['cells']):
            i=clip(c)
            if i<0:continue
            next_cell=p['cells'][pos+1] if pos+1<len(p['cells']) else None
            j=clip(next_cell) if next_cell is not None else -1
            eligible=(p['domain']=='title' and not p['still'] and not c['command'] and not c['still']
                      and j>=0 and bool(next_cell.get('source_flags',next_cell['flags'])&8)
                      and not next_cell['still'] and rows[i].get('audio_formats')==rows[j].get('audio_formats')
                      and rows[i]['audio_streams']==rows[j]['audio_streams'])
            following.setdefault(i,set()).add(j if eligible else -1)
    return following


def plan(graph,report,work):
    rows=report['cells'];following=successors(graph,rows);cache={};result={};unresolved=[]
    def fragments(i,ordinal):
        key=(i,ordinal)
        if key not in cache:
            path=Path(work)/f'cell-{i:05d}'/f'audio-{ordinal:02d}.ac3'
            if not path.is_file():cache[key]=None
            else:
                prefix,suffix=edge_fragments(path)
                cache[key]=(None,prefix,suffix)
        return cache[key]
    for i,next_clips in sorted(following.items()):
        for ordinal,slot in enumerate(rows[i]['audio_streams']):
            formats=rows[i].get('audio_formats',[])
            if ordinal>=len(formats) or formats[ordinal][0]!='ac3':continue
            original=fragments(i,ordinal)
            if original is None or not original[2]:continue
            if len(next_clips)!=1 or -1 in next_clips:
                unresolved.append(dict(clip=i,slot=slot,reason='ambiguous-or-nonseamless-successor'));continue
            j=next(iter(next_clips));following_audio=fragments(j,ordinal)
            if following_audio is None:continue
            suffix=original[2];prefix=following_audio[1];frame=suffix+prefix
            try:valid=bool(prefix) and len(frame)==frame_size(frame)
            except ValueError:valid=False
            if not valid:
                unresolved.append(dict(clip=i,slot=slot,next_clip=j,reason='fragments-do-not-form-one-complete-frame'));continue
            result.setdefault(i,{})[slot]=dict(next_clip=j,suffix_bytes=len(suffix),
                suffix_sha256=hashlib.sha256(suffix).hexdigest(),prefix_bytes=len(prefix),
                prefix_sha256=hashlib.sha256(prefix).hexdigest(),prefix_hex=prefix.hex(),
                frame_sha256=hashlib.sha256(frame).hexdigest(),frame_bytes=len(frame))
    return result,unresolved


def complete(path,recovery):
    """Append only the independently identified continuation to source audio."""
    path=Path(path);size=recovery['suffix_bytes'];prefix=bytes.fromhex(recovery['prefix_hex'])
    with path.open('rb') as f:f.seek(-size,2);suffix=f.read()
    if hashlib.sha256(suffix).hexdigest()!=recovery['suffix_sha256'] or hashlib.sha256(prefix).hexdigest()!=recovery['prefix_sha256']:
        raise ValueError('AC-3 boundary provenance changed')
    frame=suffix+prefix
    if len(frame)!=frame_size(frame) or hashlib.sha256(frame).hexdigest()!=recovery['frame_sha256']:
        raise ValueError('AC-3 boundary reconstruction changed')
    with path.open('ab') as f:f.write(prefix)
