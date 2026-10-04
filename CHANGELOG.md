# Changelog

## 0.2.0a1 — UHD-BD default

- Integrate the DVD VM/BD-J author; default output plays in stock VLC with Java.
- Add GUI output choice and explicit no-patch note; preserve legacy jobs/watches and player option.
- Include BDMV authoring, media audit, measured UDF writing, payload checks and stock-VLC startup in progress.
- Add author-uhd for existing HEVC DVD backups, source/runtime-bound reuse, safe publication and failure evidence.
- Reject unrelated completed-disc folders during cache reuse, recheck source bytes before publication, and create new output directories.
- Require still-picture padding to retain the complete original compressed picture sequence.
- Expand the author/navigation corpus to 13 commercial DVD backups, separately from 117 legacy converted examples.
- Package Java sources, stock-player harness and the Hadris Windows author with complete fork source/notices.


## 0.1.0a3 — First public release candidate

- Protect diagnostic outputs and redact escaped private paths.
- Measure physical VOBU duration for target bitrate and prevent noisy FFmpeg measurement hangs.
- Package complete runtime resources and keep jobs/player state in writable per-user directories.


## 0.1.0a2 - unreleased

- Independently validate UDF descriptor checksums, character sets and terminating
  descriptors before publishing a newly authored ISO and when verifying one.
  The existing genisoimage/mkisofs backend does not have BD2HEVC's writer defect.

- Added output-storage-sensitive queue handling. A proven disk-full failure
  removes only the current job's newly created ISO/authoring partial, preserves
  pre-existing outputs and resumable work, pauses queued and watched work, and
  exposes the reason until the user explicitly resumes.
- Added explicit compact-format and private-player capability contracts to new
  jobs and authored verification reports; incompatible recorded contracts must
  be replanned rather than silently resumed.
- Added a media-free compatibility history with sampled rename-stable disc
  identity, evidence/outcome classification and optional manual VLC reviews.
  Captured identities survive later deletion of disposable source ISOs.
- Classified terminal outcomes as verified, passed-with-warnings, source-media,
  runtime-environment, canceled or converter failures without weakening the
  automated pass/fail gate.
- Moved new managed binary work to short hashed per-user paths, avoiding the
  Windows path-length exposure created by descriptive job and cell names.
- Batched final raw-domain compaction so one Python process loads the large
  layout once while preserving atomic, resumable per-domain outputs.
- Made the GUI launcher reproducible from its checked-in C# source and icon; the
  shortcut installer rebuilds a missing or stale launcher as a windowless app.
- Reconciled VMG/VTS menu-compaction documentation and added authoritative
  capability, invariant/repair, and disposable-fixture guidance.

- Fixed target-bitrate conversions of sector-preserving VMG menus and other
  fixed-layout cells by retrying only an overflowing cell at bounded lower
  VBR/CBR targets. Compact title layouts still retain the exact requested
  target and grow their VOBUs instead of lowering quality.
- Completed full-domain video compaction by rebuilding `VIDEO_TS.VOB`,
  relocating VMGI first-play/menu PGCs, C_ADT and VOBU_ADMAP tables, packing
  the VMGI/menu/BUP extent, and moving every following title-set start. VMG
  menus now retain the exact requested video control even when individual
  HEVC VOBUs are larger than their old MPEG-2 slots.

## Unreleased

- Route every title VTS through the complete physical-VTS scheduler when its
  plan proves gap-free coverage, with an automatic per-title fallback for
  unusual ordinary layouts. This batch-extracts each VTS once, encodes shared
  physical cells once, and records global-title/PGC provenance beside the
  grouped report.
- Apply the existing depth-two encode/validation pipeline to grouped ordinary
  VTSs. One NVENC encode may overlap one random-access/full-decode validation;
  all validation gates remain mandatory and reports now state the active
  scheduling and validation policy.
- Keep watched batches recoverable when the converter runtime temporarily
  loses a dependency or encounters a Windows sharing denial. One runtime-wide
  error now leaves discs waiting instead of falsely failing every ISO, and
  resuming a watch repairs matching historical entries in place.
- Discover FFmpeg/FFprobe from the existing open-source BD2HEVC tool bundle as
  a fallback when Windows has not placed them on `PATH`.

- Made Python job and watched-batch state publication retry transient Windows
  sharing violations while retaining atomic JSON replacement. Failed or
  stopped watchers can now be resumed in place from the GUI or CLI without
  clearing their discovery ledger or requeueing completed outputs.
- Recalibrated overall progress from 3,355 seconds of uninterrupted
  post-optimization conversions. Video now occupies about 80% of the displayed
  work, compact planning/audio layout about 6%, compact-domain writing about
  6%, ISO authoring about 5%, and final gates the remainder. Overlapping audio
  can no longer force overall progress forward independently, and compact VTS
  progress is weighted by planned sectors instead of treating tiny and large
  title sets equally.
