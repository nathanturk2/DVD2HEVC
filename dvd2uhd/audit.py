# SPDX-License-Identifier: GPL-3.0-or-later
"""Original navigation, clip reuse, track maps and compressed-picture audit."""
import hashlib,json,os,re,subprocess,tempfile,zipfile,itertools
from pathlib import Path
from .author import build_java,discover,run,unique_cells,probe_media
from .ifo import inspect
from .source import Disc
from .iso import sha256
from .bdmv import primary_streams
from .subtitle_controls import languages as subtitle_languages


def picture_payload_matches(original, authored, repeated=False):
    """Repetition must retain every original picture before adding still padding."""
    if not original or len(authored) < len(original):
        return False
    if not repeated:
        return authored == original
    return all(value == original[n % len(original)] for n, value in enumerate(authored))


def vcl_hashes(path,ffmpeg):
    """Stream Annex-B NALs; ignore muxer-added AUD/parameter sets, hash VCL bytes."""
    with tempfile.TemporaryFile() as errors:
        p=subprocess.Popen([str(ffmpeg),'-nostdin','-v','error','-i',str(path),'-map','0:v:0',
                            '-c:v','copy','-f','hevc','-'],stdout=subprocess.PIPE,stderr=errors,
                           creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        pending=b'';hashes=[];start=re.compile(b'\x00\x00(?:\x00)?\x01')
        def add(nal):
            nal=nal.rstrip(b'\0')
            if len(nal)>=2 and ((nal[0]>>1)&63)<=31:hashes.append(hashlib.sha256(nal).hexdigest())
        while chunk:=p.stdout.read(1024*1024):
            pending+=chunk;matches=list(start.finditer(pending))
            if len(matches)>1:
                for a,b in zip(matches,matches[1:]):add(pending[a.end():b.start()])
                pending=pending[matches[-1].start():]
            if len(pending)>32*1024*1024:
                p.kill();p.wait();raise ValueError('Video NAL exceeds bounded audit buffer')
        matches=list(start.finditer(pending))
        if matches:add(pending[matches[0].end():])
        if p.wait():
            errors.seek(0);raise RuntimeError(errors.read(3000).decode('utf-8','replace'))
    if not hashes:raise ValueError('No HEVC picture NALs found')
    return hashes


def audit(root,*,media=False,output=None,progress=None,bdj_api=None):
    root=Path(root).resolve();r=json.loads((root/'conversion-report.json').read_text())
    if r.get('schema')!='dvd2uhd-author-v1':raise ValueError('Expected DVD2UHD authored folder')
    g=json.loads((root/'navigation.json').read_text());work=root.parent/(root.name+'.work')
    with Disc(r['source']) as source:original=inspect(source)
    if original['titles']!=g['titles'] or original['domains']!=g['domains']:
        raise ValueError('Original title/chapter or domain mappings changed')
    actual={p['key']:p for p in g['pgcs']}
    expected_keys={p['key'] for p in original['pgcs']}
    if set(actual)!=expected_keys:raise ValueError('PGC inventory changed')
    cell_refs={};missing=[];native_playlists={}
    for p in original['pgcs']:
        q=actual[p['key']]
        for name in ('pre','post','cell_commands','palette','audio','subpicture','programs','entry','next','prev','up','still','playback_mode','uops'):
            if p[name]!=q[name]:raise ValueError(p['key']+': changed '+name)
        if len(p['cells'])!=len(q['cells']):raise ValueError('Cell count changed')
        for c,d in zip(p['cells'],q['cells']):
            for name in ('number','first','last','vob_id','cell_id','command','still','duration','interleaved','block_type','block_mode'):
                if c[name]!=d[name]:raise ValueError(p['key']+': changed cell '+name)
            if d.get('source_flags',d['flags'])!=c['flags']:raise ValueError('Cell source flags changed')
            clip=d.get('clip',d.get('playlist',-1))
            if clip<0:missing.append(p['key']+':'+str(c['number']));continue
            key=(p['vts'],p['domain']!='title',c['first'],c['last'])
            if c['interleaved']:key+=(c['vob_id'],c['cell_id'])
            if list(key)!=r['cells'][clip]['domain']:raise ValueError('Physical cell mapping changed')
            if key in cell_refs and cell_refs[key]!=clip:raise ValueError('Shared physical cell was duplicated')
            cell_refs[key]=clip
            for sub,ext in (('STREAM','m2ts'),('CLIPINF','clpi')):
                if not (root/'BDMV'/sub/f'{clip:05d}.{ext}').is_file():raise ValueError('Missing authored clip')
            if not (root/'BDMV/PLAYLIST'/f"{d['playlist']:05d}.mpls").is_file():raise ValueError('Missing logical playlist')
            row=r['cells'][clip]
            if 'subtitle_controls' in row:
                expected=[dict(logical=n,language=lang) for n,lang in enumerate(subtitle_languages(original,[(p,c)]))]
                if row['subtitle_controls']!=expected:raise ValueError('Native subtitle logical slots/languages changed')
                if bool(d.get('native_subtitle_controls'))!=bool(expected):raise ValueError('Native subtitle runtime flag disagrees with media')
                playlist=d['playlist']
                if playlist not in native_playlists:
                    native_playlists[playlist]=primary_streams((root/'BDMV/PLAYLIST'/f'{playlist:05d}.mpls').read_bytes())
                tables=native_playlists[playlist]
                item=0
                if len(tables)>1:
                    join=next(j for j in r['joined_title_playlists'] if j['playlist']==playlist)
                    item=join['cells'].index(c['number'])
                if [s['language'] for s in tables[item]['subtitle']]!=[s['language'] for s in expected]:
                    raise ValueError('MPLS native subtitle inventory/order/languages changed')
    if r['complete'] and missing:raise ValueError('Complete report has omitted cells')
    joined_count=0
    for j in r.get('joined_title_playlists',[]):
        q=actual[j['pgc']];data=(root/'BDMV/PLAYLIST'/f"{j['playlist']:05d}.mpls").read_bytes()
        numbers=j.get('cells',[c['number'] for c in q['cells']])
        if numbers!=list(range(numbers[0],numbers[-1]+1)):raise ValueError('Joined cells are not contiguous')
        cells=[q['cells'][n-1] for n in numbers]
        if any(c['still'] or c['command'] for c in cells[:-1]) or cells[-1]['still']:
            raise ValueError('Joined playlist skips an original still or cell-end command')
        if any(c['playlist']!=j['playlist'] for c in cells):raise ValueError('Joined playlist mapping changed')
        chapters=[n for n in q['programs'] if numbers[0]<=n<=numbers[-1]]
        if chapters!=j['chapter_cells']:raise ValueError('Joined original chapter inventory changed')
        start=int.from_bytes(data[8:12],'big');mark=int.from_bytes(data[12:16],'big')
        n=int.from_bytes(data[start+6:start+8],'big');pos=start+10;ids=[];connections=[];periods=[]
        for _ in range(n):
            ids.append(int(data[pos+2:pos+7]));connections.append(data[pos+12]&15)
            periods.append(int.from_bytes(data[pos+18:pos+22],'big')-int.from_bytes(data[pos+14:pos+18],'big'))
            pos+=2+int.from_bytes(data[pos:pos+2],'big')
        if ids!=j['clips'] or ids!=[c['clip'] for c in cells]:raise ValueError('Joined playlist changed original clip order')
        if connections!=j.get('connections',[1]*len(ids)):raise ValueError('Joined connection conditions changed')
        if periods!=[round(c['duration']/2) for c in cells]:raise ValueError('Joined playlist changed original DVD presentation timing')
        n=int.from_bytes(data[mark+4:mark+6],'big')
        if n!=len(chapters):raise ValueError('Joined chapter count changed')
        for k,cell in enumerate(chapters):
            pos=mark+6+14*k;ref=int.from_bytes(data[pos+2:pos+4],'big')
            stamp=int.from_bytes(data[pos+4:pos+8],'big')
            if ref!=cell-numbers[0] or stamp!=r['cells'][ids[ref]]['playlist_times'][0]:
                raise ValueError('Joined chapter position changed')
        joined_count+=1
    classes=build_java(work/'audit-classes',bdj_api)
    graphics=run([discover('java'),'-Xmx64m','-cp',classes,'org.dvd2uhd.Trace','graphics',root/'BDMV/JAR/00000.jar']).strip()
    result=dict(schema='dvd2uhd-audit-v1',navigation_unchanged=True,physical_reuse_preserved=True,
                pgcs=len(g['pgcs']),titles=len(g['titles']),physical_cells=len(cell_refs),missing=missing,
                graphics=graphics,media_scope='not-requested',media=[],playback_certified=False)
    result.update(joined_playlists_verified=joined_count,navigation_sha256=sha256(root/'navigation.json'),
                  xlet_sha256=sha256(root/'BDMV/JAR/00000.jar'),
                  native_subtitle_playlists_verified=len(native_playlists))
    if media:
        from .boundary_audio import successors
        allowed_successors=successors(g,r['cells'])
        result['media_scope']='all-authored-cells-compressed-hevc-audio-payloads-slot-order-and-av-start-offsets'
        for row in r['cells']:
            i=row['index'];src=work/f'cell-{i:05d}/source.vob';dst=root/f'BDMV/STREAM/{i:05d}.m2ts'
            print(f'Audit cell {i+1}/{len(r["cells"])}',flush=True)
            if progress:progress(i,len(r['cells']))
            if sha256(src)!=row['source_sha256']:raise ValueError('Cached original cell changed')
            probe,source_diagnostics=probe_media(src,r['tools']['ffprobe'])
            source_audio=sorted((s for s in probe['streams'] if s['codec_type']=='audio'),key=lambda s:int(s['id'],0)&7)
            audio=[int(s['id'],0)&7 for s in source_audio]
            refs=[c for p in g['pgcs'] for c in p['cells'] if c.get('clip',c.get('playlist'))==i]
            if any(c.get('audio_streams')!=row['audio_streams'] for c in refs) or set(audio)!=set(row['audio_streams']):raise ValueError('Audio slot identity/order changed')
            if row['video_codec']!='hevc':raise ValueError('MPEG-2 compressed-picture audit is not implemented')
            a=vcl_hashes(src,r['tools']['ffmpeg']);b=vcl_hashes(dst,r['tools']['ffmpeg'])
            equal=picture_payload_matches(a,b,bool(row.get('still_repeated')))
            if not equal:raise ValueError(f'HEVC picture payload changed in clip {i}')
            dst_probe,output_diagnostics=probe_media(dst,r['tools']['ffprobe'])
            dst_audio=[s for s in dst_probe['streams'] if s['codec_type']=='audio']
            if len(dst_audio)!=len(audio):raise ValueError('Authored audio track count changed')
            native=dst_probe['streams']
            if 'subtitle_controls' in row and len([s for s in native if s['codec_name']=='hdmv_pgs_subtitle'])!=len(row['subtitle_controls']):
                raise ValueError('M2TS native subtitle stream count changed')
            checked_audio=[]
            source_video=next(s for s in probe['streams'] if s['codec_type']=='video')
            dst_video=next(s for s in dst_probe['streams'] if s['codec_type']=='video')
            for s in source_audio:
                slot=int(s['id'],0)&7;position=row['audio_streams'].index(slot);d=dst_audio[position]
                if s['codec_name']!=d['codec_name']:
                    checked_audio.append(dict(slot=slot,scope='converted-format-not-payload-certified'));continue
                if s['codec_name'] not in ('ac3','eac3','dts'):
                    checked_audio.append(dict(slot=slot,scope='payload-audit-not-implemented'));continue
                source_hash=audio_hash(src,s['index'],r['tools']['ffmpeg'])
                output_hash=audio_hash(dst,d['index'],r['tools']['ffmpeg'])
                fragments=None
                recovery=row.get('audio_boundary_recovery',{}).get(str(slot))
                if recovery:
                    if allowed_successors.get(i)!={recovery['next_clip']}:raise ValueError('Audio repair crosses an ambiguous/nonseamless DVD branch')
                    if s['codec_name']!='ac3':raise ValueError('Boundary recovery changed original audio format')
                    next_src=work/f"cell-{recovery['next_clip']:05d}/source.vob"
                    next_probe,_=probe_media(next_src,r['tools']['ffprobe'])
                    next_audio=next(t for t in next_probe['streams'] if t['codec_type']=='audio' and int(t['id'],0)&7==slot)
                    prefix=audio_prefix(next_src,next_audio['index'],r['tools']['ffmpeg'],recovery['prefix_bytes'])
                    if hashlib.sha256(prefix).hexdigest()!=recovery['prefix_sha256'] or prefix.hex()!=recovery['prefix_hex']:
                        raise ValueError('Recovered audio is not the original successor prefix')
                    original_frames=audio_inventory(src,s['index'],r['tools']['ffmpeg'])
                    if original_frames['suffix_bytes']!=recovery['suffix_bytes'] or original_frames['suffix_sha256']!=recovery['suffix_sha256']:
                        raise ValueError('Recovered audio is not the original predecessor suffix')
                    expected_frames=audio_inventory(src,s['index'],r['tools']['ffmpeg'],prefix)
                    authored_frames=audio_inventory(dst,d['index'],r['tools']['ffmpeg'])
                    if expected_frames['frames']!=original_frames['frames']+1 or any(expected_frames[k]!=authored_frames[k] for k in ('frames_sha256','frames','frame_bytes')):
                        raise ValueError('Reconstructed complete audio access units changed')
                    fragments=dict(source=original_frames,expected_with_successor_prefix=expected_frames,authored=authored_frames,recovery=recovery)
                elif source_hash!=output_hash:
                    if s['codec_name'] in ('ac3','eac3'):
                        original_frames=audio_inventory(src,s['index'],r['tools']['ffmpeg'])
                        authored_frames=audio_inventory(dst,d['index'],r['tools']['ffmpeg'])
                        if any(original_frames[k]!=authored_frames[k] for k in ('frames_sha256','frames','frame_bytes')):
                            raise ValueError(f'Complete audio access units changed in clip {i} slot {slot}')
                        fragments=dict(source=original_frames,authored=authored_frames)
                    else:raise ValueError(f'Compressed audio payload changed in clip {i} slot {slot}')
                source_offset=(float(s['start_time'])-float(source_video['start_time']))*1000
                output_offset=(float(d['start_time'])-float(dst_video['start_time']))*1000
                if abs(source_offset-output_offset)>1:raise ValueError(f'A/V start offset changed in clip {i} slot {slot}: {source_offset} -> {output_offset} ms')
                evidence=dict(slot=slot,scope='all-compressed-payload-and-av-start-offset' if fragments is None else 'complete-access-units-and-av-start-offset',sha256=source_hash,
                              original_offset_ms=source_offset,authored_offset_ms=output_offset)
                if fragments is not None:evidence['boundary_fragments']=fragments
                if recovery:evidence['scope']='complete-access-units-with-source-boundary-reconstruction-and-av-start-offset'
                checked_audio.append(evidence)
            result['media'].append(dict(clip=i,original_picture_nals=len(a),authored_picture_nals=len(b),
                                       still_repeated=bool(row.get('still_repeated')),audio_streams=audio,
                                       audio_payloads=checked_audio,probe_diagnostics=dict(source=source_diagnostics,authored=output_diagnostics),
                                       vcl_sha256=hashlib.sha256(''.join(a).encode()).hexdigest()))
    target=Path(output) if output else root/'fidelity-audit.json'
    target.write_text(json.dumps(result,indent=2),encoding='utf-8')
    return result


def audio_hash(path,stream,ffmpeg):
    """Hash elementary audio bytes independently of PES/TS packet boundaries."""
    with tempfile.TemporaryFile() as errors:
        child=subprocess.Popen([str(ffmpeg),'-nostdin','-v','error','-i',str(path),'-map',f'0:{stream}',
                                '-c','copy','-f','data','-'],stdout=subprocess.PIPE,stderr=errors,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        digest=hashlib.sha256();size=0
        try:
            while chunk:=child.stdout.read(1024*1024):digest.update(chunk);size+=len(chunk)
            if child.wait(timeout=60):
                errors.seek(0);raise RuntimeError(errors.read(3000).decode('utf8','replace'))
        finally:
            child.stdout.close()
            if child.poll() is None:child.kill();child.wait()
        if not size:raise ValueError('No audio payload')
        return digest.hexdigest()


def audio_inventory(path,stream,ffmpeg,continuation=b''):
    from .audio_frames import inventory
    with tempfile.TemporaryFile() as errors:
        child=subprocess.Popen([str(ffmpeg),'-nostdin','-v','error','-i',str(path),'-map',f'0:{stream}',
                                '-c','copy','-f','data','-'],stdout=subprocess.PIPE,stderr=errors,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        try:
            result=inventory(itertools.chain(iter(lambda:child.stdout.read(65536),b''),[continuation]))
            if child.wait(timeout=60):raise RuntimeError('Audio inventory demux failed')
            return result
        finally:
            child.stdout.close()
            if child.poll() is None:child.kill();child.wait()


def audio_prefix(path,stream,ffmpeg,size):
    if not 0<size<=3840:raise ValueError('Unbounded original audio continuation')
    with tempfile.TemporaryFile() as errors:
        child=subprocess.Popen([str(ffmpeg),'-nostdin','-v','error','-i',str(path),'-map',f'0:{stream}',
                                '-c','copy','-f','data','-'],stdout=subprocess.PIPE,stderr=errors,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        try:
            prefix=child.stdout.read(size)
            if len(prefix)!=size:raise ValueError('Truncated original audio continuation')
            return prefix
        finally:
            child.kill();child.wait();child.stdout.close()
