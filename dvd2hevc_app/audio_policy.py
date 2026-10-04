"""DVD IFO language-aware audio policy resolution."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from .ifo import AUDIO_ATTR_SIZE, VTS_AUDIO_ATTRS_OFFSET, VTS_AUDIO_COUNT_OFFSET
from .iso import close_iso_image, open_iso_image
from .pipeline import PipelineError


DVD_AUDIO_CODECS = {
    0: "ac3",
    2: "mp1",
    3: "mp2",
    4: "pcm_dvd",
    6: "dts",
}

COMPACT_STEREO_INPUT_CODECS = {"ac3", "dts", "pcm_dvd", "mp1", "mp2"}


def normalize_language(value: str | None) -> str:
    text = str(value or "und").strip().lower()
    aliases = {
        "en": "eng", "english": "eng", "fr": "fra", "french": "fra",
        "de": "deu", "german": "deu", "es": "spa", "spanish": "spa",
        "it": "ita", "italian": "ita", "ja": "jpn", "japanese": "jpn",
        "unknown": "und", "undefined": "und",
    }
    return aliases.get(text, text)


def _ifo_audio_tracks(data: bytes) -> list[dict[str, Any]]:
    if len(data) <= VTS_AUDIO_ATTRS_OFFSET or data[:12] != b"DVDVIDEO-VTS":
        raise PipelineError("Invalid VTS IFO while resolving audio languages")
    count = int(data[VTS_AUDIO_COUNT_OFFSET])
    if not 0 <= count <= 8:
        raise PipelineError(f"Invalid VTS audio stream count: {count}")
    tracks: list[dict[str, Any]] = []
    for ordinal in range(count):
        offset = VTS_AUDIO_ATTRS_OFFSET + ordinal * AUDIO_ATTR_SIZE
        attributes = data[offset : offset + AUDIO_ATTR_SIZE]
        if len(attributes) != AUDIO_ATTR_SIZE:
            raise PipelineError("Truncated VTS audio attributes")
        raw_language = attributes[2:4]
        language = (
            raw_language.decode("ascii").lower()
            if len(raw_language) == 2 and all(65 <= value <= 122 for value in raw_language)
            else "und"
        )
        tracks.append({
            "ordinal": ordinal,
            "language": normalize_language(language),
            "ifo_language_code": language,
            "codec": DVD_AUDIO_CODECS.get(attributes[0] >> 5, f"dvd-format-{attributes[0] >> 5}"),
            "channels": int(attributes[1] & 0x07) + 1,
        })
    return tracks


def resolve_audio_policy(
    source: Path,
    destination: Path,
    *,
    default_mode: str = "passthrough",
    language_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    if default_mode not in {"passthrough", "compact-stereo"}:
        raise PipelineError(f"Unsupported default audio mode: {default_mode}")
    overrides = {
        normalize_language(key): str(value).lower()
        for key, value in (language_overrides or {}).items()
    }
    if any(value not in {"passthrough", "compact-stereo"} for value in overrides.values()):
        raise PipelineError("Audio language overrides must be passthrough or compact-stereo")
    source = source.resolve()
    image = open_iso_image(source)
    title_sets: list[dict[str, Any]] = []
    try:
        entries = [entry for entry in image.list_children(udf_path="/VIDEO_TS") if entry is not None]
        names = sorted(
            entry.file_identifier().decode("utf-8", "replace")
            for entry in entries
            if entry.is_file() and entry.file_identifier().upper().startswith(b"VTS_")
            and entry.file_identifier().upper().endswith(b"_0.IFO")
        )
        for name in names:
            handle = io.BytesIO()
            image.get_file_from_iso_fp(handle, udf_path=f"/VIDEO_TS/{name}")
            tracks = _ifo_audio_tracks(handle.getvalue())
            vts = int(name[4:6])
            for track in tracks:
                language = str(track["language"])
                track["action"] = overrides.get(language, overrides.get("other", default_mode))
            processing_required = any(track["action"] == "compact-stereo" for track in tracks)
            blockers = []
            if processing_required:
                for track in tracks:
                    codec = str(track["codec"])
                    action = str(track["action"])
                    if action == "compact-stereo" and codec not in COMPACT_STEREO_INPUT_CODECS:
                        blockers.append(
                            f"stream {track['ordinal']} cannot be downmixed from {codec}"
                        )
                    elif action == "passthrough" and codec != "ac3":
                        blockers.append(
                            f"stream {track['ordinal']} cannot retain {codec} while this VTS is rebuilt"
                        )
            title_sets.append({
                "vts": vts,
                "tracks": tracks,
                "processing_required": processing_required,
                "blockers": blockers,
            })
    finally:
        close_iso_image(image)
    blocked = [
        f"VTS {row['vts']}: {', '.join(row['blockers'])}"
        for row in title_sets if row["blockers"]
    ]
    if blocked:
        raise PipelineError(
            "Compact-stereo supports DVD AC-3, DTS, LPCM, and MPEG audio inputs, but an affected VTS can only "
            "retain AC-3 tracks byte-exact; "
            + "; ".join(blocked)
        )
    result = {
        "schema": "dvd2hevc-audio-policy-v1",
        "status": "resolved",
        "source": str(source),
        "default_mode": default_mode,
        "language_overrides": overrides,
        "processing_required": any(row["processing_required"] for row in title_sets),
        "resolved_pipeline_mode": (
            "compact-stereo" if any(row["processing_required"] for row in title_sets) else "passthrough"
        ),
        "title_sets": title_sets,
        "notes": [
            "Language codes come from VTS IFO descriptors and remain attached by stream ordinal.",
            "Selected AC-3, DTS, LPCM, or MPEG audio tracks become DVD-compatible AC-3; passthrough AC-3 remains byte-exact.",
        ],
    }
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def track_policy_for_vts(policy: dict[str, Any], vts: int) -> dict[int, dict[str, Any]]:
    row = next((item for item in policy.get("title_sets") or [] if int(item["vts"]) == int(vts)), None)
    if row is None:
        return {}
    return {int(track["ordinal"]): dict(track) for track in row.get("tracks") or []}
