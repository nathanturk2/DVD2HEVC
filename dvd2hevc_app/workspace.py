"""Temporary storage preferences and per-disc starting capacity guidance."""
import json
from pathlib import Path
from .atomic import write_json_atomic
from .config import config_root
from .pipeline import PipelineError

GIB=1024**3


def configured_work_root():
    path=config_root()/'workspace.json'
    if not path.is_file():return None
    try:
        data=json.loads(path.read_text(encoding='utf-8'))
        value=data.get('work_root')
        if value is not None and not isinstance(value,str):raise ValueError('Invalid workspace path')
        return Path(value).expanduser().resolve() if value else None
    except (OSError,ValueError,AttributeError) as error:
        raise PipelineError('Could not read temporary workspace preference: '+str(path)) from error


def save_work_root(value):
    root=str(Path(value).expanduser().resolve()) if value and str(value).strip() else None
    if root and Path(root).exists() and not Path(root).is_dir():
        raise PipelineError('Temporary work root must be a folder')
    write_json_atomic(config_root()/'workspace.json',dict(schema='dvd2hevc-workspace-v1',work_root=root))


def capacity_estimate(source_bytes):
    # Includes multiple extracted/remuxed media copies and an SSD-authored ISO.
    # This is a planning allowance, not an exact prediction or an allocation.
    storage=max(16*GIB,int(source_bytes)*6)
    return dict(workspace_bytes=storage,ram_disk_free_ram_bytes=storage+8*GIB,
                ram_headroom_bytes=8*GIB)


def workspace_guidance(source=None):
    if source is not None and Path(source).is_file():
        estimate=capacity_estimate(Path(source).stat().st_size)
        return ('An SSD can substantially speed up file-heavy authoring. Allow about '
                f"{estimate['workspace_bytes']/GIB:.0f} GiB of workspace. An existing RAM disk would need about "
                f"{estimate['ram_disk_free_ram_bytes']/GIB:.0f} GiB of free RAM, including 8 GiB left for processing. "
                'Starting estimate only: unusual discs can exceed this allowance. '
                'Measure unfamiliar discs on an SSD before using a RAM disk.')
    return ('An SSD can substantially speed up file-heavy authoring; choose its temporary-work folder here. '
            'Blank uses the saved/default location. An existing RAM disk also works; allow its capacity '
            'plus at least 8 GiB of free RAM for processing. RAM-disk work is lost when it is unmounted or power is lost.')
