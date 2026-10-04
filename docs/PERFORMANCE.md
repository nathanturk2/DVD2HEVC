# Extraction performance and physical-VTS grouping

DVD2HEVC's first correct extraction path opened the UDF image once for every
cell. A cell late in a VOB also caused pycdlib to stream that whole VOB while
the range sink discarded all bytes outside the cell. This was safe, but a VTS
containing many small cells could reread the same VOB many times.

## Implemented single-pass extraction

`extract_domain_sector_segment_sets` opens an ISO once, walks the relevant VOB
files once, and fans each input chunk into every intersecting output. It
supports both contiguous physical cells and the ordered discontinuous extents
used by branching/interleaved physical tasks. Overlapping input ranges are
allowed because two authored navigation paths can legitimately select some of
the same source sectors.

The production paths use it as follows:

- a logical title batches all uncached physical cells in that title;
- a VMG or VTS menu batches all uncached physical menu cells;
- a physical/interleaved VTS batches the logical segmented input and, when
  necessary, its complete physical span for every uncached task.

Validated cell reports remain the resume boundary. Cached cells are omitted
from the batch and are not extracted again. Every output is first written to a
private temporary file, checked for exact sector length and scanned as VOB
data, then atomically moved into place. This changes input I/O only; HEVC
encoding, cell identity, VOBU assignment, navigation data, compaction, and final
disc verification are unchanged.

A real-ISO smoke benchmark on the two-task VTS 5 of the Mission: Impossible
bonus disc measured 71 ms for two legacy extraction passes and 22 ms for one
batch pass (3.23x). Both output files were SHA-256 identical. The fixture is
deliberately small, so the ratio is more useful than the absolute times.

## Measured read amplification

Completed Mission: Impossible 2 reports allow the bytes pycdlib had to stream
to be reconstructed from each cell range and its domain VOB inventory:

| Extraction schedule | Approximate VOB bytes read |
|---|---:|
| Previous per-cell extraction | 42.83 GB |
| Current batching within each logical-title task | 16.22 GB |
| Proposed one batch per physical VTS | 7.01 GB |

The implemented title-local batch removes about 62% of the old read traffic on
that disc. Physical-VTS scheduling could remove another 56.8% of the remaining
traffic, or 83.6% relative to the original extractor. VTS 8 is the clearest
case: 11 logical titles cover 22 physical cells. Per-title batching would read
about 10.43 GB, while one physical-VTS pass would read about 1.22 GB.

## Feasibility of ordinary physical-VTS scheduling

The existing physical path already groups VTSs that contain interleaving,
blocked logical titles, or unreferenced C_ADT cells. Ordinary ready titles are
still launched one at a time and combined only after conversion. Their shared
workspace avoids duplicate *encodes*, but does not avoid repeated graph
processes, UDF opens, VOB passes, report copying, or final duplicate comparison.

Grouping ordinary work by VTS is implemented as the managed default:

1. Inspect the physical graph once and select every ready PGC in the VTS.
2. Build the union of physical cells, keyed by VOB id, cell id, and exact sector
   range; reject partial overlaps just as the current planner does.
3. Batch-extract and encode each physical cell once, in physical order.
4. Write the existing combined `dvd2hevc-vts-conversion-v0` report directly.
5. Retain a lightweight `vtsNN-title-provenance.json` beside each grouped VTS
   report so progress, diagnostics, and navigation-to-PGC traceability are not
   lost.

Every title VTS is offered to the complete physical scheduler. A VTS is grouped
only after the planner proves gap-free, non-overlapping coverage of its authored
title domain. Mandatory branching or unreferenced-cell VTSs still fail closed if
that proof is unavailable; an otherwise ordinary novel layout falls back to the
older verified per-title scheduler.

The quality contract already has the required scope: main-title and top-N
overrides are promoted to the complete VTS specifically because physical cells
may be shared. No cell in one VTS can therefore request two incompatible rate
controls.

