"""Repair BD-J/transport metadata without changing pictures, audio or allocation."""
import copy
import hashlib
import io
import os
from pathlib import Path
import shutil
import zipfile


class RuntimeAllocationError(ValueError):
    pass


def repaired_jar(original, replacements):
    """Produce an equal-length unsigned JAR; retain every other entry exactly."""
    source=zipfile.ZipFile(io.BytesIO(original))
    names=source.namelist()
    if len(set(names))!=len(names):raise ValueError('Duplicate runtime JAR entries')
    if any(name.upper().endswith(('.SF','.RSA','.DSA','.EC')) for name in names):
        raise ValueError('Signed runtime JARs require an explicit signing workflow')
    if not replacements or not set(replacements).issubset(names):
        raise ValueError('Runtime replacements must name existing entries')
    if any(not name.endswith('.class') or not data.startswith(b'\xca\xfe\xba\xbe') for name,data in replacements.items()):
        raise ValueError('Runtime repair accepts compiled Java classes only')
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as target:
        for info in source.infolist():
            target.writestr(copy.copy(info),replacements.get(info.filename,source.read(info)),compresslevel=9)
    padding=len(original)-len(stream.getvalue())
    if not 0<=padding<=65535:
        raise RuntimeAllocationError('Repaired runtime does not fit the existing JAR allocation')
    with zipfile.ZipFile(stream,'a') as target:target.comment=b' '*padding
    result=stream.getvalue();check=zipfile.ZipFile(io.BytesIO(result))
    if len(result)!=len(original) or check.testzip() is not None or check.namelist()!=names:
        raise ValueError('Repaired runtime JAR failed inventory/CRC verification')
    for name in names:
        if check.read(name)!=replacements.get(name,source.read(name)):
            raise ValueError('Repaired runtime changed an unrelated resource')
    return result,dict(changed_entries=sorted(replacements),jar_bytes=len(result),padding_bytes=padding,
                      original_jar_sha256=hashlib.sha256(original).hexdigest(),
                      repaired_jar_sha256=hashlib.sha256(result).hexdigest())


def _identity(path):
    value=Path(path).stat()
    return value.st_size,value.st_mtime_ns,value.st_ino


