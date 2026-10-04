"""Experimental authoring pipeline; source read only, output must be new."""
from __future__ import annotations
import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from .source import Disc, FormatError
from .ifo import inspect
from .pes import packets,pci,cell_blocks,pci_uops
from .spu import Assembler,decode
from . import binary,bdmv

ROOT=Path(__file__).resolve().parents[1]


def run(command,log=None,timeout=300):
    result=subprocess.run(list(map(str,command)),capture_output=True,timeout=timeout,
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
    text=(result.stdout+result.stderr).decode("utf-8","replace")
    if log:
        Path(log).write_text(text,encoding="utf-8")
    if result.returncode:
        raise RuntimeError(f"{Path(str(command[0])).name} exited {result.returncode}: {text[-3000:]}")
    return text


def discover(name,override=None):
    if override:
        p=Path(override).resolve()
        if not p.is_file():raise FileNotFoundError(p)
        return str(p)
    if name=="tsmuxer":
        for p in (ROOT.parent/"bd2hevc/tools/tsmuxer/tsMuxeR.exe",ROOT/"tools/tsMuxeR.exe"):
            if p.is_file():return str(p)
    p=shutil.which("tsMuxeR" if name=="tsmuxer" else name)
    if not p:raise RuntimeError(f"Required tool not found: {name}")
    return p


def api_jar(override=None):
    if override:return Path(override).resolve()
    for root in (Path(os.environ.get("ProgramFiles","C:/Program Files"))/"VideoLAN/VLC/plugins/access",):
        jars=sorted(root.glob("libbluray-j2se-*.jar"))
        if jars:return jars[-1]
    raise RuntimeError("Supply --bdj-api pointing to stock VLC's libbluray-j2se JAR")


def build_java(folder,api=None):
    folder=Path(folder)
    folder.mkdir(parents=True,exist_ok=True)
    sources=ROOT/"java/org/dvd2uhd"
    if not sources.is_dir():sources=Path(__file__).parent/"java/org/dvd2uhd"
    run([discover("javac"),"-source","7","-target","7","-cp",api_jar(api),"-d",folder,
         *sorted(sources.glob("*.java"))],folder/"javac.log")
    return folder


def eligibility(graph):
    issues=[]
    for p in graph["pgcs"]:
        if p["playback_mode"]:
            issues.append(f"{p['key']}: random/shuffle PGC")
        for c in p["cells"]:
            if c["block_type"]:
                issues.append(f"{p['key']} cell {c['number']}: multi-angle block")
    for t in graph["titles"]:
        if t["angles"]>1:issues.append(f"title {t['number']}: multiple angles")
        if not t.get("parts"):issues.append(f"title {t['number']}: no chapter mapping")
    return issues


def unique_cells(graph):
    cells={}
    for p in graph["pgcs"]:
        for c in p["cells"]:
            key=(p["vts"],p["domain"]!="title",c["first"],c["last"])
            if c["interleaved"]:key+=(c["vob_id"],c["cell_id"])
            cells.setdefault(key,[]).append((p,c))
    return cells


def scan_graphics(blocks):
    """Scan complete cached/extracted sectors without rewriting source media."""
    assembler=Assembler()
    menus=[]
    pictures=[]
    resets=[];uops=[]
    origin=None
    audio_ids=set()
    for block in blocks:
        for sid,body,stamp in packets(block):
            if 0xe0<=sid<=0xef and stamp is not None and origin is None:
                origin=stamp
            if 0xc0<=sid<=0xdf:audio_ids.add(sid)
            if sid==0xbf:
                restriction=pci_uops(body)
                if restriction and (not uops or restriction[1]!=uops[-1][1]):uops.append(restriction)
                m=pci(body)
                if m and (not menus or m!=menus[-1]):menus.append(m)
            if sid==0xbd:
                if body and 0x80<=body[0]<=0xaf:audio_ids.add(body[0])
                for sub,pt,raw in assembler.feed(body,stamp):
                    if pt is None:raise FormatError("SPU packet has no presentation timestamp")
                    events=[]
                    decoded=decode(raw,2**50,events)
                    pictures.extend((sub,pt,p) for p in decoded)
                    resets.extend((sub,pt+date) for date in events)
    if any(assembler.pending.values()):raise FormatError("Incomplete SPU packet at cell boundary")
    if origin is None:
        # Navigation-only cells may be valid DVD control placeholders; cannot fake video silently.
        raise FormatError("Cell has no timed video payload")
    return origin,menus,pictures,resets,uops,audio_ids


def extract_cell(disc,key,refs,folder,graphics_dir,index):
    vts,menu,first,last=key[:4]
    folder.mkdir(parents=True,exist_ok=True);digest=hashlib.sha256()
    with (folder/'source.vob').open('wb') as out:
        def copied():
            blocks=disc.sectors(vts,menu,first,last)
            if len(key)>4:blocks=cell_blocks(blocks,key[4:])
            for block in blocks:out.write(block);digest.update(block);yield block
        origin,menus,pictures,resets,uops,audio_ids=scan_graphics(copied())
    resource=f"graphics/{index:05d}.gfx"
    binary.graphics(menus,pictures,origin,graphics_dir/resource,resets,uops)
    for p,c in refs:
        c.update(playlist=index,graphics=resource,source_flags=c["flags"])
        c["flags"] &= ~4  # physical interleaving has been resolved by DSI
    return dict(origin=origin,menus=len(menus),pictures=len(pictures),spu_resets=len(resets),uop_changes=len(uops),
                forced_pictures=sum(bool(pic.forced) for _,_,pic in pictures),
                regional_colour_pictures=sum(bool(pic.colcon) for _,_,pic in pictures),
                audio_ids=sorted(audio_ids),source_sha256=digest.hexdigest())


def probe_media(path,ffprobe):
    """Keep JSON separate from decoder diagnostics on stderr."""
    result=subprocess.run([str(ffprobe),'-v','error','-show_streams','-of','json',str(path)],
                          stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=300,
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    diagnostics=result.stderr.decode('utf8','replace')
    if result.returncode:raise RuntimeError(diagnostics[:3000])
    return json.loads(result.stdout),diagnostics


def still_padding_seconds(duration_ticks,still_seconds,infinite_still,audio_end):
    """Bounded native video with a silent tail; DVD wait is enforced by the VM."""
    return max(60.0 if infinite_still else 1.0,duration_ticks/90000+min(still_seconds,60),audio_end)+(1 if infinite_still or still_seconds else 0)


def still_silent_tail_ms(probe,native_times):
    """Conservatively certify the native asset's tail contains no source audio."""
    video=next(s for s in probe['streams'] if s['codec_type']=='video')
    ends=[]
    for s in probe['streams']:
        if s['codec_type']!='audio':continue
        if 'duration' not in s or 'start_time' not in s or 'start_time' not in video:return 0
        ends.append(1000*(float(s['duration'])+float(s['start_time'])-float(video['start_time'])))
    return max(0,round((native_times[1]-native_times[0])/45-max(ends,default=0)))


def mux_cell(folder,index,output,ffmpeg,ffprobe,tsmuxer,duration_ticks=0,infinite_still=False,sequence_end=True,languages=None,still_seconds=0,subtitles=None,audio_recovery=None):
    src=folder/"source.vob"
    probe,diagnostics=probe_media(src,ffprobe)
    (folder/'source-probe-diagnostics.log').write_text(diagnostics)
    videos=[s for s in probe["streams"] if s["codec_type"]=="video"]
    if len(videos)!=1:raise FormatError("Expected one primary video stream in DVD cell")
    video=videos[0]
    if video["codec_name"] not in ("hevc","mpeg2video"):
        raise FormatError("Unsupported DVD video codec: "+video["codec_name"])
    fps=video.get("avg_frame_rate","0/0")
    if fps=="0/0":fps=video["r_frame_rate"]
    a,b=map(int,fps.split("/"))
    if a<=0 or b<=0:raise FormatError("Video has no usable frame rate")
    cmd=[ffmpeg,"-nostdin","-hide_banner","-loglevel","warning","-y","-i",src,
         "-map","0:v:0","-c","copy","-avoid_negative_ts","make_zero"]
    # PES discovery order is not stable across DVD cells. Explicit maps keep
    # the same physical slot on the same Blu-ray stream across shared clips.
    audio=sorted((s for s in probe["streams"] if s["codec_type"]=="audio"),key=lambda s:int(s['id'],0)&7)
    for s in audio:cmd.extend(['-map',f"0:{s['index']}"])
    audio_streams=[int(s['id'],0)&7 for s in audio]
    if len(set(audio_streams))!=len(audio_streams):raise FormatError('Ambiguous physical DVD audio slots')
    for i,s in enumerate(audio):
        if s["codec_name"] not in ("ac3","dts","eac3","pcm_s16le","pcm_s24le"):
            cmd.extend([f"-c:a:{i}","pcm_s16le" if s["codec_name"]=="pcm_dvd" else "ac3"])
    cmd.append(folder/"media.mkv")
    run(cmd,folder/"ffmpeg.log")
    padded=False;short_presentation_ms=0
    if video['codec_name']=='hevc':
        # DVD still cells can have only two identical compressed pictures. Their
        # audio can make the VOB large, so file size cannot identify a still.
        # BD-J EOF can tear down the decoder before a short clip is presented.
        # Repeat the same independently decodable picture, with no re-encoding.
        check=run([ffmpeg,"-nostdin","-v","error","-i",folder/"media.mkv",
                   "-map","0:v:0","-frames:v","3","-f","framemd5","-"])
        frames=[line for line in check.splitlines() if line and not line.startswith("#")]
        hashes=[line.rsplit(",",1)[1].strip() for line in frames]
        if 0<len(hashes)<=2 and len(set(hashes))==1:
            short_presentation_ms=round(len(hashes)*1000*b/a)
            audio_end=max((float(s.get('duration',0))+float(s.get('start_time',0))-float(video.get('start_time',0)) for s in audio),default=0)
            # A bounded silent video tail can loop for any DVD still wait.
            # Do not inflate a 254-second static menu into thousands of copies.
            pad_seconds=still_padding_seconds(duration_ticks,still_seconds,infinite_still,audio_end)
            padding=[ffmpeg,"-nostdin","-hide_banner","-loglevel","warning","-y",
                     "-stream_loop","-1",'-t',str(pad_seconds),
                     "-i",folder/"media.mkv"]
            # A static DVD menu may have minutes of audio and only two video
            # frames. Repeat video alone; retain audio once, in canonical order.
            if audio:padding.extend(['-i',folder/'media.mkv'])
            padding.extend(['-map','0:v:0'])
            if audio:padding.extend(['-map','1:a'])
            padding.extend(['-c','copy',folder/'still.mkv'])
            run(padding,folder/'still.log')
            os.replace(folder/"still.mkv",folder/"media.mkv");padded=True
    mux_input=folder/'media.mkv'
    if padded or sequence_end and video['codec_name']=='hevc':
        # Give tsMuxeR an explicit terminal AU/sequence delimiter. Some long
        # repeated still MKVs otherwise leave its final VCL NAL incomplete.
        mux_input=folder/('still.hevc' if padded else 'terminated.hevc')
        run([ffmpeg,'-nostdin','-v','error','-y','-i',folder/'media.mkv',
             '-map','0:v:0','-c','copy','-f','hevc',mux_input],folder/'still-annexb.log')
        with mux_input.open('ab') as f:f.write(bytes.fromhex('00000001480180'))
    tracks=run([tsmuxer,folder/'media.mkv'],folder/"tracks.log")
    found=[]
    current={}
    for line in tracks.splitlines():
        if line.startswith("Track ID:"):
            if current:found.append(current)
            current={"track":line.split(":",1)[1].strip()}
        elif ":" in line and current:
            k,v=line.split(":",1);current[k.strip()]=v.strip()
    if current:found.append(current)
    separate=mux_input.suffix=='.hevc'
    # The MKV detector can label valid AC-3 tracks "Can't detect stream type"
    # after a cell cut. Never use its partial list to pair DVD physical slots.
    # Probe all actual output tracks and feed compressed audio independently.
    mux_probe,_=probe_media(folder/'media.mkv',ffprobe)
    mux_audio=[s for s in mux_probe['streams'] if s['codec_type']=='audio']
    if len(mux_audio)!=len(audio):raise FormatError('Intermediate audio stream inventory changed')
    video_tracks=[{'Stream ID':'V_MPEGH/ISO/HEVC'}] if separate else [t for t in found if t.get('Stream ID','').startswith('V_')]
    codec_ids={'ac3':'A_AC3','eac3':'A_AC3','dts':'A_DTS','pcm_s16le':'A_LPCM','pcm_s24le':'A_LPCM'}
    audio_tracks=[]
    for stream in mux_audio:
        if stream['codec_name'] not in codec_ids:raise FormatError('Unsupported intermediate audio codec: '+stream['codec_name'])
        audio_tracks.append({'Stream ID':codec_ids[stream['codec_name']],'track':str(stream['index']+1)})
    found=video_tracks+audio_tracks
    lines=["MUXOPT --blu-ray-v3 --vbr --vbv-len=500 --no-pcr-on-video-pid"]
    audio_offsets=[]
    for stream in audio:
        if 'start_time' not in stream or 'start_time' not in video:raise FormatError('Audio/video timestamps are missing')
        audio_offsets.append(round((float(stream['start_time'])-float(video['start_time']))*1000))
    audio_track=0
    audio_containers=[]
    for t in found:
        codec=t.get("Stream ID","")
        if not codec.startswith(("V_","A_")):continue
        opts=["track="+t["track"]] if 'track' in t else []
        if codec.startswith("V_"):opts.append(f"fps={a/b:.6f}")
        else:
            opts.append(f'timeshift={audio_offsets[audio_track]}ms')
            opts.append('lang='+(languages or {}).get(audio_streams[audio_track],'und'))
        track_input=mux_input if codec.startswith('V_') else folder/'media.mkv'
        if codec in ('A_AC3','A_DTS'):
            # tsMuxeR's MKV reader can omit trailing compressed audio frames.
            # Elementary streams preserve every complete source access unit.
            track_input=folder/(f'audio-{audio_track:02d}'+('.ac3' if codec=='A_AC3' else '.dts'))
            original=audio[audio_track]
            copied=original['codec_name'] in ('ac3','eac3','dts')
            run([ffmpeg,'-nostdin','-v','error','-y','-i',src if copied else folder/'media.mkv',
                 '-map',f"0:{original['index']}" if copied else f'0:a:{audio_track}',
                 '-c','copy','-f','data',track_input],folder/f'audio-{audio_track:02d}.log')
            recovery=(audio_recovery or {}).get(str(audio_streams[audio_track]),(audio_recovery or {}).get(audio_streams[audio_track]))
            if recovery:
                if codec!='A_AC3' or original['codec_name']!='ac3':raise FormatError('Boundary recovery requires original AC-3')
                from .boundary_audio import complete
                complete(track_input,recovery)
            opts=[opt for opt in opts if not opt.startswith('track=')]
        if codec.startswith('A_'):
            audio_containers.append('elementary' if track_input.suffix in ('.ac3','.dts') else 'mkv');audio_track+=1
        lines.append(f'{codec}, "{track_input}", '+", ".join(opts))
    if len(lines)!=2+len(audio):raise FormatError("tsMuxeR omitted a source audio/video stream")
    subtitle_channels=[]
    if subtitles:
        from .subtitle_controls import clear_stream
        if len(subtitles)>32:raise FormatError('Too many logical DVD subtitle channels')
        # Tiny DVD cells can report 100 fps, including short fades. This
        # clear-only HD graphics plane has its own valid 25 fps clock;
        # original video and its timestamps stay intact.
        clear=clear_stream(25)
        for logical,language in enumerate(subtitles):
            # Separate input paths are essential: tsMuxeR coalesces duplicate
            # references to the same SUP reader into a single output stream.
            track=folder/f'subtitle-control-{logical:02d}.sup';track.write_bytes(clear)
            lines.append(f'S_HDMV/PGS, "{track}", lang={language}')
            subtitle_channels.append(dict(logical=logical,language=language))
    meta=folder/"mux.meta"
    meta.write_text("\n".join(lines)+"\n",encoding="utf-8")
    template,data=install_mux(folder,index,output,tsmuxer,[s['language'] for s in subtitle_channels])
    return template,dict(video_codec=video["codec_name"],width=video["width"],height=video["height"],
                         fps=fps,audio_count=len(audio),audio_streams=audio_streams,
                         audio_formats=[(s['codec_name'],s.get('sample_rate'),s.get('channels')) for s in audio],
                         audio_offsets_ms=audio_offsets,
                         audio_languages=[(languages or {}).get(slot,'und') for slot in audio_streams],
                         audio_containers=audio_containers,
                         audio_boundary_recovery=audio_recovery or {},
                         subtitle_controls=subtitle_channels,
                         playlist_times=bdmv.playlist_times(data),still_repeated=padded,sequence_end=separate,
                         still_silent_tail_ms=still_silent_tail_ms(probe,bdmv.playlist_times(data)) if padded else 0,
                         sequence_end_version='eos-only' if separate else None,
                         short_presentation_ms=short_presentation_ms)


def install_mux(folder,index,output,tsmuxer,subtitle_languages):
    """Mux prepared inputs and atomically replace only this physical clip."""
    folder=Path(folder);output=Path(output);meta=folder/'mux.meta'
    authored=folder/"bd"
    run([tsmuxer,meta,authored],folder/"tsmuxer.log")
    bd=authored/"BDMV"
    template=(bd/"index.bdmv").read_bytes()
    clpi=next((bd/"CLIPINF").glob("*.clpi"))
    stream=next((bd/"STREAM").glob("*.m2ts"))
    playlist=next((bd/"PLAYLIST").glob("*.mpls"))
    data=bdmv.rename_playlist(playlist.read_bytes(),index)
    native=bdmv.primary_streams(data)[0]['subtitle']
    if [s['language'] for s in native]!=subtitle_languages:
        raise FormatError('Muxer changed native subtitle channel inventory/order/languages')
    for sub,ext,original in (("CLIPINF","clpi",clpi),("STREAM","m2ts",stream)):
        target=output/"BDMV"/sub/f"{index:05d}.{ext}"
        target.parent.mkdir(parents=True,exist_ok=True)
        # STREAM may be shared by hard link with a disposable test clone.
        # Replace atomically so remuxing cannot overwrite another disc's media.
        temporary=target.with_suffix(target.suffix+'.tmp')
        shutil.copyfile(original,temporary);os.replace(temporary,target)
        if sub=="CLIPINF":
            backup=output/"BDMV/BACKUP"/sub/target.name
            backup.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(target,backup)
    for prefix in ("", "BACKUP/"):
        target=output/"BDMV"/(prefix+"PLAYLIST")/f"{index:05d}.mpls"
        target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
    return template,data


def author(source,output,*,tsmuxer=None,ffmpeg=None,ffprobe=None,bdj_api=None,max_cells=None,menus_only=False,progress=None):
    source,output=Path(source).resolve(),Path(output).resolve()
    if output.exists():raise FileExistsError(f"Output must be new: {output}")
    if output==source or source.is_dir() and source in output.parents:
        raise ValueError("Output cannot be inside the source DVD")
    tools=dict(tsmuxer=discover("tsmuxer",tsmuxer),ffmpeg=discover("ffmpeg",ffmpeg),ffprobe=discover("ffprobe",ffprobe))
    classes=build_java(output.parent/(output.name+".work")/"classes",bdj_api)
    work=classes.parent
    resources=work/"resources"
    report=dict(schema="dvd2uhd-author-v1",source=str(source),output=str(output),complete=False,
                experimental=True,tools=tools,cells=[],limitations=[
                    "Compatible DVD seamless cells use condition 5; full-movie gapless playback is not certified.",
                    "Original subtitle artwork is BD-J graphics; native PGS channels carry selection/clear events only.",
                    "Source HEVC is kept at DVD resolution; this is not UHD hardware conformance certification.",
                    "DVD UOP masks cover BD-J keys and native audio/subtitle changes; native seeking and random/angle playback remain incomplete."],
                runtime_model_version=6,graphics_version=4)
    with Disc(source) as disc:
        graph=inspect(disc)
        issues=eligibility(graph)
        if issues:raise FormatError("Unsupported DVD structures: "+"; ".join(issues[:20]))
        # Validate every command with the actual Java VM, before copying large media.
        binary.model(graph,work/"disc-scan.bin")
        report["command_validation"]=run([discover("java"),"-cp",classes,"org.dvd2uhd.Trace","validate",work/"disc-scan.bin"]).strip()
        output.mkdir()
        (output/"conversion-report.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
        template=None
        selected=list(unique_cells(graph).items())
        if menus_only:selected=[(k,v) for k,v in selected if k[1]]
        if max_cells is not None:selected=selected[:max_cells]
        if len(selected)>2000:raise FormatError("BD-J playlist range exceeds 2000")
        try:
            for index,(key,refs) in enumerate(selected):
                print(f"Cell {index+1}/{len(selected)}: {key}",flush=True)
                folder=work/f"cell-{index:05d}"
                details=extract_cell(disc,key,refs,folder,resources,index)
                from .languages import audio_languages
                from .subtitle_controls import languages as subtitle_languages
                template,media=mux_cell(folder,index,output,languages=audio_languages(graph,refs),subtitles=subtitle_languages(graph,refs),duration_ticks=max(c["duration"] for p,c in refs),
                                       infinite_still=any(c['still']==255 or c['number']==len(p['cells']) and p['still']==255 for p,c in refs),
                                       still_seconds=max((c['still'] if c['still']<255 else 0)+(p['still'] if c['number']==len(p['cells']) and p['still']<255 else 0) for p,c in refs),**tools)
                ordinals=media['audio_streams']
                if set(ordinals)!={sid&7 for sid in details["audio_ids"]}:
                    raise FormatError("Audio packet identities do not match authored tracks")
                for p,c in refs:
                    c["audio_streams"]=ordinals;c['still_repeated']=media['still_repeated']
                    c['still_keepalive']=media['still_repeated'] and media['still_silent_tail_ms']>=750
                    c['native_subtitle_controls']=bool(media['subtitle_controls'])
                    c['presentation_ms']=c['duration']//90 or media['short_presentation_ms']
                details.update(media);details.update(index=index,domain=key,pgcs=[p["key"] for p,c in refs])
                report["cells"].append(details)
                (output/"conversion-report.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
                if progress:progress(index+1,len(selected))
            if template is None:raise FormatError("No media cells selected")
            if not menus_only and max_cells is None:
                from .boundary_audio import plan
                repairs,unresolved=plan(graph,report,work)
                report['audio_boundary_unresolved']=unresolved
                for index,recovery in repairs.items():
                    print(f'Recover AC-3 boundary in clip {index}',flush=True)
                    row=report['cells'][index];refs=selected[index][1]
                    template,media=mux_cell(work/f'cell-{index:05d}',index,output,
                        languages=audio_languages(graph,refs),subtitles=subtitle_languages(graph,refs),
                        audio_recovery=recovery,duration_ticks=max(c['duration'] for p,c in refs),**tools)
                    row.update(media)
                    for p,c in refs:
                        c['still_keepalive']=media['still_repeated'] and media['still_silent_tail_ms']>=750
            bdmv.join_titles(output,graph,report)
            binary.model(graph,resources/"disc.bin")
            (output/"navigation.json").write_text(json.dumps(graph,indent=2),encoding="utf-8")
            bdmv.bootstrap(output,template)
            jar=output/"BDMV/JAR/00000.jar"
            jar.parent.mkdir(parents=True,exist_ok=True)
            with zipfile.ZipFile(jar,"w",zipfile.ZIP_DEFLATED) as z:
                for p in sorted(classes.rglob("*.class")):z.write(p,p.relative_to(classes).as_posix())
                for p in sorted(resources.rglob("*")):
                    if p.is_file():z.write(p,p.relative_to(resources).as_posix())
            report['graphics_validation']=run([discover('java'),'-Xmx64m','-cp',classes,
                                               'org.dvd2uhd.Trace','graphics',jar],work/'graphics-validation.log').strip()
            report["complete"]=not menus_only and max_cells is None
            report["status"]="authored-experimental" if report["complete"] else "partial-debug-disc"
        except BaseException as e:
            report["status"]="failed";report["error"]=str(e)
            raise
        finally:
            (output/"conversion-report.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    return report
