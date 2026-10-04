"""Versioned format and runtime contracts shared by jobs, reports, and VLC."""

from __future__ import annotations

from typing import Any

from . import __version__


CONTRACT_SCHEMA = "dvd2hevc-capability-contract-v1"
FORMAT_PROFILE = "dvd-hevc-compact-v1"
PLAYER_ABI = "dvd2hevc-vlc3-dvdnav-hevc-v1"
JOB_SCHEMA = "dvd2hevc-job-v1"
COMPATIBILITY_REGISTRY_SCHEMA = "dvd2hevc-compatibility-registry-v1"


def capability_contract(output_format: str = 'dvd-hevc') -> dict[str, Any]:
    """Return the exact public capabilities expected by this converter build.

    This deliberately describes format invariants rather than individual disc
    workarounds.  It travels with new jobs and local compatibility snapshots so
    a resumed conversion cannot silently cross an incompatible implementation.
    """
    contract = {
        "schema": CONTRACT_SCHEMA,
        "application_version": __version__,
        "format_profile": FORMAT_PROFILE,
        "player_abi": PLAYER_ABI,
        "video": {
            "codec": "hevc",
            "profile": "main-8",
            "program_stream_type": "0x24",
            "random_access": "closed-idr-with-repeated-vps-sps-pps-per-vobu",
        },
        "compaction": {
            "title_vobs": True,
            "vts_menu_vobs": True,
            "vmg_menu_vob": True,
            "ifo_and_nav_relocation": True,
            "shared_physical_cells_encoded_once": True,
        },
        "cadence": "physical-cell-idet-bwdif-v1",
        "menu_still_guard": "single-picture-one-tick-eos-v1",
        "audio": {
            "passthrough": True,
            "compact_stereo_output": "dvd-ac3-48khz",
            "compact_stereo_inputs": ["ac3", "dts", "lpcm", "mpeg1", "mpeg2"],
        },
    }
    if output_format == 'uhd-bd':
        contract.update(format_profile='dvd-vm-bdj-hevc-v1',player_abi='stock-vlc-libbluray-bdj-v6',
                        navigation='dvd-vm-in-bdj-v6', multi_angle=False,random_shuffle=False,
                        subtitles='original-bdj-artwork-and-native-clear-pgs-selection')
        contract['video']={**contract['video'],'container':'bdmv-m2ts','udf_revision':'2.50'}
    return contract


def contract_compatible(value: object) -> bool:
    """Return whether a recorded contract is safe for this converter/player ABI."""
    if not isinstance(value, dict):
        return False
    return (
        value.get("schema") == CONTRACT_SCHEMA
        and (value.get("format_profile"),value.get("player_abi")) in {
            (FORMAT_PROFILE,PLAYER_ABI),('dvd-vm-bdj-hevc-v1','stock-vlc-libbluray-bdj-v6')}
    )
