"""DVD-VM/BD-J authoring and verified stock-VLC ISO publication."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

from .atomic import write_json_atomic
from .paths import ROOT
from .pipeline import PipelineError
from .progress import progress_event
from .subprocess_utils import hidden_subprocess_kwargs

OUTPUT_FORMATS = ('uhd-bd', 'dvd-hevc')

def output_format(settings):
    # Existing queued jobs/watches keep their original format contract.
    return settings.get('output_format', 'dvd-hevc')

def find_stock_vlc_root(explicit=None):
    choices = ([Path(explicit)] if explicit else [
        Path(os.environ.get('DVD2HEVC_STOCK_VLC_ROOT', '')),
        Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'VideoLAN/VLC',
        Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'VideoLAN/VLC',
        Path(shutil.which('vlc') or '.').parent,
    ])
    for candidate in choices:
        candidate = candidate.expanduser().resolve()
        if ((candidate / 'vlc.exe').is_file() and (candidate / 'libvlc.dll').is_file()
                and list((candidate / 'plugins/access').glob('libbluray-j2se-*.jar'))):
            return candidate
    return None

def discover_uhd_tools(vlc_root=None, *, tsmuxer=None, udf_tool=None, java_home=None):
    player = find_stock_vlc_root(vlc_root)
    java_home = java_home or os.environ.get('JAVA_HOME')
    java = (Path(java_home) / 'bin/java.exe') if java_home else Path(shutil.which('java') or '')
    javac = (Path(java_home) / 'bin/javac.exe') if java_home else Path(shutil.which('javac') or '')
    if not java_home and java.is_file():
        java_home = str(java.resolve().parents[1])
    mux = tsmuxer or os.environ.get('DVD2HEVC_TSMUXER') or shutil.which('tsMuxeR')
    if not mux:
        sibling = ROOT.parent / 'bd2hevc/tools/tsmuxer/tsMuxeR.exe'
        if sibling.is_file(): mux = str(sibling)
    writer = udf_tool or os.environ.get('DVD2HEVC_UDF_TOOL')
    if not writer:
        bundled = ROOT / 'tools/hadris-udf/bin/hadris-udf.exe'
        writer = str(bundled) if bundled.is_file() else shutil.which('hadris-udf')
    api = sorted((player / 'plugins/access').glob('libbluray-j2se-*.jar'))[-1] if player else None
    result = dict(stock_vlc=str(player) if player else None,
                  bdj_api=str(api) if api else None,
                  java=str(java) if java.is_file() else None,
                  javac=str(javac) if javac.is_file() else None,
                  java_home=str(java_home) if java_home else None,
                  tsmuxer=str(Path(mux).resolve()) if mux and Path(mux).is_file() else None,
                  udf_tool=str(Path(writer).resolve()) if writer and Path(writer).is_file() else None)
    return result

def require_uhd_tools(**options):
    from importlib.util import find_spec
    tools = discover_uhd_tools(**options)
    missing = [name for name in ('stock_vlc','bdj_api','java','javac','tsmuxer','udf_tool') if not tools[name]]
    missing += [name for name in ('PIL', 'pycdlib') if find_spec(name) is None]
    if missing:
        raise PipelineError('Missing UHD-BD requirements: ' + ', '.join(missing) +
                            '. Stock VLC with BD-J and a Java 11 JDK are required; VLC patching is unnecessary.')
    return tools

def check_structure(source):
    from dvd2uhd.source import Disc
    from dvd2uhd.ifo import inspect
    from dvd2uhd.author import eligibility, unique_cells
    with Disc(source) as disc:
        graph = inspect(disc)
    issues = eligibility(graph)
    if len(unique_cells(graph)) > 2000: issues.append('more than 2000 physical clips')
    if issues:
        raise PipelineError('UHD-BD output does not yet support this DVD structure: ' + '; '.join(issues[:20]) +
                            '. The explicit dvd-hevc legacy output remains available.')
    return graph

def _emit(phase, completed=0, total=1):
    progress_event('mux', 'progress', phase, scope='uhd-output',
                   completed=completed, total=total)

def _source_signature(source, phase='uhd-source-check'):
    from dvd2uhd.iso import sha256
    paths = [source] if source.is_file() else sorted(p for p in source.rglob('*') if p.is_file())
    if not paths: raise PipelineError('Empty HEVC DVD source')
    rows = []
    for index, path in enumerate(paths):
        rows.append((path.name if source.is_file() else path.relative_to(source).as_posix(),
                     path.stat().st_size, sha256(path)))
        _emit(phase, index + 1, len(paths))
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()

def _runtime_signature():
    import dvd2uhd
    root = Path(dvd2uhd.__file__).parent
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob('*') if p.suffix in ('.py', '.java')):
        digest.update(path.relative_to(root).as_posix().encode()); digest.update(path.read_bytes())
    return digest.hexdigest()

def stock_vlc_gate(image, tools, destination):
    destination.mkdir(parents=True, exist_ok=False)
    command = [sys.executable, str(ROOT / 'tools/test-stock-bluray.py'), tools['stock_vlc'],
               str(image), '--java-home', tools['java_home'], '--output', str(destination),
               '--actions', '', '--wait', '12', '--timeout', '120', '--audio']
    with (destination / 'capture.log').open('w', encoding='utf-8') as log:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, timeout=150,
                                **hidden_subprocess_kwargs())
    receipt = json.loads((destination / 'result.json').read_text(encoding='utf-8'))
    navigation = (destination / 'libbluray.log').read_text(encoding='utf-8', errors='replace')
    if (result.returncode or receipt.get('error') or receipt.get('in_progress')
            or receipt.get('frames', 0) <= 0 or 'DVD2UHD PLAY ' not in navigation
            or 'DVD2UHD ERROR' in navigation):
        raise PipelineError('Stock VLC BD-J startup gate failed; retained logs: ' + str(destination))
    return dict(passed=True, scope='original-first-play-bdj-and-decoded-video',
                frames=receipt['frames'], audio_samples=receipt.get('audio_samples',0),
                result=str(destination / 'result.json'))

def author_uhd(source, output, work, *, vlc_root=None, tsmuxer=None, udf_tool=None,
               java_home=None, label='DVD2HEVC'):
    from dvd2uhd.author import author
    from dvd2uhd.audit import audit
    from dvd2uhd.iso import create, payload, sha256
    source, output, work = (Path(p).expanduser().resolve() for p in (source, output, work))
    if source == output or (source.is_dir() and source in output.parents):
        raise PipelineError('UHD-BD output must be separate from the source')
    if work == source or (source.is_dir() and source in work.parents):
        raise PipelineError('UHD-BD workspace must be separate from the source')
    tools = require_uhd_tools(vlc_root=vlc_root, tsmuxer=tsmuxer, udf_tool=udf_tool, java_home=java_home)
    from .tools import discover_tools
    media_tools = discover_tools()
    if not media_tools.get('ffmpeg') or not media_tools.get('ffprobe'):
        raise PipelineError('FFmpeg and FFprobe are required for UHD-BD authoring')
    os.environ['JAVA_HOME'] = tools['java_home']
    os.environ['PATH'] = str(Path(tools['java_home']) / 'bin') + os.pathsep + os.environ['PATH']
    check_structure(source)
    work.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    signature = dict(source=str(source), source_sha256=_source_signature(source),
                     runtime_sha256=_runtime_signature(), tsmuxer_sha256=sha256(Path(tools['tsmuxer'])),
                     udf_tool_sha256=sha256(Path(tools['udf_tool'])), label=label)
    receipt_path = work / 'uhd-output.json'
    previous = json.loads(receipt_path.read_text(encoding='utf-8')) if receipt_path.is_file() else {}
    if output.exists():
        if previous.get('signature') != signature or previous.get('output') != str(output):
            raise PipelineError('Existing output is not this same verified UHD-BD job; it will not be overwritten')
        folder = Path(previous['folder'])
        # Reuse only after independent payload comparison, even if later publication failed.
        _emit('uhd-verify-iso'); payload(output, folder)
        if not previous.get('stock_vlc_gate',{}).get('passed'):
            raise PipelineError('Existing UHD-BD output has no recorded stock-VLC gate')
        if _source_signature(source, 'uhd-source-stable') != signature['source_sha256']:
            raise PipelineError('Source changed while verifying UHD-BD output')
        manifest=previous.get('manifest')
        if manifest:
            manifest['image']=str(output)
            write_json_atomic(output.with_suffix('.manifest.json'),manifest)
        if previous.get('passed'): return previous
    else:
        folder = (Path(previous['folder']) if previous.get('signature') == signature
                  and previous.get('folder') else None)
        if folder is not None:
            report_path = folder / 'conversion-report.json'
            report = json.loads(report_path.read_text(encoding='utf-8')) if report_path.is_file() else {}
            if (not report.get('complete') or report.get('schema') != 'dvd2uhd-author-v1'
                    or Path(report.get('source', '')).resolve() != source):
                folder = None
        if folder is None:
            # Failed attempts remain diagnosable; retries never delete unrelated work.
            folder = work / ('bdmv-' + uuid.uuid4().hex[:12])
            author(source, folder, tsmuxer=tools['tsmuxer'], bdj_api=tools['bdj_api'],
                   ffmpeg=media_tools['ffmpeg'],ffprobe=media_tools['ffprobe'],
                   progress=lambda done,total: _emit('uhd-author', done,total))
        _emit('uhd-audit')
        audited = audit(folder, media=True, bdj_api=tools['bdj_api'],progress=lambda done,total: _emit('uhd-audit',done,total))
        if audited['missing']: raise PipelineError('UHD-BD audit has missing cells')
        previous = dict(schema='dvd2hevc-uhd-output-v1', passed=False, signature=signature,
                        source=str(source), folder=str(folder), output=str(output), tools=tools,
                        full_media_audit=str(folder/'fidelity-audit.json'), playback_certified=False)
        write_json_atomic(receipt_path, previous)
        staging = output.parent / ('.' + output.name + '.' + uuid.uuid4().hex + '.part')
        _emit('uhd-author-iso')
        manifest = create(folder, staging, tool=tools['udf_tool'], label=label,
                          writer_progress=lambda done,total: _emit('uhd-author-iso',done,total),
                          progress=lambda done,total: _emit('uhd-verify-iso',done,total))
        _emit('uhd-stock-vlc')
        gate = stock_vlc_gate(staging, tools, work / ('vlc-' + uuid.uuid4().hex[:12]))
        previous['stock_vlc_gate'] = gate
        previous['manifest'] = manifest
        write_json_atomic(receipt_path,previous)
        if _source_signature(source, 'uhd-source-stable') != signature['source_sha256']:
            raise PipelineError('Source changed during UHD-BD authoring; retained staging ISO')
        # The output appears only after payload and stock-VLC gates pass.
        if output.exists(): raise PipelineError('Output appeared while authoring; retained verified staging ISO')
        if os.name == 'nt':
            os.rename(staging,output)  # Windows rename refuses an existing target.
        else:
            os.link(staging, output)
            staging.unlink()
        manifest['image'] = str(output)
        write_json_atomic(output.with_suffix('.manifest.json'), manifest)
    previous['passed'] = True
    previous['size'] = output.stat().st_size
    previous['finished_at'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    write_json_atomic(receipt_path, previous)
    _emit('uhd-complete',1,1)
    return previous

def cmd_author_uhd(args):
    result = author_uhd(args.source, args.output, args.work_dir,
                        vlc_root=args.vlc_root, tsmuxer=args.tsmuxer, udf_tool=args.udf_tool,
                        java_home=args.java_home, label=args.label)
    print('Verified UHD-BD output: ' + result['output'])
    return 0

def add_output_options(parser):
    parser.add_argument('--output-format', choices=OUTPUT_FORMATS,
                        help='uhd-bd (default): stock VLC with Java; dvd-hevc: legacy patched-VLC DVD')
    parser.add_argument('--tsmuxer', help='tsMuxeR executable for UHD-BD authoring')
    parser.add_argument('--udf-tool', help='Hadris streaming UDF executable for UHD-BD')
    parser.add_argument('--java-home', help='Java 11 JDK directory for UHD-BD authoring/playback')

def add_uhd_commands(commands):
    parser = commands.add_parser('author-uhd', help='Upgrade an existing HEVC DVD ISO/folder to stock-VLC UHD-BD')
    parser.add_argument('source'); parser.add_argument('output')
    parser.add_argument('--work-dir', required=True)
    parser.add_argument('--vlc-root', help='Stock VLC directory with libbluray BD-J')
    parser.add_argument('--tsmuxer'); parser.add_argument('--udf-tool'); parser.add_argument('--java-home')
    parser.add_argument('--label', default='DVD2HEVC')
    parser.set_defaults(func=cmd_author_uhd)