- Made runner status publication atomic and retry-safe on Windows. Production
  PowerShell stages now use `File.Replace` instead of `Move-Item -Force`, so
  concurrent GUI polling, antivirus, or indexing cannot falsely fail a
  resumable conversion after its media work has passed.
- Made compact-stereo accept genuinely silent title sets when either the
  compact-first per-VOBU audio inventory or the extractor's complete VOB scan
  proves that no DVD audio packets exist. Missing metadata still fails closed
  for any title set containing or lacking complete evidence about audio.
- Added a compact-first title pipeline. Managed ordinary and physical VTS
  conversions now keep
  exact video-only HEVC intermediates until the one authoritative final DVD
  layout, eliminating obsolete sector-preserving repack, split-back, hash, and
  reread passes. One hardware encode may overlap one full-decode validation,
  with hard locks retaining one encoder session and one validation reader.
- Moved compact-stereo work forward for ordinary and physical title sets. Audio
  is prefetched beside video and reused by the final domain pass; compact
  planning records DVD audio-sector identities during its existing video scan,
  avoiding a second whole-cell read when audio sector counts are applied.
- Versioned compact-first cache identity and retained the legacy low-level
  sector-preserving prototype for diagnostics. Resumed managed jobs cannot
  accidentally accept an older intermediate under the optimized contract.
- Kept converted-disc output folders ISO-only. Final authoring logs, structural
  verification, and CSS-safety reports now live in each job's
  `work/final-output-reports` directory; resumed legacy jobs relocate their
  output-adjacent sidecars there instead of leaving them beside the ISO.
- Expanded automatic-cadence reports with beginning/middle/end sample
  classifications, mixed-region evidence, repeated-field ratios, the selected
  cadence, and the exact FFmpeg filter chain. The GUI and documentation now
  distinguish the release-gated cell-level BWDIF policy from future
  within-cell Decomb, inverse-telecine, and Main10 profiles.
- Added single-pass multi-cell extraction for title, menu, and physical VTS
  work. One ISO/VOB walk now fans out contiguous or discontinuous sector ranges
  to all uncached cell inputs, while exact-size/VOB validation and atomic output
  replacement preserve the previous safety contract.
- Documented physical-VTS scheduling feasibility and measured extraction read
  amplification. On Mission: Impossible 2, title-local batching models at
  16.22 GB read versus 42.83 GB previously; a future ordinary VTS scheduler can
  reduce that further to about 7.01 GB without changing the disc format.
- Restored a narrowly scoped HEVC display guard for one-picture DVD menu
  cells. The independently decodable picture is repeated one 90 kHz tick later
  and terminated with EOS, matching the visually accepted Intern regression
  image; moving menus are not duplicated. Compact VTS-menu planning and writing
  now apply the same policy as sector-preserving VMG menus.
- Made GUI and CLI progress cumulative and monotonic. Fresh queued jobs now
  begin at 0%; video is weighted across the whole disc, compact audio reports
  one global cell counter, and mux/staging retains completed milestones instead
  of resetting its bar for each new subtask.
- Replaced the production Auto-CQ approximation with source-derived target
  bitrate mode. The default `1.00x` remains 25% of measured MPEG-2 video
  bitrate, now sent directly to the encoder as user-selectable VBR or CBR.
- Added native target-bitrate command profiles for NVENC, QSV, AMF, and x265,
  exact rate-control identity through compaction/resume reports, updated GUI
  controls, and automatic migration of older Auto-CQ presets and queued jobs.
- Kept HandBrake-style `cq:N` as a separate manual quality mode; target-bitrate
  conversions no longer consult the historical NVENC CQ calibration curve.
- Added optional untagged output naming to the GUI and CLI. Tagged names remain
  the default for Play Movie compatibility; `--no-filename-tags` generates
  `Movie - converted.iso` instead.
- Added persistent watched batches for continuously growing intake folders.
  They use stable-file and active-writer gating, a durable fingerprint ledger,
  safe existing-output handling, GUI/CLI stop and reset controls, and survive
  the GUI closing.
- Fixed a Windows-only liveness check that could terminate the dispatcher it
  was inspecting, which could create stale running records and repeated helper
  launches. Planning and conversion now share one stale-safe cross-process
  work lock, dispatcher startup is serialized, and all command-line children
  use a shared no-visible-console policy.
- Added Pause all semantics for watched-folder discovery and a GUI/CLI Cancel
  all operation that stops watchers, cancels waiting jobs, safely stops active
  work, retains resumable data, and prevents canceled watched fingerprints from
  being added again.
- Starting or resetting a watched batch now resumes a globally paused queue,
  preserving Cancel-all generation state. Active watchers held by Pause all are
  displayed as paused instead of misleadingly appearing to be running.
- Clarified watched-batch workload as Found, Active, Waiting, Done, and
  Attention. Discovery remains responsive while another disc owns the single
  work slot, and the GUI header includes watched discs waiting for admission.
- Replaced the fixed 45% ordinary-title plateau with media-duration-weighted
  overall progress and live FFmpeg timestamp progress inside long title and
  animated-menu cells. New task measurements no longer inherit a completed
  preceding task's counter, and failures retain the stage reached instead of
  jumping back to 0%.
