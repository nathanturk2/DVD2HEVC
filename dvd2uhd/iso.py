# SPDX-License-Identifier: GPL-3.0-or-later
"""UDF 2.50 authoring with BD2HEVC's streaming Hadris tool and payload audit."""
import hashlib
import json
import re
import shutil
import threading
from pathlib import Path
from .author import ROOT,run
from .udf_validation import validate_udf
from .udf_reader import UdfImage


def sha256(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        while b:=f.read(8*1024*1024):h.update(b)
    return h.hexdigest()


def support_directory(output, root=None):
    """Keep writer artifacts out of the movie directory, using short stable names."""
    output=Path(output).resolve()
    root=Path(root).resolve() if root is not None else output.parent/'Conversion files'
    token=hashlib.sha256(str(output).casefold().encode('utf-8')).hexdigest()[:16]
    return root/token


def verify_manifest(image, rows, progress=None):
    """Independently recheck a published ISO after its temporary media is pruned."""
    image=Path(image)
    udf=UdfImage(image)
    expected={row['path']:row for row in rows}
    if not expected or len(expected)!=len(rows) or set(expected)!=set(udf.files):
        raise ValueError('ISO payload inventory differs from verified manifest')
    with image.open('rb') as stream:
        for index,(name,row) in enumerate(sorted(expected.items()),1):
            entry=udf.files[name];remaining=entry['size'];digest=hashlib.sha256()
            if entry['size']!=row['size']:raise ValueError('ISO payload size mismatch: '+name)
            for offset,length in entry['extents']:
                stream.seek(offset);count=min(remaining,length)
                while count:
                    block=stream.read(min(count,8*1024*1024))
                    if not block:raise ValueError('Truncated ISO payload: '+name)
                    digest.update(block);count-=len(block);remaining-=len(block)
            if remaining or digest.hexdigest()!=row['sha256']:
                raise ValueError('ISO payload mismatch: '+name)
            if progress:progress(index,len(expected))
    return rows


def payload(image,source,progress=None):
    udf=UdfImage(image)
    files={p.relative_to(source).as_posix():p for p in source.rglob('*') if p.is_file()}
    if set(files)!=set(udf.files):raise ValueError('ISO payload inventory differs from source folder')
    rows=[]
    with image.open('rb') as f:
        for name,p in sorted(files.items()):
            entry=udf.files[name];h=hashlib.sha256();remaining=entry['size']
            for offset,length in entry['extents']:
                f.seek(offset);count=min(remaining,length)
                while count:
                    b=f.read(min(count,8*1024*1024))
                    if not b:raise ValueError('Truncated ISO payload: '+name)
                    h.update(b);count-=len(b);remaining-=len(b)
            if remaining or entry['size']!=p.stat().st_size or h.hexdigest()!=sha256(p):
                raise ValueError('ISO payload mismatch: '+name)
            rows.append(dict(path=name,size=entry['size'],sha256=h.hexdigest()))
            if progress:progress(len(rows),len(files))
    return rows


def create(source,output,tool=None,label='DVD2UHD',progress=None,writer_progress=None,artifacts_dir=None):
    source,output=Path(source).resolve(),Path(output).resolve()
    if output.exists():raise FileExistsError('ISO output must be new: '+str(output))
    if source==output or source in output.parents:raise ValueError('ISO output cannot be inside source folder')
    artifacts=Path(artifacts_dir).resolve() if artifacts_dir is not None else support_directory(output)
    if artifacts==source or source in artifacts.parents:
        raise ValueError('ISO supporting files cannot be inside source folder')
    if not (source/'BDMV/index.bdmv').is_file() or not (source/'BDMV/JAR/00000.jar').is_file():
        raise ValueError('Expected an authored DVD2UHD BDMV folder')
    if not tool:
        bundled=ROOT.parent/'bd2hevc/tools/hadris-udf/bin/hadris-udf.exe'
        tool=bundled if bundled.is_file() else shutil.which('hadris-udf')
    if not tool:raise RuntimeError('Supply --udf-tool pointing to BD2HEVC streaming Hadris UDF 2.2.0')
    tool=Path(tool).resolve()
    output.parent.mkdir(parents=True,exist_ok=True)
    artifacts.mkdir(parents=True,exist_ok=True)
    needed=sum(p.stat().st_size for p in source.rglob('*') if p.is_file())+64*1024*1024
    if shutil.disk_usage(output.parent).free<needed:raise OSError('Insufficient space for ISO')
    # Failed images remain for diagnosis; existing images are never replaced.
    with output.open('xb'):pass
    command=[tool,'create',source,'--output',output,'--volume-name',re.sub('[^A-Za-z0-9_-]','_',label)[:32] or 'DVD2UHD',
         '--revision','2.50']
    ended=threading.Event();monitor=None
    if writer_progress:
        counter=artifacts/'writer.progress';command+=['--progress-file',counter]
        total=needed-64*1024*1024
        def poll():
            last=-1
            while not ended.wait(0.5):
                try:value=int(counter.read_text().strip())
                except (OSError,ValueError):continue
                if value!=last:writer_progress(min(value,total),total);last=value
        monitor=threading.Thread(target=poll,daemon=True);monitor.start()
    try:run(command,artifacts/'creation.log',timeout=3600)
    finally:
        ended.set()
        if monitor:monitor.join(timeout=2)
    if writer_progress:writer_progress(total,total)
    verify=run([tool,'verify',output,'--verbose'],artifacts/'verification.log',timeout=3600)
    info=run([tool,'info',output],artifacts/'info.log',timeout=300)
    if not re.search(r'UDF\s+revision:\s*2\.50\b',info,re.IGNORECASE):
        raise ValueError('ISO writer did not report UDF revision 2.50')
    if re.search(r'\b[1-9][0-9]* errors?\b',verify):raise ValueError('UDF writer verification reported errors')
    descriptors=validate_udf(output)
    rows=payload(output,source,progress)
    result=dict(schema='dvd2uhd-iso-v1',image=str(output),udf_revision='2.50',verified=True,
                bytes=output.stat().st_size,tool=str(tool),tool_sha256=sha256(tool),
                independent_descriptors=descriptors,files=rows)
    (artifacts/'manifest.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    return result
