# DVD-HEVC Sector-Preserving Profile v0

Status: alpha profile with complete compact-image fixtures and a pinned patched
VLC implementation.

## Purpose

The profile preserves an existing DVD backup's physical navigation layout while
replacing MPEG-2 video elementary-stream payloads with HEVC. It is deliberately
not a DVD-Video standard extension and requires a compatible player.

## Invariants

The writer first creates a sector-preserving compatibility stage:

1. Every staged VOB remains the same byte length as its source.
2. Every VOB sector remains 2,048 bytes.
3. NAV packs remain at their original logical sector positions.
4. VMG/VTS IFO and BUP files remain byte-identical in that compatibility stage.
5. Subpicture packets remain byte-identical. Audio packets are either
   byte-identical passthrough or use the explicitly declared compact-stereo
   extension below.
6. VOB file boundaries, VOB IDs, cell IDs, PGCs, programs, chapters, angle
   blocks, and DVD VM commands remain unchanged.
7. Output content is not CSS-encrypted.

## Video

- Codec: HEVC/H.265 Main, 8-bit for profile v0.
- Bitstream: Annex B.
- PES stream ID: the source DVD video stream ID, normally `0xE0`.
- Program stream signalling: a program stream map declares MPEG stream type
  `0x24` for the HEVC video stream. Its fixed-length control byte is `0xc0`,
  with the CRC recalculated normally. This clears the legacy scrambling bits
  seen at DVD sector offset `0x14`, preventing libdvdcss from falsely
  decrypting an already-clear HEVC sector while retaining the 20-byte packet
  size required by both sector-preserving and compact layouts.
- Source resolution, display aspect ratio, frame cadence, and field behavior
  are preserved unless a later user-selectable conversion mode says otherwise.
- Random-access points must be aligned sufficiently for cell, chapter, angle,
  menu, and VOBU navigation.

## Sector packing

The original video PES sectors define a hard capacity budget. HEVC access units
are repacketized into those slots. Unused capacity is represented by valid
program-stream padding. NAV, audio, subpicture, and other non-video sectors are
copied at the same positions.

If an encoded interval exceeds its physical capacity, the writer retries it at
a smaller target. It must fail atomically rather than move sectors or silently
damage navigation. Relocating VOBUs and rewriting navigation is outside v0.

## Player signalling

The first player is a pinned, patched VLC 3.0.23 build. VLC's general MPEG-PS
code already maps stream type `0x24` to HEVC, but its stock DVDNAV module
discards PSM packets and assumes DVD video is MPEG-2. The project patch retains
the PSM, configures the DVD video ES from it, and makes still-frame flushing
codec-aware.

An ISO-root manifest may describe the writer and profile revision for diagnosis,
but correct demuxing should be driven by in-stream signalling.

## Compatibility

Unmodified hardware DVD players and ordinary DVD software are not expected to
play this profile. A disc may remain mixed-codec during development: unsupported
physical cells can remain MPEG-2 and must be listed in the conversion report.

## Compact final profile

The published ISO is derived from the validated sector-preserving stage. The
compact pass removes HEVC padding from title domains, relocates VOBUs, and
rewrites the affected PGC sector fields, C_ADT, VOBU_ADMAP, time maps, VOB file
boundaries, title-set positions, and UDF extents. Menu allocations remain
sector-preserved because their small residual padding is not worth weakening
proven button/control addressing.

Each compact VTS is committed through a copy-on-write stage and re-read before
the next VTS is moved. The final image must pass UDF file hashes, an exact
normalized libdvdread physical graph, a complete CSS-safe HEVC map audit, and
patched-VLC title/menu hardware gates. Compaction is therefore downstream of,
and cannot weaken, the sector-preserving compatibility baseline.

## Compact-stereo audio extension

The optional extension applies only to title domains. Each original audio
selection keeps its DVD stream ordinal and IFO language metadata. AC-3, DTS,
LPCM, MPEG-1 audio, and MPEG-2 audio can become 48 kHz AC-3 stereo; existing
AC-3 mono/stereo remains elementary-stream-identical. The writer maps both DVD
private substreams and ordinary MPEG audio stream IDs into AC-3
`private_stream_1` PES, updates the corresponding IFO coding descriptor, reuses
source audio pack positions where possible, and may add valid packs inside an
expanded VOBU.
Any expanded VOBU is retimed within its original SCR interval; encode quality
and audio bitrate are never reduced to force a legacy local sector count.

Before staging, every generated PES packet is read back from every compact VOBU
and compared byte-for-byte with the plan. IFO audio coding, sample-frequency,
and channel descriptors are then updated for all retained stream ordinals.
Menu audio remains passthrough.
