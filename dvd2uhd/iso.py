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


def create(source,output,tool=None,label='DVD2UHD',progress=None,writer_progress=None):
    source,output=Path(source).resolve(),Path(output).resolve()
    if output.exists():raise FileExistsError('ISO output must be new: '+str(output))
    if source==output or source in output.parents:raise ValueError('ISO output cannot be inside source folder')
    if not (source/'BDMV/index.bdmv').is_file() or not (source/'BDMV/JAR/00000.jar').is_file():
        raise ValueError('Expected an authored DVD2UHD BDMV folder')
    if not tool:
        bundled=ROOT.parent/'bd2hevc/tools/hadris-udf/bin/hadris-udf.exe'
        tool=bundled if bundled.is_file() else shutil.which('hadris-udf')
    if not tool:raise RuntimeError('Supply --udf-tool pointing to BD2HEVC streaming Hadris UDF 2.2.0')
    tool=Path(tool).resolve()
    output.parent.mkdir(parents=True,exist_ok=True)
    needed=sum(p.stat().st_size for p in source.rglob('*') if p.is_file())+64*1024*1024
    if shutil.disk_usage(output.parent).free<needed:raise OSError('Insufficient space for ISO')
    # Failed images remain for diagnosis; existing images are never replaced.
    with output.open('xb'):pass
    command=[tool,'create',source,'--output',output,'--volume-name',re.sub('[^A-Za-z0-9_-]','_',label)[:32] or 'DVD2UHD',
         '--revision','2.50']
    ended=threading.Event();monitor=None
    if writer_progress:
        counter=output.with_suffix('.progress');command+=['--progress-file',counter]
        total=needed-64*1024*1024
        def poll():
            last=-1
            while not ended.wait(0.5):
                try:value=int(counter.read_text().strip())
                except (OSError,ValueError):continue
                if value!=last:writer_progress(min(value,total),total);last=value
        monitor=threading.Thread(target=poll,daemon=True);monitor.start()
    try:run(command,output.with_suffix('.creation.log'),timeout=3600)
    finally:
        ended.set()
        if monitor:monitor.join(timeout=2)
    if writer_progress:writer_progress(total,total)
    verify=run([tool,'verify',output,'--verbose'],output.with_suffix('.verification.log'),timeout=3600)
    info=run([tool,'info',output],output.with_suffix('.info.log'),timeout=300)
    if not re.search(r'UDF\s+revision:\s*2\.50\b',info,re.IGNORECASE):
        raise ValueError('ISO writer did not report UDF revision 2.50')
    if re.search(r'\b[1-9][0-9]* errors?\b',verify):raise ValueError('UDF writer verification reported errors')
    descriptors=validate_udf(output)
    rows=payload(output,source,progress)
    result=dict(schema='dvd2uhd-iso-v1',image=str(output),udf_revision='2.50',verified=True,
                bytes=output.stat().st_size,tool=str(tool),tool_sha256=sha256(tool),
                independent_descriptors=descriptors,files=rows)
    output.with_suffix('.manifest.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    return result
