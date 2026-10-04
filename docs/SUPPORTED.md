# Supported scope

The main Windows workflow accepts readable, already-decrypted DVD ISO backups.
The mandatory source scan rejects scrambled packets and invalid sectors before
encoding. DVD2HEVC does not decrypt or repair incomplete backups.

## Default UHD-BD output

HEVC media is authored into BDMV/UDF 2.50 with the DVD VM emulated in BD-J.
Stock VLC with libbluray BD-J and a Java 11 JDK is the tested environment;
patching VLC is unnecessary. Original menus, highlights, extras, chapters,
shared branching footage and audio/subtitle selection are retained subject to
the explicit alpha limits in [UHD_BD.md](UHD_BD.md). Multi-angle and
random/shuffle PGCs currently fail during planning. Native chapter lists can
cover only one compatible run when a PGC has independent control/still cells.

Video stays at source DVD resolution. This output does not create 4K/HDR or
establish UHD hardware conformance. Whole-film decoded continuity and every
navigation route are not certified. The documented test corpus contains ten
commercial DVD backups, plus the generated Colour Lab integration fixture.

## Legacy HEVC DVD output

`--output-format dvd-hevc` keeps the original custom DVD program-stream format
and private patched VLC 3.0.23 contract. It uses genisoimage/mkisofs. See
[VLC_BUILD.md](VLC_BUILD.md) and the explicitly legacy
[workflow reference](WORKFLOW_REFERENCE.md). Existing queued jobs/watches keep
this format when their recorded settings predate the new output choice.

## Encoding and audio

VTS and VMG menu VOBs participate in the encoding and compaction workflow,
alongside title VOBs. The new author then carries their original navigation
and highlight data into BD-J.

NVENC, QSV, AMF and software x265 are selectable; each must initialize and pass
the codec/random-access runtime probe on the actual machine. NVENC has the
strongest commercial conversion evidence. Software x265 passes the generated
integrated disc; QSV/AMF need their own hardware release matrix. Numeric CQ
values are not calibrated identically across backends. Balanced target-bitrate
uses measured MPEG-2 payload/duration rather than a CQ translation.

Passthrough preserves the encoding stage's source audio. Compact-stereo writes
48 kHz AC-3 for selected title tracks while keeping logical ordinals/languages.
The UHD-BD muxer preserves supported streams and reports explicitly when an
audio format requires conversion or only complete access units are verified.
Original menu audio plays once during padded still presentations.

Read the saved reports for actual checks performed. A successful job proves its
automated source/graph/media/ISO/startup checks; it does not prove every route.
