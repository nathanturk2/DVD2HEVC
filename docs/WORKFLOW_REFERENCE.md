> UHD-BD is the current default and uses stock VLC with Java; no VLC patch is needed. This document describes the legacy HEVC DVD output or the intermediate DVD encoding stage. See [UHD_BD.md](UHD_BD.md) and the [current README](../README.md).

# Detailed workflow reference

This reference preserves the previous detailed guide. Start with the [current README](../README.md). Repository-local `reports/` paths in older examples now default to per-user state storage; phase commands are for development.

# DVD2HEVC

DVD2HEVC converts a local, decrypted DVD ISO backup into a smaller custom ISO
whose MPEG-2 video has been replaced by HEVC/H.265. It preserves the authored
disc experience: menus, extras, normal/extended branching cuts, chapters,
angles, audio and subtitle selection, button highlights, and DVD VM commands.
The compact authoring path rebuilds every video domainâ€”title VOBs, VTS menus,
and the VMG menu in `VIDEO_TS.VOB`â€”and relocates the corresponding VTSI/VMGI
navigation tables instead of forcing HEVC into the old MPEG-2 sector sizes.

The output is deliberately not standards-compliant DVD-Video. It requires the
project's patched VLC 3.0.23 build; ordinary DVD players and stock VLC do not
understand HEVC inside DVD program streams. DVD2HEVC does not decrypt discs,
provide keys, download media, or include copyrighted disc assets.

Project status: alpha. The complete workflow has passed difficult-disc testing
on *The Intern*, *The Bourne Identity*, *Taken 2*, and *Taken 3*, including
shared clips and normal/extended cuts. Compatibility beyond those fixtures is
community-tested rather than guaranteed.

The exact current scope is maintained in
[docs/CAPABILITIES.md](CAPABILITIES.md), and the rules used to prevent
fixture-specific overfitting are in [docs/INVARIANTS.md](INVARIANTS.md).

## Installation Requirements

DVD2HEVC currently targets Windows 10/11 with Python 3.10 or newer. NVIDIA
NVENC is the release-gated default, while Intel QSV, AMD AMF, and software x265
can be selected when FFmpeg and the installed hardware support them. Install
the Python package from the project directory:

```powershell
python -m pip install -e .
```

The complete workflow also needs:

- FFmpeg and FFprobe with at least one supported HEVC encoder on `PATH`.
- HandBrakeCLI on `PATH`.
- Git, Visual Studio 2022 C++ Build Tools, and Meson to build the native
  `dvdinspect` helper with `native\build-native.ps1`.
- `genisoimage` or `mkisofs`, either natively or in WSL Ubuntu 24.04.
- The private patched VLC 3.0.23 build described in
  [docs/VLC_BUILD.md](VLC_BUILD.md).

Run `python dvd2hevc.py tools` after setup. It reports every detected path and
does not claim the complete converter is ready until all of these requirements
are present.

## Quick Start

The normal Windows entry point is the **DVD2HEVC** desktop shortcut. It opens
the graphical interface with four areas:

- **Convert** keeps source/output naming, video quality, encoder, deinterlace,
  audio, and language rules together.
- **Batch queue** finds ISO backups in a directory and applies the current
  Convert settings to each selected disc. A watched batch labels the complete
  workload as Found, Active, Waiting, Done, and Attention while admitting only
  one disc to planning/conversion at a time.
- **Jobs & progress** manages pause/resume/cancel/play actions and shows an
  overall media-duration-weighted bar plus independent video, audio, and
  mux/staging lanes. Long video cells report live FFmpeg media-time progress.
- **Presets & tools** loads/saves BD2HEVC-style presets, checks dependencies,
  and safely verifies or prepares the private HEVC-compatible VLC player.

The GUI calls the same job/configuration APIs as the CLI; GUI jobs can be
managed from the command line and vice versa. Reinstall the shortcut after
moving the source tree with:

