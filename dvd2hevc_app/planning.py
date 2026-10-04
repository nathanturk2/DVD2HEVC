"""Conversion-plan generation from a read-only disc scan."""

from __future__ import annotations

from typing import Any


def build_plan(scan: dict[str, Any]) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    blockers: list[str] = []
    totals = scan["video_ts"]["totals"]
    if scan.get("scan_depth") != "full":
        blockers.append("A full VOB sector scan is required before conversion")
    if totals.get("scrambled_pes_packets"):
        blockers.append("The source contains PES packets marked as scrambled")
    if totals.get("invalid_sectors"):
        blockers.append("The source contains invalid or non-pack-aligned VOB sectors")

    for vob in scan["video_ts"]["vobs"]:
        stats = vob.get("stats") or {}
        video_packets = int(stats.get("video_pes_packets") or 0)
        if not video_packets:
            action = "copy"
            reason = "No MPEG video PES packets were detected"
        elif blockers:
            action = "blocked"
            reason = blockers[0]
        else:
            action = "reencode-candidate"
            reason = "Video sectors can be analyzed for sector-preserving HEVC replacement"
        actions.append(
            {
                "file": vob["name"],
                "domain": vob["domain"],
                "action": action,
                "reason": reason,
                "video_payload_capacity": stats.get("video_payload_bytes"),
                "nav_packs": stats.get("nav_packs"),
            }
        )

    return {
        "format_profile": "dvd-hevc-sector-preserving-v0",
        "source": scan["source"],
        "ready_for_rewriter_prototype": not blockers,
        "blockers": blockers,
        "actions": actions,
        "summary": {
            "reencode_candidates": sum(item["action"] == "reencode-candidate" for item in actions),
            "copy": sum(item["action"] == "copy" for item in actions),
            "blocked": sum(item["action"] == "blocked" for item in actions),
        },
    }