- Removed the obsolete sector-preserving fit requirement from compact title
  conversion. Exact requested encodes may now locally expand VOBUs during final
  compaction while complete decode, IDR, navigation, layout, and final-disc
  gates remain authoritative; no hidden bitrate reduction is introduced.
- Extended that structural compaction to VTS menu domains. Animated menus can
  now rebalance sectors across their complete cells at the requested quality;
  menu PGC, cell-address, VOBU-map, and title-start IFO addresses are rewritten
  and validated against the final physical DVD graph instead of lowering video
  quality to fit obsolete per-VOBU slots.
- Generalized compact-stereo input beyond AC-3 to DVD DTS, LPCM, MPEG-1 audio,
  and MPEG-2 audio. The compact writer now recognizes both private substream
  and direct MPEG audio packet identities, validates cross-codec timestamp
  grids, emits DVD AC-3, and rewrites IFO coding/channel descriptors while
  retaining stream ordinals and language metadata.
- Hardened per-cell audio discovery against FFmpeg phantom streams created by
  arbitrary boundary PES bytes. Only codec/ID pairs in authored DVD audio
  ranges are admitted; a valid DVD stream that lacks decodable parameters
  still fails explicitly instead of being silently omitted.
- Fixed compact-base staging argument order for menu-preserving builds and
  made compact-stereo background jobs use an observable hidden Python process,
  allowing audio, compact layout, authoring, and verification to complete.
- Propagated safe-cancel markers into the nested title/menu runner so a running
  watched job now stops after its active encode unit instead of starting the
  next title inside the same disc.

## 0.1.0a1 - 2026-07-14

- Added the Phase 8 `auto`, `start`, sequential `queue`, `status --watch`,
  `jobs`, safe-boundary `cancel`/`resume`, `play`, `preset`, and `diagnose`
  user workflows.
- Added HandBrake-style CQ presets, NVENC/deinterlace preflight, atomic job
  records, and privacy-conscious diagnostic bundles.
- Added BD2HEVC-style HEVC encoder selection for NVENC, Intel QSV, AMD AMF, and
  libx265 across presets, queues, title/menu/interleaved conversion, progress,
  compaction identity, and resumable staging. Preflight now performs a real
  repeated-IDR/header probe; real VOBUs retain stricter per-boundary validation.
- Added full-disc compact-stereo for AC-3 title streams, including multi-track
  IFO metadata, exact compact-PES readback, byte-exact preservation of existing
  mono/stereo tracks, and the `compact-stereo` preset.
- Added BD2HEVC-style video/audio/mux progress lanes, bounded audio/staging
  overlap, queue pause/resume, queue positions, and safe-boundary cancellation.
- Added a responsive Windows GUI with single-disc and batch planning, editable
  output naming, queue management, lane progress, presets, diagnostics, a
  generated multi-resolution icon, native launcher, and desktop shortcut.
- Improved GUI polish with pointer-routed Convert-page wheel scrolling, clearer
  card/tab/header colour, colour-coded progress lanes and job states, and an
  explicit DVD-to-H.265 icon applied to the window, taskbar, launcher, and shortcut.
- Added delayed hover explanations across all GUI controls, F1 quick-start and
  expanded Help entries, plus a state-aware private VLC setup button. VLC setup
  is manifest/hash verified, repeat-safe, staged transactionally, and never
  modifies the normal VLC installation.
- Standardized default output names as `Movie (DVD) (HEVC).iso`: `(DVD)` is the
  visible library format and `(HEVC)` is the technical private-player marker.
  Replanning an already tagged name is idempotent.
- Promoted Auto CQ to the balanced default at that release, with a configurable 0.25x-4.00x
  bitrate multiplier and BD2HEVC-style main-title/top-N quality overrides that
  preserve physical DVD clip reuse by promoting shared VTS domains.
- Added IFO-derived language audio rules shared by GUI, CLI, presets, and job
  records. Selected AC-3 languages can use compact-stereo while unaffected
  tracks remain elementary-stream-identical in their original slots.
- Added a read-only ISO compatibility overlay for otherwise valid UDF DVDs
  whose ISO-9660 directory lengths omit sector-padding bytes, as found on the
  Mission: Impossible 2 backup. UDF and media payload bytes remain untouched.
- Passed the full Bourne compact-stereo release gate at 2,665,879,552 bytes,
  66.911% below source, with 44/44 UDF files and an exact physical graph.
- Completed full-disc compact HEVC fixtures for The Intern, The Bourne
  Identity, Taken 2, and Taken 3.
- Preserved shared physical cells and normal/extended branching without
  duplicate encodes.
- Fixed HEVC animated-menu cell transitions in patched VLC by resetting the
  menu clock and recreating only the HEVC video decoder.
- Added pinned VLC 3.0.23 preparation, release, supported-scope, and third-party
  licensing documentation.

This is an alpha release. Outputs require the project's patched VLC and inputs
must already be decrypted DVD ISO backups.
