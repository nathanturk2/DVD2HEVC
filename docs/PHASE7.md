# Phase 7: difficult-disc compatibility

> Historical custom-DVD implementation notes. For the current default UHD-BD output and stock-VLC requirements, see [UHD_BD.md](UHD_BD.md).

Phase 7 generalizes the proven *The Intern* pipeline before another complete
disc encode. Its fixtures are *The Bourne Identity*, *Taken 2*, and *Taken 3*.
The workflow deliberately performs quick physical planning and small NVENC
gates before any long background job.

## Current results

- Native quick graphs and compatibility plans exist for all three fixtures.
- *The Bourne Identity*: 30/30 logical titles are safe for the contiguous-cell
  pipeline. Its 114 logical cell references reduce to 60 unique physical cells;
  the final coverage gate also found one C_ADT cell not referenced by any title.
- *Taken 2*: the ordinary-title gate passed, and the normal/extended feature
  titles now use the Phase 7 segmented conversion path for their 44
  interleaved branching-cell references.
- *Taken 3*: 6/9 titles are safe. Its feature cuts use 56 interleaved cells; one
  additional title maps to multiple PGCs and is also blocked for now.
- The first *Taken 2* NVENC gate (title 3, two cells, 23 VOBUs, 276 frames)
  passed at CQ 27. A repeat run reused both validated physical cells.
- An attempted *Taken 2* feature-title gate fails before extraction or encode,
  proving that the compatibility plan is enforced by the runner.
- The complete *Taken 2* VTS 5 run passed all 72 unique physical tasks: 16,634
  VOBUs, 212,407 output frames, byte-identical audio, and exact split-back
  readback. Twenty-four reused PGC references do not create duplicate encodes.
- A sector-preserving 7,109,896,192-byte ISO hashes back exactly and retains the
  source physical graph. Manual playback confirms both normal and extended
  cuts, with the extended cut actually longer, plus working audio and subtitle
  selection.
- Interleaved compaction now preserves physical ILVU order while relocating
  NAV, PGCIT, C_ADT, VOBU_ADMAP, and time-map addresses. The compact VTS 5 has
  1,478,844 sectors versus 3,383,030 (56.286% smaller); its authored ISO is
  3,210,002,432 bytes versus 7,109,896,192 (54.852% smaller).
- The plan-driven full-disc build has now passed. It converts all five title
  sets plus both menu domains, then compacts every title VTS into a
  3,101,440,000-byte ISO versus the 7,109,468,160-byte source (56.376% smaller,
  2.292x compression). All 21 UDF files match the final stage, and the complete
  compact physical graph matches. A same-size signalling repair changed only
  the 17,192 HEVC program-stream maps, retaining every layout and compaction
  result. The playback-fixed image SHA-256 is
  `257eaf4cfa2d675bdfe00aac01d6ce2e18833a1ab64981fd6f54f70c92a184d5`.
- User visual acceptance of the playback-fixed compact image passed. The menu
  displays and animates correctly, and the severe multi-second freeze pattern
  from false libdvdcss decryption is gone. Taken 2 is now a permanent
  branching, resume, shared-clip, and CSS-safe playback regression fixture.
- The generalized driver completed *Taken 3* without disc-specific title/VTS
  counts. It converted 186 cells across eight title/menu domains, including a
  four-task ordinary multi-PGC VTS and 114 unique VTS 4 physical tasks. Those
  114 tasks cover 16,088 VOBUs and produce 200,007 frames while retaining the
  53 reused logical references without duplicate physical encodes.
- The compact *Taken 3* ISO is 2,957,803,520 bytes versus the 7,748,837,376-byte
  source, a 61.829% reduction. Its six title domains shrink from 3,617,960 to
  1,432,353 sectors at exact NVENC P6/CQ 27; 16,243 VOBUs shrink, none expand,
  and no quality fallback occurs. All 24 UDF files and the full physical graph
  verify, all 16,310 HEVC maps pass the CSS-safe audit, and patched-VLC title
  and menu hardware gates report first pictures with zero HEVC corruption.
  User-performed moving-video and navigation acceptance passed, including the
  branching cuts and menus. Taken 3 is now the second locked branching fixture.
- The generalized *Bourne Identity* build also passes. It converts 145 cells
  across ten title and eleven menu domains. The title layout contains all 60
  unique title-referenced cells plus the otherwise-unreferenced VTS 10 C_ADT
  cell, for 61 complete physical replacements and 21,073 VOBUs.