```powershell
powershell -ExecutionPolicy Bypass -File tools\install-gui-shortcut.ps1
```

Every actionable GUI control has delayed hover help, and **F1** opens a concise
quick start. Under **Presets & tools**, **Verify / set up HEVC VLC** is
state-aware: if the compatible private player is already present it performs a
verification-only no-op. Otherwise it confirms the exact source, unmodified
VLC runtime, and private destination before building. The normal VLC
installation is only read as a copy source and is never patched or overwritten.
See [docs/VLC_BUILD.md](VLC_BUILD.md) for first-time build requirements.

Check FFmpeg, the default NVENC path, HandBrakeCLI, the native disc inspector,
and patched VLC:

```powershell
python dvd2hevc.py tools
```

Probe every selectable encoder using DVD-resolution Main-profile HEVC and two
forced random-access boundaries:

```powershell
python dvd2hevc.py tools --probe-all-encoders
```

Preview the complete conversion without encoding or modifying the source:

```powershell
python dvd2hevc.py auto "D:\DVD backups\Movie.iso" --dry-run
```

Convert in the foreground with the balanced target-bitrate VBR default:

```powershell
python dvd2hevc.py auto "D:\DVD backups\Movie.iso"
```

For long conversions, queue the job in the background and return immediately:

```powershell
python dvd2hevc.py start "D:\DVD backups\Movie.iso" "F:\DVD HEVC\Movie (DVD) (HEVC).iso"
python dvd2hevc.py status --watch
```

Queue a whole directory. Jobs run one at a time so they do not compete for the
GPU, disk, or validation tools:

```powershell
python dvd2hevc.py queue "D:\DVD backups" --output-dir "F:\DVD HEVC" --preset compact
python dvd2hevc.py jobs --active
```

For an intake folder that keeps receiving new backups, start a persistent
watched batch instead:

```powershell
python dvd2hevc.py watch-folder "D:\Incoming DVDs" --output-dir "F:\DVD HEVC" --recursive
python dvd2hevc.py watches
```

A watched batch snapshots the selected conversion settings and maintains a
durable per-file ledger under `reports/jobs/watched-batches`. It continues
polling after the conversion queue becomes empty and after the GUI closes. An
explicit **Start watched batch** or `watch-folder` command resumes Pause all so
the newly requested watch genuinely begins; an existing watcher held by Pause
all is labelled `paused`. An
ISO must remain unchanged for 60 seconds before it is planned. On Windows,
DVD2HEVC also verifies that no process still holds the ISO open for writing;
the ledger reports `waiting-for-backup` until that writer closes. Together these
checks prevent a paused, partially copied, or actively authored image from being
opened. Stable images are considered from oldest modification time to newest,
giving a newly finished backup more time to receive its final name. If an ISO is
renamed, its matching durable ledger entry follows it instead of treating it as
a duplicate new disc; a queued/running job is stopped and rebuilt with the new
source and output names. Each path and size/modified-time fingerprint is queued once. Existing
output ISOs are marked as already handled and are never overwritten. Canceling
a watched job records that terminal state against the fingerprint; the watcher
does not silently add it again.

Use **Stop selected** in the GUI or `stop-watch WATCH_ID` to stop discovering
new files; already queued jobs continue. **Reset selected** or
`reset-watch WATCH_ID` starts a fresh watch with the same folders and settings
but an empty discovery history. Existing outputs still remain protected, so
move or delete an output first only when intentionally reprocessing it. Active
watches resume when the GUI is opened after a restart. A Windows logon startup
entry may also run `tools/dvd2hevc-recover.pyw` with `pythonw.exe`; this restores
active watchers and requeues a conversion interrupted by the previous Windows
boot without showing a console or overriding Pause all.

Withdraw a job that has not started, or requeue a retained failed/canceled job:

```powershell
python dvd2hevc.py cancel JOB_ID
python dvd2hevc.py resume JOB_ID
```

