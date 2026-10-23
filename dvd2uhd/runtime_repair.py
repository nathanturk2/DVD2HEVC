"""Replace BD-J classes without changing any media or UDF allocation."""
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


def patch_image(source,candidate,replacements,progress=None):
    """Copy once, patch only allocated JAR bytes, then verify from the SSD copy.

    A masked SHA-256 from the original read must match an independent candidate
    read with just the JAR spans masked. This checks every non-JAR byte without
    a second full HDD read. The original image is never written.
    """
    from .udf_reader import UdfImage
    from .udf_validation import validate_udf
    source,candidate=(Path(p).resolve() for p in (source,candidate))
    if source==candidate or candidate.exists():raise FileExistsError('Runtime repair requires a new candidate image')
    original_identity=_identity(source);udf=UdfImage(source)
    entry=udf.files['BDMV/JAR/00000.jar']
    if entry['size']>256*1024**2:raise ValueError('Runtime JAR exceeds the bounded repair limit')
    original=udf.read('BDMV/JAR/00000.jar',limit=256*1024**2)
    jar,record=repaired_jar(original,replacements)
    spans=[];remaining=len(jar);consumed=0
    for offset,length in entry['extents']:
        take=min(length,remaining)
        if take:spans.append((offset,offset+take,consumed));consumed+=take;remaining-=take
    if remaining or any(lo<0 or hi>original_identity[0] for lo,hi,_ in spans):
        raise ValueError('Runtime JAR allocation is outside the image')
    ordered=sorted(spans)
    if any(a[1]>b[0] for a,b in zip(ordered,ordered[1:])):raise ValueError('Overlapping runtime JAR allocations')
    for name,other in udf.files.items():
        if name=='BDMV/JAR/00000.jar':continue
        remaining=other['size']
        for offset,length in other['extents']:
            count=min(length,remaining);remaining-=count
            if count and any(max(offset,lo)<min(offset+count,hi) for lo,hi,_ in spans):
                raise ValueError('Runtime JAR allocation aliases another payload file')
    candidate.parent.mkdir(parents=True,exist_ok=True)
    if shutil.disk_usage(candidate.parent).free<original_identity[0]+64*1024**2:
        raise OSError(28,'Insufficient workspace for runtime repair')
    def masked(block,at):
        result=bytearray(block)
        for lo,hi,_ in spans:
            begin=max(lo,at)-at;end=min(hi,at+len(block))-at
            if begin<end:result[begin:end]=b'\0'*(end-begin)
        return result
    original_hash=hashlib.sha256();expected=hashlib.sha256();at=0
    with source.open('rb') as reader,candidate.open('xb') as writer:
        while block:=reader.read(8*1024**2):
            original_hash.update(block);expected.update(masked(block,at));patched=bytearray(block)
            for lo,hi,jar_at in spans:
                begin=max(lo,at);end=min(hi,at+len(block))
                if begin<end:patched[begin-at:end-at]=jar[jar_at+begin-lo:jar_at+end-lo]
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
        raise ValueError('Runtime candidate changed bytes outside the JAR allocation')
    changed=UdfImage(candidate)
    if changed.files!=udf.files or changed.read('BDMV/JAR/00000.jar',limit=256*1024**2)!=jar:
        raise ValueError('Runtime repair changed UDF allocation or JAR bytes')
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
    record.update(all_non_jar_bytes_identical=True,non_jar_sha256=actual.hexdigest(),
                  original_image_sha256=original_hash.hexdigest(),image_sha256=final_hash.hexdigest(),
                  original_identity=list(original_identity),independent_descriptors=descriptors,
                  manifest=dict(image=str(candidate),verified=True,bytes=at,files=rows))
    return record