- The compact *Bourne Identity* ISO is 2,786,334,720 bytes versus the
  8,056,725,504-byte source, a 65.416% reduction. Its title domains shrink from
  3,698,047 to 1,124,475 sectors at exact NVENC P6/CQ 27; 21,063 VOBUs shrink,
  none expand, and no quality fallback occurs. All 44 UDF files and the full
  physical graph verify, all 22,409 HEVC maps pass the CSS-safe audit, and both
  patched-VLC hardware gates report first pictures with zero HEVC corruption.
  Initial user testing found one remaining intermittent VTS 1 submenu issue,
  described below; the corrected image awaits the focused visual recheck.

The Phase 6 automation remains a baseline fixture, not a general disc runner:
it intentionally hard-codes *The Intern*'s title/VTS/menu layout. Phase 7 adds
plan-driven entry points rather than silently applying those assumptions.

The generalized driver is now implemented. It derives the number of title
sets, ordinary titles, physical/shared-cell VTSes, and menu domains from the
compatibility plan. It performs the full clear-content scan, converts each
unique physical cell once, stages every title and menu, compacts all title
sets, authors and verifies the ISO, audits CSS-safe stream maps, and runs
patched-VLC hardware title/menu gates. Every expensive boundary is resumable.

One Phase 7 fixture also exposed a five-second authored still consisting of a
single MPEG-2 picture in a very small program stream. FFprobe's normal format
autodetection threshold was larger than the resulting HEVC cell, despite the
cell being valid. DVD2HEVC now explicitly probes and decodes known replacement
cells as MPEG program streams, so tiny stills receive the same full frame-count
and complete-decode validation as moving cells.

The final Bourne coverage gate exposed a different authored edge case: a short
title-domain C_ADT cell physically exists after the last logical-title cell,
but no VMG title or title PGC references it. DVD2HEVC no longer treats logical
title coverage as complete physical coverage. Wholly unreferenced C_ADT ranges
become explicit synthetic physical tasks, while partially covered ranges remain
a hard failure. The Bourne VTS 10 task converts all four physical cells and all
48 VOBUs, including that dormant/command-reachable material.

### Animated menu to still-menu transition

Bourne VTS 1 uses animated introductions followed by separate one-picture menu
cells marked as indefinite DVD stills. On some first visits, DVDNAV activated
the correct button subpicture but VLC retained the animation's textless final
background instead of displaying the new menu picture. Leaving and returning
usually made it appear, confirming that navigation and button data were intact.

Source-ISO comparison proved that the brief textless frame is authored video:
the menu lettering is baked into the following cell, while the orange button
highlight is a separate subpicture. The failure was therefore not missing text
or bad navigation. That next cell restarts its SCR/PTS timeline near zero. A
normal DVD MPEG-2 decoder recovers from the discontinuity, but VLC's D3D11 HEVC
decoder could retain the preceding cell after a flush.

The DVDNAV patch now resets the input clock and recreates only the HEVC video
decoder at title-zero menu-cell changes. It does not restart title playback or
alter audio/subpicture registrations. Moving menus preserve their authored
picture sequence and receive an HEVC end-of-sequence delimiter. A genuinely
one-picture menu uses the narrower guard proven by the accepted Intern image:
the same closed-IDR access unit is repeated one 90 kHz tick later and EOS ends
the repeat. This lets hardware output present the visible still before DVDNAV
reaches end-of-cell without changing its DVD still duration or navigation.

The original compact 2,786,334,720-byte image passed five consecutive hardware-
decoded main-to-Bonus animated transitions, forward navigation through two more
Bonus pages, backward page navigation, and a subsequent theatrical-title start.
Every page showed its baked-in lettering and active highlight. The verbose VLC
log recorded all intended menu-cell restarts with no invalid NAL, corruption,
late-picture, or decoder errors.

## Interleaved branching model

For the branching feature cuts, a PGC cell's first/last sector span contains
alternating extents for both stories. Treating the span as one ordinary cell
mixes two MPEG-2 prediction streams and can cause intermittent corruption.

`plan-interleaved` now resolves a PGC cell's VOB/cell identity against C_ADT:

- *Taken 2* VTS 5: all 44 interleaved cells resolve; 2,215,354 selected sectors
  are separated from 2,060,427 alternate-story sectors.
- *Taken 3* VTS 4: all 56 resolve; 1,231,128 selected sectors are separated from
  946,813 alternate-story sectors.

The first real *Taken 2* branching cell was concatenated from six discontinuous
extents. It contains 35,417 sectors, 171 NAV packs, and 2,076 frames across
83.016 seconds. Structural scan and complete MPEG-2 decode passed with zero
invalid sectors.

