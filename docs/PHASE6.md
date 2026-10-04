# Phase 6: compact DVD2HEVC profile

> Historical custom-DVD implementation notes. For the current default UHD-BD output and stock-VLC requirements, see [UHD_BD.md](UHD_BD.md).

Phase 6 has started with a complete variable-size VOBU layout for *The Intern*.
The compact profile removes Phase 4's local fixed-sector constraint: an encoded
VOBU may use as many sectors as its selected quality requires, while the next
VOBU and every downstream reference move accordingly.

## Quality policy

The planner uses `exact-no-fallback` quality selection. Quality values follow
BD2HEVC and HandBrake's familiar CQ convention: `cq:20` is passed through as
NVENC CQ 20, lower numbers mean higher quality/larger output, and users do not
need to learn a second scale. For the first baseline it requires the existing
NVENC CQ 20/P6 attempt for every physical VOBU and
fails if that exact attempt is absent. It never substitutes CQ 22 or CQ 24 to
make an old MPEG-2 slot fit.

Phase 4 had locally reduced 68 of 15,421 VOBUs: 67 used CQ 22 and one used CQ
24. The compact CQ 20 layout restores the intended quality for all 68. Sixty-one
tight VOBUs grow locally, while 14,689 other VOBUs shrink.

If a future disc exceeds a user-selected total media target, the CLI should
offer a larger output or a consistent whole-disc quality change. It must not
silently reduce quality only in locally difficult scenes. P7 can be offered as
a slower high-quality NVENC option after a controlled P6/P7 comparison; it is
not required to solve sector capacity.

The first 60-second live-action study found that P7 did not materially improve
P6: its size and VMAF differed by less than 0.03 at every tested value. A second
study normalized three feature samples to lossless, zero-timestamp FFV1 before
encoding, which prevents DVD cell timestamp discontinuities from invalidating
the comparison:

| HandBrake-style quality | Average kbit/s | Average VMAF | Worst frame |
| --- | ---: | ---: | ---: |
| `cq:20` | 3,481.8 | 97.3672 | 92.4344 |
| `cq:22` | 2,684.9 | 96.8434 | 90.3394 |
| `cq:24` | 2,061.4 | 96.2293 | 88.4574 |
| `cq:26` | 1,599.2 | 95.5275 | 85.5182 |

This supports three honest choices rather than pretending one CQ fits every
archive: `cq:20` high, `cq:22` balanced, and `cq:24` compact. CQ 24 uses about
40.8% fewer video bytes than CQ 20 in these samples. It remains a candidate,
not a silent default change, until grain, high-motion, credits, and menu samples
also pass.

### Standards-equivalent automatic quality

`estimate-quality` implements the neutral source-ratio policy discussed during
Phase 6. It demuxes and counts MPEG-2 elementary video bytes (excluding audio,
NAV, and VOB overhead), measures duration, multiplies the source video bitrate
by 0.25, and maps that HEVC target onto the measured P6 CQ/bitrate curve. NVENC
accepts half-step values, so the estimator does not have to discard useful
precision.

For a generic 6.0 Mbit/s DVD video stream, the standard-equivalent target is
1.5 Mbit/s and the current curve estimates `cq:26.48`, rounded to `cq:26.5` with
a predicted 1.495 Mbit/s average. Across all 73 baseline title/menu cell files,
the measured MPEG-2 average was 5.658 Mbit/s, giving a 1.415 Mbit/s target and
`cq:26.93`, rounded to `cq:27` with a predicted 1.401 Mbit/s average.

```powershell
python dvd2hevc.py estimate-quality <domain reports...> `
  --report work\phase6\equivalent-quality.json
```

CQ is deliberately the final mode because it can move bits from easy frames to
difficult ones instead of enforcing the derived average on every interval. The
production feedback loop will sample-encode at the estimated half-step, measure
the actual average, and adjust by half steps until it is within tolerance. The
25% standard ratio remains the policy; the CQ curve is only its content-aware
implementation.

The policy is now available as the `compact-auto` preset. It can reuse a saved
measurement so every title and menu is encoded at the same disc-wide quality:

```powershell
python dvd2hevc.py convert-title <iso> 1 <workspace> `
  --quality-preset compact-auto `
  --quality-estimate work\phase6\quality.json

python dvd2hevc.py plan-compact <layout.json> <domain reports...> `
  --quality compact-auto `
  --quality-estimate work\phase6\quality.json