Running jobs and their helper-process trees stop immediately while completed,
resumable atomic work remains on disk. Pause all later discs
and watched-folder discovery without interrupting the active conversion:

```powershell
python dvd2hevc.py pause-queue
python dvd2hevc.py resume-queue
```

To regain control of the entire intake and queue in one operation, use the GUI
**Cancel all** button or:

```powershell
python dvd2hevc.py cancel-all
```

This stops every watched batch, cancels all waiting jobs, immediately stops
the active job's entire process tree, and leaves the queue paused. Completed outputs and resumable
work are retained. Planning and conversion share one global cross-process work
slot, and every command-line child is launched without a visible Windows
console, so different queue entry points cannot run multiple discs or produce
PowerShell/cmd/WSL window storms.

Before a new disc starts, DVD2HEVC checks the free space on the volume holding
its managed workspace. A watched disc with insufficient room remains visibly
`waiting-for-space` and retries automatically instead of being recorded as a
media failure. After a managed job passes final verification, bulky extracted
VOBs and reproducible video/audio intermediates are removed automatically;
plans, logs, navigation metadata, validation reports, and failed/canceled job
caches remain available for diagnosis and resume.

If the destination drive nevertheless fills during conversion, DVD2HEVC
removes only that job's newly created incomplete ISO (including its exact
hidden authoring `.part` file) and pauses both the conversion queue and watched
folder discovery. An ISO that existed before the job was planned is never
deleted. Free enough destination space, then press **Resume all** in the GUI or
run `python dvd2hevc.py resume-queue`; this explicit action acknowledges that
storage is ready again. Resume the failed disc itself when it should be retried.

New managed binary workspaces use short private paths under the per-user local
application-data directory instead of repeating long disc names below the report
tree. Existing and explicitly selected work directories remain resumable at
their recorded paths.
The destination library remains ISO-only: final authoring, structural
verification, and CSS-safety diagnostics are retained inside the job's recorded
`work_root/final-output-reports` directory rather than beside the finished
image.

Terminal jobs also update a local media-free compatibility history. It retains
sampled source/output identity, settings, structural evidence and outcome after
a source ISO is deleted. See
[docs/COMPATIBILITY_HISTORY.md](COMPATIBILITY_HISTORY.md), or run:

```powershell
python dvd2hevc.py compatibility-history --rebuild
python dvd2hevc.py review-job JOB_ID --result passed --note "Menus and feature checked"
```

Open a completed ISO or job id in the required private VLC build:

```powershell
python dvd2hevc.py play "F:\DVD HEVC\Movie (DVD) (HEVC).iso"
python dvd2hevc.py play 20260714-170227-Movie
```

When no output name is supplied, DVD2HEVC uses
`Movie (DVD) (HEVC).iso`. The `(DVD)` format tag remains visible in compatible
library frontends, while `(HEVC)` identifies the file as requiring the private
DVD2HEVC VLC player. This exact two-tag contract prevents an unrelated ISO from
being mistaken for a converted disc.

Automatic filename tags are optional. In the GUI, clear **Add (DVD) (HEVC) to
generated filenames**. In the CLI, pass `--no-filename-tags` (or its
`--no-output-tags` alias). Generated names then use `Movie - converted.iso`.
Explicit output filenames are always preserved exactly as entered. Untagged
outputs remain valid DVD2HEVC images, but tag-dependent library frontends such
as Play Movie will not discover them automatically.

## Quality And Deinterlacing

The default mode derives a video target directly from the measured MPEG-2
bitrate. VBR varies bitrate with scene complexity while targeting that average;
CBR constrains the encoder to a constant or near-constant rate. Manual CQ still
uses the same direction and spelling as HandBrake: lower values are larger and
higher quality. One exact rate-control instruction is used for encoding and
final compaction, with no hidden per-cell quality reduction.
If one requested HEVC VOBU is larger than its old MPEG-2 sector slot, compact
mode allocates that VOBU additional sectors and recovers the space elsewhere;
it does not silently lower the requested bitrate. Complete decode, per-VOBU
random-access, relocated navigation, and final ISO validation must still pass.

