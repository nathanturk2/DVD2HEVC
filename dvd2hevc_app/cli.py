"""DVD2HEVC command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .audio import (
    parse_audio_bitrate,
    prototype_compact_audio,
    prototype_compact_audio_batch,
    prototype_compact_audio_domain,
)
from .audio_policy import resolve_audio_policy
from .author import author_dvd_iso, verify_authored_iso
from .branching import convert_interleaved_vts, prototype_interleaved_cell
from .compact import (
    plan_compact_audio_layout,
    plan_compact_layout,
    prototype_compact_domain,
    prototype_compact_domain_batch,
)
from .compatibility import build_compatibility_plan
from .compat_registry import add_compatibility_registry_commands
from .iso import DiscScanError
from .ifo import rewrite_compact_vmgi, rewrite_compact_vts_ifo
from .interleaved import build_interleaved_unit_plan
from .extract import extract_title_sector_range, extract_title_sector_segments
from .frontend import (
    add_user_commands,
    cmd_job_status,
    find_authoring_backend,
    find_patched_vlc_root,
)
from .setup_tools import add_build_inspector_command
from .diagnostics import add_diagnose_command
from .encoders import (
    HEVC_ENCODERS,
    available_hevc_encoders,
    encoder_label,
    parse_rate_control,
    probe_hevc_encoder,
)
from .planning import build_plan
from .pipeline import PipelineError, convert_menu_domain, convert_title
from .quality import compact_auto_preset, estimate_equivalent_quality, resolve_disc_quality_policy
from .repair import audit_css_safe_hevc_psms, repair_css_safe_hevc_psms, verify_css_safe_hevc_psms
from .scan import scan_disc
from .staging import (
    combine_title_reports,
    stage_complete_dvd,
    extend_complete_stage,
    stage_compact_vts,
    stage_compact_vmg,
    stage_title_dvd,
    verify_complete_stage,
    verify_staged_title,
)
from .tools import discover_tools
from .validation import (
    compare_vlc_navigation_logs,
    validate_navigation_only_menu_logs,
    validate_vlc_dvdhevc_log,
)


def parse_cq_quality(value: str | int | float) -> int | float:
    """Accept BD2HEVC/HandBrake-style CQ spellings such as cq:20."""
    text = str(value).strip().lower()
    if text.startswith("cq:"):
        text = text[3:]
    try:
        quality = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid CQ quality: {value}") from exc
    if not 0 <= quality <= 51:
        raise argparse.ArgumentTypeError("CQ quality must be between 0 and 51")
    return int(quality) if quality.is_integer() else quality


def parse_cq_quality_list(value: str) -> tuple[int | float, ...]:
    try:
        return tuple(parse_cq_quality(item) for item in value.split(",") if item.strip())
    except argparse.ArgumentTypeError as exc:
        raise PipelineError(str(exc)) from exc


def parse_video_quality(value: str | int | float) -> int | float | str:
    """Parse manual CQ or an exact VBR/CBR target for low-level workers."""
    try:
        control = parse_rate_control(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if control["mode"] == "cq":
        quality = float(control["quality"])
        return int(quality) if quality.is_integer() else quality
    return str(control["canonical"])


def parse_video_quality_list(value: str) -> tuple[int | float | str, ...]:
    try:
        return tuple(parse_video_quality(item) for item in value.split(",") if item.strip())
    except argparse.ArgumentTypeError as exc:
        raise PipelineError(str(exc)) from exc


def parse_compact_quality(value: str) -> int | float | str:
    if value.strip().lower() in {"auto", "compact", "compact-auto"}:
        return "compact-auto"
    return parse_video_quality(value)


def write_report(path: str | None, value: dict[str, Any]) -> None:
    if not path:
        return
    destination = Path(path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(value, indent=2), encoding="utf-8")


def resolve_encoding_quality(args: argparse.Namespace) -> tuple[tuple[int | float, ...], dict[str, Any] | None]:
    if args.quality_preset != "compact-auto":
        values = parse_video_quality_list(args.quality_values)
        return values, None
    if not args.quality_estimate:
        raise PipelineError(
            "compact-auto encoding requires --quality-estimate from estimate-quality"
        )
    estimate_path = Path(args.quality_estimate).resolve()
    estimate = json.loads(estimate_path.read_text(encoding="utf-8"))
    preset = compact_auto_preset(estimate)
    preset["estimate_report"] = str(estimate_path)
    return (preset["resolved_quality"],), preset


def print_scan_summary(scan: dict[str, Any]) -> None:
    video_ts = scan["video_ts"]
    totals = video_ts["totals"]
    print(f"DVD2HEVC scan: {scan['label']}")
    print(f"Source: {scan['source']}")
    print(f"VTS sets: {video_ts['vts_count']}  VOB files: {len(video_ts['vobs'])}")
    if scan["scan_depth"] == "full":
        gib = totals["vob_bytes"] / (1024 ** 3)
        capacity = totals["video_payload_bytes"] / (1024 ** 3)
        print(f"VOB data: {gib:.2f} GiB  video payload capacity: {capacity:.2f} GiB")
        print(f"VOB sectors: {totals['vob_sectors']:,}  NAV packs: {totals['nav_packs']:,}")
        print(f"Scrambled PES: {totals['scrambled_pes_packets']:,}  invalid sectors: {totals['invalid_sectors']:,}")
        print(f"Content appears decrypted: {'yes' if scan['content_decrypted'] else 'NO'}")
    title_scan = scan.get("title_scan") or {}
    if title_scan.get("titles"):
        print(f"Titles: {title_scan['title_count']}")
        for title in title_scan["titles"]:
            duration = int(title.get("duration_seconds") or 0)
            print(f"  {title['index']:>2}: {duration // 3600:02}:{duration // 60 % 60:02}:{duration % 60:02}  chapters={title['chapter_count']} angles={title.get('angle_count')}")
    elif title_scan.get("error"):
        print(f"Title scan warning: {title_scan['error']}")
    graph = scan.get("physical_graph") or {}
    if graph.get("summary"):
        summary = graph["summary"]
        print(
            "Physical graph: "
            f"titles={summary['global_titles']} pgcs={summary['title_pgcs']} "
            f"cells={summary['referenced_cells']} unique-cells={summary['unique_physical_cells']} "
            f"title-vobus={summary['title_vobus']} menu-vobus={summary['menu_vobus']}"
        )
    elif graph.get("error"):
        print(f"Native graph warning: {graph['error']}")


def cmd_tools(args: argparse.Namespace) -> int:
    from .uhd import discover_uhd_tools
    selected_format = getattr(args,'output_format',None) or 'uhd-bd'
    uhd = discover_uhd_tools() if selected_format == 'uhd-bd' else None
    tools = discover_tools()
    patched_vlc = find_patched_vlc_root()
    available = (
        available_hevc_encoders(str(tools["ffmpeg"])) if tools.get("ffmpeg") else ()
    )
    selected = str(getattr(args, "encoder", "hevc_nvenc") or "hevc_nvenc")
    probe_targets = HEVC_ENCODERS if getattr(args, "probe_all_encoders", False) else (selected,)
    probes: dict[str, dict[str, Any]] = {}
    if tools.get("ffmpeg") and tools.get("ffprobe"):
        for encoder in probe_targets:
            probes[encoder] = probe_hevc_encoder(
                str(tools["ffmpeg"]), str(tools["ffprobe"]), encoder, "p6"
            )
    authoring = uhd['udf_tool'] if uhd else find_authoring_backend()
    print(f"DVD2HEVC {__version__} conversion preflight")
    for name, value in tools.items():
        if name == "platform":
            continue
        print(f"  {name:16} {value if value else 'not found'}")
    print(f"  {'hevc_encoders':16} {', '.join(available) if available else 'none'}")
    for encoder in HEVC_ENCODERS:
        if encoder in probes:
            probe = probes[encoder]
            state = "operational" if probe.get("operational") else f"unavailable ({probe.get('error')})"
        else:
            state = "compiled (not runtime-probed)" if encoder in available else "not compiled"
        print(f"  {encoder:16} {state}")
    print(f"  {'iso_authoring':16} {authoring if authoring else 'not found'}")
    print(f"  {'patched_vlc':16} {patched_vlc if patched_vlc else 'not found'}")
    if uhd:
        for name,value in uhd.items(): print(f"  {name:16} {value or 'not found'}")
        print('  UHD-BD output: stock VLC with Java; no VLC patch required.')
    missing = [
        name
        for name in ("ffmpeg", "ffprobe", "handbrake", "dvdinspect")
        if not tools.get(name)
    ]
    if not tools.get("python_pycdlib"):
        missing.append("pycdlib")
    if not probes.get(selected, {}).get("operational"):
        missing.append(f"operational {selected}")
    if not authoring:
        missing.append("genisoimage/mkisofs")
    if uhd:
        missing.extend(name for name in ('stock_vlc','bdj_api','java','javac','tsmuxer','udf_tool') if not uhd[name])
    elif not patched_vlc:
        missing.append("patched VLC")
    if missing:
        print("Missing conversion requirements: " + ", ".join(missing), file=sys.stderr)
        return 1
    print(f"Complete menu-preserving conversion: ready with {encoder_label(selected)}")
    return 0


def run_scan(args: argparse.Namespace) -> dict[str, Any]:
    return scan_disc(
        Path(args.source),
        inspect_vobs=not args.quick,
        use_handbrake=not args.no_handbrake,
        use_native=not args.no_native,
        min_title_duration=args.min_title_duration,
    )


def cmd_scan(args: argparse.Namespace) -> int:
    scan = run_scan(args)
    write_report(args.report, scan)
    if args.json:
        print(json.dumps(scan, indent=2))
    else:
        print_scan_summary(scan)
    totals = scan["video_ts"]["totals"]
    return 2 if totals.get("scrambled_pes_packets") or totals.get("invalid_sectors") else 0


def cmd_plan(args: argparse.Namespace) -> int:
    scan = run_scan(args)
    plan = build_plan(scan)
    report = {"scan": scan, "plan": plan}
    write_report(args.report, report)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_scan_summary(scan)
        summary = plan["summary"]
        print("Plan:")
        print(f"  reencode candidates: {summary['reencode_candidates']}")
        print(f"  copy: {summary['copy']}  blocked: {summary['blocked']}")
        print(f"  ready for VOB rewriter prototype: {'yes' if plan['ready_for_rewriter_prototype'] else 'no'}")
        for blocker in plan["blockers"]:
            print(f"  blocker: {blocker}")
    return 0 if plan["ready_for_rewriter_prototype"] else 2


def cmd_compatibility_plan(args: argparse.Namespace) -> int:
    if args.scan_report:
        scan = json.loads(Path(args.scan_report).resolve().read_text(encoding="utf-8"))
    else:
        scan = scan_disc(
            Path(args.source),
            inspect_vobs=False,
            use_handbrake=False,
            use_native=True,
            min_title_duration=1,
        )
    result = build_compatibility_plan(scan)
    write_report(args.destination, result)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        summary = result["summary"]
        print(f"Phase 7 compatibility plan: {result['label']}")
        print(
            f"  titles={summary['global_titles']} ready={summary['ready_titles']} "
            f"blocked={summary['blocked_titles']} shared-refs={summary['shared_cell_references']}"
        )
        print(
            f"  unique-cells={summary['unique_physical_cells']} "
            f"interleaved={summary['interleaved_cells']} "
            f"overlaps={summary['overlapping_reference_ranges']}"
        )
        if result["recommended_first_gate"]:
            gate = result["recommended_first_gate"]
            print(
                f"  first gate: title {gate['title']} VTS {gate['vts']} "
                f"duration={gate['duration_seconds']:.3f}s cells={gate['unique_cells']}"
            )
        for blocker in result["blockers"]:
            print(f"  blocker: {blocker['code']} (titles={blocker['affected_titles']})")
        print(f"Report: {Path(args.destination).resolve()}")
    return 0


def cmd_plan_interleaved(args: argparse.Namespace) -> int:
    if args.scan_report:
        scan = json.loads(Path(args.scan_report).resolve().read_text(encoding="utf-8"))
    else:
        scan = scan_disc(
            Path(args.source),
            inspect_vobs=False,
            use_handbrake=False,
            use_native=True,
            min_title_duration=1,
        )
    result = build_interleaved_unit_plan(scan, args.vts)
    write_report(args.destination, result)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        summary = result["summary"]
        print(f"Phase 7 physical-VTS plan: {result['label']} VTS {result['vts']}")
        print(
            f"  cells={summary['pgc_cells']} interleaved={summary['interleaved_cells']} "
            f"resolved={summary['resolved_interleaved_cells']} "
            f"unresolved={summary['unresolved_interleaved_cells']}"
        )
        print(
            f"  selected-sectors={summary['selected_interleaved_sectors']:,} "
            f"skipped-alternate-sectors={summary['skipped_alternate_sectors']:,} "
            f"shared-segment-refs={summary['shared_segment_references']}"
        )
        print(
            "  segmented extraction: "
            f"{'ready' if result['segmented_extraction_ready'] else 'BLOCKED'}; "
            "segmented repack: "
            f"{'ready' if result['segmented_repack_ready'] else 'BLOCKED'}"
        )
        print(
            f"  unique conversion tasks={summary['unique_conversion_tasks']} "
            f"reused references={summary['conversion_task_references_reused']} "
            f"complete coverage={'yes' if result['complete_sector_preserving_coverage'] else 'NO'}"
        )
        print(f"Report: {Path(args.destination).resolve()}")
    return 0 if result["segmented_extraction_ready"] else 2


def cmd_extract_interleaved_cell(args: argparse.Namespace) -> int:
    plan = json.loads(Path(args.plan).resolve().read_text(encoding="utf-8"))
    if plan.get("schema") != "dvd2hevc-interleaved-unit-plan-v0":
        raise PipelineError("Unsupported interleaved-unit plan schema")
    if not plan.get("segmented_extraction_ready"):
        raise PipelineError("Interleaved-unit plan did not pass the segmented extraction gate")
    matches = [
        row for row in plan.get("cells", [])
        if int(row["pgc"]) == args.pgc and int(row["pgc_cell"]) == args.cell
    ]
    if len(matches) != 1:
        raise PipelineError(f"PGC {args.pgc} cell {args.cell} is not unique in the plan")
    cell = matches[0]
    if not cell.get("interleaved"):
        raise PipelineError(f"PGC {args.pgc} cell {args.cell} is not interleaved")
    result = extract_title_sector_segments(
        Path(plan["source"]),
        vts=int(plan["vts"]),
        segments=cell["segments"],
        destination=Path(args.destination),
    )
    report = {
        "schema": "dvd2hevc-interleaved-extraction-v0",
        "plan": str(Path(args.plan).resolve()),
        "pgc": args.pgc,
        "pgc_cell": args.cell,
        "cell": cell,
        "extraction": result,
    }
    write_report(args.report, report)
    stats = result["vob_stats"]
    print(
        f"Extracted interleaved PGC {args.pgc} cell {args.cell}: "
        f"segments={result['segment_count']} sectors={result['sector_count']:,}"
    )
    print(
        f"  NAV packs={stats['nav_packs']:,} video PES={stats['video_pes_packets']:,} "
        f"invalid sectors={stats['invalid_sectors']:,}"
    )
    print(f"Destination: {result['destination']}")
    return 0 if not stats["invalid_sectors"] else 2


def cmd_prototype_interleaved_cell(args: argparse.Namespace) -> int:
    result = prototype_interleaved_cell(
        Path(args.plan),
        pgc_number=args.pgc,
        pgc_cell_number=args.cell,
        workspace=Path(args.workspace),
        quality_values=parse_video_quality_list(args.quality_values),
        preset=args.preset,
        encoder=args.encoder,
        cadence=args.cadence,
        ambiguous_cadence=args.ambiguous_cadence,
        allow_compact_expansion=args.allow_compact_expansion,
        prefer_compact_input=args.prefer_compact_input,
    )
    summary = result["summary"]
    print(
        f"Interleaved HEVC round trip passed: segments={summary['segments']} "
        f"VOBUs={summary['vobus']} frames={summary['output_frames']}"
    )
    if summary["alternate_sectors_unchanged"] is None:
        print("  physical-sector construction deferred to final compaction")
    else:
        print(
            "  alternate sectors unchanged="
            f"{'yes' if summary['alternate_sectors_unchanged'] else 'NO'}; "
            "selected readback="
            f"{'match' if summary['selected_readback_matches'] else 'MISMATCH'}"
        )
    return 0


def cmd_convert_interleaved_vts(args: argparse.Namespace) -> int:
    result = convert_interleaved_vts(
        Path(args.plan),
        workspace=Path(args.workspace),
        quality_values=parse_video_quality_list(args.quality_values),
        preset=args.preset,
        encoder=args.encoder,
        cadence=args.cadence,
        ambiguous_cadence=args.ambiguous_cadence,
        allow_compact_expansion=args.allow_compact_expansion,
        prefer_compact_input=args.prefer_compact_input,
        pipeline_depth=args.pipeline_depth,
        audio_prefetch_root=(
            Path(args.audio_prefetch_root) if args.audio_prefetch_root else None
        ),
        audio_policy=Path(args.audio_policy) if args.audio_policy else None,
        stereo_audio_bitrate=parse_audio_bitrate(args.stereo_audio_bitrate),
        mono_audio_bitrate=parse_audio_bitrate(args.mono_audio_bitrate),
        audio_workers=args.audio_workers,
    )
    summary = result["summary"]
    print(
        f"Converted interleaved VTS {result['vts']}: "
        f"tasks={summary['unique_physical_tasks']} VOBUs={summary['vobus']} "
        f"frames={summary['output_frames']}"
    )
    return 0


def cmd_extract_cell(args: argparse.Namespace) -> int:
    result = extract_title_sector_range(
        Path(args.source),
        vts=args.vts,
        first_sector=args.first_sector,
        last_sector=args.last_sector,
        destination=Path(args.destination),
    )
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        stats = result["vob_stats"]
        print(f"Extracted VTS {args.vts:02d} sectors {args.first_sector}-{args.last_sector}")
        print(f"Destination: {result['destination']}")
        print(f"Sectors: {result['sector_count']:,}  bytes: {result['size']:,}")
        print(
            f"Video PES: {stats['video_pes_packets']:,}  NAV packs: {stats['nav_packs']:,} "
            f"scrambled: {stats['scrambled_pes_packets']:,} invalid sectors: {stats['invalid_sectors']:,}"
        )
    return 0


def cmd_validate_vlc_log(args: argparse.Namespace) -> int:
    result = validate_vlc_dvdhevc_log(Path(args.log), minimum_cell_changes=args.minimum_cell_changes)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Patched VLC DVD-HEVC validation: {'PASS' if result['passed'] else 'FAIL'}")
        for name, passed in result["checks"].items():
            print(f"  {'ok' if passed else 'MISSING':7} {name}")
        observations = result["observations"]
        print(
            f"  observed cells={observations['cell_change_count']} PSMs={observations['hevc_psm_accept_count']}\n"
            f"  first pictures={observations['first_picture_count']} "
            f"HEVC corruption={sum(observations['hevc_corruption_counts'].values())}\n"
            "  observed audio/video fallback: "
            f"AC-3={'yes' if observations['ac3_packetizer'] else 'no'}, "
            f"MPEG-2={'yes' if observations['mpeg2_packetizer'] else 'no'}"
        )
    return 0 if result["passed"] else 2


def cmd_validate_vlc_menu_equivalence(args: argparse.Namespace) -> int:
    result = validate_navigation_only_menu_logs(
        Path(args.source_log),
        Path(args.output_log),
        minimum_cell_changes=args.minimum_cell_changes,
    )
    if args.report:
        write_report(args.report, result)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(
            "Source-relative navigation-only menu validation: "
            f"{'PASS' if result['passed'] else 'FAIL'}"
        )
        for name, passed in result["checks"].items():
            print(f"  {'ok' if passed else 'MISSING':7} {name}")
    return 0 if result["passed"] else 2


def cmd_convert_title(args: argparse.Namespace) -> int:
    quality_values, quality_preset = resolve_encoding_quality(args)
    if not quality_values:
        raise PipelineError("At least one quality value is required")
    if quality_preset and args.encoder != "hevc_nvenc":
        raise PipelineError("compact-auto CQ calibration currently requires hevc_nvenc")
    report = convert_title(
        Path(args.source),
        title_number=args.title,
        workspace=Path(args.workspace),
        preset="p6" if quality_preset else args.preset,
        quality_values=quality_values,
        threads=args.threads,
        encoder=args.encoder,
        cadence=args.cadence,
        ambiguous_cadence=args.ambiguous_cadence,
        quality_preset=quality_preset,
        allow_compact_expansion=args.allow_compact_expansion,
        prefer_compact_input=args.prefer_compact_input,
        audio_prefetch_root=(
            Path(args.audio_prefetch_root) if args.audio_prefetch_root else None
        ),
        audio_policy=Path(args.audio_policy) if args.audio_policy else None,
        stereo_audio_bitrate=parse_audio_bitrate(args.stereo_audio_bitrate),
        mono_audio_bitrate=parse_audio_bitrate(args.mono_audio_bitrate),
        audio_workers=args.audio_workers,
    )
    summary = report["summary"]
    print(
        f"Converted title {args.title}: cells={summary['cells_converted']} "
        f"VOBUs={summary['vobus_converted']} frames={summary['output_frames']}"
    )
    return 0


def cmd_convert_menu(args: argparse.Namespace) -> int:
    quality_values, quality_preset = resolve_encoding_quality(args)
    if not quality_values:
        raise PipelineError("At least one quality value is required")
    if quality_preset and args.encoder != "hevc_nvenc":
        raise PipelineError("compact-auto CQ calibration currently requires hevc_nvenc")
    report = convert_menu_domain(
        Path(args.source),
        domain=args.domain,
        vts_number=args.vts,
        workspace=Path(args.workspace),
        preset="p6" if quality_preset else args.preset,
        quality_values=quality_values,
        encoder=args.encoder,
        cadence=args.cadence,
        ambiguous_cadence=args.ambiguous_cadence,
        quality_preset=quality_preset,
        allow_compact_expansion=args.allow_compact_expansion,
    )
    summary = report["summary"]
    print(
        f"Converted {args.domain} VTS {args.vts}: cells={summary['cells_converted']} "
        f"VOBUs={summary['vobus_converted']} frames={summary['output_frames']}"
    )
    return 0


def cmd_stage_title(args: argparse.Namespace) -> int:
    result = stage_title_dvd(Path(args.report), Path(args.destination))
    summary = result["summary"]
    print(
        f"Staged title {result['title']}: cells={summary['replacement_cells']} "
        f"PSMs={summary['program_stream_maps']} title-bytes={summary['title_vob_bytes']}"
    )
    return 0


def cmd_stage_disc(args: argparse.Namespace) -> int:
    result = stage_complete_dvd(
        [Path(path) for path in args.reports], Path(args.destination),
        source=Path(args.source) if args.source else None,
    )
    summary = result["summary"]
    print(
        f"Staged complete DVD: domains={summary['conversion_domains']} "
        f"cells={summary['replacement_cells']} PSMs={summary['program_stream_maps']}"
    )
    return 0


def cmd_extend_stage(args: argparse.Namespace) -> int:
    result = extend_complete_stage(
        Path(args.base_stage), [Path(path) for path in args.reports], Path(args.destination)
    )
    summary = result["summary"]
    print(
        f"Extended complete DVD: added={summary['additional_domains']} "
        f"cells={summary['replacement_cells']} PSMs={summary['program_stream_maps']} "
        f"COW-files={summary['copy_on_write_files']}"
    )
    return 0


def cmd_stage_compact_vts(args: argparse.Namespace) -> int:
    result = stage_compact_vts(
        Path(args.base_stage),
        Path(args.layout),
        vts=args.vts,
        compact_vob=Path(args.compact_vob),
        compact_ifo=Path(args.compact_ifo),
        compact_bup=Path(args.compact_bup),
        compact_menu_vob=Path(args.compact_menu_vob) if args.compact_menu_vob else None,
        destination=Path(args.destination),
    )
    summary = result["summary"]
    print(
        f"Compact VTS {args.vts} stage: sectors={summary['compact_vob_sectors']:,} "
        f"VOBUs={summary['vobus']} hardlinks={summary['hardlinked_files']}"
    )
    return 0


def cmd_stage_compact_vmg(args: argparse.Namespace) -> int:
    result = stage_compact_vmg(
        Path(args.base_stage),
        Path(args.layout),
        compact_vob=Path(args.compact_vob),
        compact_ifo=Path(args.compact_ifo),
        compact_bup=Path(args.compact_bup),
        destination=Path(args.destination),
    )
    summary = result["summary"]
    print(
        f"Compact VMG stage: sectors={summary['compact_vmg_vob_sectors']:,} "
        f"VOBUs={summary['vmg_vobus']} hardlinks={summary['hardlinked_files']}"
    )
    return 0


def cmd_author_iso(args: argparse.Namespace) -> int:
    result = author_dvd_iso(
        Path(args.stage),
        Path(args.destination),
        label=args.label,
        wsl_distro=args.wsl_distro,
        log_path=Path(args.log) if getattr(args, "log", None) else None,
        report_path=Path(args.report) if getattr(args, "report", None) else None,
    )
    print(f"Authored DVD UDF ISO: {result['destination']} ({result['size']:,} bytes)")
    print(f"Backend: {result['backend']}")
    return 0


def cmd_verify_iso(args: argparse.Namespace) -> int:
    result = verify_authored_iso(
        Path(args.report),
        report_path=(
            Path(args.output_report)
            if getattr(args, "output_report", None)
            else None
        ),
    )
    summary = result["summary"]
    print(
        f"Authored ISO verification: PASS ({summary['matching_files']}/{summary['files']} files, "
        f"{summary['bytes']:,} bytes; physical graph exact)"
    )
    return 0


def cmd_repair_css_safe_psm(args: argparse.Namespace) -> int:
    result = repair_css_safe_hevc_psms(Path(args.source), Path(args.destination))
    write_report(args.report, result)
    print(
        "CSS-safe HEVC PSM repair: PASS "
        f"({result['maps_rewritten']:,}/{result['hevc_program_stream_maps']:,} maps rewritten; "
        "compact layout unchanged)"
    )
    print(f"Destination: {result['destination']}")
    return 0


def cmd_verify_css_safe_psm(args: argparse.Namespace) -> int:
    result = verify_css_safe_hevc_psms(Path(args.source), Path(args.destination))
    write_report(args.report, result)
    print(
        "CSS-safe HEVC PSM verification: PASS "
        f"({result['changed_sectors']:,} sectors; only PSM bytes changed; layout unchanged)"
    )
    return 0


def cmd_audit_css_safe_psm(args: argparse.Namespace) -> int:
    result = audit_css_safe_hevc_psms(Path(args.image))
    write_report(args.report, result)
    print(
        f"CSS-safe HEVC PSM audit: {'PASS' if result['passed'] else 'FAIL'} "
        f"(maps={result['hevc_program_stream_maps']:,}, "
        f"legacy={result['legacy_css_ambiguous_maps']:,})"
    )
    return 0 if result["passed"] else 2


def cmd_plan_compact(args: argparse.Namespace) -> int:
    reports = [Path(path) for path in args.reports]
    quality = args.quality
    preset: dict[str, Any] | None = None
    quality_by_vts: dict[str, int | float | str] | None = None
    if args.disc_quality_policy:
        policy = json.loads(Path(args.disc_quality_policy).resolve().read_text(encoding="utf-8-sig"))
        if policy.get("schema") not in {
            "dvd2hevc-disc-quality-policy-v1", "dvd2hevc-disc-quality-policy-v2"
        }:
            raise PipelineError("Unsupported disc quality policy")
        quality = parse_compact_quality(str(policy["general"]["resolved"]))
        quality_by_vts = {
            str(vts): parse_compact_quality(str(row["resolved"]))
            for vts, row in (policy.get("quality_by_vts") or {}).items()
        }
        preset = policy
    if quality == "compact-auto":
        if args.quality_estimate:
            estimate_path = Path(args.quality_estimate).resolve()
            estimate = json.loads(estimate_path.read_text(encoding="utf-8"))
        else:
            estimate_path = (
                Path(args.quality_report).resolve()
                if args.quality_report
                else Path(args.destination).resolve().with_suffix(".quality.json")
            )
            estimate = estimate_equivalent_quality(
                reports,
                ratio=args.hevc_source_ratio,
                destination=estimate_path,
            )
        preset = compact_auto_preset(estimate, requested_ratio=args.hevc_source_ratio)
        preset["estimate_report"] = str(estimate_path)
        quality = preset["resolved_quality"]
        print(
            f"compact-auto: MPEG-2 {preset['source_mpeg2_bps'] / 1_000_000:.3f} Mb/s -> "
            f"HEVC target {preset['target_hevc_bps'] / 1_000_000:.3f} Mb/s -> cq:{quality:g}"
        )
    result = plan_compact_layout(
        reports,
        Path(args.destination),
        quality=quality,
        audio_mode=args.audio_mode,
        stereo_audio_bitrate=parse_audio_bitrate(args.stereo_audio_bitrate),
        mono_audio_bitrate=parse_audio_bitrate(args.mono_audio_bitrate),
        quality_preset=preset,
        quality_by_vts=quality_by_vts,
    )
    summary = result["summary"]
    print(
        f"Compact layout planned at exact video control {quality}: "
        f"VOBUs={summary['vobus']:,} sectors={summary['original_sectors']:,} -> "
        f"{summary['compact_sectors']:,} saved={summary['saved_percent']:.3f}%"
    )
    print(
        f"VOBUs shrinking={summary['vobus_shrinking']:,} "
        f"expanding={summary['vobus_expanding']:,}; no quality fallback allowed"
    )
    if args.audio_mode == "compact-stereo":
        print(
            "Audio policy: compact-stereo requested; exact sector counts follow "
            "the validated per-domain audio tasks"
        )
    else:
        print("Audio policy: passthrough (byte-exact source packets)")
    return 0


def cmd_plan_compact_audio(args: argparse.Namespace) -> int:
    result = plan_compact_audio_layout(
        Path(args.layout),
        Path(args.audio_report),
        Path(args.destination),
        domain=args.domain,
        vts=args.vts,
    )
    summary = result["summary"]
    print(
        f"Compact audio layout: sectors={summary['compact_sectors']:,} "
        f"audio={summary['original_audio_sectors']:,} -> {summary['compact_audio_sectors']:,} "
        f"saved={summary['saved_percent']:.3f}% total"
    )
    return 0


def cmd_prototype_compact_domain(args: argparse.Namespace) -> int:
    result = prototype_compact_domain(
        Path(args.layout), domain=args.domain, vts=args.vts, destination=Path(args.destination)
    )
    summary = result["summary"]
    print(
        f"Compact {args.domain} VTS {args.vts} prototype: "
        f"sectors={summary['original_sectors']:,} -> {summary['compact_sectors']:,} "
        f"saved={summary['saved_percent']:.3f}% VOBUs={summary['vobus']}"
    )
    return 0


def cmd_prototype_compact_domain_batch(args: argparse.Namespace) -> int:
    try:
        tasks = json.loads(Path(args.tasks).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"Could not read compact-domain batch tasks: {args.tasks}") from exc
    if not isinstance(tasks, list):
        raise PipelineError("Compact-domain batch tasks must be a JSON array")
    result = prototype_compact_domain_batch(
        Path(args.layout), tasks, Path(args.report)
    )
    print(
        f"Compact-domain batch passed: domains={result['summary']['domains']} "
        f"sectors={result['summary']['compact_sectors']:,} layout_reads=1"
    )
    return 0


def cmd_prototype_compact_audio(args: argparse.Namespace) -> int:
    result = prototype_compact_audio(
        Path(args.source),
        Path(args.destination),
        stereo_bitrate=parse_audio_bitrate(args.stereo_audio_bitrate),
        mono_bitrate=parse_audio_bitrate(args.mono_audio_bitrate),
        workers=args.audio_workers,
    )
    summary = result["summary"]
    source_bytes = summary.get("estimated_source_elementary_bytes")
    saved_percent = summary.get("estimated_saved_percent")
    print(
        f"Compact stereo AC-3 prototype: tracks={summary['tracks']} "
        f"bytes={source_bytes if source_bytes is not None else 'unknown'} -> "
        f"{summary['output_elementary_bytes']:,} "
        f"saved={f'{saved_percent:.3f}%' if saved_percent is not None else 'unknown'}"
    )
    return 0


def cmd_prototype_compact_audio_domain(args: argparse.Namespace) -> int:
    result = prototype_compact_audio_domain(
        Path(args.layout),
        Path(args.destination),
        domain=args.domain,
        vts=args.vts,
        stereo_bitrate=parse_audio_bitrate(args.stereo_audio_bitrate),
        mono_bitrate=parse_audio_bitrate(args.mono_audio_bitrate),
        workers=args.audio_workers,
        audio_policy=(Path(args.audio_policy) if args.audio_policy else None),
    )
    summary = result["summary"]
    print(
        f"Compact audio domain VTS {args.vts}: cells={summary['physical_cells']} "
        f"streams={summary['audio_streams']} "
        f"cell-stream encodes={summary['encoded_cell_streams']}"
    )
    print(f"Report: {Path(args.destination).resolve() / 'compact-audio-domain-report.json'}")
    return 0


def cmd_prototype_compact_audio_batch(args: argparse.Namespace) -> int:
    result = prototype_compact_audio_batch(
        Path(args.layout),
        Path(args.destination),
        vts_values=args.vts,
        stereo_bitrate=parse_audio_bitrate(args.stereo_audio_bitrate),
        mono_bitrate=parse_audio_bitrate(args.mono_audio_bitrate),
        audio_workers=args.audio_workers,
        domain_workers=args.domain_workers,
        audio_policy=(Path(args.audio_policy) if args.audio_policy else None),
    )
    summary = result["summary"]
    print(
        f"Compact audio batch: domains={summary['title_domains']} "
        f"cells={summary['physical_cells']} "
        f"cell-stream encodes={summary['encoded_cell_streams']}"
    )
    print(f"Report: {Path(args.destination).resolve() / 'compact-audio-batch-report.json'}")
    return 0


def cmd_estimate_quality(args: argparse.Namespace) -> int:
    result = estimate_equivalent_quality(
        [Path(path) for path in args.inputs],
        ratio=args.hevc_source_ratio,
        destination=Path(args.report) if args.report else None,
    )
    source = result["source"]
    standard = result["standard_equivalence"]
    cq = result["cq_estimate"]
    print(
        f"MPEG-2 elementary video: {source['average_video_bps'] / 1_000_000:.3f} Mb/s; "
        f"standard-equivalent HEVC: {standard['target_hevc_bps'] / 1_000_000:.3f} Mb/s"
    )
    print(
        f"Estimated P6 HandBrake-style quality: cq:{cq['estimated_cq']:.2f} "
        f"(nearest half cq:{cq['nearest_half_cq']:g}, integer cq:{cq['nearest_integer_cq']:g})"
    )
    print("CQ is content-dependent; a representative sample verification remains required")
    return 0


def cmd_resolve_quality_policy(args: argparse.Namespace) -> int:
    plan = json.loads(Path(args.plan).resolve().read_text(encoding="utf-8-sig"))
    scan = json.loads(Path(args.scan).resolve().read_text(encoding="utf-8-sig"))
    result = resolve_disc_quality_policy(
        plan,
        scan,
        quality=args.quality,
        target_bitrate_multiplier=args.target_bitrate_multiplier,
        bitrate_mode=args.bitrate_mode,
        main_title_quality=args.main_title_quality,
        top_n_quality=args.top_n_quality,
        top_n_count=args.top_n_count,
        destination=Path(args.destination),
    )
    print(
        f"Quality policy: general={result['general']['resolved']} "
        f"multiplier={result['target_bitrate']['bitrate_multiplier']:g}x "
        f"mode={result['target_bitrate']['mode'].upper()} "
        f"override={result['title_override']['mode']}"
    )
    return 0


def cmd_resolve_audio_policy(args: argparse.Namespace) -> int:
    overrides: dict[str, str] = {}
    for value in args.language or []:
        if "=" not in value:
            raise PipelineError(f"Invalid language audio override: {value!r}")
        language, action = value.split("=", 1)
        overrides[language.strip()] = action.strip()
    result = resolve_audio_policy(
        Path(args.source), Path(args.destination),
        default_mode=args.default_mode,
        language_overrides=overrides,
    )
    print(
        f"Audio policy: default={result['default_mode']} "
        f"resolved={result['resolved_pipeline_mode']} "
        f"overrides={len(result['language_overrides'])}"
    )
    return 0


def cmd_rewrite_compact_vts_ifo(args: argparse.Namespace) -> int:
    result = rewrite_compact_vts_ifo(
        Path(args.layout),
        vts=args.vts,
        source_ifo=Path(args.source_ifo),
        destination_ifo=Path(args.destination_ifo),
        destination_bup=Path(args.destination_bup) if args.destination_bup else None,
    )
    summary = result["summary"]
    print(
        f"Compact VTS {args.vts} IFO: title sectors "
        f"{summary['title_vob_sectors']['old']:,} -> {summary['title_vob_sectors']['new']:,}; "
        f"PGC cells={summary['pgcit']['cells']} VOBUs={summary['vobu_admap_entries']} "
        f"time-map entries={summary['time_maps']['entries']}"
    )
    return 0


def cmd_rewrite_compact_vmgi(args: argparse.Namespace) -> int:
    result = rewrite_compact_vmgi(
        Path(args.layout),
        source_ifo=Path(args.source_ifo),
        destination_ifo=Path(args.destination_ifo),
        destination_bup=Path(args.destination_bup) if args.destination_bup else None,
    )
    summary = result["summary"]
    print(
        "Compact VMGI: menu sectors "
        f"{summary['menu_vob_sectors']['old']:,} -> "
        f"{summary['menu_vob_sectors']['new']:,}; "
        f"first-play cells={summary['first_play_pgc']['cells']} "
        f"VOBUs={summary['menu_tables']['vobu_admap_entries']}"
    )
    return 0


def cmd_compare_navigation(args: argparse.Namespace) -> int:
    result = compare_vlc_navigation_logs(
        Path(args.source_log),
        Path(args.output_log),
        compact_relocation=args.compact_relocation,
    )
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"DVDNAV source/output trace: {'PASS' if result['passed'] else 'FAIL'}")
        print(
            f"  source events={result['source_event_count']} fields={result['source_trace_count']}  "
            f"output events={result['output_event_count']} fields={result['output_trace_count']}"
        )
        if result["first_mismatch"] is not None:
            print(f"  first mismatch: {result['first_mismatch']}")
    return 0 if result["passed"] else 2


def cmd_verify_stage(args: argparse.Namespace) -> int:
    report_path = Path(args.report)
    stage = json.loads(report_path.read_text(encoding="utf-8"))
    if stage.get("schema") == "dvd2hevc-complete-staged-dvd-v0":
        result = verify_complete_stage(report_path)
    else:
        result = verify_staged_title(report_path)
    print(f"Staged replacement read-back: PASS ({len(result['cells'])} cells)")
    return 0


def cmd_title_status(args: argparse.Namespace) -> int:
    if not getattr(args, "workspace", None):
        return cmd_job_status(args)
    path = Path(args.workspace)
    if not path.exists():
        return cmd_job_status(args)
    if path.is_dir():
        report_path = path / "status.json"
        if not report_path.is_file():
            report_path = path / "title-report.json"
        if not report_path.is_file():
            report_path = path / "menu-report.json"
    else:
        report_path = path
    if not report_path.is_file():
        raise PipelineError(f"Title report not found: {report_path}")
    # Windows PowerShell 5's UTF8 Set-Content emits a BOM. Background runners
    # use it for atomic status files, so accept both BOM and BOM-less JSON.
    report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    if report.get("schema") == "dvd2hevc-phase7-vts-status-v0":
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(f"Phase 7 VTS conversion status: {report.get('state', 'unknown')}")
            print(
                f"  tasks: {int(report.get('completed_tasks', 0))}/"
                f"{int(report.get('total_tasks', 0))}  step: {report.get('step')}"
            )
            print(f"  {report.get('message', '')}")
            if report.get("output"):
                print(f"  output: {report['output']}")
            print(f"  status: {report_path.resolve()}")
        return 2 if report.get("state") == "failed" else 0
    if report.get("schema") == "dvd2hevc-phase7-remaining-status-v0":
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(f"Phase 7 remaining domains: {report.get('state', 'unknown')}")
            print(f"  step: {report.get('step')}  {report.get('message', '')}")
            print(
                f"  title reports={len(report.get('title_reports') or [])} "
                f"combined VTS={len(report.get('combined_reports') or [])} "
                f"menu reports={len(report.get('menu_reports') or [])}"
            )
            print(f"  status: {report_path.resolve()}")
        return 2 if report.get("state") == "failed" else 0
    if report.get("schema") == "dvd2hevc-phase7-full-disc-status-v0":
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(f"Phase 7 full-disc build: {report.get('state', 'unknown')}")
            print(f"  stage: {report.get('stage')}  {report.get('message', '')}")
            if report.get("final_stage"):
                print(f"  final stage: {report['final_stage']}")
            print(f"  output ISO: {report.get('output_iso')}")
            print(f"  status: {report_path.resolve()}")
        return 2 if report.get("state") == "failed" else 0
    if report.get("schema") == "dvd2hevc-phase7-general-disc-status-v0":
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(f"Phase 7 generalized disc build: {report.get('state', 'unknown')}")
            print(f"  stage: {report.get('stage')}  {report.get('message', '')}")
            physical = report.get("physical_vts") or []
            completed = report.get("physical_reports") or []
            if physical:
                print(f"  physical VTS reports: {len(completed)}/{len(physical)}")
            nested_paths = list((report_path.parent / "physical").glob("vts*/status.json"))
            nested_paths.append(report_path.parent / "ordinary-and-menus" / "status.json")
            nested_reports: list[dict[str, Any]] = []
            for nested_path in nested_paths:
                if not nested_path.is_file():
                    continue
                try:
                    nested = json.loads(nested_path.read_text(encoding="utf-8-sig"))
                except (OSError, json.JSONDecodeError):
                    continue
                if nested.get("state") == "running":
                    nested_reports.append(nested)
            if nested_reports:
                nested = max(nested_reports, key=lambda value: str(value.get("updated", "")))
                if nested.get("schema") == "dvd2hevc-phase7-vts-status-v0":
                    print(
                        f"  active VTS {int(nested.get('vts', 0))}: "
                        f"{int(nested.get('completed_tasks', 0))}/"
                        f"{int(nested.get('total_tasks', 0))} tasks"
                    )
                elif nested.get("schema") == "dvd2hevc-phase7-remaining-status-v0":
                    active_step = str(nested.get("step") or "")
                    print(f"  active ordinary/menu step: {active_step}")
                    if active_step.startswith("convert-title-"):
                        try:
                            title_number = int(active_step.rsplit("-", 1)[1])
                            plan = json.loads(Path(report["plan"]).read_text(encoding="utf-8-sig"))
                            task = next(
                                row for row in plan.get("title_tasks", [])
                                if int(row.get("title", 0)) == title_number
                            )
                            active_report_path = (
                                report_path.parent / "ordinary-and-menus" /
                                str(task["workspace"]) / "title-report.json"
                            )
                            active_report = json.loads(
                                active_report_path.read_text(encoding="utf-8-sig")
                            )
                            print(
                                f"  active title cells: {len(active_report.get('cells') or [])}/"
                                f"{int(active_report.get('unique_cell_count') or 0)}"
                            )
                        except (OSError, ValueError, KeyError, StopIteration, json.JSONDecodeError):
                            pass
            if report.get("final_stage"):
                print(f"  final stage: {report['final_stage']}")
            print(f"  output ISO: {report.get('output_iso')}")
            print(f"  status: {report_path.resolve()}")
        return 2 if report.get("state") == "failed" else 0
    if report.get("schema") == "dvd2hevc-phase7-compact-vts-test-status-v0":
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(f"Phase 7 compact VTS test: {report.get('state', 'unknown')}")
            print(f"  step: {report.get('step')}  {report.get('message', '')}")
            print(f"  output ISO: {report.get('output_iso')}")
            print(f"  status: {report_path.resolve()}")
        return 2 if report.get("state") == "failed" else 0
    if args.json:
        print(json.dumps(report, indent=2))
        return 2 if report.get("status") == "failed" else 0
    completed = len(report.get("cells", []))
    total = int(report.get("unique_cell_count") or 0)
    label = "Menu" if report.get("domain") else "Title"
    print(f"{label} conversion status: {report.get('status', 'unknown')}")
    print(f"  cells: {completed}/{total}")
    if report.get("cells"):
        last = report["cells"][-1]
        print(f"  last passed: {last.get('name')}")
    if report.get("status") == "passed" and report.get("summary"):
        summary = report["summary"]
        print(f"  frames: {summary.get('output_frames'):,}  VOBUs: {summary.get('vobus_converted'):,}")
    if report.get("error"):
        print(f"  error: {report['error']}")
    print(f"  report: {report_path.resolve()}")
    return 2 if report.get("status") == "failed" else 0


def cmd_combine_titles(args: argparse.Namespace) -> int:
    result = combine_title_reports([Path(path) for path in args.reports], Path(args.destination))
    summary = result["summary"]
    print(
        f"Combined VTS {result['vts']}: cells={summary['physical_cells']}/{summary['expected_physical_cells']} "
        f"VOBUs={summary['vobus']}/{summary['expected_vobus']}"
    )
    print(f"Report: {Path(args.destination).resolve()}")
    return 0


def add_scan_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("source", help="Path to a decrypted DVD ISO backup")
    parser.add_argument("--quick", action="store_true", help="List the ISO structure without reading every VOB sector")
    parser.add_argument("--no-handbrake", action="store_true", help="Skip logical title discovery")
    parser.add_argument("--no-native", action="store_true", help="Skip the native libdvdread physical graph")
    parser.add_argument("--min-title-duration", type=int, default=1, help="Minimum HandBrake title duration in seconds (default: 1)")
    parser.add_argument("--report", help="Write the complete JSON report to this path")
    parser.add_argument("--json", action="store_true", help="Print the complete JSON report")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dvd2hevc",
        description="Convert decrypted DVD ISO backups to compact HEVC while preserving the full-disc experience.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "quick start:\n"
            "  python dvd2hevc.py tools\n"
            "  python dvd2hevc.py auto MOVIE.iso --dry-run\n"
            "  python dvd2hevc.py start MOVIE.iso --quality cq:24\n"
            "  python dvd2hevc.py status --watch\n\n"
            "The low-level commands below remain available for diagnostics and development."
        ),
    )
    parser.add_argument("--version", action="version", version=f"DVD2HEVC {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    add_user_commands(commands)
    add_diagnose_command(commands)
    add_build_inspector_command(commands)
    add_compatibility_registry_commands(commands)
    p_tools = commands.add_parser("tools", help="Show available dependencies")
    p_tools.add_argument('--output-format',choices=('uhd-bd','dvd-hevc'),default='uhd-bd')
    p_tools.add_argument("--encoder", choices=HEVC_ENCODERS, default="hevc_nvenc", help="Runtime-probe this HEVC encoder")
    p_tools.add_argument("--probe-all-encoders", action="store_true", help="Runtime-probe every supported HEVC backend")
    p_tools.set_defaults(func=cmd_tools)
    p_scan = commands.add_parser("scan", help="Inspect a DVD ISO without modifying it")
    add_scan_options(p_scan)
    p_scan.set_defaults(func=cmd_scan)
    p_plan = commands.add_parser("plan", help="Create an initial per-VOB conversion plan")
    add_scan_options(p_plan)
    p_plan.set_defaults(func=cmd_plan)
    p_compat = commands.add_parser(
        "compatibility-plan",
        help="Plan Phase 7 work from the physical graph without starting encodes",
    )
    p_compat.add_argument("source", help="Path to the source ISO")
    p_compat.add_argument("destination", help="New compatibility-plan JSON report")
    p_compat.add_argument("--scan-report", help="Reuse an existing scan JSON with a native graph")
    p_compat.add_argument("--json", action="store_true", help="Print the complete compatibility plan")
    p_compat.set_defaults(func=cmd_compatibility_plan)
    p_interleaved = commands.add_parser(
        "plan-interleaved",
        help="Plan complete physical VTS tasks, including discontinuous branching extents",
    )
    p_interleaved.add_argument("source", help="Path to a decrypted DVD ISO backup")
    p_interleaved.add_argument("vts", type=int, help="Video title set containing interleaved cells")
    p_interleaved.add_argument("destination", help="Where to write the interleaved-unit JSON plan")
    p_interleaved.add_argument("--scan-report", help="Reuse an authoritative native scan JSON")
    p_interleaved.add_argument("--json", action="store_true", help="Print the complete plan as JSON")
    p_interleaved.set_defaults(func=cmd_plan_interleaved)
    p_extract_interleaved = commands.add_parser(
        "extract-interleaved-cell", help="Concatenate one planned branching cell's physical extents"
    )
    p_extract_interleaved.add_argument("plan", help="Passed plan-interleaved JSON report")
    p_extract_interleaved.add_argument("pgc", type=int, help="One-based title PGC number")
    p_extract_interleaved.add_argument("cell", type=int, help="One-based PGC cell number")
    p_extract_interleaved.add_argument("destination", help="Concatenated diagnostic VOB path")
    p_extract_interleaved.add_argument("--report", help="Write the extraction map and statistics to JSON")
    p_extract_interleaved.set_defaults(func=cmd_extract_interleaved_cell)
    p_prototype_interleaved = commands.add_parser(
        "prototype-interleaved-cell",
        help="Encode, repack, and split back one planned branching cell",
    )
    p_prototype_interleaved.add_argument("plan", help="Passed plan-interleaved JSON report")
    p_prototype_interleaved.add_argument("pgc", type=int, help="One-based title PGC number")
    p_prototype_interleaved.add_argument("cell", type=int, help="One-based PGC cell number")
    p_prototype_interleaved.add_argument("workspace", help="Diagnostic encode and report directory")
    p_prototype_interleaved.add_argument("--quality-values", default="cq:27", help="Ordered cq:N, vbr:BPS, or cbr:BPS controls")
    p_prototype_interleaved.add_argument("--encoder", choices=HEVC_ENCODERS, default="hevc_nvenc")
    p_prototype_interleaved.add_argument("--preset", default="p6", help="Encoder efficiency preset (default: p6)")
    p_prototype_interleaved.add_argument("--cadence", choices=("auto", "progressive", "deinterlace50"), default="auto")
    p_prototype_interleaved.add_argument(
        "--ambiguous-cadence", choices=("fail", "progressive", "deinterlace50"), default="deinterlace50"
    )
    p_prototype_interleaved.add_argument("--allow-compact-expansion", action="store_true")
    p_prototype_interleaved.add_argument(
        "--prefer-compact-input", action="store_true",
        help="Keep exact video-only HEVC for final compaction instead of making a sector-preserving intermediate",
    )
    p_prototype_interleaved.set_defaults(func=cmd_prototype_interleaved_cell)
    p_convert_interleaved = commands.add_parser(
        "convert-interleaved-vts",
        help="Resumably convert every unique physical task in a planned title VTS",
    )
    p_convert_interleaved.add_argument("plan", help="Passed plan-interleaved JSON report")
    p_convert_interleaved.add_argument("workspace", help="Resumable VTS task and report directory")
    p_convert_interleaved.add_argument("--quality-values", default="cq:27", help="Ordered cq:N, vbr:BPS, or cbr:BPS controls")
    p_convert_interleaved.add_argument("--encoder", choices=HEVC_ENCODERS, default="hevc_nvenc")
    p_convert_interleaved.add_argument("--preset", default="p6", help="Encoder efficiency preset (default: p6)")
    p_convert_interleaved.add_argument("--cadence", choices=("auto", "progressive", "deinterlace50"), default="auto")
    p_convert_interleaved.add_argument(
        "--ambiguous-cadence", choices=("fail", "progressive", "deinterlace50"), default="deinterlace50"
    )
    p_convert_interleaved.add_argument("--allow-compact-expansion", action="store_true")
    p_convert_interleaved.add_argument(
        "--prefer-compact-input", action="store_true",
        help="Use compact video intermediates and defer DVD sector construction to final compaction",
    )
    p_convert_interleaved.add_argument(
        "--pipeline-depth", type=int, choices=range(1, 9), default=1,
        help="Bounded encode/validation pipeline depth; managed execution uses at most two workers",
    )
    p_convert_interleaved.add_argument(
        "--audio-prefetch-root",
        help="Optional compact-audio root populated concurrently for later layout reuse",
    )
    p_convert_interleaved.add_argument(
        "--audio-policy", help="Resolved per-language compact-audio policy JSON"
    )
    p_convert_interleaved.add_argument("--stereo-audio-bitrate", default="256k")
    p_convert_interleaved.add_argument("--mono-audio-bitrate", default="128k")
    p_convert_interleaved.add_argument(
        "--audio-workers", type=int, choices=range(1, 9), default=2,
        help="Maximum compact-audio tracks encoded concurrently during prefetch",
    )
    p_convert_interleaved.set_defaults(func=cmd_convert_interleaved_vts)
    p_extract = commands.add_parser("extract-cell", help="Extract a physical title-domain sector range for diagnostics")
    p_extract.add_argument("source", help="Path to a decrypted DVD ISO backup")
    p_extract.add_argument("vts", type=int, help="Video title set number")
    p_extract.add_argument("first_sector", type=int, help="First title-domain sector, inclusive")
    p_extract.add_argument("last_sector", type=int, help="Last title-domain sector, inclusive")
    p_extract.add_argument("destination", help="New staging VOB path")
    p_extract.add_argument("--json", action="store_true", help="Print the extraction report as JSON")
    p_extract.set_defaults(func=cmd_extract_cell)
    p_vlc_log = commands.add_parser("validate-vlc-log", help="Check a verbose patched-VLC DVDNAV playback log")
    p_vlc_log.add_argument("log", help="Path to the verbose VLC log")
    p_vlc_log.add_argument("--json", action="store_true", help="Print the validation report as JSON")
    p_vlc_log.add_argument("--minimum-cell-changes", type=int, default=1, help="Required DVDNAV cell transitions (default: 1)")
    p_vlc_log.set_defaults(func=cmd_validate_vlc_log)
    p_vlc_menu = commands.add_parser(
        "validate-vlc-menu-equivalence",
        help="Compare source/output navigation-only DVD menu probes",
    )
    p_vlc_menu.add_argument("source_log", help="Verbose VLC log from the source DVD")
    p_vlc_menu.add_argument("output_log", help="Verbose VLC log from the HEVC DVD")
    p_vlc_menu.add_argument("--minimum-cell-changes", type=int, default=1)
    p_vlc_menu.add_argument("--report", help="Optional JSON result path")
    p_vlc_menu.add_argument("--json", action="store_true")
    p_vlc_menu.set_defaults(func=cmd_validate_vlc_menu_equivalence)
    p_convert = commands.add_parser("convert-title", help="Convert one single-PGC DVD title into validated staged cells")
    p_convert.add_argument("source", help="Path to a decrypted DVD ISO backup")
    p_convert.add_argument("title", type=int, help="Global DVD title number")
    p_convert.add_argument("workspace", help="Directory for staged cells, logs, and reports")
    p_convert.add_argument("--encoder", choices=HEVC_ENCODERS, default="hevc_nvenc", help="HEVC encoder (default: hevc_nvenc)")
    p_convert.add_argument("--cadence", choices=("auto", "interlaced", "progressive", "deinterlace50"), default="auto", help="Output cadence policy (default: auto)")
    p_convert.add_argument("--ambiguous-cadence", choices=("fail", "progressive", "deinterlace50"), default="deinterlace50", help="Auto-mode fallback for inconclusive short/mixed cells")
    p_convert.add_argument("--preset", default="p6", help="Encoder efficiency preset p1-p7 (default: p6)")
    p_convert.add_argument("--quality-values", "--crf-values", dest="quality_values", default="cq:20,cq:22,cq:24,cq:26,cq:28,cq:30", help="Ordered cq:N, vbr:BPS, or cbr:BPS controls")
    p_convert.add_argument(
        "--quality-preset", choices=("manual", "compact-auto"), default="manual",
        help="compact-auto uses the measured DVD bitrate and forces NVENC P6",
    )
    p_convert.add_argument(
        "--quality-estimate", help="JSON report from estimate-quality, required by compact-auto",
    )
    p_convert.add_argument("--threads", type=int, default=4, help="x265 worker pool size (default: 4)")
    p_convert.add_argument(
        "--allow-compact-expansion", action="store_true",
        help="Keep the requested encode when compaction can allocate extra sectors to overflowing VOBUs",
    )
    p_convert.add_argument(
        "--prefer-compact-input", action="store_true",
        help="Validate exact HEVC directly and construct DVD sectors once during final compaction",
    )
    p_convert.add_argument("--audio-prefetch-root", help=argparse.SUPPRESS)
    p_convert.add_argument("--audio-policy", help=argparse.SUPPRESS)
    p_convert.add_argument("--stereo-audio-bitrate", default="256k", help=argparse.SUPPRESS)
    p_convert.add_argument("--mono-audio-bitrate", default="128k", help=argparse.SUPPRESS)
    p_convert.add_argument(
        "--audio-workers", type=int, choices=range(1, 9), default=2,
        help=argparse.SUPPRESS,
    )
    p_convert.set_defaults(func=cmd_convert_title)
    p_menu = commands.add_parser("convert-menu", help="Convert every physical cell in a VMG or VTS menu domain")
    p_menu.add_argument("source", help="Path to a decrypted DVD ISO backup")
    p_menu.add_argument("domain", choices=("vmg_menu", "vts_menu"), help="Menu domain to convert")
    p_menu.add_argument("vts", type=int, help="0 for VMG, otherwise the VTS number")
    p_menu.add_argument("workspace", help="Directory for staged cells, logs, and reports")
    p_menu.add_argument("--encoder", choices=HEVC_ENCODERS, default="hevc_nvenc")
    p_menu.add_argument("--cadence", choices=("auto", "progressive", "deinterlace50"), default="auto")
    p_menu.add_argument("--ambiguous-cadence", choices=("fail", "progressive", "deinterlace50"), default="progressive")
    p_menu.add_argument("--preset", default="p6", help="Encoder efficiency preset (default: p6)")
    p_menu.add_argument("--quality-values", default="cq:20,cq:22,cq:24,cq:26,cq:28,cq:30", help="Ordered cq:N, vbr:BPS, or cbr:BPS controls")
    p_menu.add_argument(
        "--quality-preset", choices=("manual", "compact-auto"), default="manual",
        help="compact-auto encodes only the measured half-step CQ with NVENC P6",
    )
    p_menu.add_argument(
        "--quality-estimate", help="JSON report from estimate-quality, required by compact-auto",
    )
    p_menu.add_argument(
        "--allow-compact-expansion", action="store_true",
        help="Keep exact menu encodes and rebalance VOBU sectors during compact VTS staging",
    )
    p_menu.set_defaults(func=cmd_convert_menu)
    p_stage = commands.add_parser("stage-title", help="Apply a passed title report to a new DVD folder")
    p_stage.add_argument("report", help="Passed convert-title JSON report")
    p_stage.add_argument("destination", help="New DVD folder staging destination")
    p_stage.set_defaults(func=cmd_stage_title)
    p_stage_disc = commands.add_parser("stage-disc", help="Apply title and menu reports to a complete DVD folder")
    p_stage_disc.add_argument("destination", help="New complete DVD folder staging destination")
    p_stage_disc.add_argument("reports", nargs="*", help="Passed title/menu conversion reports")
    p_stage_disc.add_argument(
        "--source", help="Source ISO, required when creating an unmodified compact-layout base stage",
    )
    p_stage_disc.set_defaults(func=cmd_stage_disc)
    p_extend_stage = commands.add_parser(
        "extend-stage", help="Add disjoint conversions to a passed complete stage with hardlink copy-on-write"
    )
    p_extend_stage.add_argument("base_stage", help="Passed complete sector-preserving stage")
    p_extend_stage.add_argument("destination", help="New extended complete stage")
    p_extend_stage.add_argument("reports", nargs="+", help="Additional passed title/menu reports")
    p_extend_stage.set_defaults(func=cmd_extend_stage)
    p_stage_compact = commands.add_parser(
        "stage-compact-vts", help="Create a validated hardlinked stage with one compact VTS"
    )
    p_stage_compact.add_argument("base_stage")
    p_stage_compact.add_argument("layout")
    p_stage_compact.add_argument("vts", type=int)
    p_stage_compact.add_argument("compact_vob")
    p_stage_compact.add_argument("compact_ifo")
    p_stage_compact.add_argument("compact_bup")
    p_stage_compact.add_argument("destination")
    p_stage_compact.add_argument(
        "--compact-menu-vob", help="Optional compact VTS_nn_0.VOB when the layout includes vts_menu",
    )
    p_stage_compact.set_defaults(func=cmd_stage_compact_vts)
    p_stage_compact_vmg = commands.add_parser(
        "stage-compact-vmg",
        help="Create a validated hardlinked stage with a compact VIDEO_TS.VOB",
    )
    p_stage_compact_vmg.add_argument("base_stage")
    p_stage_compact_vmg.add_argument("layout")
    p_stage_compact_vmg.add_argument("compact_vob")
    p_stage_compact_vmg.add_argument("compact_ifo")
    p_stage_compact_vmg.add_argument("compact_bup")
    p_stage_compact_vmg.add_argument("destination")
    p_stage_compact_vmg.set_defaults(func=cmd_stage_compact_vmg)
    p_author = commands.add_parser("author-iso", help="Create a DVD-Video ordered UDF ISO from a passed stage")
    p_author.add_argument("stage", help="Complete staged DVD root")
    p_author.add_argument("destination", help="New ISO path")
    p_author.add_argument("--label", default="DVD2HEVC", help="ASCII volume label, up to 32 characters")
    p_author.add_argument("--wsl-distro", default="Ubuntu-24.04", help="WSL distro containing genisoimage")
    p_author.add_argument("--report", help="Write the authoring report to this path")
    p_author.add_argument("--log", help="Write the ISO-authoring tool log to this path")
    p_author.set_defaults(func=cmd_author_iso)
    p_verify_iso = commands.add_parser("verify-iso", help="Hash an authored UDF ISO and compare its DVD graph")
    p_verify_iso.add_argument("report", help="JSON report emitted by author-iso")
    p_verify_iso.add_argument("--output-report", help="Write the verification report to this path")
    p_verify_iso.set_defaults(func=cmd_verify_iso)
    p_repair_psm = commands.add_parser(
        "repair-css-safe-psm",
        help="Copy an older DVD2HEVC image while preventing false libdvdcss decryption",
    )
    p_repair_psm.add_argument("source", help="Existing sector-aligned DVD2HEVC ISO")
    p_repair_psm.add_argument("destination", help="New repaired ISO path")
    p_repair_psm.add_argument("--report", help="Write the repair summary to JSON")
    p_repair_psm.set_defaults(func=cmd_repair_css_safe_psm)
    p_verify_psm = commands.add_parser(
        "verify-css-safe-psm",
        help="Prove that a repaired image differs only in CSS-safe HEVC PSM bytes",
    )
    p_verify_psm.add_argument("source", help="Original DVD2HEVC ISO")
    p_verify_psm.add_argument("destination", help="Repaired DVD2HEVC ISO")
    p_verify_psm.add_argument("--report", help="Write the comparison summary to JSON")
    p_verify_psm.set_defaults(func=cmd_verify_css_safe_psm)
    p_audit_psm = commands.add_parser(
        "audit-css-safe-psm",
        help="Check that every DVD2HEVC program-stream map is safe from false CSS decryption",
    )
    p_audit_psm.add_argument("image", help="DVD2HEVC ISO to inspect")
    p_audit_psm.add_argument("--report", help="Write the audit summary to JSON")
    p_audit_psm.set_defaults(func=cmd_audit_css_safe_psm)
    p_compact = commands.add_parser("plan-compact", help="Plan variable-size VOBUs at one exact encode quality")
    p_compact.add_argument("destination", help="New compact-layout JSON report")
    p_compact.add_argument("reports", nargs="+", help="Complete passed title/menu domain reports")
    p_compact.add_argument(
        "--quality", type=parse_compact_quality, default=20,
        help="Exact HandBrake-style CQ (cq:20) or compact-auto to derive CQ from DVD bitrate",
    )
    p_compact.add_argument(
        "--hevc-source-ratio", type=float, default=0.25,
        help="compact-auto HEVC/MPEG-2 bitrate ratio (default: 0.25)",
    )
    p_compact.add_argument(
        "--quality-estimate",
        help="Reuse a JSON report from estimate-quality instead of measuring again",
    )
    p_compact.add_argument(
        "--quality-report",
        help="Where compact-auto writes its new measurement (default: beside the layout)",
    )
    p_compact.add_argument(
        "--disc-quality-policy",
        help="Resolved full-disc general/VTS quality policy",
    )
    p_compact.add_argument(
        "--audio-mode", choices=("passthrough", "compact-stereo"), default="passthrough",
        help="Audio policy; passthrough remains the default",
    )
    p_compact.add_argument("--stereo-audio-bitrate", default="256k")
    p_compact.add_argument("--mono-audio-bitrate", default="128k")
    p_compact.set_defaults(func=cmd_plan_compact)
    p_compact_audio_plan = commands.add_parser(
        "plan-compact-audio",
        help="Apply validated compact AC-3 packet counts to an exact-CQ layout",
    )
    p_compact_audio_plan.add_argument("layout", help="Base plan-compact JSON report")
    p_compact_audio_plan.add_argument("audio_report", help="Passed compact-audio prototype report")
    p_compact_audio_plan.add_argument("destination", help="New combined video/audio compact layout")
    p_compact_audio_plan.add_argument("domain", choices=("title", "vts_menu", "vmg_menu"))
    p_compact_audio_plan.add_argument("vts", type=int)
    p_compact_audio_plan.set_defaults(func=cmd_plan_compact_audio)
    p_compact_domain = commands.add_parser(
        "prototype-compact-domain", help="Write a raw non-angle compact domain before IFO staging"
    )
    p_compact_domain.add_argument("layout", help="Passed plan-compact JSON report")
    p_compact_domain.add_argument("domain", choices=("title", "vts_menu", "vmg_menu"))
    p_compact_domain.add_argument("vts", type=int)
    p_compact_domain.add_argument("destination", help="New raw compact VOB path")
    p_compact_domain.set_defaults(func=cmd_prototype_compact_domain)
    p_compact_domain_batch = commands.add_parser(
        "prototype-compact-domain-batch",
        help="Write several raw compact domains while loading their layout once",
    )
    p_compact_domain_batch.add_argument("layout", help="Passed plan-compact JSON report")
    p_compact_domain_batch.add_argument("tasks", help="JSON array of domain/VTS/destination tasks")
    p_compact_domain_batch.add_argument("report", help="New resumable batch report")
    p_compact_domain_batch.set_defaults(func=cmd_prototype_compact_domain_batch)
    p_compact_audio = commands.add_parser(
        "prototype-compact-audio",
        help="Downmix each playable stream to validated DVD-compatible AC-3",
    )
    p_compact_audio.add_argument("source", help="Extracted DVD cell/domain VOB to inspect")
    p_compact_audio.add_argument("destination", help="Directory for AC-3 tracks and validation report")
    p_compact_audio.add_argument(
        "--stereo-audio-bitrate", default="256k", help="Stereo AC-3 bitrate (default: 256k)"
    )
    p_compact_audio.add_argument(
        "--mono-audio-bitrate", default="128k", help="Mono AC-3 bitrate (default: 128k)"
    )
    p_compact_audio.add_argument("--audio-workers", type=int, default=2, choices=range(1, 9))
    p_compact_audio.set_defaults(func=cmd_prototype_compact_audio)
    p_compact_audio_domain = commands.add_parser(
        "prototype-compact-audio-domain",
        help="Downmix every physical cell/audio stream in one compact title domain",
    )
    p_compact_audio_domain.add_argument("layout", help="Passed plan-compact JSON report")
    p_compact_audio_domain.add_argument("domain", choices=("title",))
    p_compact_audio_domain.add_argument("vts", type=int)
    p_compact_audio_domain.add_argument("destination", help="Resumable compact-audio domain workspace")
    p_compact_audio_domain.add_argument("--stereo-audio-bitrate", default="256k")
    p_compact_audio_domain.add_argument("--mono-audio-bitrate", default="128k")
    p_compact_audio_domain.add_argument("--audio-workers", type=int, default=2, choices=range(1, 9))
    p_compact_audio_domain.add_argument("--audio-policy")
    p_compact_audio_domain.set_defaults(func=cmd_prototype_compact_audio_domain)
    p_compact_audio_batch = commands.add_parser(
        "prototype-compact-audio-batch",
        help="Encode compact audio for several title sets with bounded parallel lanes",
    )
    p_compact_audio_batch.add_argument("layout", help="Passed base compact layout")
    p_compact_audio_batch.add_argument("destination", help="Resumable compact-audio batch workspace")
    p_compact_audio_batch.add_argument("--vts", type=int, nargs="+", help="Title sets; default: all")
    p_compact_audio_batch.add_argument("--stereo-audio-bitrate", default="256k")
    p_compact_audio_batch.add_argument("--mono-audio-bitrate", default="128k")
    p_compact_audio_batch.add_argument("--audio-workers", type=int, default=2, choices=range(1, 9))
    p_compact_audio_batch.add_argument("--domain-workers", type=int, default=2, choices=range(1, 9))
    p_compact_audio_batch.add_argument("--audio-policy")
    p_compact_audio_batch.set_defaults(func=cmd_prototype_compact_audio_batch)
    p_quality = commands.add_parser(
        "estimate-quality",
        help="Derive standard-equivalent HEVC bitrate and an empirical NVENC CQ",
    )
    p_quality.add_argument("inputs", nargs="+", help="Source cell VOBs or conversion/layout JSON reports")
    p_quality.add_argument(
        "--hevc-source-ratio", type=float, default=0.25,
        help="HEVC/MPEG-2 equal-quality bitrate ratio (default: 0.25)",
    )
    p_quality.add_argument("--report", help="Write the measurement and CQ estimate to JSON")
    p_quality.set_defaults(func=cmd_estimate_quality)
    p_quality_policy = commands.add_parser(
        "resolve-quality-policy",
        help=argparse.SUPPRESS,
    )
    p_quality_policy.add_argument("plan")
    p_quality_policy.add_argument("scan")
    p_quality_policy.add_argument("destination")
    p_quality_policy.add_argument("--quality", default="target-bitrate")
    p_quality_policy.add_argument("--target-bitrate-multiplier", type=float, default=1.0)
    p_quality_policy.add_argument("--bitrate-mode", choices=("vbr", "cbr"), default="vbr")
    p_quality_policy.add_argument(
        "--auto-cq-multiplier", dest="target_bitrate_multiplier", type=float,
        help=argparse.SUPPRESS,
    )
    p_quality_policy.add_argument("--main-title-quality")
    p_quality_policy.add_argument("--top-n-quality")
    p_quality_policy.add_argument("--top-n-count", type=int, default=0)
    p_quality_policy.set_defaults(func=cmd_resolve_quality_policy)
    p_audio_policy = commands.add_parser("resolve-audio-policy", help=argparse.SUPPRESS)
    p_audio_policy.add_argument("source")
    p_audio_policy.add_argument("destination")
    p_audio_policy.add_argument("--default-mode", choices=("passthrough", "compact-stereo"), default="passthrough")
    p_audio_policy.add_argument("--language", action="append", metavar="LANG=MODE")
    p_audio_policy.set_defaults(func=cmd_resolve_audio_policy)
    p_ifo = commands.add_parser(
        "rewrite-compact-vts-ifo",
        help="Rewrite a no-menu VTS IFO/BUP for a compact title domain",
    )
    p_ifo.add_argument("layout", help="Passed plan-compact JSON report")
    p_ifo.add_argument("vts", type=int, help="Video title set number")
    p_ifo.add_argument("source_ifo", help="Source VTS_nn_0.IFO")
    p_ifo.add_argument("destination_ifo", help="Rewritten VTS_nn_0.IFO")
    p_ifo.add_argument("--destination-bup", help="Optional matching VTS_nn_0.BUP")
    p_ifo.set_defaults(func=cmd_rewrite_compact_vts_ifo)
    p_vmgi = commands.add_parser(
        "rewrite-compact-vmgi",
        help="Rewrite VIDEO_TS.IFO/BUP for a compact VMG menu domain",
    )
    p_vmgi.add_argument("layout", help="Passed plan-compact JSON report")
    p_vmgi.add_argument("source_ifo", help="Source VIDEO_TS.IFO")
    p_vmgi.add_argument("destination_ifo", help="Rewritten VIDEO_TS.IFO")
    p_vmgi.add_argument("--destination-bup", help="Optional matching VIDEO_TS.BUP")
    p_vmgi.set_defaults(func=cmd_rewrite_compact_vmgi)
    p_nav = commands.add_parser("compare-navigation", help="Compare normalized source/output VLC DVDNAV logs")
    p_nav.add_argument("source_log", help="Verbose VLC log from the unmodified source")
    p_nav.add_argument("output_log", help="Verbose VLC log from the staged output")
    p_nav.add_argument("--json", action="store_true", help="Print the complete comparison report")
    p_nav.add_argument(
        "--compact-relocation", action="store_true",
        help="Ignore only physical sector fields expected to change under compaction",
    )
    p_nav.set_defaults(func=cmd_compare_navigation)
    p_verify_stage = commands.add_parser("verify-stage", help="Hash-check staged ranges against validated replacements")
    p_verify_stage.add_argument("report", help="dvd2hevc-stage-report.json in a staged DVD folder")
    p_verify_stage.set_defaults(func=cmd_verify_stage)
    p_status = commands.add_parser("status", help="Show a background job or low-level workspace status")
    p_status.add_argument("workspace", nargs="?", help="Job id, conversion workspace, or report; defaults to newest job")
    p_status.add_argument(
        "--watch", nargs="?", const=2.0, default=0.0, type=float, metavar="SECONDS",
        help="Refresh until the job finishes (default interval: 2 seconds)",
    )
    p_status.add_argument("--width", type=int, default=30, help="Progress-bar width")
    p_status.add_argument("--json", action="store_true", help="Print the complete title report")
    p_status.set_defaults(func=cmd_title_status)
    p_combine = commands.add_parser("combine-titles", help="Combine passed title reports into complete physical VTS coverage")
    p_combine.add_argument("destination", help="New combined VTS conversion report")
    p_combine.add_argument("reports", nargs="+", help="Passed title-report.json files")
    p_combine.set_defaults(func=cmd_combine_titles)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except DiscScanError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (PipelineError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
