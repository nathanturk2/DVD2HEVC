# Roadmap and feedback gates

This file preserves milestone history. Current capabilities are authoritative
in `CAPABILITIES.md`; compatibility rules are authoritative in `INVARIANTS.md`.

## Completed foundation

- The custom sector-preserving v0 format invariants are written down.
- ISO scanning uses UDF directly and does not mount or extract an image.
- Every VOB sector is structurally parsed as a 2,048-byte MPEG-2 PS pack.
- Genuine PES headers, NAV packs, PSM packets, padding, scrambling flags, and
  video payload capacity are counted.
- HandBrake supplies provisional logical title/chapter/angle discovery.
- `plan` produces preliminary per-VOB candidates and blocks scrambled or
  structurally invalid sources.

The four initial fixture ISOs all pass the structural gate. Their checked-in
reports are development artifacts and are excluded from source control because
they contain local absolute paths and disc-specific metadata.

## Phase 1 complete: authoritative physical disc graph

The first Windows-native `dvdinspect` helper now uses pinned libdvdread and
emits stable JSON describing VMG titles, title sets, chapters, PGCs, programs,
cells, sector ranges, VOB/cell IDs, VOBU maps, stream counts, attributes, menu
language units, and command counts. Remaining work adds libdvdnav behavior
traces and strengthens these cross-checks:

- map native sector ranges to the ISO scanner's exact VOB file extents;
- validate PGC durations against HandBrake titles without assuming equal title
  counts;
- add audio/subpicture language details rather than counts alone;
- record source/output libdvdnav event traces;
- distinguish legitimate repeated cell references from alternate navigation
  paths when generating physical write actions.

Its output becomes the authority for planning. HandBrake remains a useful
independent comparison but must not determine what physical content is written.

Feedback gate: for every fixture, all HandBrake title durations must be
explainable from the native physical graph, every referenced sector must fall in
the scanned VOB extent, and source/output libdvdnav traces must eventually agree.

## Phase 2 complete: one-cell HEVC experiment

The Intern VTS 2 cells 1 and 2 have been extracted, encoded, packetized into
their original video-sector slots, structurally validated, completely decoded,
and navigated through a patched VLC 3.0.23 DVDNAV demux. Program stream maps use
HEVC type `0x24`; sector counts, NAV packs, and AC-3 data are preserved. See
`docs/PHASE2.md` for measurements and the repeatable headless feedback gate.

The prototype writer operates only on a staging copy and refuses in-place input.
It validates capacity before making an atomic staged-file replacement. Automated
decode-before-commit, bounded quality retries, and support-bundle capture belong
to the Phase 3 pipeline; it will never relocate sectors in profile v0.

## Phase 3 complete: automated multi-cell title

The Intern title 3 now passes an automated four-cell conversion, full title-VOB
staging, read-back hashes, complete cell decodes, audio hashes, frame counts,
patched-VLC playback, and exact normalized source/output DVDNAV traces. It also
exercises a cell shared with title 2 and a physical-sector jump. See
`docs/PHASE3.md` for measurements and the capacity-feedback issue found and fixed.

## Phase 4 complete: hardware encoding and complete VTS 1 titles

The Intern VTS 1 now has validated progressive NVENC replacements for all 20
physical title cells and all 14,682 VOBUs. Titles 1-3, representative chapter
seeks, full accelerated main-feature playback, staged hashes, and exact complete
source/output DVDNAV traces pass. See `docs/PHASE4.md`.

## Phase 5 complete: menus and sector-preserving ISO

The Intern now has complete VTS/VMG menu conversion, a complete folder, and an
authored DVD-Video-ordered UDF image. All 17 `VIDEO_TS` files hash back, the
source/output physical graphs agree, a full sector scan passes, and title/menu
startup works through patched VLC. Headless libdvdnav action traces match for
all four top-level buttons plus submenu/title and submenu/return paths. See
`docs/PHASE5.md`.

The authored-image key-scan delay is resolved without another VLC patch:
verified clear DVD2HEVC images launch with libdvdread's existing
`DVDREAD_NOKEYS=1` switch. Release packaging must apply it only to outputs that
passed the clear-content gate, never globally to source DVDs.

## Historical milestone: Phase 6 compact DVD-HEVC profile

The sector-preserving profile intentionally retains the source disc size and
uses padding for HEVC's unused capacity. Once full-disc navigation is proven on
all compatibility fixtures, a separate compact profile can reclaim that space
by relocating VOBUs and rewriting NAV, IFO, VOB-boundary, and UDF references.
Both profiles will remain available: sector-preserving for lowest behavioral
risk, compact for maximum storage savings.

Both title sets are now compacted for The Intern. The combined 4.591 GB ISO
passes exact UDF hashes, the full libdvdread graph, titles 1–4, menu startup,
and a seek through synthesized SCR-timed packs. VTS 1 contains all 14,682
VOBUs at CQ 20 and is 33.403% smaller; VTS 2 is 55.154% smaller with compact
stereo. The `compact-auto` preset converts measured DVD MPEG-2 bitrate into a
disc-wide NVENC P6 half-step CQ (CQ 27 for this fixture) and refuses to reuse a
different encode. Remaining baseline work is compact menu layout, general
multi-track compact audio, and the full menu-action return-path regression.
See `docs/PHASE6.md`.

