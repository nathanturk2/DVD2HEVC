# Phase 8: user experience and release hardening

> Historical custom-DVD implementation notes. For the current default UHD-BD output and stock-VLC requirements, see [UHD_BD.md](UHD_BD.md).

Phase 8 promotes the difficult-disc pipeline proven in Phase 7 into an end-user
workflow. The conversion engine is unchanged: every source is still scanned for
clear content, physical cells are deduplicated, branching extents are handled
through the physical plan, all title/menu domains are converted, title VOBs are
compacted, the ISO is authored and read back, CSS-safe signalling is audited,
and patched-VLC hardware gates run before success is reported.

## End-user commands

- `auto`: foreground plan, conversion, compaction, authoring, and validation.
- `start`: plan one disc, add it to the background queue, and return.
- `queue`: discover multiple ISO files and convert them sequentially.
- `status [JOB] --watch`: display pipeline stage and active nested work.
- `jobs`: list active, failed, or completed jobs.
- `cancel`: withdraw queued work or immediately stop a running job's complete process tree.
- `cancel-all`: stop watched batches, cancel waiting work, immediately stop
  the active conversion process tree, and leave the system paused.
- `resume`: requeue a planned, failed, or canceled job by id.
- `pause-queue` / `resume-queue`: hold later discs and watched-folder discovery
  while allowing the current safe unit/disc to finish.
- `play`: launch a converted ISO through the private patched VLC.
- `preset`: manage reusable HandBrake-style CQ/deinterlace settings.
- `diagnose`: create a redacted, media-free support zip.

The low-level Phase 2-7 commands remain public. They are useful for isolating a
cell, rebuilding one VTS, checking a VLC log, or contributing to the writer,
but a normal user no longer needs to assemble the pipeline by hand.

## Defaults and safeguards

The balanced default is NVENC P6 at `cq:24`. `--encoder` and saved presets can
select `hevc_nvenc`, `hevc_qsv`, `hevc_amf`, or `libx265`; all four names are
HEVC-only. The compact compatibility preset
is `cq:27`, and the high-quality preset is `cq:20`. CQ follows HandBrake's
direction: lower is larger/higher quality. One exact value is used for both the
cell encodes and the compact layout, preventing silent quality fallback.

Automatic deinterlacing is enabled by default. Progressive material remains
progressive; detected interlaced and ambiguous short cells are deinterlaced.
Every planning and conversion path, including foreground, fixed batch, and
watched batch entry points, shares one cross-process work lock. Dispatcher
startup is separately serialized and stale locks are recovered. All Python
child processes use the Windows no-console policy, so PowerShell, cmd, WSL,
FFmpeg, and HandBrake helpers remain hidden. A large queue therefore cannot
start competing disc jobs or make the desktop unusable.

Each runner records the Windows boot session in its durable job. At dispatcher
or optional logon recovery startup, a job left behind by a different boot is
requeued ahead of later work and resumes its validated atomic caches. A runner
that exits within the current boot still fails normally, so automatic restart
recovery does not conceal converter defects. Active watched batches are also
restored without overriding the user's Pause all state.

`status --watch` follows the BD2HEVC model with a top pipeline bar and separate
video, audio, and mux/staging lanes. Progress events are append-only JSON, so a
detached worker can be inspected without owning its console. Compact-stereo uses
two bounded levels by default: two title domains, with two audio tracks per
domain. That audio lane overlaps sector-stage assembly and readback; authoring
and navigation-table mutation remain deterministic and serial.

Audio passthrough remains the fidelity-first default. The built-in
`compact-stereo` preset (or `--audio-mode compact-stereo`) converts every
multichannel title-domain audio selection independently to 48 kHz AC-3,
preserving its IFO ordinal and language metadata. Existing AC-3 mono/stereo is
elementary-stream-identical passthrough rather than a larger lossy re-encode.
Default downmix rates are 256 kbit/s stereo and 128 kbit/s mono.

Preflight requires the source ISO, pycdlib, FFmpeg/FFprobe, the selected HEVC
backend, HandBrakeCLI, the native `dvdinspect` helper, and patched VLC. It runs
a short real encode and requires repeated parameter sets plus IDRs at forced
boundaries rather than trusting FFmpeg's encoder list. A quick physical
compatibility plan is saved before a job is accepted. The expensive full-sector
decryption/integrity scan remains the first conversion stage.

## Release artifacts

The private VLC preparation script pins VLC 3.0.23 and its exact source commit,
applies the checked-in patch, rebuilds only the DVDNAV plugin, and writes a
manifest with source, patch, and plugin hashes. Source releases exclude work
directories, reports, ISOs, logs, native binaries, and third-party VLC files.

The compact-stereo release gate uses the many-extra Bourne fixture. Its 10
title sets and 61 physical cells produce a 2,665,879,552-byte CQ 27 image,
66.911% below source. Title domains are 71.183% smaller. All 44 UDF files, the
physical graph, 22,409 CSS-safe maps, and patched-VLC hardware title/menu gates
pass; existing 192 kbit/s stereo tracks are elementary-stream-identical.

## Remaining limitations

- Full-disc compact-stereo accepts AC-3, DTS, LPCM, MPEG-1 audio, and MPEG-2
  audio title sources and writes DVD-compatible AC-3 with remapped PES/IFO
  descriptors. Selective passthrough inside a rebuilt VTS remains byte-exact
  for AC-3 only; a fully passthrough VTS retains every authored audio format.
- Menu-domain audio remains passthrough. It is normally negligible compared
  with video and changing it would add risk around interactive still timing.
- Title, VTS-menu and VMG-menu video domains are structurally compacted. Menu
  audio remains passthrough because its savings are negligible and rebuilding
  it would add risk around interactive still timing.
- Separate discs remain serial. Concurrency is bounded within one disc so the
  Windows desktop remains usable and NVENC jobs do not contend.
- NVENC is release-gated. QSV and libx265 currently pass the strict synthetic
  profile probe but need full authored-disc qualification; AMF needs an AMD
  validation machine. Manual numeric CQ values are backend-native rather than
  cross-encoder calibrated. The default source-derived target-bitrate VBR/CBR
  policy is implemented for all four backends without CQ calibration.
- The output remains a project format requiring patched VLC; broader player
  support is not a Phase 8 claim.
