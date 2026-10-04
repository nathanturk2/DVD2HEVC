"""DVD-compatible compact-stereo audio prototyping for Phase 6."""

from __future__ import annotations

import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from .pipeline import PipelineError
from .progress import progress_event
from .repack import encode_pes_timestamp
from .tools import discover_tools
from .transport import AudioPes, read_audio_pes
from .audio_policy import track_policy_for_vts


DEFAULT_STEREO_BITRATE = 256_000
DEFAULT_MONO_BITRATE = 128_000
COMPACT_AUDIO_POLICY = "preserve-ac3-transcode-dvd-audio-stereo-v3"
COMPACT_STEREO_INPUT_CODECS = {"ac3", "dts", "pcm_dvd", "mp1", "mp2"}
DVD_AC3_BITRATES = {
    32_000, 40_000, 48_000, 56_000, 64_000, 80_000, 96_000,
    112_000, 128_000, 160_000, 192_000, 224_000, 256_000,
    320_000, 384_000, 448_000,
}


def _merge_track_rate_variants(
    previous: dict[str, Any], current: dict[str, Any]
) -> dict[str, Any]:
    """Aggregate legal per-cell bitrate changes without calling them formats."""
    result = dict(current)
    for singular, plural in (
        ("source_bitrate", "source_bitrates"),
        ("target_bitrate", "target_bitrates"),
    ):
        rates = {
            int(rate)
            for track in (previous, current)
            for rate in (
                track.get(plural)
                or ([track.get(singular)] if track.get(singular) is not None else [])
            )
        }
        result[plural] = sorted(rates)
        if singular == "source_bitrate":
            result[singular] = next(iter(rates)) if len(rates) == 1 else None
        elif rates:
            # IFO descriptors do not store bitrate.  Keep a compatible scalar
            # for older reports while the complete set remains authoritative.
            result[singular] = max(rates)
    return result


def build_dvd_ac3_pes_packets(
    packets: list[AudioPes],
    *,
    substream_id: int = 0x80,
    maximum_packet_bytes: int = 2034,
) -> list[bytes]:
    """Reframe timestamped AC-3 into DVD private_stream_1 PES packets.

    A physical DVD cell is allowed to begin in the middle of an AC-3 access
    unit. FFmpeg preserves that leading fragment as its own timestamped PES
    packet when copying the elementary stream. Keep such fragments before the
    first complete access unit so adjacent cells join byte-for-byte; they are
    continuation data and must not be advertised as a new access unit.
    """
    if not 0x80 <= substream_id <= 0x87:
        raise PipelineError(f"Invalid DVD AC-3 substream ID: 0x{substream_id:02x}")
    if maximum_packet_bytes <= 18 or maximum_packet_bytes > 0xFFFF + 6:
        raise PipelineError("Invalid DVD audio PES packet size")
    stream = bytearray()
    frame_starts: list[tuple[int, int]] = []
    for packet in packets:
        if packet.pts is None:
            raise PipelineError("Compact AC-3 PES packet has no PTS")
        if packet.payload.startswith(b"\x0b\x77"):
            frame_starts.append((len(stream), int(packet.pts)))
        elif frame_starts:
            raise PipelineError("Compact AC-3 PES payload does not begin with an AC-3 frame")
        stream.extend(packet.payload)
    payload_capacity = maximum_packet_bytes - 18
    output: list[bytes] = []
    cursor = 0
    frame_index = 0
    while cursor < len(stream):
        end = min(len(stream), cursor + payload_capacity)
        while frame_index < len(frame_starts) and frame_starts[frame_index][0] < cursor:
            frame_index += 1
        next_frame_index = frame_index
        while next_frame_index < len(frame_starts) and frame_starts[next_frame_index][0] < end:
            next_frame_index += 1
        starts = frame_starts[frame_index:next_frame_index]
        frame_index = next_frame_index
        elementary = bytes(stream[cursor:end])
        if starts:
            first_offset, first_pts = starts[0]
            first_access_unit_pointer = 1 + first_offset - cursor
            if first_access_unit_pointer > 0xFFFF or len(starts) > 0xFF:
                raise PipelineError("DVD AC-3 packet access-unit metadata is out of range")
            dvd_header = bytes([substream_id, len(starts)]) + first_access_unit_pointer.to_bytes(2, "big")
            body = b"\x81\x80\x05" + encode_pes_timestamp(first_pts, 0x2) + dvd_header + elementary
        else:
            # A stream may end with the tail of its final AC-3 frame. Commercial
            # DVD authoring represents this as a zero-header, zero-pointer PES
            # packet with no PTS rather than inventing a new access unit.
            dvd_header = bytes([substream_id, 0, 0, 0])
            body = b"\x81\x00\x00" + dvd_header + elementary
        pes = b"\x00\x00\x01\xbd" + len(body).to_bytes(2, "big") + body
        if len(pes) < 18:
            # DVD private_stream_1 packets need enough bytes for the PES and
            # four-byte substream headers.  A cell can legitimately end with
            # only 1-4 continuation bytes, so keep those elementary bytes and
            # make the packet structurally valid with PES-header stuffing.
            stuffing = 18 - len(pes)
            header_length = pes[8]
            payload_start = 9 + header_length
            mutable = bytearray(pes)
            mutable[payload_start:payload_start] = b"\xff" * stuffing
            mutable[8] = header_length + stuffing
            mutable[4:6] = (len(mutable) - 6).to_bytes(2, "big")
            pes = bytes(mutable)
        shortfall = maximum_packet_bytes - len(pes)
        if 0 < shortfall < 6:
            # An MPEG padding packet needs at least six bytes. Use legal PES
            # header stuffing for a 1-5 byte pack remainder instead, keeping
            # the DVD substream payload and all AC-3 bytes unchanged.
            header_length = pes[8]
            payload_start = 9 + header_length
            mutable = bytearray(pes)
            mutable[payload_start:payload_start] = b"\xff" * shortfall
            mutable[8] = header_length + shortfall
            mutable[4:6] = (len(mutable) - 6).to_bytes(2, "big")
            pes = bytes(mutable)
        if len(pes) > maximum_packet_bytes:
            raise AssertionError("DVD AC-3 PES packet exceeded its sector capacity")
        output.append(pes)
        cursor = end
    return output


def count_ac3_access_units(packets: list[AudioPes]) -> int:
    """Count complete AC-3 frame starts, excluding a cell-leading fragment."""
    return sum(packet.payload.startswith(b"\x0b\x77") for packet in packets)


