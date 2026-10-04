"""Sector-level VOB and MPEG program-stream inspection."""

from __future__ import annotations

from dataclasses import asdict, dataclass


DVD_SECTOR_SIZE = 2048


@dataclass
class VobStats:
    bytes: int = 0
    sectors: int = 0
    pack_sectors: int = 0
    invalid_sectors: int = 0
    pes_packets: int = 0
    video_pes_packets: int = 0
    audio_pes_packets: int = 0
    private_pes_packets: int = 0
    nav_packs: int = 0
    program_stream_maps: int = 0
    padding_packets: int = 0
    video_payload_bytes: int = 0
    scrambled_pes_packets: int = 0
    scrambled_video_packets: int = 0
    scrambled_audio_packets: int = 0
    scrambled_private_packets: int = 0
    trailing_bytes: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


class VobSectorScanner:
    """A file-like sink that validates and counts complete DVD sectors."""

    def __init__(self) -> None:
        self.stats = VobStats()
        self._buffer = bytearray()

    def write(self, data: bytes) -> int:
        self.stats.bytes += len(data)
        self._buffer.extend(data)
        complete = len(self._buffer) // DVD_SECTOR_SIZE
        for _ in range(complete):
            sector = bytes(self._buffer[:DVD_SECTOR_SIZE])
            del self._buffer[:DVD_SECTOR_SIZE]
            self.inspect_sector(sector)
        return len(data)

    def tell(self) -> int:
        return self.stats.bytes

    def seek(self, *_args: object) -> int:
        # pycdlib writes extracted file data sequentially; this makes the sink
        # compatible with its output-file interface without storing the VOB.
        return self.stats.bytes

    def finish(self) -> VobStats:
        self.stats.trailing_bytes = len(self._buffer)
        if self._buffer:
            self.stats.invalid_sectors += 1
        return self.stats

    def inspect_sector(self, sector: bytes) -> None:
        stats = self.stats
        stats.sectors += 1
        if len(sector) != DVD_SECTOR_SIZE or sector[:4] != b"\x00\x00\x01\xba":
            stats.invalid_sectors += 1
            return
        stats.pack_sectors += 1
        if (sector[4] & 0xC0) != 0x40:
            stats.invalid_sectors += 1
            return

        offset = 14 + (sector[13] & 0x07)
        sector_has_nav = False
        while offset + 6 <= DVD_SECTOR_SIZE:
            if sector[offset : offset + 3] != b"\x00\x00\x01":
                # A pack may end in zero stuffing that is not a PS packet.
                if any(sector[offset:]):
                    stats.invalid_sectors += 1
                break
            stream_id = sector[offset + 3]
            packet_length = int.from_bytes(sector[offset + 4 : offset + 6], "big")
            packet_end = offset + 6 + packet_length
            if packet_end > DVD_SECTOR_SIZE:
                stats.invalid_sectors += 1
                break

            if stream_id == 0xBF:
                sector_has_nav = True
            elif stream_id == 0xBC:
                stats.program_stream_maps += 1
            elif stream_id == 0xBE:
                stats.padding_packets += 1

            if stream_id == 0xBD or 0xC0 <= stream_id <= 0xEF:
                self._inspect_pes(sector, offset, packet_end, stream_id)
            offset = packet_end
        if sector_has_nav:
            stats.nav_packs += 1

    def _inspect_pes(self, sector: bytes, offset: int, packet_end: int, stream_id: int) -> None:
        stats = self.stats
        if offset + 9 > packet_end or (sector[offset + 6] & 0xC0) != 0x80:
            return
        stats.pes_packets += 1
        scrambled = ((sector[offset + 6] >> 4) & 0x03) != 0
        if 0xE0 <= stream_id <= 0xEF:
            stats.video_pes_packets += 1
            payload_start = offset + 9 + sector[offset + 8]
            if payload_start <= packet_end:
                stats.video_payload_bytes += packet_end - payload_start
            if scrambled:
                stats.scrambled_video_packets += 1
        elif 0xC0 <= stream_id <= 0xDF:
            stats.audio_pes_packets += 1
            if scrambled:
                stats.scrambled_audio_packets += 1
        else:
            stats.private_pes_packets += 1
            if scrambled:
                stats.scrambled_private_packets += 1
        if scrambled:
            stats.scrambled_pes_packets += 1
