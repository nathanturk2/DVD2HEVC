# SPDX-License-Identifier: GPL-3.0-or-later
"""Bound low-bitrate menu read-ahead without changing compressed media."""
MIN_KBPS = 4000
MAX_KBPS = 48000


def mux_options(interactive, has_audio):
    # tsMuxeR only enables minimum-rate stuffing when a maximum is also set.
    return f' --minbitrate={MIN_KBPS} --maxbitrate={MAX_KBPS}' if interactive and has_audio else ''


def repair_candidates(graph, report, files):
    """Select shared physical menu clips once, using retained authoring evidence.

    Native still assets can outlast the IFO presentation; short-video audio
    assets can have the opposite mismatch. Use the longer known interval.
    Never select movie-only clips or synthesize timing for unknown cells.
    """
    references = {}
    for pgc in graph['pgcs']:
        for cell in pgc['cells']:
            clip = cell.get('clip', cell.get('playlist', -1))
            if clip >= 0:
                references.setdefault(clip, []).append(cell)
    sizes = {row['path']: row['size'] for row in files}
    result = []
    for row in report['cells']:
        clip = row['index']
        if (clip not in references or not row.get('audio_streams') or
                not row.get('interactive_buttons', bool(row.get('menus')))):
            continue
        start, end = row['playlist_times']
        millis = max([round((end-start)/45)] + [
            cell.get('presentation_ms', cell['duration']//90) for cell in references[clip]])
        size = sizes[f'BDMV/STREAM/{clip:05d}.m2ts']
        # Allow mux rounding at the rate floor; very short cells cannot create
        # the long navigation stalls this repair addresses.
        if millis > 1000 and size*8 < millis*MIN_KBPS*0.95:
            result.append(dict(clip=clip, millis=millis, original_bytes=size,
                               estimated_added_bytes=max(0, (millis*MIN_KBPS+7)//8-size)))
    return result