def validate_dvd_ac3_pes_packets(
    packets: list[bytes],
    expected_elementary: bytes,
    *,
    expected_access_units: int | None = None,
) -> dict[str, int]:
    rebuilt = bytearray()
    access_units = 0
    for pes in packets:
        if len(pes) < 18 or pes[:4] != b"\x00\x00\x01\xbd":
            raise PipelineError("Invalid generated DVD AC-3 PES packet")
        payload_start = 9 + pes[8]
        payload = pes[payload_start:]
        if len(payload) < 4 or not 0x80 <= payload[0] <= 0x87:
            raise PipelineError("Generated DVD AC-3 packet has an invalid substream header")
        count = payload[1]
        pointer = int.from_bytes(payload[2:4], "big")
        if count == 0:
            if pointer != 0:
                raise PipelineError("Generated continuation-only DVD AC-3 metadata is invalid")
        else:
            sync = pointer + 3
            if sync < 4 or payload[sync : sync + 2] != b"\x0b\x77":
                raise PipelineError("Generated DVD AC-3 first-access-unit pointer is invalid")
        access_units += count
        rebuilt.extend(payload[4:])
    if bytes(rebuilt) != expected_elementary:
        raise PipelineError("Generated DVD AC-3 PES packets changed the elementary stream")
    if expected_access_units is not None and access_units != expected_access_units:
        raise PipelineError(
            f"Generated DVD AC-3 access-unit count changed: {access_units} != {expected_access_units}"
        )
    return {"packets": len(packets), "access_units": access_units, "elementary_bytes": len(rebuilt)}


def parse_audio_bitrate(value: str | int) -> int:
    """Parse CLI-style audio bitrates and require a DVD AC-3 rate."""
    if isinstance(value, int):
        bitrate = value
    else:
        normalized = value.strip().lower()
        multiplier = 1
        if normalized.endswith("k"):
            normalized = normalized[:-1]
            multiplier = 1000
        try:
            bitrate = int(normalized) * multiplier
        except ValueError as exc:
            raise PipelineError(f"Invalid audio bitrate: {value}") from exc
    if bitrate not in DVD_AC3_BITRATES:
        choices = ", ".join(f"{rate // 1000}k" for rate in sorted(DVD_AC3_BITRATES))
        raise PipelineError(f"Unsupported DVD AC-3 bitrate {bitrate}; choose one of: {choices}")
    return bitrate


def compact_audio_channels(source_channels: int) -> int:
    if source_channels <= 0:
        raise PipelineError(f"Invalid source audio channel count: {source_channels}")
    return 1 if source_channels == 1 else 2


def compact_audio_target(
    source_channels: int,
    *,
    stereo_bitrate: int = DEFAULT_STEREO_BITRATE,
    mono_bitrate: int = DEFAULT_MONO_BITRATE,
) -> dict[str, int | str]:
    channels = compact_audio_channels(source_channels)
    return {
        "codec": "ac3",
        "channels": channels,
        "sample_rate": 48_000,
        "bitrate": mono_bitrate if channels == 1 else stereo_bitrate,
    }


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, check=False, text=True, capture_output=True,
        **hidden_subprocess_kwargs(),
    )


def _probe(path: Path, ffprobe: str) -> dict[str, Any]:
    command = [
        ffprobe, "-v", "error", "-show_streams", "-show_format",
        "-of", "json", str(path),
    ]
    completed = _run(command)
    if completed.returncode:
        raise PipelineError(f"FFprobe failed for {path}: {completed.stderr.strip()}")
    return json.loads(completed.stdout)