The first `compact-auto` run has now passed at CQ 27. Every title, extra, and
menu uses the disc-wide setting; the authored ISO is 2.744 GB and passes exact
UDF hashes, the physical graph, titles 1–4, seeking, menu startup, all four
top-level buttons, the submenu/title transition, and submenu return. Menu VOB
allocations remain sector-preserved because reclaiming their remaining roughly
4 MB does not justify changing proven button/control addresses in the baseline.
General multi-track compact audio and difficult-disc coverage remain.

## Phase 7: difficult-disc compatibility

Validate both profiles against The Bourne Identity, Taken 2, and Taken 3,
including duplicated extras, shared cells, and normal/extended branching cuts.

Discovery and the bounded gates are complete. The Bourne Identity is
30/30 ready under the contiguous-cell pipeline; Taken 2 is 8/10 and Taken 3 is
6/9 because their feature cuts use physical interleaving. Plan-driven NVENC and
cache reuse pass on a safe Taken 2 extra. C_ADT mapping resolves every examined
branch cell, and the first six-extent logical extraction fully decodes. The
segmented repack/split-back now passes on both a six-extent cell and the next
twenty-extent cell-entry boundary. The complete Taken 2 all-domain build now
passes: all title and menu video is HEVC, all five title sets are compacted,
all 21 ISO files and the physical graph verify, and the result is 56.376%
smaller than the source. A false CSS-detection edge case in the custom HEVC
program-stream map was isolated and fixed without changing any packet size or
disc offset. Software and hardware title/menu playback plus a multi-cell
transition now pass with zero HEVC parser corruption. User-performed visual
acceptance also passed, including the repaired animated menu. Taken 2 is now a
locked regression fixture. Taken 3 has also completed the generalized full-disc
automation: its 2,957,803,520-byte ISO is 61.829% smaller, exact-CQ compaction,
UDF files, physical graph, CSS-safe maps, and patched-VLC hardware title/menu
gates all pass. The Bourne Identity's many-extra/shared-title gate now passes as
well: all 30 titles, 11 menu domains, and one unreferenced physical C_ADT cell
are converted; its 2,786,334,720-byte image is 65.416% smaller and passes all
exact-CQ, UDF, graph, CSS-safe, and hardware gates. Phase 7 automated coverage
is complete, and Taken 3 has passed user visual acceptance. Bourne user testing
then exposed an intermittent animated-intro to one-picture-still submenu race:
buttons were active while VLC retained the preceding textless background. Its
authored next cell restarts its clock near zero, and a decoder flush alone was
not reliable under D3D11 hardware decoding. DVDNAV now resets the menu-cell
clock and recreates only the HEVC video decoder at title-zero cell changes; the
disc retains one picture plus HEVC EOS. Five repeated animated transitions,
multi-page Bonus navigation, and a following title start passed autonomously
without parser or decoder errors. See `docs/PHASE7.md`.

## Phase 8: release hardening

Implementation complete for the first public alpha. The proven generalized
driver is exposed through `auto`, `start`, sequential `queue`, immediate
`cancel`/`resume`, HandBrake-style CQ, reusable presets, automatic
deinterlacing, conversion preflight, atomic job records, `status --watch`, job
listing, patched-VLC launch, and redacted support bundles. The difficult-disc
pipeline itself remains unchanged. VLC preparation is pinned to 3.0.23 and its
exact source commit, rejects unrelated tracked changes, and records patch and
plugin hashes. Supported-scope, release-checklist, CI, third-party licensing,
and alpha metadata are present. The clean source archive and its internal hash
manifest have been audited, and its CLI plus real difficult-disc dry run pass.

Compact-stereo is now a full-disc AC-3 title option rather than a one-track
prototype. Phase 8 also has bounded audio/domain workers, append-only
video/audio/mux progress lanes, queue pause/resume and immediate running-job
cancellation. Bourne's 10 title sets, 61 cells, and five-track domain pass the
audio gate. The complete authored CQ 27 image also passes at 2,665,879,552
bytes (66.911% below source), including exact UDF/graph verification and
hardware title/menu checks.

A fresh complete conversion from the packaged release remains a publication
gate for an eventual tagged release; it is not an implementation blocker and
should be run as a long background release-candidate job.

## Compatibility progression

1. The Intern: simple full-title and menu baseline.
2. The Bourne Identity: many extras and duplicated logical titles.
3. Taken 2: normal and extended editions sharing physical content.
4. Taken 3: second branching-edition regression gate.

Visible VLC tests remain occasional release gates. Routine navigation checks
will use headless libdvdnav traces and patched LibVLC decode callbacks so the
Windows desktop remains available.