```

The encoding preset forces NVENC P6 and the resolved half-step CQ. The layout
records source bitrate, the 25% target, predicted output bitrate, and the
estimate report. It requires that exact encode and never substitutes an
existing CQ 20 attempt. For *The Intern*, 5.658 Mbit/s MPEG-2 resolves to a
1.415 Mbit/s target and `cq:27` (predicted 1.401 Mbit/s).

The complete CQ 27 feedback run finished in 18 minutes 57 seconds. Its measured
HEVC elementary payload averaged 1.334 Mbit/s, 5.7% below the 1.415 Mbit/s
target and inside the accepted 10% CQ tolerance, so no CQ 26.5 retry was
needed. All 15,421 VOBUs and all 73 physical title/menu cells validated at the
same exact setting. The resulting layout uses 1,337,313 sectors instead of
3,362,805 (60.232% smaller), with zero growing VOBUs.

## Optional compact stereo

The Phase 6 audio option follows BD2HEVC's user-facing policy while remaining
DVD-compatible:

- `passthrough` remains the default;
- `compact-stereo` outputs AC-3 at 48 kHz;
- multichannel sources become 256 kbit/s stereo;
- mono sources remain mono at 128 kbit/s;
- stream count, order, IDs, language attributes, timing, menus, and VM audio
  selection must remain intact.

The first VTS 2 prototype converted one 448 kbit/s 5.1 AC-3 track to 256 kbit/s
stereo AC-3. Full decode and codec/channel/sample-rate validation passed, and
the elementary stream shrank from 603,904 to 345,088 bytes (42.857%). It is not
yet placed into the compact VOB. A timestamped MPEG-TS pass compensates for the
AC-3 encoder's fixed 256-sample delay: all 337 packets remain, and source/output
first and last PTS match exactly (4,826 and 972,506). Private-stream PES
packetization now also passes: 337 timestamped AC-3 access units become 172 DVD
`private_stream_1` PES packets, the terminal continuation packet follows the
source convention, and all 345,088 elementary bytes remain hash-identical. Pack
sector placement, NAV audio sync offsets, IFO attributes, and the smaller VOBU
budget are the next gate.

## First navigable compact VTS

The no-menu VTS 2 control rewrite now patches the VTS extent, title PGC cell
playback sectors, C_ADT, all 22 VOBU_ADMAP entries, and all 10 VTS time-map seek
entries. Its seven-sector IFO/BUP size and title-VOB start remain unchanged.
libdvdread re-opened the compact stage with zero NAV lookup errors. Patched VLC
passed HEVC/AC-3 title playback, and source/compact DVDNAV traces match after
ignoring only the physical `cell_length`, `pg_length`, `cell_start`, and
`pg_start` values that compaction necessarily changes. A five-second mid-title
seek also passed with identical semantic traces, validating the time map.

### Compact-stereo VTS and authored ISO gate

The combined CQ 20 plus compact-stereo layout replaced all 300 source AC-3 pack
sectors in VTS 2 with 172 timestamped stereo packets. Every VOBU had enough
existing pack/SCR headers, so no timing sector was synthesized. The resulting
domain is 1,640 sectors (3,358,720 bytes), a 55.154% reduction from its 3,657
source sectors and 128 sectors smaller than the video-only compact prototype.

Media and control validation passed:

- the generated AC-3 elementary MD5 matches the audio extracted from the VOB;
- FFprobe reports AC-3 stereo, 48 kHz, 256 kbit/s, and 10.752 seconds;
- full audio and HEVC decode pass with all 270 video frames;
- NAV audio-sync addresses point to the first compact audio pack in each VOBU;
- VTSI audio attributes changed from six channels to two;
- libdvdread re-opened all 22 NAV packs with the new 1,640-sector graph;
- normal and five-second-seek patched-VLC traces match the source behavior.

A hardlinked compact-stage transaction then authored
`THE_INTERN_VTS2_COMPACT_STEREO.iso` (6,884,036,608 bytes). All 17 UDF
`VIDEO_TS` files hash back exactly, the ISO graph equals the validated compact
stage graph, and normal/seek ISO playback passes with VLC identifying two AC-3
channels. This is a deliberately partial compact disc: VTS 1 remains at its
Phase 5 size while VTS 2 exercises the complete compact authoring path.

### Full VTS 1 and combined-title ISO gate

The compact writer now handles VOBUs that grow at exact CQ without degrading
them. It synthesizes MPEG-2 pack headers, distributes SCR values inside the
source VOBU timing interval, and raises the declared program mux rate only when
the denser pack cadence requires it. All 61 growing VOBUs passed; 369 video
sectors were synthesized and the highest mux-rate value was 28,141. VTS 1
completed in 98 seconds without re-encoding:

- 3,351,956 source sectors became 2,232,296 compact sectors (33.403% smaller);
- all 14,682 VOBUs use exact CQ 20;
- 20 physical cells, 21 PGC references, 14,682 VOBU-map entries, and 2,024
  time-map entries were relocated;
- the unchanged 5,060-sector VTS menu was retained;
- the title domain was split into five DVD-sized VOB files.

The first authoring attempt revealed that VMGI TT_SRPT still placed VTS 2 at
its original sector, so `genisoimage` inserted 1,119,660 padding sectors. The
stage transaction now recalculates title-set starts; VTS 2 moved from sector
3,359,252 to 2,239,592. The corrected combined-title ISO is 4,590,968,832
bytes, compared with 6,884,032,512 bytes before global relocation. All 15
VIDEO_TS files hash exactly from UDF and the authored libdvdread graph equals
the staged graph.

Patched VLC passes title starts for titles 1–4, a 35-second seek through the
largest synthesized-sector region, and menu startup. Matched source/output
title, seek, and menu traces are exact after excluding only the four physical
relocation fields. VTS 1 retains passthrough audio in this gate; VTS 2 uses
compact stereo. Menu video remains the validated Phase 5 HEVC version and has
not yet been compacted.

The subsequent compact-auto image applies CQ 27 to every title, extra, VMG
menu, and VTS menu. Title VOBUs are physically compacted; menu VOBs retain
their small sector allocations so their button/control layout remains at the
already proven Phase 5 addresses. VTS 2 also retains its validated 256 kbit/s
compact-stereo track. The authored image is 2,743,570,432 bytes, 60.146% smaller
than the earlier 6,884,032,512-byte authored baseline and 40.240% smaller than
the CQ 20 compact-title image.

All 13 UDF files hash exactly and the authored physical graph equals the stage.
Modified VLC passes titles 1–4, the relocated seek, and menu startup. Headless
libdvdnav also passes all four top-level buttons plus the `2,2` submenu/title
path and `3,3` submenu/return path. Active-menu comparison ignores only physical
cell/program sector lengths and read-loop counts; command order, highlights,
domains, cells, durations, streams, activation status, and destinations remain
exact.

Across the full baseline disc, its AC-3 elementary payload is approximately
1.358 GB. Compact stereo projects 0.908 GB, saving about 450 MB. Combining that
with the normalized CQ study gives this preliminary VOB projection:

| Quality | Compact stereo VOB | Reduction from source VOB |
| --- | ---: | ---: |
| `cq:20` | 3.853 GiB | 1.66x / 39.9% |
| `cq:22` | 3.194 GiB | 2.01x / 50.2% |
| `cq:24` | 2.679 GiB | 2.39x / 58.2% |
| `cq:26` | 2.298 GiB | 2.79x / 64.2% |

These are estimates until each CQ is planned from full encodes and compact
audio is packetized. A 4x whole-disc claim would be misleading: codec gains
apply to video, while audio, NAV, subtitles, system packets, and authoring
overhead remain. On this audio-heavy disc, even compact stereo leaves about
0.92 GiB of non-video VOB data.

## First full-disc projection

The `plan-compact` command inspected all 3,362,805 source VOB sectors and the
exact CQ 20 attempt for all 15,421 VOBUs:

- original VOB bytes: 6,887,024,640;
- projected compact VOB bytes: 4,586,862,592;
- projected saving: 2,300,162,048 bytes (33.398%);
- VOBUs shrinking: 14,689;
- VOBUs expanding: 61;
- every VOBU quality: CQ 20.

The projected VOB payload is about 4.27 GiB. Adding IFO/BUP and the measured
authoring overhead still projects just under the nominal 4.7 GB capacity of a
single-layer DVD. This is a projection, not yet an authored compact image.

Domain breakdown:

| Domain | Original sectors | Compact sectors | Saving |
| --- | ---: | ---: | ---: |
| VTS 1 titles | 3,351,956 | 2,232,296 | 33.403% |
| VTS 2 title | 3,657 | 1,768 | 51.654% |
| VMG menus | 2,132 | 916 | 57.036% |
| VTS 1 menus | 5,060 | 4,699 | 7.134% |

## Rewrite checklist

The layout report supplies new domain-local cell and VOBU starts. The remaining
implementation work is deliberately staged:

1. Build compact MPEG-PS VOBUs, including valid pack SCR values, while retaining
   audio, subpicture, system, and other non-video packets.
2. Rewrite NAV PCI/DSI LBNs, VOBU end addresses, reference/search pointers, and
   audio/subpicture synchronization offsets.
3. Rewrite VMG/VTS C_ADT, VOBU_ADMAP, and every PGC cell sector field.
4. Recalculate VTS menu/title starts, VOB file boundaries, and IFO/BUP copies.
5. Author UDF, then repeat the Phase 5 file, graph, sector, decode, title, menu,
   button, submenu, and return-path gates.

The small two-cell VTS 2 gate and complete multi-file VTS 1 gate have passed.
Remaining baseline work is compact menu layout, compact stereo for every
eligible audio stream while retaining stream order/language, and the complete
menu-action and return-path suite.

```powershell
python dvd2hevc.py plan-compact work\phase6\the-intern-cq20-layout.json `
  work\phase4\vts1-nvenc-report.json `
  work\phase4\title4-nvenc-auto\title-report.json `
  work\phase5\vmg-menu-nvenc\menu-report.json `
  work\phase5\vts1-menu-nvenc\menu-report.json `
  --quality cq:20

python dvd2hevc.py prototype-compact-audio work\phase6\vts2-source.vob `
  work\phase6\vts2-compact-audio --stereo-audio-bitrate 256k