def _number(value: Any) -> float | None:
    if value in (None, "", "N/A"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _audio_streams(probe: dict[str, Any]) -> list[dict[str, Any]]:
    return [stream for stream in probe.get("streams", []) if stream.get("codec_type") == "audio"]


def _dvd_source_audio_streams(probe: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep only physically valid, decodable DVD audio streams from a PS probe."""
    result: list[dict[str, Any]] = []
    for stream in _audio_streams(probe):
        codec = str(stream.get("codec_name") or "").lower()
        stream_id = parse_stream_id(stream.get("id"))
        packet_id = (stream_id & 0xFF) if stream_id is not None else None
        valid_id = (
            codec == "ac3" and packet_id is not None and 0x80 <= packet_id <= 0x87
            or codec == "dts" and packet_id is not None and 0x88 <= packet_id <= 0x8F
            or codec == "pcm_dvd" and packet_id is not None and 0xA0 <= packet_id <= 0xA7
            or codec in {"mp1", "mp2"} and packet_id is not None and 0xC0 <= packet_id <= 0xC7
        )
        if not valid_id:
            # FFmpeg can occasionally promote arbitrary private/PES bytes to a
            # phantom audio stream at a cell boundary (for example 0xDA MP2).
            # Such IDs are outside every DVD audio range and are not IFO tracks.
            continue
        if int(stream.get("channels") or 0) <= 0 or int(stream.get("sample_rate") or 0) <= 0:
            raise PipelineError(
                f"DVD {codec} stream {stream.get('id')} has no decodable format parameters"
            )
        result.append(stream)
    return result


def parse_stream_id(value: Any) -> int | None:
    if value in (None, "", "N/A"):
        return None
    try:
        return int(str(value), 0)
    except (TypeError, ValueError):
        return None


def dvd_audio_ordinal(stream: dict[str, Any], probe_ordinal: int) -> int:
    """Map FFmpeg's physical DVD stream id to the IFO audio-stream ordinal."""
    stream_id = parse_stream_id(stream.get("id"))
    codec = str(stream.get("codec_name") or "").lower()
    ranges = {
        "ac3": (0x80, 0x87),
        "dts": (0x88, 0x8F),
        "pcm_dvd": (0xA0, 0xA7),
    }
    if codec in ranges and stream_id is not None:
        first, last = ranges[codec]
        if first <= stream_id <= last:
            return stream_id - first
    if codec in {"mp1", "mp2", "mp3"} and stream_id is not None:
        packet_id = stream_id & 0xFF
        if 0xC0 <= packet_id <= 0xC7:
            return packet_id - 0xC0
    if not 0 <= probe_ordinal <= 7:
        raise PipelineError("DVD title domains support at most eight audio streams")
    return probe_ordinal


def dvd_private_audio_substream_id(codec: str, ordinal: int) -> int:
    """Return the authored private_stream_1 ID for an AC-3 or DTS ordinal."""
    codec = str(codec).lower()
    first = {"ac3": 0x80, "dts": 0x88}.get(codec)
    if first is None or not 0 <= int(ordinal) <= 7:
        raise PipelineError(f"Unsupported DVD private audio stream: {codec} ordinal {ordinal}")
    return first + int(ordinal)


def dvd_audio_packet_identity(codec: str, ordinal: int) -> tuple[int, int | None]:
    """Return (PES stream ID, optional private substream ID) for DVD audio."""
    codec = str(codec).lower()
    if codec in {"ac3", "dts", "pcm_dvd"}:
        first = {"ac3": 0x80, "dts": 0x88, "pcm_dvd": 0xA0}[codec]
        if not 0 <= int(ordinal) <= 7:
            raise PipelineError(f"Invalid DVD audio ordinal: {ordinal}")
        return 0xBD, first + int(ordinal)
    if codec in {"mp1", "mp2"}:
        if not 0 <= int(ordinal) <= 7:
            raise PipelineError(f"Invalid DVD audio ordinal: {ordinal}")
        return 0xC0 + int(ordinal), None
    raise PipelineError(f"Unsupported DVD audio packet identity: {codec} ordinal {ordinal}")


def _audio_packet_bounds(path: Path, ffprobe: str, audio_ordinal: int) -> dict[str, int]:
    completed = _run([
        ffprobe, "-v", "error", "-select_streams", f"a:{audio_ordinal}",
        "-show_packets", "-show_entries", "packet=pts", "-of", "json", str(path),
    ])
    if completed.returncode:
        raise PipelineError(f"Could not inspect audio timestamps in {path}: {completed.stderr.strip()}")
    packets = [packet for packet in json.loads(completed.stdout).get("packets", []) if packet.get("pts") is not None]
    if not packets:
        raise PipelineError(f"No timestamped audio packets found in {path}")
    return {
        "first_pts": int(packets[0]["pts"]),
        "last_pts": int(packets[-1]["pts"]),
        "packets": len(packets),
    }


def _audio_frame_bounds(path: Path, ffprobe: str, audio_ordinal: int) -> dict[str, int]:
    """Measure decoded audio coverage independently of source PES framing."""
    completed = _run([
        ffprobe, "-v", "error", "-select_streams", f"a:{audio_ordinal}",
        "-show_frames", "-show_entries",
        "frame=pts,best_effort_timestamp,nb_samples", "-of", "json", str(path),
    ])
    if completed.returncode:
        raise PipelineError(f"Could not inspect decoded audio in {path}: {completed.stderr.strip()}")
    frames: list[tuple[int, int]] = []
    for frame in json.loads(completed.stdout).get("frames", []):
        pts = frame.get("pts")
        if pts is None:
            pts = frame.get("best_effort_timestamp")
        samples = frame.get("nb_samples")
        if pts is None or samples is None:
            continue
        frames.append((int(pts), int(samples)))
    if not frames:
        raise PipelineError(f"No timestamped decoded audio frames found in {path}")
    return {
        "first_pts": frames[0][0],
        "last_pts": frames[-1][0],
        "packets": len(frames),
        "samples": sum(samples for _pts, samples in frames),
    }


def _audio_timestamp_alignment(
    source: dict[str, int],
    output: dict[str, int],
    *,
    reencoded: bool,
    source_codec: str = "ac3",
) -> str | None:
    """Describe a safe timestamp match, including a split cell-entry frame."""
    if reencoded and "samples" in source and "samples" in output:
        sample_difference = output["samples"] - source["samples"]
        common_start = abs(source["first_pts"] - output["first_pts"]) <= 1
        if (
            str(source_codec).lower() == "ac3"
            and source["packets"] == output["packets"]
            and sample_difference == 0
            and common_start
            and abs(source["last_pts"] - output["last_pts"]) <= 1
        ):
            # PES packet boundaries are not decoded-frame boundaries.  This
            # proves every fixed-size AC-3 access unit and sample was retained,
            # even when FFmpeg normalizes malformed duplicate boundary PTS.
            return "decoded-ac3-exact-coverage"
        if (
            str(source_codec).lower() != "ac3"
            and 0 <= sample_difference < 3_072
            and abs(source["first_pts"] - output["first_pts"]) <= 5_760
            and abs(source["last_pts"] - output["last_pts"]) <= 5_760
        ):
            # DTS uses 512-sample frames while AC-3 uses 1536, and FFmpeg's
            # encoder can add priming plus a final rounded access unit.  Bound
            # the entire difference to less than two AC-3 frames and bound
            # both endpoints to the same two-frame window: no source samples
            # may disappear, but legal encoder padding is accepted.
            return "decoded-cross-codec-bounded-coverage"
        return None
    if source == output:
        return "exact"
    if (
        not reencoded
        and source["packets"] == output["packets"]
        and abs(source["first_pts"] - output["first_pts"]) <= 1
        and abs(source["last_pts"] - output["last_pts"]) <= 1
        and (
            source["first_pts"] == output["first_pts"]
            or source["last_pts"] == output["last_pts"]
        )
    ):
        # Remuxing byte-identical AC-3 through MPEG-TS can round one PES
        # endpoint by one 90 kHz clock tick.  Packet coverage must remain
        # identical here, and the caller subsequently proves that the
        # demuxed elementary stream is byte-for-byte identical to the source.
        return "passthrough-one-tick-mux-rounding"
    if (
        reencoded
        and str(source_codec).lower() != "ac3"
        and source["packets"] > 0
        and output["packets"] > 0
        and abs(source["first_pts"] - output["first_pts"]) <= 1
        and abs(source["last_pts"] - output["last_pts"]) <= 2_880
    ):
        # DTS and AC-3 use different access-unit durations. Their packet counts
        # and final frame-start PTS therefore cannot match, even when the same
        # decoded samples are covered. A common first PTS plus no more than one
        # 32 ms AC-3 frame of end-grid difference proves bounded alignment.
        return "cross-codec-frame-grid"
    if (
        reencoded
        and source["packets"] == output["packets"]
        and source["first_pts"] - output["first_pts"]
        == source["last_pts"] - output["last_pts"]
        and abs(source["first_pts"] - output["first_pts"]) == 1
    ):
        # FFmpeg may round an AC-3 encoder's fixed delay by one 90 kHz clock
        # tick when the source cell starts between tick boundaries. Requiring
        # the same delta at both ends proves this is a uniform offset, not
        # drift or a dropped frame.
        return "uniform-one-tick-rounding"
    if (
        reencoded
        and 1 <= source["packets"] - output["packets"] <= 2
        and output["first_pts"] - source["first_pts"] - 2_880
        == output["last_pts"] - source["last_pts"]
        and abs(output["last_pts"] - source["last_pts"]) <= 1
    ):
        # A 48 kHz AC-3 frame is 1536 samples (32 ms / 2880 ticks). A cell may
        # start with only the tail of the preceding frame. That single frame
        # can span two source PES packets, while the decoder correctly emits
        # no new access unit for either fragment. Requiring one frame of PTS
        # movement and an identical end point proves this is not lost audio.
        fragments = source["packets"] - output["packets"]
        prefix = (
            "cell-leading-continuation-trimmed"
            if fragments == 1
            else "cell-leading-continuation-fragments-trimmed"
        )
        return (
            prefix
            if output["last_pts"] == source["last_pts"]
            else f"{prefix}-one-tick-rounding"
        )
    return None


def build_compact_audio_command(
    ffmpeg: str,
    source: Path,
    destination: Path,
    *,
    stream_index: int,
    channels: int,
    bitrate: int,
) -> list[str]:
    """Build one deterministic, timestamp-preserving AC-3 downmix command."""
    return [
        ffmpeg, "-hide_banner", "-y", "-loglevel", "error",
        "-copyts", "-i", str(source), "-map", f"0:{stream_index}",
        "-vn", "-sn", "-dn", "-c:a", "ac3", "-ac", str(channels),
        "-b:a", str(bitrate), "-ar", "48000",
        # FFmpeg's native AC-3 encoder has a fixed 256-sample delay. Offset the
        # 90 kHz output timestamps by 480 ticks so DVD audio PTS remain exact.
        "-output_ts_offset", "0.005333333", "-mpegts_copyts", "1",
        "-muxdelay", "0", "-muxpreload", "0", "-f", "mpegts", str(destination),
    ]


def build_compact_audio_copy_command(
    ffmpeg: str,
    source: Path,
    destination: Path,
    *,
    stream_index: int,
) -> list[str]:
    """Remux an already mono/stereo AC-3 track without a lossy generation."""
    return [
        ffmpeg, "-hide_banner", "-y", "-loglevel", "error",
        "-copyts", "-i", str(source), "-map", f"0:{stream_index}",
        "-vn", "-sn", "-dn", "-c:a", "copy", "-mpegts_copyts", "1",
        "-muxdelay", "0", "-muxpreload", "0", "-f", "mpegts", str(destination),
    ]


def _prototype_compact_track(
    source: Path,
    destination: Path,
    stream: dict[str, Any],
    probe_ordinal: int,
    *,
    ffmpeg: str,
    ffprobe: str,
    format_duration: float | None,
    stereo_bitrate: int,
    mono_bitrate: int,
    action: str = "compact-stereo",
    language: str | None = None,
    ifo_codec: str | None = None,
) -> dict[str, Any]:
    source_channels = int(stream.get("channels") or 0)
    source_codec = str(stream.get("codec_name") or "").lower()
    source_bitrate_value = _number(stream.get("bit_rate"))
    if action not in {"passthrough", "compact-stereo"}:
        raise PipelineError(f"Unsupported audio track action: {action}")
    if action == "passthrough" and source_codec != "ac3":
        raise PipelineError("Selective audio passthrough currently requires AC-3")
    if action == "compact-stereo" and source_codec not in COMPACT_STEREO_INPUT_CODECS:
        raise PipelineError(
            f"Compact-stereo cannot currently transcode DVD {source_codec or 'unknown'} audio"
        )
    preserve_elementary = source_codec == "ac3" and (
        action == "passthrough" or source_channels <= 2
    )
    if preserve_elementary:
        if str(stream.get("sample_rate")) != "48000":
            raise PipelineError("DVD AC-3 passthrough requires a 48 kHz source")
        if source_bitrate_value is None:
            raise PipelineError("DVD AC-3 passthrough source has no declared bitrate")
        source_bitrate = parse_audio_bitrate(round(source_bitrate_value))
        target: dict[str, Any] = {
            "codec": "ac3",
            "channels": source_channels,
            "sample_rate": 48_000,
            "bitrate": source_bitrate,
        }
    else:
        target = compact_audio_target(
            source_channels,
            stereo_bitrate=stereo_bitrate,
            mono_bitrate=mono_bitrate,
        )
    stream_index = int(stream["index"])
    ordinal = dvd_audio_ordinal(stream, probe_ordinal)
    output = destination / f"audio-{ordinal:02d}.ts"
    elementary_output = destination / f"audio-{ordinal:02d}.ac3"
    task = f"stream-{ordinal}"
    progress_event("audio", "start", task, source=source.name, channels=source_channels)
    try:
        command = (
            build_compact_audio_copy_command(
                ffmpeg, source, output, stream_index=stream_index,
            )
            if preserve_elementary
            else build_compact_audio_command(
                ffmpeg,
                source,
                output,
                stream_index=stream_index,
                channels=int(target["channels"]),
                bitrate=int(target["bitrate"]),
            )
        )
        completed = _run(command)
        if completed.returncode:
            raise PipelineError(
                f"Compact audio encode failed for stream {stream_index}: {completed.stderr.strip()}"
            )
        output_probe = _probe(output, ffprobe)
        output_streams = _audio_streams(output_probe)
        if len(output_streams) != 1:
            raise PipelineError(f"Expected one compact audio stream in {output}")
        actual = output_streams[0]
        expected = {
            "codec_name": "ac3",
            "channels": int(target["channels"]),
            "sample_rate": str(target["sample_rate"]),
        }
        for key, value in expected.items():
            if actual.get(key) != value:
                raise PipelineError(
                    f"Compact audio validation failed for {output}: "
                    f"{key}={actual.get(key)!r}, expected {value!r}"
                )
        timestamp_evidence = "pes-packets" if preserve_elementary else "decoded-frames"
        timestamp_reader = _audio_packet_bounds if preserve_elementary else _audio_frame_bounds
        source_timestamps = timestamp_reader(source, ffprobe, probe_ordinal)
        output_timestamps = timestamp_reader(output, ffprobe, 0)
        timestamp_status = _audio_timestamp_alignment(
            source_timestamps,
            output_timestamps,
            reencoded=not preserve_elementary,
            source_codec=source_codec,
        )
        if timestamp_status is None:
            raise PipelineError(
                f"Compact audio timestamp mismatch for stream {stream_index}: "
                f"{source_timestamps} != {output_timestamps}"
            )
        demux = _run([
            ffmpeg, "-hide_banner", "-y", "-v", "error", "-i", str(output),
            "-map", "0:a:0", "-c:a", "copy", "-f", "ac3", str(elementary_output),
        ])
        if demux.returncode:
            raise PipelineError(f"Could not demux compact AC-3 from {output}: {demux.stderr.strip()}")
        elementary_identity = "reencoded"
        exact_source_bytes: int | None = None
        if preserve_elementary:
            source_elementary = destination / f"audio-{ordinal:02d}.source.ac3"
            source_demux = _run([
                ffmpeg, "-hide_banner", "-y", "-v", "error", "-i", str(source),
                "-map", f"0:{stream_index}", "-c:a", "copy", "-f", "ac3", str(source_elementary),
            ])
            if source_demux.returncode:
                raise PipelineError(
                    f"Could not demux source AC-3 from {source}: {source_demux.stderr.strip()}"
                )
            if source_elementary.read_bytes() != elementary_output.read_bytes():
                raise PipelineError("Mono/stereo AC-3 passthrough changed the elementary stream")
            exact_source_bytes = source_elementary.stat().st_size
            source_elementary.unlink()
            elementary_identity = "byte-exact"
        timestamped_pes = read_audio_pes(output)
        target_substream_id = 0x80 + ordinal
        dvd_pes_packets = build_dvd_ac3_pes_packets(
            timestamped_pes,
            substream_id=target_substream_id,
        )
        dvd_pes_validation = validate_dvd_ac3_pes_packets(
            dvd_pes_packets,
            elementary_output.read_bytes(),
            expected_access_units=count_ac3_access_units(timestamped_pes),
        )
        dvd_pes_output = destination / f"audio-{ordinal:02d}.dvd-pes"
        dvd_pes_output.write_bytes(b"".join(dvd_pes_packets))
        decode = _run([
            ffmpeg, "-hide_banner", "-v", "error", "-i", str(output),
            "-map", "0:a:0", "-f", "null", "-",
        ])
        if decode.returncode:
            raise PipelineError(f"Compact audio decode failed for {output}: {decode.stderr.strip()}")
        duration = _number(stream.get("duration")) or format_duration
        estimated_source_bytes = exact_source_bytes if preserve_elementary else (
            round(source_bitrate_value * duration / 8)
            if source_bitrate_value is not None and duration is not None
            else None
        )
        result = {
            "ordinal": ordinal,
            "source_probe_ordinal": probe_ordinal,
            "source_stream_index": stream_index,
            "source_stream_id": stream.get("id"),
            "source_codec": stream.get("codec_name"),
            "source_ifo_codec": ifo_codec or stream.get("codec_name"),
            "source_channels": source_channels,
            "source_bitrate": round(source_bitrate_value) if source_bitrate_value is not None else None,
            "duration_seconds": duration,
            "language": language or (stream.get("tags") or {}).get("language"),
            "requested_action": action,
            "target": target,
            "processing": "passthrough" if preserve_elementary else "downmix",
            "elementary_identity": elementary_identity,
            "timestamped_output": str(output),
            "elementary_output": str(elementary_output),
            "dvd_private_stream_output": str(dvd_pes_output),
            "dvd_private_stream": {
                **dvd_pes_validation,
                "substream_id": f"0x{target_substream_id:02x}",
                "sector_bytes": len(dvd_pes_packets) * 2048,
            },
            "output_bytes": elementary_output.stat().st_size,
            "estimated_source_bytes": estimated_source_bytes,
            "timestamp_alignment": {
                **source_timestamps,
                "output_first_pts": output_timestamps["first_pts"],
                "output_last_pts": output_timestamps["last_pts"],
                "output_packets": output_timestamps["packets"],
                **(
                    {
                        "output_samples": output_timestamps["samples"],
                        "evidence": timestamp_evidence,
                    }
                    if "samples" in output_timestamps
                    else {"evidence": timestamp_evidence}
                ),
                "status": timestamp_status,
            },
            "full_decode": "passed",
        }
    except BaseException:
        progress_event("audio", "failed", task, source=source.name)
        raise
    progress_event("audio", "done", task, source=source.name)
    return result


def prototype_compact_audio(
    source: Path,
    destination: Path,
    *,
    stereo_bitrate: int = DEFAULT_STEREO_BITRATE,
    mono_bitrate: int = DEFAULT_MONO_BITRATE,
    workers: int = 2,
    allow_no_audio: bool = False,
    track_policy: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Transcode every playable audio stream to DVD-safe compact AC-3 tracks.

    The result includes timestamped elementary audio and validated DVD private
    stream PES packets ready for the compact VOBU writer.
    """
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_file():
        raise PipelineError(f"Compact-audio source is missing: {source}")
    stereo_bitrate = parse_audio_bitrate(stereo_bitrate)
    mono_bitrate = parse_audio_bitrate(mono_bitrate)
    tools = discover_tools()
    ffmpeg = tools.get("ffmpeg")
    ffprobe = tools.get("ffprobe")
    if not ffmpeg or not ffprobe:
        raise PipelineError("FFmpeg and FFprobe are required for compact audio")

    source_probe = _probe(source, ffprobe)
    streams = _dvd_source_audio_streams(source_probe)
    if not streams and not allow_no_audio:
        raise PipelineError(f"No playable audio streams found in {source}")
    if len(streams) > 8:
        raise PipelineError(f"DVD title domain has more than eight audio streams: {source}")
    destination.mkdir(parents=True, exist_ok=True)
    format_duration = _number(source_probe.get("format", {}).get("duration"))
    workers = max(1, min(int(workers), max(1, len(streams))))
    arguments = list(enumerate(streams))
    def policy_for(stream: dict[str, Any], probe_ordinal: int) -> dict[str, Any]:
        ordinal = dvd_audio_ordinal(stream, probe_ordinal)
        return dict((track_policy or {}).get(ordinal) or {})
    if workers == 1:
        tracks = [
            _prototype_compact_track(
                source, destination, stream, probe_ordinal,
                ffmpeg=ffmpeg, ffprobe=ffprobe, format_duration=format_duration,
                stereo_bitrate=stereo_bitrate, mono_bitrate=mono_bitrate,
                action=str(policy_for(stream, probe_ordinal).get("action") or "compact-stereo"),
                language=policy_for(stream, probe_ordinal).get("language"),
                ifo_codec=policy_for(stream, probe_ordinal).get("codec"),
            )
            for probe_ordinal, stream in arguments
        ]
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dvd2hevc-audio") as executor:
            tracks = list(executor.map(
                lambda item: _prototype_compact_track(
                    source, destination, item[1], item[0],
                    ffmpeg=ffmpeg, ffprobe=ffprobe, format_duration=format_duration,
                    stereo_bitrate=stereo_bitrate, mono_bitrate=mono_bitrate,
                    action=str(policy_for(item[1], item[0]).get("action") or "compact-stereo"),
                    language=policy_for(item[1], item[0]).get("language"),
                    ifo_codec=policy_for(item[1], item[0]).get("codec"),
                ),
                arguments,
            ))
    tracks.sort(key=lambda track: int(track["ordinal"]))
    ordinals = [int(track["ordinal"]) for track in tracks]
    if len(ordinals) != len(set(ordinals)):
        raise PipelineError(f"DVD audio streams resolve to duplicate IFO ordinals: {ordinals}")

    source_estimate = sum(
        int(track["estimated_source_bytes"]) for track in tracks
        if track["estimated_source_bytes"] is not None
    )
    output_bytes = sum(int(track["output_bytes"]) for track in tracks)
    report = {
        "schema": "dvd2hevc-compact-audio-prototype-v0",
        "status": "passed",
        "audio_mode": "compact-stereo",
        "processing_policy": COMPACT_AUDIO_POLICY,
        "source": str(source),
        "stereo_bitrate": stereo_bitrate,
        "mono_bitrate": mono_bitrate,
        "tracks": tracks,
        "summary": {
            "tracks": len(tracks),
            "estimated_source_elementary_bytes": source_estimate or None,
            "output_elementary_bytes": output_bytes,
            "estimated_saved_bytes": source_estimate - output_bytes if source_estimate else None,
            "estimated_saved_percent": (
                round(100 * (source_estimate - output_bytes) / source_estimate, 3)
                if source_estimate else None
            ),
            "dvd_private_stream_packets": sum(
                int(track["dvd_private_stream"]["packets"]) for track in tracks
            ),
            "dvd_private_stream_sector_bytes": sum(
                int(track["dvd_private_stream"]["sector_bytes"]) for track in tracks
            ),
            "downmixed_tracks": sum(track.get("processing") == "downmix" for track in tracks),
            "preserved_tracks": sum(track.get("processing") == "passthrough" for track in tracks),
        },
        "limits": ["This per-cell report must be incorporated into a compact domain layout"],
    }
    report_path = destination / "compact-audio-report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def _compact_audio_report_matches(
    report: dict[str, Any],
    source: Path,
    *,
    stereo_bitrate: int,
    mono_bitrate: int,
    track_policy: dict[int, dict[str, Any]] | None = None,
) -> bool:
    """Reject stale or incomplete per-cell audio cache entries."""
    try:
        if (
            report.get("schema") != "dvd2hevc-compact-audio-prototype-v0"
            or report.get("status") != "passed"
            or report.get("audio_mode") != "compact-stereo"
            or report.get("processing_policy") != COMPACT_AUDIO_POLICY
            or Path(str(report.get("source"))).resolve() != source.resolve()
            or int(report.get("stereo_bitrate")) != stereo_bitrate
            or int(report.get("mono_bitrate")) != mono_bitrate
        ):
            return False
        for fallback_ordinal, track in enumerate(report.get("tracks") or []):
            ordinal = int(track.get("ordinal", fallback_ordinal))
            requested = str(((track_policy or {}).get(ordinal) or {}).get("action") or "compact-stereo")
            if str(track.get("requested_action") or "compact-stereo") != requested:
                return False
            if str(track.get("source_codec") or "").lower() == "ac3" and (
                requested == "passthrough" or int(track["source_channels"]) <= 2
            ):
                target = {
                    "channels": int(track["source_channels"]),
                    "bitrate": int(track["source_bitrate"]),
                    "sample_rate": 48_000,
                }
                if track.get("processing") != "passthrough" or track.get("elementary_identity") != "byte-exact":
                    return False
            else:
                target = compact_audio_target(
                    int(track["source_channels"]),
                    stereo_bitrate=stereo_bitrate,
                    mono_bitrate=mono_bitrate,
                )
                if track.get("processing") != "downmix":
                    return False
            if any(int(track["target"][key]) != int(target[key]) for key in ("channels", "bitrate", "sample_rate")):
                return False
            for key in ("timestamped_output", "elementary_output", "dvd_private_stream_output"):
                if not Path(str(track[key])).is_file():
                    return False
    except (KeyError, TypeError, ValueError, OSError):
        return False
    return True


def prefetch_compact_audio_cells(
    cells: list[dict[str, Any]],
    destination: Path,
    *,
    vts: int,
    stereo_bitrate: int = DEFAULT_STEREO_BITRATE,
    mono_bitrate: int = DEFAULT_MONO_BITRATE,
    workers: int = 2,
    audio_policy: Path | None = None,
) -> dict[str, Any]:
    """Prepare per-cell compact audio before the final video layout exists.

    The normal compact-audio domain pass uses the same cache contract and
    destination names, so these artifacts are verified and reused later.  This
    lets a long interleaved video conversion hide most audio work without
    weakening the final layout and IFO checks.
    """
    destination = destination.resolve() / f"vts{int(vts):02d}"
    destination.mkdir(parents=True, exist_ok=True)
    stereo_bitrate = parse_audio_bitrate(stereo_bitrate)
    mono_bitrate = parse_audio_bitrate(mono_bitrate)
    track_policy: dict[int, dict[str, Any]] = {}
    if audio_policy is not None:
        policy = json.loads(audio_policy.resolve().read_text(encoding="utf-8-sig"))
        track_policy = track_policy_for_vts(policy, int(vts))

    completed: list[dict[str, Any]] = []
    progress_event(
        "audio", "prefetch-start", f"vts{int(vts):02d}",
        current=0, total=len(cells), vts=int(vts),
    )
    for index, cell in enumerate(cells, start=1):
        name = str(cell["name"])
        source = Path(cell["source_cell"]).resolve()
        cell_root = destination / name
        report_path = cell_root / "compact-audio-report.json"
        report: dict[str, Any] | None = None
        if report_path.is_file():
            try:
                candidate = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                candidate = {}
            if _compact_audio_report_matches(
                candidate, source, stereo_bitrate=stereo_bitrate,
                mono_bitrate=mono_bitrate, track_policy=track_policy,
            ):
                report = candidate
        if report is None:
            report = prototype_compact_audio(
                source, cell_root,
                stereo_bitrate=stereo_bitrate, mono_bitrate=mono_bitrate,
                workers=workers, allow_no_audio=True, track_policy=track_policy,
            )
        completed.append({
            "name": name,
            "source_cell": str(source),
            "report": str(report_path.resolve()),
            "tracks": len(report.get("tracks") or []),
        })
        progress_event(
            "audio", "prefetch-progress", f"vts{int(vts):02d}",
            current=index, total=len(cells), vts=int(vts),
        )

    result = {
        "schema": "dvd2hevc-compact-audio-prefetch-v1",
        "status": "passed",
        "vts": int(vts),
        "cells": completed,
        "summary": {
            "physical_cells": len(completed),
            "cell_streams": sum(int(row["tracks"]) for row in completed),
        },
    }
    report_path = destination / "compact-audio-prefetch-report.json"
    report_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    progress_event(
        "audio", "prefetch-done", f"vts{int(vts):02d}",
        current=len(cells), total=len(cells), vts=int(vts),
    )
    return result


def prototype_compact_audio_domain(
    layout_path: Path,
    destination: Path,
    *,
    domain: str,
    vts: int,
    stereo_bitrate: int = DEFAULT_STEREO_BITRATE,
    mono_bitrate: int = DEFAULT_MONO_BITRATE,
    workers: int = 2,
    audio_policy: Path | None = None,
    progress_callback: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Encode every unique physical cell's audio for one compact title domain."""
    layout_path = layout_path.resolve()
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    row = next(
        (item for item in layout.get("domains", []) if item.get("domain") == domain and int(item.get("vts", -1)) == vts),
        None,
    )
    if row is None:
        raise PipelineError(f"Compact layout has no {domain} VTS {vts}")
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    stereo_bitrate = parse_audio_bitrate(stereo_bitrate)
    mono_bitrate = parse_audio_bitrate(mono_bitrate)
    track_policy: dict[int, dict[str, Any]] = {}
    if audio_policy is not None:
        policy = json.loads(audio_policy.resolve().read_text(encoding="utf-8-sig"))
        track_policy = track_policy_for_vts(policy, vts)
    cell_reports: list[dict[str, Any]] = []
    tracks_by_ordinal: dict[int, dict[str, Any]] = {}
    for index, cell in enumerate(row.get("cells") or [], start=1):
        name = str(cell["name"])
        cell_root = destination / name
        report_path = cell_root / "compact-audio-report.json"
        progress_event(
            "audio", "cell-start", name, current=index, total=len(row.get("cells") or []), vts=vts,
        )
        report: dict[str, Any]
        if report_path.is_file():
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if not _compact_audio_report_matches(
                report,
                Path(cell["source_cell"]),
                stereo_bitrate=stereo_bitrate,
                mono_bitrate=mono_bitrate,
                track_policy=track_policy,
            ):
                report = prototype_compact_audio(
                    Path(cell["source_cell"]), cell_root,
                    stereo_bitrate=stereo_bitrate, mono_bitrate=mono_bitrate,
                    workers=workers, allow_no_audio=True,
                    track_policy=track_policy,
                )
        else:
            report = prototype_compact_audio(
                Path(cell["source_cell"]), cell_root,
                stereo_bitrate=stereo_bitrate, mono_bitrate=mono_bitrate,
                workers=workers, allow_no_audio=True,
                track_policy=track_policy,
            )
        for track in report.get("tracks") or []:
            source_codec = str(track.get("source_codec") or "").lower()
            if source_codec not in COMPACT_STEREO_INPUT_CODECS:
                raise PipelineError(
                    f"Full-disc compact-stereo cannot transcode {source_codec or 'unknown'}; "
                    f"{name} stream {track.get('ordinal')}"
                )
            source_id = parse_stream_id(track.get("source_stream_id"))
            ordinal = int(track["ordinal"])
            source_packet_id, source_substream_id = dvd_audio_packet_identity(source_codec, ordinal)
            expected_probe_id = source_substream_id if source_substream_id is not None else source_packet_id
            if source_id is None or (source_id & 0xFF) != expected_probe_id:
                raise PipelineError(
                    f"{source_codec.upper()} stream {ordinal} has unexpected DVD substream id "
                    f"{track.get('source_stream_id')}"
                )
            previous = tracks_by_ordinal.get(ordinal)
            stable = {
                "ordinal": ordinal,
                "source_codec": track["source_codec"],
                "source_ifo_codec": track.get("source_ifo_codec") or track["source_codec"],
                "source_channels": int(track["source_channels"]),
                "target_channels": int(track["target"]["channels"]),
                "target_bitrate": int(track["target"]["bitrate"]),
                "target_bitrates": [int(track["target"]["bitrate"])],
                "source_bitrate": int(track["source_bitrate"]) if track.get("source_bitrate") else None,
                "source_bitrates": (
                    [int(track["source_bitrate"])] if track.get("source_bitrate") else []
                ),
                "processing": str(track["processing"]),
                "elementary_identity": str(track["elementary_identity"]),
                "source_packet_id": f"0x{source_packet_id:02x}",
                "source_substream_id": (
                    f"0x{source_substream_id:02x}" if source_substream_id is not None else None
                ),
                "substream_id": f"0x{0x80 + ordinal:02x}",
                "language": track.get("language"),
            }
            if previous is not None and any(
                previous[key] != stable[key]
                for key in (
                    "source_codec", "source_ifo_codec", "source_channels", "target_channels",
                    "processing", "elementary_identity", "substream_id",
                    "source_packet_id", "source_substream_id",
                )
            ):
                raise PipelineError(f"Audio stream {ordinal} changes format between physical cells")
            if previous is not None:
                stable = _merge_track_rate_variants(previous, stable)
            tracks_by_ordinal[ordinal] = stable
        cell_reports.append({
            "name": name,
            "source_cell": str(Path(cell["source_cell"]).resolve()),
            "first_sector": int(cell["cell"]["first_sector"]),
            "last_sector": int(cell["cell"]["last_sector"]),
            "report": str(report_path),
            "tracks": len(report.get("tracks") or []),
        })
        progress_event(
            "audio", "cell-done", name, current=index, total=len(row.get("cells") or []), vts=vts,
        )
        if progress_callback is not None:
            progress_callback()
    # Physical DVD audio IDs may be sparse when authored descriptors are unused.
    # Retain their real ordinals so navigation still selects the same streams.
    ordinals = sorted(tracks_by_ordinal)
    report = {
        "schema": "dvd2hevc-compact-audio-domain-v1",
        "status": "passed",
        "audio_mode": "compact-stereo",
        "processing_policy": COMPACT_AUDIO_POLICY,
        "layout": str(layout_path),
        "source": str(Path(layout["source"]).resolve()),
        "domain": domain,
        "vts": vts,
        "stereo_bitrate": parse_audio_bitrate(stereo_bitrate),
        "mono_bitrate": parse_audio_bitrate(mono_bitrate),
        "tracks": [tracks_by_ordinal[ordinal] for ordinal in ordinals],
        "cells": cell_reports,
        "summary": {
            "physical_cells": len(cell_reports),
            "audio_streams": len(tracks_by_ordinal),
            "encoded_cell_streams": sum(int(item["tracks"]) for item in cell_reports),
            "downmixed_streams": sum(track["processing"] == "downmix" for track in tracks_by_ordinal.values()),
            "preserved_streams": sum(track["processing"] == "passthrough" for track in tracks_by_ordinal.values()),
        },
    }
    report_path = destination / "compact-audio-domain-report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def prototype_compact_audio_batch(
    layout_path: Path,
    destination: Path,
    *,
    vts_values: list[int] | None = None,
    stereo_bitrate: int = DEFAULT_STEREO_BITRATE,
    mono_bitrate: int = DEFAULT_MONO_BITRATE,
    audio_workers: int = 2,
    domain_workers: int = 2,
    audio_policy: Path | None = None,
) -> dict[str, Any]:
    """Encode several title domains with bounded disc- and stream-level parallelism."""
    layout_path = layout_path.resolve()
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    stereo_bitrate = parse_audio_bitrate(stereo_bitrate)
    mono_bitrate = parse_audio_bitrate(mono_bitrate)
    available = sorted(
        int(row["vts"])
        for row in layout.get("domains") or []
        if row.get("domain") == "title"
    )
    requested = available if vts_values is None else sorted(set(int(value) for value in vts_values))
    missing = sorted(set(requested) - set(available))
    if missing:
        raise PipelineError(f"Compact audio batch contains absent title sets: {missing}")
    if not requested:
        raise PipelineError("Compact audio batch has no title domains")
    audio_workers = max(1, min(8, int(audio_workers)))
    domain_workers = max(1, min(int(domain_workers), len(requested)))
    cells_by_vts = {
        int(row["vts"]): len(row.get("cells") or [])
        for row in layout.get("domains") or []
        if row.get("domain") == "title" and int(row["vts"]) in requested
    }
    total_cells = sum(cells_by_vts.values())
    completed_cells = 0
    progress_lock = threading.Lock()

    def cell_done() -> None:
        nonlocal completed_cells
        with progress_lock:
            completed_cells += 1
            progress_event(
                "audio", "progress", "compact-stereo",
                scope="audio-batch-cells", current=completed_cells, total=total_cells,
            )

    progress_event(
        "audio", "batch-start", "compact-stereo",
        scope="audio-batch-cells", current=0, total=total_cells,
    )

    def encode(vts: int) -> dict[str, Any]:
        progress_event("audio", "domain-start", f"vts{vts:02d}", vts=vts)
        try:
            result = prototype_compact_audio_domain(
                layout_path,
                destination / f"vts{vts:02d}",
                domain="title",
                vts=vts,
                stereo_bitrate=stereo_bitrate,
                mono_bitrate=mono_bitrate,
                workers=audio_workers,
                audio_policy=audio_policy,
                progress_callback=cell_done,
            )
        except BaseException:
            progress_event("audio", "domain-failed", f"vts{vts:02d}", vts=vts)
            raise
        progress_event("audio", "domain-done", f"vts{vts:02d}", vts=vts)
        return result

    completed: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=domain_workers, thread_name_prefix="dvd2hevc-audio-domain") as executor:
        futures = {executor.submit(encode, vts): vts for vts in requested}
        for current, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            completed.append(result)
            progress_event(
                "audio", "batch-progress", f"vts{int(result['vts']):02d}",
                current=current, total=len(requested), vts=int(result["vts"]),
            )
    completed.sort(key=lambda item: int(item["vts"]))
    report = {
        "schema": "dvd2hevc-compact-audio-batch-v1",
        "status": "passed",
        "audio_mode": "compact-stereo",
        "processing_policy": COMPACT_AUDIO_POLICY,
        "layout": str(layout_path),
        "source": str(Path(layout["source"]).resolve()),
        "stereo_bitrate": stereo_bitrate,
        "mono_bitrate": mono_bitrate,
        "audio_workers": audio_workers,
        "domain_workers": domain_workers,
        "domains": [
            {
                "vts": int(result["vts"]),
                "report": str(destination / f"vts{int(result['vts']):02d}" / "compact-audio-domain-report.json"),
                "physical_cells": int(result["summary"]["physical_cells"]),
                "audio_streams": int(result["summary"]["audio_streams"]),
                "encoded_cell_streams": int(result["summary"]["encoded_cell_streams"]),
                "downmixed_streams": int(result["summary"]["downmixed_streams"]),
                "preserved_streams": int(result["summary"]["preserved_streams"]),
            }
            for result in completed
        ],
        "summary": {
            "title_domains": len(completed),
            "physical_cells": sum(int(result["summary"]["physical_cells"]) for result in completed),
            "encoded_cell_streams": sum(int(result["summary"]["encoded_cell_streams"]) for result in completed),
            "downmixed_streams": sum(int(result["summary"]["downmixed_streams"]) for result in completed),
            "preserved_streams": sum(int(result["summary"]["preserved_streams"]) for result in completed),
        },
    }
    report_path = destination / "compact-audio-batch-report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    progress_event(
        "audio", "batch-done", "compact-stereo",
        scope="audio-batch-cells", current=total_cells, total=total_cells,
    )
    return report
from .subprocess_utils import hidden_subprocess_kwargs