Segmented extraction and repacking now pass. The first six-extent cell was
encoded at NVENC P6/CQ 27, repacked across 171 VOBUs, and decoded for all 2,076
frames. Its logical replacement hashed back exactly after being split over the
original physical extents, while all 38,662 alternate-story sectors remained
byte-identical. Audio hashes also match.

The following cell-entry boundary was tested separately. Its 20 extents, 727
VOBUs, and 8,715 frames pass at CQ 27, including one IDR/PSM at the logical cell
entry. This covers a PGC cell boundary that falls inside a shared physical
interleaved unit.

The complete task planner reduces *Taken 2* VTS 5 from 96 PGC references to 72
unique physical conversion tasks, and *Taken 3* VTS 4 from 167 references to
114 tasks. Both task sets cover their title domains from sector zero through
the final sector with no gaps or overlaps. A generic ordinary/shared-cell gate
also passes, and its repeat run reuses the validated task without re-encoding.

## Compact branching result

The compact planner allocates replacement VOBUs in original physical order,
not PGC playback order. This keeps each shared clip once and retains alternating
branch ILVUs. Every logical task still receives one entry IDR and one HEVC PSM.

The compact writer validates the relocated graph before IFO staging. On the
Taken 2 result it closed 636,851 forward/backward search targets and 21,065 ILVU
relationships over all 16,634 VOBUs. It also found and now preserves the DVD
terminal-ILVU sentinel (`0xffffffff` address and `0xffff` size). The output has
zero invalid/scrambled sectors, no synthesized video sectors, and no quality
fallback from CQ 27.

The rewritten VTS 5 IFO covers all 96 PGC cell references, 371 C_ADT rows,
16,634 VOBU_ADMAP entries, and 3,652 time-map entries. Staging, UDF authoring,
file hashes, and the compact libdvdread graph all pass.

An earlier hybrid manual gate exposed a VLC state issue when returning from an
HEVC title to an original MPEG-2 menu: navigation and audio continued but the
menu intro picture was black. The retained PSM outlived its VTS/domain. The VLC
patch now destroys and reinitializes that domain-local PSM on
`DVDNAV_VTS_CHANGE`; the incremental private-plugin build passes, and the exact
title-to-menu sequence now passes manually. A longer extended-title -> menu ->
normal-title sequence exposed a second same-VTS re-entry failure (audio but no
video), which led to the every-VOBU PSM/IDR policy below. The rebuilt VTS and
all-domain ISO pass structural checks; the final moving-video regression is a
user-performed acceptance gate.

Verbose stock-VLC comparison then proved that the original disc deliberately
resumes the extended PGC at its saved mid-film cell even after the user returns
through the menus. The source MPEG-2 remains decodable there; the first HEVC
layout did not, because its PSM and IDR policy covered cell starts only. Phase 7
now makes every video-bearing VOBU a true random-access point with a repeated
HEVC program-stream map, VPS/SPS/PPS, and IDR. This preserves authored resume,
chapter, and time-map entry behaviour instead of changing the DVD commands.

Two NVENC P6/CQ 27 measurements found negligible storage cost. A 45-VOBU cell
became 0.282% smaller after compaction; the actual 200-VOBU resume cell grew by
only 0.109% (20 sectors, about 40 KiB). Both passed complete decode, frame
counts, audio hashes, and every-VOBU random-access validation. The full-disc
runner detects and resumably rebuilds older cell-entry VTS reports before it
authors a final image.

### CSS-safe sector signalling fix

The graph-perfect compact image exposed severe freezes only through DVDNAV;
playing the same VOB directly was clean. Instrumented comparison proved that
libdvdcss was changing otherwise-clear HEVC sectors. The original fixed-length
program-stream map placed byte `0xe0` at DVD sector offset `0x14`; its `0x20`
bit pattern can be mistaken for legacy PES scrambling and trigger false
decryption.

DVD2HEVC now writes the standards-valid control byte `0xc0` and recalculates
the map CRC. The packet remains exactly 20 bytes, so VOBU, NAV, IFO, UDF, and
compact-layout offsets do not move. A deterministic migration rewrote all
17,192 maps in the final Taken 2 image. A complete sector comparison confirms
that those map bytes are the only differences. Patched-VLC software and
D3D11VA gates pass for both title and menu playback, and a two-cell transition
passes with a first picture and zero SPS, PPS, or NAL parser corruption.

Both Phase 7 drivers audit final images and automatically migrate an older
cached image without re-encoding. The standalone audit, repair, and full
sector-verification commands provide the same feedback loop for existing
outputs.

