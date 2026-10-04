"""Minimal MPEG-TS video PES reader for Phase 2 HEVC staging."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


TS_PACKET_SIZE = 188


@dataclass
class VideoPes:
    pts: int | None
    dts: int | None
    payload: bytes


@dataclass
class AudioPes:
    pts: int | None
    payload: bytes


HEVC_END_OF_SEQUENCE = b"\x00\x00\x01\x48\x01"


def hevc_nal_types(payload: bytes) -> list[int]:
    """Return Annex-B HEVC NAL unit types in elementary-stream order."""
    result: list[int] = []
    offset = 0
    while offset < len(payload) - 3:
        start = payload.find(b"\x00\x00\x01", offset)
        if start < 0:
            break
        header = start + 3
        if header < len(payload):
            result.append((payload[header] >> 1) & 0x3F)
        offset = header + 1
    return result


def has_hevc_idr(payload: bytes) -> bool:
    """Whether an HEVC access unit contains an IDR_W_RADL or IDR_N_LP NAL."""
    return any(nal_type in {19, 20} for nal_type in hevc_nal_types(payload))


def append_hevc_end_of_sequence(packets: list[VideoPes]) -> list[VideoPes]:
    """Delimit a terminal HEVC access unit so VLC can display one-frame menus."""
    if not packets:
        return []
    result = list(packets)
    last = result[-1]
    if not last.payload.endswith(HEVC_END_OF_SEQUENCE):
        result[-1] = VideoPes(
            pts=last.pts,
            dts=last.dts,
            payload=last.payload + HEVC_END_OF_SEQUENCE,
        )
    return result


def prepare_hevc_dvd_menu(packets: list[VideoPes]) -> list[VideoPes]:
    """Make a terminal DVD menu picture observable to hardware decoders.

    A one-picture DVD still can reach end-of-cell after the HEVC decoder emits
    its frame but before VLC's hardware video output presents it.  Repeat that
    independently decodable access unit one 90 kHz tick later, then append EOS
    to the repeat.  This is the same minimal guard used by the accepted Intern
    regression image: the visible still and DVD timing remain unchanged, while
    the first frame has a following access unit that lets it leave the decoder.
    Moving and multi-picture menu cells keep their authored picture sequence
    and receive only the terminal EOS delimiter.
    """
    if len(packets) != 1:
        return append_hevc_end_of_sequence(packets)
    packet = packets[0]
    payload = (
        packet.payload[:-len(HEVC_END_OF_SEQUENCE)]
        if packet.payload.endswith(HEVC_END_OF_SEQUENCE)
        else packet.payload
    )

    def next_tick(value: int | None) -> int | None:
        return None if value is None else (value + 1) & ((1 << 33) - 1)

    return [
        VideoPes(pts=packet.pts, dts=packet.dts, payload=payload),
        VideoPes(
            pts=next_tick(packet.pts),
            dts=next_tick(packet.dts),
            payload=payload + HEVC_END_OF_SEQUENCE,
        ),
    ]


def decode_pes_timestamp(data: bytes) -> int:
    if len(data) < 5:
        raise ValueError("Truncated PES timestamp")
    return (
        ((data[0] >> 1) & 0x07) << 30
        | data[1] << 22
        | ((data[2] >> 1) & 0x7F) << 15
        | data[3] << 7
        | ((data[4] >> 1) & 0x7F)
    )


def parse_video_pes(data: bytes) -> VideoPes:
    if len(data) < 9 or data[:3] != b"\x00\x00\x01" or not 0xE0 <= data[3] <= 0xEF:
        raise ValueError("Not an MPEG video PES packet")
    header_length = data[8]
    payload_start = 9 + header_length
    if payload_start > len(data):
        raise ValueError("Truncated MPEG video PES header")
    timestamp_flags = (data[7] >> 6) & 0x03
    pts = decode_pes_timestamp(data[9:14]) if timestamp_flags in {2, 3} else None
    dts = decode_pes_timestamp(data[14:19]) if timestamp_flags == 3 else pts
    return VideoPes(pts=pts, dts=dts, payload=data[payload_start:])


def parse_audio_pes(data: bytes) -> AudioPes:
    if len(data) < 9 or data[:4] != b"\x00\x00\x01\xbd":
        raise ValueError("Not a private_stream_1 audio PES packet")
    header_length = data[8]
    payload_start = 9 + header_length
    if payload_start > len(data):
        raise ValueError("Truncated audio PES header")
    timestamp_flags = (data[7] >> 6) & 0x03
    pts = decode_pes_timestamp(data[9:14]) if timestamp_flags in {2, 3} else None
    return AudioPes(pts=pts, payload=data[payload_start:])


def _ts_payload(packet: bytes) -> tuple[int, bool, bytes] | None:
    if len(packet) != TS_PACKET_SIZE or packet[0] != 0x47:
        raise ValueError("Invalid MPEG-TS sync byte")
    pid = ((packet[1] & 0x1F) << 8) | packet[2]
    payload_start = bool(packet[1] & 0x40)
    adaptation_control = (packet[3] >> 4) & 0x03
    if adaptation_control in {0, 2}:
        return pid, payload_start, b""
    offset = 4
    if adaptation_control == 3:
        offset += 1 + packet[4]
    if offset > TS_PACKET_SIZE:
        raise ValueError("Invalid MPEG-TS adaptation field")
    return pid, payload_start, packet[offset:]


def read_video_pes(path: Path) -> list[VideoPes]:
    selected_pid: int | None = None
    current = bytearray()
    packets: list[VideoPes] = []

    with path.open("rb") as handle:
        while packet := handle.read(TS_PACKET_SIZE):
            if len(packet) != TS_PACKET_SIZE:
                raise ValueError("MPEG-TS file has trailing partial packet")
            parsed = _ts_payload(packet)
            if parsed is None:
                continue
            pid, payload_start, payload = parsed
            is_video_start = payload_start and len(payload) >= 4 and payload[:3] == b"\x00\x00\x01" and 0xE0 <= payload[3] <= 0xEF
            if selected_pid is None and is_video_start:
                selected_pid = pid
            if pid != selected_pid:
                continue
            if payload_start:
                if current:
                    packets.append(parse_video_pes(bytes(current)))
                    current.clear()
                if not is_video_start:
                    continue
            current.extend(payload)
    if current:
        packets.append(parse_video_pes(bytes(current)))
    if selected_pid is None or not packets:
        raise ValueError(f"No video PES packets found in MPEG-TS file: {path}")
    return packets


def read_audio_pes(path: Path) -> list[AudioPes]:
    """Read one AC-3/private_stream_1 PID from an MPEG-TS file."""
    selected_pid: int | None = None
    current = bytearray()
    packets: list[AudioPes] = []
    with path.open("rb") as handle:
        while packet := handle.read(TS_PACKET_SIZE):
            if len(packet) != TS_PACKET_SIZE:
                raise ValueError("MPEG-TS file has trailing partial packet")
            pid, payload_start, payload = _ts_payload(packet)
            is_audio_start = payload_start and payload.startswith(b"\x00\x00\x01\xbd")
            if selected_pid is None and is_audio_start:
                selected_pid = pid
            if pid != selected_pid:
                continue
            if payload_start:
                if current:
                    packets.append(parse_audio_pes(bytes(current)))
                    current.clear()
                if not is_audio_start:
                    continue
            current.extend(payload)
    if current:
        packets.append(parse_audio_pes(bytes(current)))
    if selected_pid is None or not packets:
        raise ValueError(f"No private_stream_1 audio PES packets found in MPEG-TS file: {path}")
    return packets