- `balanced` (default): target-bitrate VBR at `1.00x`, NVENC P6.
- `compact`: `cq:27`, the setting used by the Phase 7 compatibility fixtures.
- `high-quality`: `cq:20`, for users who prefer the familiar HandBrake margin.
- `compact-stereo`: target-bitrate VBR plus 256 kbit/s AC-3 stereo tracks.
- Any explicit half step, such as `--quality cq:24.5`.

```powershell
python dvd2hevc.py auto Movie.iso --quality cq:24
python dvd2hevc.py auto Movie.iso --preset high-quality
python dvd2hevc.py preset save my-dvds --quality cq:23.5 --deinterlace auto
python dvd2hevc.py auto Movie.iso --preset compact-stereo
python dvd2hevc.py auto Movie.iso --quality target-bitrate --target-bitrate-multiplier 1.20
python dvd2hevc.py auto Movie.iso --quality target-bitrate --bitrate-mode cbr
python dvd2hevc.py auto Movie.iso --quality target-bitrate --main-title-quality cq:20
python dvd2hevc.py auto Movie.iso --quality target-bitrate --top-n-count 3 --top-n-quality cq:22
```

Target-bitrate mode uses the mandatory full-sector scan to measure physical
MPEG-2 video payload and targets 25% of that bitrate at `1.00x`.
`--target-bitrate-multiplier 1.20` retains 20% more target bitrate; `0.80`
targets 20% less. The accepted range is `0.25x` to `4.00x`. The exact measured
source bitrate, multiplier, VBR/CBR selection, and resolved target are retained
in completed reports. Target mode does not estimate or select a CQ.

Like BD2HEVC, general quality may be supplemented with main-title or top-N
quality. DVD alternate cuts can share the same physical cells, so DVD2HEVC
applies an explicit title override to the entire affected VTS. It does not
duplicate a clip or splice different encoder prediction chains merely to make
two logical titles use different settings.

## Selectable HEVC encoders

The end-user commands, queue records, and saved presets accept the same four
HEVC backend names as BD2HEVC:

```powershell
python dvd2hevc.py auto Movie.iso --encoder hevc_nvenc
python dvd2hevc.py auto Movie.iso --encoder hevc_qsv
python dvd2hevc.py auto Movie.iso --encoder hevc_amf
python dvd2hevc.py auto Movie.iso --encoder libx265
python dvd2hevc.py preset save intel-dvds --quality cq:24 --encoder hevc_qsv
```

`--encoder-preset p1` through `p7` is backend-neutral: p1 is fastest and p7 is
slowest/highest-efficiency. It maps to each encoder's native presets. All
choices remain H.265/HEVC; there is no H.264 or MPEG-2 fallback.

DVD2HEVC is stricter than ordinary file encoding. Preflight actually starts
the selected backend, and every real VOBU must subsequently contain Main
profile 8-bit HEVC, an AUD, repeated VPS/SPS/PPS, and a closed IDR at its forced
DVD random-access boundary. B-frames are disabled. A backend that is merely
listed by FFmpeg but cannot satisfy this profile is rejected.

| Encoder | Current release status | Notes |
| --- | --- | --- |
| `hevc_nvenc` | Release-gated | Full difficult-disc ISO and patched-VLC gates pass; default. |
| `hevc_qsv` | Experimental | Runtime profile and repeated-boundary probe pass on the development machine; a complete authored-disc gate is still required. |
| `libx265` | Experimental | Runtime profile and repeated-boundary probe pass; much slower and not yet full-disc release-gated. It is the only backend allowed to retain interlaced output in low-level mode. |
| `hevc_amf` | Experimental/unverified | The command profile is implemented, but no compatible AMD device is present on the development machine. Preflight correctly rejects it there. |

