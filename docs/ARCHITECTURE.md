# Architecture

DVD2HEVC keeps logical DVD behavior and physical sector layout separate:

1. `iso` reads the UDF `VIDEO_TS` inventory without mounting or extraction.
2. `vob` parses 2,048-byte packs, genuine PES headers, NAV packs, scrambling
   flags, and available video payload capacity.
3. `handbrake` temporarily supplies logical title/chapter discovery.
4. The native `dvdinspect` helper uses libdvdread to produce the authoritative
   VMG/VTS/PGC/program/cell/VOBU/angle graph; libdvdnav traces are the behavioral
   cross-check.
5. `planning` selects physical VOB/cell assets rather than independently
   encoding logical titles, preventing duplicate work on branching editions.
6. A single-pass domain extractor opens the UDF image once and fans contiguous
   or discontinuous sector ranges into every uncached physical-cell input. The
   prototype writer then encodes bounded cells, repacketizes HEVC into original
   video-sector slots, validates VOBU budgets, and commits atomically to staging.
7. A narrowly patched VLC build retains HEVC program-stream signalling in the
   DVDNAV path; a headless log gate verifies navigation and decoder selection.
8. Complete staging overlays validated title, VTS-menu, and VMG-menu ranges,
   scans every VOB, and hash-reads every replacement before publication.
9. Compact staging removes title-domain padding one VTS at a time and rewrites
   every affected navigation table through copy-on-write intermediate stages.
10. `genisoimage` authors a DVD-Video-ordered UDF image; verification hashes every
   UDF file and requires an exact libdvdread graph match with the source.
11. The Phase 8 frontend records a quick physical plan before queue admission,
   dispatches background discs serially, and exposes append-only video, audio,
   and mux progress without attaching to the encoder console. Within one disc,
   bounded compact-audio workers overlap all-HEVC sector staging; navigation
   mutation and authoring stay serial. The worker still calls the same verified
   low-level commands rather than maintaining a second conversion implementation.

Encoder selection is carried as cache identity from the named preset through
the queue, PowerShell orchestrator, title/menu/interleaved reports, compact
layout, and final stage. A shared encoder-profile builder maps p1-p7 to NVENC,
QSV, AMF, or x265 native controls while enforcing HEVC Main, no B-frames,
closed forced IDRs, AUDs, and repeated headers. The runtime probe is an early
capability check; `validate_vobu_random_access` and complete decode remain the
authoritative gates on real disc content. Encoder/settings variants use
separate staging roots so a resumed job cannot silently reuse another backend's
validated sectors.

New jobs and authored reports include `dvd2hevc-capability-contract-v1`, naming
the compact format and patched-player ABI. Legacy jobs without a contract remain
readable, while a recorded incompatible contract is never silently resumed.
Descriptive records stay under `reports/jobs`; bulky managed artifacts use a
short hashed path under the user's local application-data directory to avoid
Windows path-length failures.

Terminal jobs feed an ignored, media-free compatibility registry. It preserves
sampled disc identity and verification evidence after disposable source images
are removed, without checking copyrighted reports into source control.

VOB files remain inventory containers, not conversion units. The physical graph
identifies cells and VOBUs so branching titles that share sectors are encoded
once and every logical navigation path continues to reference the same content.
Extraction batching and the physical-VTS scheduler analysis are documented in
`PERFORMANCE.md`.
