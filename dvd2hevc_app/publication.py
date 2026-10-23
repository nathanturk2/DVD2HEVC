"""Publish a verified image across volumes without exposing a partial final ISO."""
import hashlib
import errno
import os
from pathlib import Path
import shutil
import uuid


def rename_new(source, output):
    if os.name=='nt':os.rename(source,output)
    else:
        os.link(source,output)
        Path(source).unlink()


def same_volume(first,second):
    return os.stat(first).st_dev==os.stat(second).st_dev


def publish_image(source, output, support, progress=None, before_publish=None):
    source,output,support=(Path(p).resolve() for p in (source,output,support))
    if output.exists():raise FileExistsError('Output appeared before publication: '+str(output))
    if source==output:raise ValueError('Publication source must be separate')
    output.parent.mkdir(parents=True,exist_ok=True)
    support.mkdir(parents=True,exist_ok=True)
    if not same_volume(support,output.parent):
        raise ValueError('Publication support folder must be on the destination volume')
    if same_volume(source,output.parent):
        if before_publish:before_publish()
        rename_new(source,output)
        return dict(method='atomic-rename',verified=True)
    original=source.stat()
    identity=(original.st_size,original.st_mtime_ns,original.st_ino)
    if shutil.disk_usage(output.parent).free<original.st_size+64*1024**2:
        raise OSError(errno.ENOSPC,'Insufficient destination space for verified image transfer',str(output))
    temporary=support/('transfer-'+uuid.uuid4().hex[:12]+'.part')
    copied=0;digest=hashlib.sha256()
    with source.open('rb') as reader,temporary.open('xb') as writer:
        while block:=reader.read(8*1024*1024):
            writer.write(block);digest.update(block);copied+=len(block)
            if progress:progress('uhd-copy-output',copied,original.st_size)
        writer.flush();os.fsync(writer.fileno())
    current=source.stat()
    if (current.st_size,current.st_mtime_ns,current.st_ino)!=identity or copied!=original.st_size:
        raise ValueError('Staging image changed during transfer; retained partial copy')
    checked=0;destination_digest=hashlib.sha256()
    with temporary.open('rb') as reader:
        while block:=reader.read(8*1024*1024):
            destination_digest.update(block);checked+=len(block)
            if progress:progress('uhd-copy-verify',checked,original.st_size)
    if checked!=copied or destination_digest.digest()!=digest.digest():
        raise ValueError('Destination image transfer verification failed; retained both copies')
    if before_publish:before_publish()
    rename_new(temporary,output)
    # Retain the SSD source until the caller has durably committed its receipt.
    return dict(method='verified-copy-and-atomic-rename',verified=True,bytes=copied,
                sha256=digest.hexdigest(),retained_staging=str(source))