def patch_image(source,candidate,replacements,progress=None,*,native_still_playlists=(),audio_presentations=None,terminal_delimiter_clips=(),vlc_clipinfo=False):
    """Copy once and patch the runtime plus optional native-still metadata.

    A masked SHA-256 from the original read must match an independent candidate
    read with just the changed resource spans masked. This checks all other bytes without
    a second full HDD read. The original image is never written.
    """
    from .udf_reader import UdfImage
    from .udf_validation import validate_udf
    source,candidate=(Path(p).resolve() for p in (source,candidate))
    if source==candidate or candidate.exists():raise FileExistsError('Runtime repair requires a new candidate image')
    original_identity=_identity(source);udf=UdfImage(source)
    changes={};record=dict(changed_entries=[])
    if replacements:
        entry=udf.files['BDMV/JAR/00000.jar']
        if entry['size']>256*1024**2:raise ValueError('Runtime JAR exceeds the bounded repair limit')
        original=udf.read('BDMV/JAR/00000.jar',limit=256*1024**2)
        jar,record=repaired_jar(original,replacements)
        changes['BDMV/JAR/00000.jar']=jar
    elif not vlc_clipinfo:
        raise ValueError('Runtime repair requires class replacements or a clip-info repair')
    clipinfo_changes=[]
    if vlc_clipinfo:
        from .bdmv import vlc_clipinfo as compatible_clipinfo
        import re
        for name in sorted(udf.files):
            if not re.fullmatch(r'BDMV/(?:BACKUP/)?CLIPINF/[0-9]{5}\.clpi',name):continue
            old=udf.read(name,limit=4*1024**2);new=compatible_clipinfo(old)
            if old!=new:changes[name]=new;clipinfo_changes.append(name)
        if not changes:raise ValueError('No clip-info repair is needed')
    from .bdmv import native_still_playlist
    for playlist in native_still_playlists:
        if type(playlist)!=int or not 0<=playlist<2000:raise ValueError('Invalid native still playlist ID')
        for prefix in ('','BACKUP/'):
            name='BDMV/'+prefix+f'PLAYLIST/{playlist:05d}.mpls'
            if udf.files[name]['size']>4*1024**2:raise ValueError('Still playlist exceeds bounded metadata size')
            old=udf.read(name,limit=4*1024**2);new=native_still_playlist(old)
            if old!=new:changes[name]=new
    from .bdmv import audio_presentation_playlist
    for playlist,millis in (audio_presentations or {}).items():
        if type(playlist)!=int or not 0<=playlist<2000:raise ValueError('Invalid audio presentation playlist ID')
        for prefix in ('','BACKUP/'):
            name='BDMV/'+prefix+f'PLAYLIST/{playlist:05d}.mpls'
            if udf.files[name]['size']>4*1024**2:raise ValueError('Audio presentation playlist exceeds metadata limit')
            old=changes.get(name) or udf.read(name,limit=4*1024**2)
            new=audio_presentation_playlist(old,millis)
            if old!=new:changes[name]=new
    spans=[]
    for name,data in changes.items():
        remaining=len(data);consumed=0;entry=udf.files[name]
        if len(data)!=entry['size']:raise ValueError('Runtime repair cannot change resource allocation')
        for offset,length in entry['extents']:
            take=min(length,remaining)
            if take:spans.append((offset,offset+take,consumed,data));consumed+=take;remaining-=take
        if remaining:raise ValueError('Runtime resource allocation is incomplete')
    transport={}
    from .transport import terminal_delimiter_edits,MAX_SHORT_CLIP
    for clip in sorted(set(terminal_delimiter_clips)):
        if type(clip)!=int or not 0<=clip<2000:raise ValueError('Invalid terminal delimiter clip ID')
        name=f'BDMV/STREAM/{clip:05d}.m2ts';entry=udf.files[name]
        if entry['size']>MAX_SHORT_CLIP:raise ValueError('Short transport exceeds bounded repair limit')
        edits=terminal_delimiter_edits(udf.read(name,limit=MAX_SHORT_CLIP))
        if not edits:continue
        transport[name]=[offset for offset,_ in edits]
        for begin,data in edits:
            logical=0;consumed=0
            for offset,length in entry['extents']:
                length=min(length,entry['size']-logical)
                lo=max(begin,logical);hi=min(begin+len(data),logical+length)
                if hi>lo:
                    spans.append((offset+lo-logical,offset+hi-logical,lo-begin,data));consumed+=hi-lo
                logical+=length
            if consumed!=len(data):raise ValueError('Transport packet allocation is incomplete')
    if any(lo<0 or hi>original_identity[0] for lo,hi,_,_ in spans):
        raise ValueError('Runtime resource allocation is outside the image')
    ordered=sorted(spans)
    if any(a[1]>b[0] for a,b in zip(ordered,ordered[1:])):raise ValueError('Overlapping runtime JAR allocations')
    for name,other in udf.files.items():
        if name in changes or name in transport:continue
        remaining=other['size']
        for offset,length in other['extents']:
            count=min(length,remaining);remaining-=count
            if count and any(max(offset,lo)<min(offset+count,hi) for lo,hi,_,_ in spans):
                raise ValueError('Runtime JAR allocation aliases another payload file')
    candidate.parent.mkdir(parents=True,exist_ok=True)
    if shutil.disk_usage(candidate.parent).free<original_identity[0]+64*1024**2:
        raise OSError(28,'Insufficient workspace for runtime repair')
    def masked(block,at):
        result=bytearray(block)
        for lo,hi,_,_ in spans:
            begin=max(lo,at)-at;end=min(hi,at+len(block))-at
            if begin<end:result[begin:end]=b'\0'*(end-begin)
        return result
    original_hash=hashlib.sha256();expected=hashlib.sha256();at=0
    with source.open('rb') as reader,candidate.open('xb') as writer:
        while block:=reader.read(8*1024**2):
            original_hash.update(block);expected.update(masked(block,at));patched=bytearray(block)
            for lo,hi,resource_at,data in spans:
                begin=max(lo,at);end=min(hi,at+len(block))
                if begin<end:patched[begin-at:end-at]=data[resource_at+begin-lo:resource_at+end-lo]
            writer.write(patched);at+=len(block)
            if progress:progress('uhd-runtime-copy',at,original_identity[0])
        writer.flush();os.fsync(writer.fileno())
    if at!=original_identity[0] or _identity(source)!=original_identity:
        raise ValueError('Original image changed during runtime repair')
    candidate_identity=_identity(candidate)
    actual=hashlib.sha256();final_hash=hashlib.sha256();at=0
    with candidate.open('rb') as reader:
        while block:=reader.read(8*1024**2):
            actual.update(masked(block,at));final_hash.update(block);at+=len(block)
            if progress:progress('uhd-runtime-verify',at,original_identity[0])
    if at!=original_identity[0] or actual.digest()!=expected.digest():
        raise ValueError('Runtime candidate changed bytes outside the allowed resource allocations')
    changed=UdfImage(candidate)
    if changed.files!=udf.files or any(changed.read(name,limit=len(data))!=data for name,data in changes.items()):
        raise ValueError('Runtime repair changed UDF allocation or resource bytes')
    with candidate.open('rb') as reader:
        for lo,hi,resource_at,data in spans:
            reader.seek(lo)
            if reader.read(hi-lo)!=data[resource_at:resource_at+hi-lo]:
                raise ValueError('Runtime repair changed expected resource/transport bytes')
    descriptors=validate_udf(candidate)
    # Independent per-file hashes support ordinary ISO resume verification.
    rows=[]
    with candidate.open('rb') as reader:
        for name,file_entry in sorted(changed.files.items()):
            digest=hashlib.sha256();remaining=file_entry['size']
            for offset,length in file_entry['extents']:
                reader.seek(offset);count=min(length,remaining)
                while count:
                    block=reader.read(min(count,8*1024**2))
                    if not block:raise ValueError('Truncated runtime candidate payload')
                    digest.update(block);count-=len(block);remaining-=len(block)
            if remaining:raise ValueError('Incomplete runtime candidate payload')
            rows.append(dict(path=name,size=file_entry['size'],sha256=digest.hexdigest()))
    if _identity(source)!=original_identity:raise ValueError('Original image changed during verification')
    if _identity(candidate)!=candidate_identity:raise ValueError('Runtime candidate changed during verification')
    jar_only=set(changes)=={'BDMV/JAR/00000.jar'} and not transport
    record.update(all_non_jar_bytes_identical=jar_only,all_unmodified_bytes_identical=True,
                  media_bytes_identical=not transport,compressed_media_identical=True,transport_delimiter_packets=transport,
                  changed_files=sorted(set(changes)|set(transport)),
                  native_still_playlists=sorted(set(native_still_playlists)),unchanged_bytes_sha256=actual.hexdigest(),
                  vlc_clipinfo=clipinfo_changes,
                  audio_presentations=audio_presentations or {},
                  original_image_sha256=original_hash.hexdigest(),image_sha256=final_hash.hexdigest(),
                  original_identity=list(original_identity),independent_descriptors=descriptors,
                  manifest=dict(image=str(candidate),verified=True,bytes=at,files=rows))
    if jar_only:record['non_jar_sha256']=actual.hexdigest()
    return record