CQ spelling and direction remain familiar, but a numeric CQ/ICQ/QVBR/CRF value
is not guaranteed to be visually or size-equivalent between encoder families.
Target-bitrate VBR and CBR are translated into each backend's native bitrate
controls. Compare a short representative title before changing a large library
to an experimental backend.

`--deinterlace auto` is the default. It runs pixel-based `idet` analysis near
the beginning, middle, and end of every physical DVD cell. Confidently
progressive cells bypass deinterlacing; detected interlaced and ambiguous short
cells are deinterlaced with BWDIF to progressive 50/59.94-field motion. Reports
retain every sample classification, repeated-field count, selected cadence,
and exact FFmpeg filter chain. MPEG-2 interlace flags are not trusted by
themselves.

This release does not yet claim HandBrake-equivalent within-cell Decomb or
inverse telecine. FFmpeg's `fieldmatch`/`decimate` path can produce variable
frame timing for mixed telecined and interlaced content, so it must pass the
same VOBU-timestamp, forced-IDR, navigation, and difficult-disc gates before it
can replace the conservative cell-level policy. Use `always` or `off` only when
you have a specific reason.

The release profile remains HEVC Main 8-bit. Main10 can be useful as an
optional efficiency/gradient-preservation profile even for an 8-bit source,
but it is not assumed to add source detail and it narrows decoder
compatibility. A future Main10 option must be runtime-probed per encoder and
pass complete authored-disc and patched-VLC gates before becoming selectable.

Audio passthrough remains the default. `--audio-mode compact-stereo` independently
downmixes multichannel title audio to DVD-compatible 48 kHz AC-3 while
preserving its original selection slot and language metadata. Existing AC-3
mono/stereo is kept elementary-stream-identical, avoiding an unnecessary lossy
generation or bitrate increase. The downmix defaults are 256 kbit/s stereo and
128 kbit/s mono; both are configurable. Menu-domain audio remains passthrough.

Language overrides can be repeated and are available in GUI presets. Language
codes come from DVD IFO descriptors, so they survive cell extraction:

```powershell
python dvd2hevc.py start Movie.iso --audio-mode passthrough `
  --audio-language eng=passthrough --audio-language other=compact-stereo