```

The final disc-oriented CLI will expose the same decision as BD2HEVC. It can
already be recorded in a layout with `--audio-mode compact-stereo`; the planner
marks audio sector savings as not yet included, and the domain writer refuses
to claim a compact-stereo VOB until PES packetization is implemented.

## Cell-entry IDR and prediction continuity

Interactive testing exposed two opposite failure modes. A conventional NVENC
encode could enter a short, independently addressed cell without a true IDR,
leaving the first-play/menu path black. Forcing an IDR at every VOBU fixed that
entry but broke prediction continuity in the feature. Mixing VOBUs from
different CQ attempts caused the same class of intermittent corruption.

The automated policy is now:

1. Every physical title or menu cell forces exactly one IDR at encode time
   zero. This covers direct cell jumps, reused cells, extras, warnings, and
   still/menu entry paths.
2. Normal closed GOP cadence continues after the entry; VOBU boundaries are
   not forced to become random-access points.
3. A sector-preserving fallback selects one complete CQ attempt for the whole
   cell. It never stitches prediction chains from different encodes.
4. Compact authoring still uses the requested exact CQ attempt and may grow a
   VOBU, so its quality is not reduced to satisfy the original sector budget.
5. Each physical cell carries one HEVC program-stream map at its first video
   packet. Repeating it at every VOBU created unstable prediction behavior in
   VLC and is not part of the Phase 6 format.

The first real NVENC regression fixture contains 176 frames across 14 VOBUs.
It decoded 176/176 frames, began with an IDR, and only 4 of its 14 VOBU entries
were IDRs. The rejected all-VOBU policy is explicitly blocked by the encoder
wrapper and tests.

Before this rule was generalized, the same result was proven on the full disc
by replacing only the 20-sector VTS 1 startup cell with its IDR-entry variant.
The resulting 2,743,521,280-byte ISO preserved the root menu, an extra, the
warning path, feature playback, seeking, and the previously corrupt region.
All 13 authored files and the physical graph verified exactly.

The subsequent full automatic rebuild encoded and validated all 73 physical
cells, then compacted VTS 1 from 3,351,956 to 1,331,069 sectors and compacted
the stereo VTS 2 title from 3,657 to 819 sectors. Visual testing found a second
issue: repeating the HEVC PSM in every VOBU produced intermittent visual blocks
even when decoder logs reported no corrupt frames. Repacking without another
encode, with 20 PSMs for the 20 VTS 1 cells and two for the two VTS 2 cells,
removed the recurring artifacts while preserving menu-first playback and
normal in-session seeking.

A synthetic cold launch directly into the middle of a cell does not see the
cell-entry PSM. That VLC enhancement is explicitly deferred; it is not allowed
to destabilize normal DVD navigation or hold Phase 6 open.

The final ISO is 2,743,570,432 bytes with SHA-256
`76E02155710019C257D0BDEB731B69609E02A20A4D70F21D57681F4B66924ACA`.
All 13 files and the physical graph verify exactly. The root menu, Play path,
feature startup, interactive seek, and multiple continuous-play samples passed
with plugin SHA-256
`E7DFC374E89B25F0441CACCEB61721A439B0C930208C64CF0B414AB907C36BFB`.
The cell-entry PSM rule is enforced by the repackers, cache revision, and tests.