The main implementation details needing care are operational rather than
format-related:

- cadence analysis should run per physical cell, then resolve ambiguous cells
  from stable physical neighbours or the configured fallback; it must not
  depend on which logical title happened to run first;
- progress must weight the unique physical cells once while still naming the
  logical titles that reference them;
- VTSs with unreferenced C_ADT cells must continue through the complete
  physical plan rather than a union of only logical-title references;
- interleaved and angle VTSs should retain the existing segmented physical
  converter until the ordinary grouped scheduler is proven independently;
- resume identity should be one durable VTS manifest plus the existing
  per-cell cache, not a partially overwritten shared `title-report.json`.

## Compact-first conversion and bounded overlap

Managed ordinary and physical/interleaved title conversion no longer constructs a
DVD-sector-sized replacement for every small cell merely to dismantle it again
during full-disc compaction. Each cell retains its exact video-only HEVC stream
after frame-count, random-access, and complete-decode gates. The authoritative
compact writer performs sector allocation once, using the original physical
VOBU order so branching clips remain single-copy.

Pipeline depth 2 overlaps one hardware encode with one validation pass. Locks
enforce one encoder session and one full-cell validation reader regardless of
the requested depth, so the desktop and USB source remain responsive. For
compact-stereo title sets, a separate bounded lane prepares audio while
video proceeds; its per-cell reports use the same cache identity as the later
domain pass.

The validation pass remains mandatory. It verifies VOBU random access, frame
counts, and a complete HEVC decode—the same properties that previously exposed
intermittent corruption and no-picture outputs. The grouped scheduler pipelines
it behind encoding: while cell N is being fully decoded and checked, NVENC may
encode cell N+1. It does not run two NVENC sessions or two full-cell validation
readers at once, preserving desktop responsiveness.

The compact planner also inventories supported DVD audio packet identities
during the source-sector pass it already performs for video capacity. Applying
compact audio can therefore use those recorded counts instead of rereading the
whole source cell. Older layouts remain readable through the verified legacy
rescan path.

Plan data from the current difficult-disc set supports the optimization rather
than suggesting a one-disc special case: Gladiator has 17 logical titles in one
multi-title VTS, the Mission: Impossible bonus disc has 21 titles across three
multi-title VTSs, Bourne Identity has 28 titles across eight, and The Princess
Diaries has 16 titles across four. The change should therefore be introduced as
a general scheduler with the current per-title path retained as a fallback,
not as a title-name or fixture-specific rule.

## Empirical overall-progress calibration

The overall progress bar is calibrated from uninterrupted compact-first runs,
not from the older fixed Phase 7 milestones. Mission: Impossible and the
Mission: Impossible bonus disc supplied 3,355 seconds of clean wall-clock data;
jobs resumed after a failure were deliberately excluded.

| Conversion interval | Pooled wall time |
|---|---:|
| Source scan and policy setup | 0.50% |
| Physical, ordinary-title, and menu video work | 79.75% |
| Compact planning, base staging, and audio layout | 5.89% |
| Compact-domain VOB/IFO/stage work | 6.47% |
| ISO authoring | 4.78% |
| Final verification, CSS audit, and VLC gates | 2.61% |

Physical and ordinary video share one duration-weighted interval, so a disc
with extensive branching does not receive the same fixed physical-stage slice
as a simple disc. Compact-domain progress is weighted by planned sectors rather
than VTS count, including both the title and menu domains processed in each VTS
iteration. Audio remains visible in its independent lane but, because it
overlaps video, cannot by itself advance the overall wall-clock percentage.

## Persistent final-domain compaction

Final raw-domain compaction uses one persistent Python batch per disc. The
large final layout is parsed once, while each VMG/VTS title/menu destination and
standalone report remains atomic and independently resumable. This targets
interpreter/JSON overhead without weakening the one-disc scheduler or moving
validation out of its required dependency order.