```

When one track in a title set is compacted, other AC-3 tracks in that title set
are rebuilt byte-exact in their original selection ordinals. Selected DTS,
LPCM, MPEG-1 audio, and MPEG-2 audio tracks are decoded and written as 48 kHz
DVD-compatible AC-3 in the same selection ordinals, with matching IFO format
descriptors and PES/substream IDs. A fully passthrough VTS retains every source
format unchanged. In a selectively rebuilt VTS, non-AC-3 tracks must be selected
for compaction rather than passthrough because their original packetizers are
not yet rewritten byte-exact.

## Background Jobs And Support

Every expensive boundary is resumable and writes atomic status under
the `work_root` recorded in `reports/jobs/JOB/job.json`. `status --watch` shows a top pipeline bar plus separate
video, audio, and mux/staging task lanes. Compact audio uses bounded track and
title-set workers and overlaps all-HEVC disc staging; separate discs remain FIFO
so hundreds of library jobs cannot compete for NVENC or the authoring tools.
Ordinary and physical/interleaved title sets use compact-first intermediates: the exact HEVC
stream is decoded and checked directly, and DVD sectors are built once during
final compaction instead of being packed, split, reread, and discarded per
cell. Pipeline depth 2 can overlap one encode with one validation while hard
limits retain one hardware encoder session and one full-cell validation reader.
Compact-stereo audio for title cells is prefetched in a bounded lane and
reused by the final layout.
Fresh queued jobs begin at 0%. The pipeline and lane bars represent cumulative
work and retain completed milestones when the active subtask changes, so a bar
never resets merely because encoding gives way to compaction or authoring.

Create a privacy-conscious support bundle after a failure:

```powershell
python dvd2hevc.py diagnose JOB_ID
```

The bundle contains redacted generated reports, tool information, and a log
tail. It deliberately excludes ISO/VOB media, decryption keys, and disc assets.

The original low-level scan, extraction, repacking, compaction, authoring, and
validation commands remain available through `--help` for development and
diagnostics. `scan` reads every VOB sector by default; `--quick` inspects only
the UDF/VIDEO_TS inventory and neither mode mounts the ISO.

## Status

- Current compact format profile: implemented for title, VTS-menu and VMG-menu
  domains; the older sector-preserving profile remains a historical diagnostic
  prototype rather than the Phase 8 user workflow.
- ISO/UDF inventory: implemented.
- Structural VOB/PES, NAV-pack, capacity, and scrambling scan: implemented.
- HandBrake logical-title discovery: implemented as a temporary backend.
- Native libdvdread physical disc graph: first prototype implemented.
- Exact physical-cell extraction and VOBU capacity analysis: implemented.
- Sector-preserving HEVC cell repacker: first prototype implemented.
- Patched VLC DVDNAV HEVC playback: proven on The Intern staging fixture.
- Repeatable headless VLC validation gate: implemented.
- Automated multi-cell title conversion and validation: implemented.
- Atomic DVD-folder staging and replacement read-back: implemented.
- Source/output DVDNAV trace comparison: implemented.
- Progressive NVENC with automatic cadence detection: implemented.
- DVD random access: every video-bearing VOBU now forces a true IDR and repeats
  VPS/SPS/PPS, matching DVD resume, chapter, and time-map entry semantics.
- VOBU stream identification: every video-bearing VOBU carries an HEVC
  program-stream map, so a patched player can identify the codec after any
  authored jump rather than relying on state from an earlier cell.
- CSS-safe HEVC signalling: the fixed-length PSM control byte cannot be
  mistaken for legacy PES scrambling at sector offset `0x14`. Final-image
  audit and lossless migration commands are implemented, and both Phase 7
  drivers enforce the audit automatically without re-encoding.
- Whole-cell quality fallback: implemented; VOBUs from different CQ encodes
  are never stitched into one prediction chain.
- Complete physical VTS 1 title-domain conversion: implemented.
- Complete VMG/VTS menu-domain conversion: implemented on The Intern.
- Complete DVD folder and DVD-Video-ordered UDF authoring: implemented.
- Authored ISO file-hash, physical-graph, full-sector, and VLC gates: passed.
- Headless active-menu action traces: implemented for the baseline disc.
- Clear-HEVC key-scan delay: resolved with libdvdread's existing, output-scoped
  `DVDREAD_NOKEYS=1` switch; no additional VLC patch required.
- Phase 6 exact-CQ compact layout: implemented; all 15,421 VOBUs use CQ 20 with
  a projected 33.398% VOB-size reduction and no local quality fallback.
- HandBrake/BD2HEVC-style `cq:N` CLI spelling: implemented; lower remains higher
  quality, including half steps such as `cq:26.5`. Cross-backend equivalence is
  not claimed.
- Selectable HEVC backends: NVENC, QSV, AMF, and libx265 flow through presets,
  queues, title/menu/interleaved encoding, compaction identity, and reports.
  Strict runtime and per-VOBU random-access gates prevent a nonconforming
  backend from reaching a successful ISO.
- Source-derived target-bitrate mode: measures MPEG-2 elementary video, derives
  a 25% HEVC target at `1.00x`, and sends that exact target to native VBR or CBR
  controls without mapping it through CQ. Manual `cq:N` remains separate.
- Historical Phase 6 `compact-auto` diagnostics remain reproducible for their
  recorded fixtures, but are not used by the Phase 8 GUI, presets, or user CLI.
- DVD-compatible compact-stereo: every title-domain stream is independently
  encoded as AC-3 48 kHz, 256 kbit/s stereo or 128 kbit/s mono. Multi-cell,
  multi-stream IFO rewriting, exact PES readback, full decode, cache identity,
  bounded parallel workers, and full-disc orchestration are implemented.
- Bourne compact-audio release gate: all 10 title sets and 61 physical cells
  pass, covering 58 cell/stream encodes and a five-track 5.1 domain without
  changing audio selection ordinals.
- First full-disc mature compact-stereo image: Bourne CQ 27 authors to
  2,665,879,552 bytes, 66.911% below its source (3.022:1). Title domains are
  71.183% smaller; 44/44 UDF files, the complete physical graph, 22,409
  CSS-safe maps, and patched-VLC hardware title/menu gates pass. Existing
  192 kbit/s stereo extras are elementary-stream-identical.
- First navigable compact VTS: VTS 2 IFO/BUP, PGC, C_ADT, VOBU_ADMAP, and time
  maps rewritten; libdvdread graph, patched-VLC playback, and mid-title seek
  gates passed at 51.654% smaller VOB size.
- Compact AC-3 DVD transport: 337 access units reframed into 172 validated DVD
  private-stream PES packets with hash-identical elementary audio.
- First authored compact-stereo VTS: VTS 2 is 55.154% smaller, its stereo VOB
  and IFO metadata agree, all 17 ISO files hash back, the compact graph is exact,
  and normal plus mid-title-seek playback pass from the authored UDF image.
- First combined-title compact ISO: VTS 1 and VTS 2 are relocated into a
  4,590,968,832-byte image; all 15 files and the graph match, while titles 1â€“4,
  synthesized-pack seeking, and menu startup pass.
- First corrected disc-wide compact-auto image: continuous CQ 27 motion plus
  selective entry-cell IDR produces a 2,743,521,280-byte ISO. All 13 files and
  the physical graph verify exactly; menu startup, Play, an extra, warning and
  feature transitions, seeking, and the previous corruption region pass in the
  patched VLC build.
- First fully automatic cell-entry image: the compact-auto rebuild authors a
  2,743,570,432-byte ISO with one IDR and one HEVC map per physical title cell.
  All 13 files and the graph verify exactly; the root menu, feature transition,
  interactive mid-feature seeking, and continuous playback are visually clean
  in the patched VLC build.
- Phase 7 plan-driven compatibility gates: implemented for The Bourne Identity,
  Taken 2, and Taken 3. Ordinary/shared cells can proceed while branching cells
  fail before encode.
- First difficult-disc NVENC gate: Taken 2 title 3 passes with two validated
  cells; rerunning reuses both cached physical encodes.
- Interleaved branching conversion: all 44 Taken 2 and 56 Taken 3 branching-cell
  references resolve through C_ADT. Six- and twenty-extent Taken 2 cells pass
  NVENC CQ 27 encode, logical repack, full decode, physical split-back, audio
  hashing, selected-range readback, and byte-identical alternate-sector gates.
- Resumable unique-task VTS automation: Taken 2 reduces to 72 physical tasks
  and Taken 3 to 114, each with complete gap-free/non-overlapping domain
  coverage, atomic status, logs, and hidden background launch support.
- General full-disc automation: the Phase 7 driver derives ordinary,
  multi-PGC/shared, interleaved, and menu work from the compatibility plan,
  compacts every discovered title set, and enforces decryption, exact readback,
  UDF/physical-graph, CSS-safe signalling, and hardware playback gates.
- Tiny authored-still validation: known replacement cells are explicitly
  probed as MPEG program streams, avoiding FFprobe autodetection failure on a
  valid one-picture HEVC cell while retaining complete decode/frame checks.
- Complete Taken 2 branching VTS gate: all 72 tasks, 16,634 VOBUs, and 212,407
  frames pass. Manual playback confirms both normal and longer extended cuts,
  plus audio/subtitle switching, from the authored sector-preserving ISO.
- Per-VOBU HEVC random access: stock VLC proves Taken 2 deliberately resumes
  the extended PGC mid-film. New conversions repeat the PSM, VPS/SPS/PPS, and
  IDR in every video VOBU; 45- and 200-VOBU prototypes fully decode with exact
  audio and compact-size changes of -0.282% and +0.109%, respectively.
- Physical-order interleaved compaction: NAV SRI/ILVU, PGCIT, C_ADT,
  VOBU_ADMAP, and time maps relocate without duplicating shared branch clips.
  VTS 5 is 56.286% smaller at exact CQ 27 with no fallback; the verified
  compact ISO is 3,210,002,432 bytes, 54.852% below the sector-preserving gate.
- Hybrid HEVC-to-MPEG-2 return fix: the private VLC build now clears its
  domain-local program-stream map on `DVDNAV_VTS_CHANGE`, preventing a returned
  MPEG-2 menu from being recreated with the prior HEVC codec state.
- First all-domain difficult-disc image: every Taken 2 title set plus the VMG
  and VTS menus is HEVC at NVENC P6/CQ 27. The final 3,101,440,000-byte ISO is
  56.376% smaller than the 7,109,468,160-byte source. The authored intermediate
  has all 21 UDF files and its complete graph verified; the playback-fixed ISO
  then changes only 17,192 same-length PSM packets, retaining that graph and
  compaction exactly. Hardware/software title and menu gates, plus a two-cell
  transition, report first pictures and zero SPS/PPS/NAL corruption. Every
  expensive boundary remains resumable. User visual acceptance of the repaired
  animated menu and continuous playback passed, locking Taken 2 as a permanent
  branching/CSS-safe regression fixture.
- Second all-domain branching image: the generalized driver converts Taken 3's
  ordinary, multi-PGC, interleaved, and menu domains and authors a
  2,957,803,520-byte ISO, 61.829% below its source. Six compact title domains,
  all 24 UDF files, the complete physical graph, 16,310 CSS-safe maps, and
  patched-VLC hardware title/menu gates pass at exact NVENC P6/CQ 27 with no
  fallback. User moving-video/navigation acceptance also passed.
- Many-extra/shared-title image: all 30 Bourne Identity titles and 11 menu
  domains convert, while 114 logical cell references reuse 60 physical cells.
  A final C_ADT coverage gate also promotes one cell referenced by no logical
  title into a physical task, ensuring no title-domain MPEG-2 is left behind.
  The 2,786,334,720-byte ISO is 65.416% below its source; all 44 UDF files, the
  full graph, 22,409 CSS-safe maps, and patched-VLC hardware gates pass at exact
  NVENC P6/CQ 27 with no fallback.
- DVD still-menu transition fix: Bourne's animated-intro submenus exposed an
  HEVC clock/decoder-state race where highlights became active while VLC retained
  the preceding textless transition frame. Patched VLC now resets the menu-cell
  clock and recreates only the HEVC video decoder at title-zero cell changes.
  The disc keeps each authored still as one picture plus HEVC EOS. Five repeated
  hardware-decoded transitions, multi-page Bonus navigation, and a following
  title start pass without parser or decoder errors.

See the [Phase 2](PHASE2.md) and [Phase 3](PHASE3.md) reports for
early measurements and regression commands. The [Phase 4 report](PHASE4.md)
covers hardware encoding and complete VTS validation.
The [Phase 5 report](PHASE5.md) covers menu conversion and the first
complete authored image.
The [Phase 6 report](PHASE6.md) records the compact-layout and quality
policy now under implementation.
The [Phase 7 report](PHASE7.md) records difficult-disc planning, shared-cell
reuse, and the interleaved branching prototype.

Build the current Windows-native inspector with Visual Studio Build Tools:

```powershell
powershell -ExecutionPolicy Bypass -File native\build-native.ps1
```

The script pins libdvdread, applies the project's modern-MSVC compatibility
patch, and leaves downloaded/build files under ignored `work/` directories.

License: GPL-3.0-only. DVD2HEVC is for backups users are legally entitled to
process. It supplies no disc keys or copyrighted disc content.