The full-disc driver is resumable at every expensive boundary. It converts the
four remaining ordinary title sets and both menu domains, copy-on-write extends
the passed VTS 5 stage, verifies every replaced cell, compacts VTS 1-5 one at a
time, rewrites the VMG title-set positions after every size change, authors the
ISO, and compares both every UDF file hash and the complete physical DVD graph.
The first full run also found an orchestration-only variable collision between
the status label and compact-stage path. The path now has a distinct variable,
a regression test enforces that separation, and the resumed run reused all
passed encodes and completed VTS 1-5 compaction, authoring, and verification.

## Commands and feedback loop

```powershell
python dvd2hevc.py compatibility-plan "D:\DVD backups\TAKEN_2.iso" `
  work\phase7\taken2-plan.json --scan-report work\phase7\taken2-quick.json

powershell -ExecutionPolicy Bypass -File tools\run-phase7-title-gate.ps1 `
  -Plan work\phase7\taken2-plan.json -Title 3 `
  -WorkRoot work\phase7\taken2-title3-gate

python dvd2hevc.py plan-interleaved "D:\DVD backups\TAKEN_2.iso" 5 `
  work\phase7\taken2-vts5-interleaved.json `
  --scan-report work\phase7\taken2-quick.json

python dvd2hevc.py extract-interleaved-cell `
  work\phase7\taken2-vts5-interleaved.json 1 4 `
  work\phase7\taken2-ilvu-prototype\pgc1-cell4.vob

powershell -ExecutionPolicy Bypass -File tools\start-phase7-interleaved-vts.ps1 `
  -Plan work\phase7\taken2-vts5-interleaved.json `
  -WorkRoot work\phase7\taken2-vts5-cq27 -QualityValues cq:27

python dvd2hevc.py status work\phase7\taken2-vts5-cq27
Get-Content work\phase7\taken2-vts5-cq27\run.log -Tail 30

python dvd2hevc.py plan-compact work\phase7\taken2-vts5-compact\layout-cq27.json `
  work\phase7\taken2-vts5-cq27\vts05-conversion-report.json --quality cq:27

python dvd2hevc.py prototype-compact-domain `
  work\phase7\taken2-vts5-compact\layout-cq27.json title 5 `
  work\phase7\taken2-vts5-compact\VTS_05_TITLE_COMPACT_CQ27.vob

powershell -ExecutionPolicy Bypass -File tools\start-phase7-remaining-domains.ps1 `
  -Plan work\phase7\discovery\taken2-plan.json `
  -WorkRoot work\phase7\taken2-remaining-cq27 -QualityValues cq:27

powershell -ExecutionPolicy Bypass -File tools\start-phase7-full-disc.ps1 `
  -Plan work\phase7\discovery\taken2-plan.json `
  -BaseStage work\phase7\taken2-vts5-sector-stage `
  -InterleavedReport work\phase7\taken2-vts5-cq27\vts05-conversion-report.json `
  -WorkRoot work\phase7\taken2-full-cq27 `
  -OutputIso work\phase7\TAKEN_2_PHASE7_ALL_HEVC_COMPACT_CQ27.iso

powershell -ExecutionPolicy Bypass -File tools\start-phase7-general-disc.ps1 `
  -Plan work\phase7\discovery\taken3-plan.json `
  -FullScanReport work\phase7\discovery\taken3-full.json `
  -WorkRoot work\phase7\taken3-general-cq27 `
  -OutputIso work\phase7\TAKEN_3_PHASE7_ALL_HEVC_COMPACT_CQ27.iso `
  -QualityValues cq:27 -CompactQuality cq:27 -Preset p6 `
  -VlcRoot work\phase2\vlc-dvdhevc

python dvd2hevc.py audit-css-safe-psm `
  work\phase7\TAKEN_2_PHASE7_ALL_HEVC_COMPACT_CQ27_FIXED.iso

python dvd2hevc.py status work\phase7\taken2-full-cq27
Get-Content work\phase7\taken2-full-cq27\run.log -Tail 30

python dvd2hevc.py status work\phase7\taken3-general-cq27
Get-Content work\phase7\taken3-general-cq27\run.log -Tail 30
```

Each new disc follows the same loop: physical plan, fail-fast compatibility
gate, smallest representative cell/title, structural and full-decode checks,
then a complete VTS/disc. Long encodes run hidden with atomic status and logs;
the foreground turn returns to the user immediately.

Routine gates are programmatic. Codex does not judge moving-video quality from
screen automation. When a visual acceptance gate is required, it may open the
exact authored ISO in the patched VLC build, then the user performs playback,
menu, corruption, and quality checks from a short supplied checklist.
