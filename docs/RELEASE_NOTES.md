# DVD2HEVC 0.2.0a3 development update, 23 October 2026

- Keep hover highlighting alive in short DVD still menus, including Gladiator's
  audio setup and both subtitle pages. Independent unpadded stills now use the
  Blu-ray playlist's native infinite-still flag; the DVD VM retains control of
  finite/infinite waits and navigation. The Xlet keeps the video output open.
  No source video/audio is reencoded, replayed or expanded for this fix.
- Metadata repair can update the paired primary/backup playlist still fields
  along with the embedded runtime. Every byte outside the recorded metadata
  allocations is independently checked unchanged, including all media bytes.

The still-menu update passed live stock-VLC hover checks after waiting on all
three affected Gladiator menu pages, page switching and Main Menu return.
The regression suite now runs 305 tests: 304 passed, one optional source-disc
test skipped. Native still tests cover finite/infinite cell waits, final-cell
PGC waits, unaffected ordinary/repeated video, both MPLS copies and preservation
of all other image bytes. These selected routes do not certify every disc menu.

- Route VLC's native Disc Menu/title-0 selection into the original DVD root
  menu. Previously the persistent Xlet could continue the movie while VLC hid
  its timeline. Return to the movie restores its saved DVD cell and offset.
- Wait for asynchronous BD-J service selection before starting the next
  playlist. Reject stale input during the transition and report selection
  failure or timeout explicitly.
- Reset elapsed cell time on navigation. A DVD without a root menu now ignores
  the user menu request; prohibited native menu requests restore the movie.
- Add an allocation-preserving runtime repair primitive. It replaces existing
  Java classes in an unsigned JAR, verifies every other JAR entry and proves
  every ISO byte outside that allocation unchanged. This avoids remuxing media
  for a navigation-only repair. Separate verified candidates protect originals.

Stock-VLC regressions cover native Disc Menu, popup return, seeks, chapters and
paused menu entry on Gladiator, Taken and Terminator 2. The timeline is hidden
in menu domains and restored for movie domains. VLC can briefly hold its time
readout after a resume seek while decoding continues; this update does not
claim to remove that player behavior. Memory callbacks verify navigation and
decode, not the exact pixels of BD-J overlays or every disc route.

This update also includes the earlier SSD workspace, verified cross-volume
publication, compact still-video and incremental temporary-media cleanup fixes.

Earlier menu-service regression suite: 301 tests run, 300 passed and one optional source-disc
test skipped. Java service-event tests include delayed and reversed requests,
failure/timeout handling, native menu routing, missing menus and DVD UOPs.

# DVD2HEVC 0.2.0a2 alpha

- Match VLC/libdvdnav DVD mouse behavior: hovering selects without executing
  auto-action buttons, and a click selects and executes exactly once. This fixes
  Gladiator's scene-page arrows skipping pages while the pointer moves over them.
  Overlapping hit rectangles use the nearest button centre, with inclusive edges
  and the first button winning a tie. Original directional auto-action remains.
- Ignore mouse events against a menu whose DVD navigation has already changed.
- Add executable regression coverage of the actual BD-J mouse/key handlers,
  including repeated hover, clicks, overlap, prohibitions and stale navigation.

Local validation on 22 October 2026: 276 Python tests ran, 275 passed and one
optional original-disc test was skipped. The actual Xlet input handlers were
exercised with a test-only AWT host; production Java compiled against stock
VLC/libbluray's API and VM self-tests passed. Gladiator's repaired ISO passed
stock-VLC BD-J startup and interactive scene-menu checking. The runtime-only
repair retained every byte outside the JAR and every other JAR entry, including
the disc model and original graphic masks. New conversions include this fix;
existing authored images retain their old runtime until refreshed.

UHD-BD remains the default. This release contains the mouse fix, regression
tests and documentation; it does not add a playback-library setup feature.

# DVD2HEVC 0.2.0a1 alpha

This release makes UHD-BD the default output for new conversions. The original
DVD VM runs in BD-J, preserving menus/highlights, extras, shared clips and
audio/subtitle choices for stock VLC with Java. VLC patching is unnecessary
for this output. The explicit legacy HEVC DVD/private-player option remains
available; previously planned jobs and watched folders keep their format.

The GUI, CLI, presets, queues and watched folders carry the output choice.
The overall and mux progress bars include physical-clip authoring, full media
audit, measured UDF writing, file verification and the stock-player startup
gate. Completed encoding does not mark the whole conversion complete.
Existing HEVC DVD backups can be upgraded with `author-uhd` without reencoding
their HEVC video. Final images are protected against overwriting and published
after verification, with a receipt allowing interrupted publication recovery.
Cached authoring is checked against the requested source, including when the
command is launched from another authored disc's folder. Still repetition cannot
hide missing original pictures, new destination directories are created, and
source changes during authoring or output verification block publication.
Historical compact timezone offsets are parsed on Python 3.10 too, so old
disk-full reports cannot be treated as evidence from a current attempt.

## Local validation, 5 October 2026

- 275 Python tests run: 274 pass, one optional original-disc test skipped.
- The generated 16-second PAL Colour Lab DVD passed the complete MPEG-2 to
  HEVC to BD-J/UDF/stock-VLC workflow with software x265. Source 12,144,640
  bytes; final ISO 5,568,512 bytes; measured conversion 26.14 seconds.
  See [the generated receipt](demo-result.json).
- A fresh integrated upgrade of Taken 3 passed all 186 physical-clip media
  audits, UDF descriptors, hashes of all 952 image files, and original BD-J
  startup in stock VLC. The extended-edition route passed subtitle selection,
  chapter jump, pause/resume and original menu return through the packaged
  test harness. It retained the 53 shared physical clips between cuts.
- Three further integrated upgrades passed: I, Robot feature/bonus and The
  Bourne Supremacy, covering 458 clips and all 2,380 ISO files. Full audits match
  1,294,343 original picture NALs and 482 full compressed-audio payloads.
  Selected movie/bonus menus and native playback controls were exercised.
  The expanded independent VM comparison passes 24,584 cases, with 2,582
  unique source instructions and zero unexplained mismatches.
- A real-tool generated-disc upgrade launched from an unrelated completed
  commercial disc's folder selected the requested source and created its new
  destination directory correctly. The installed package also passed a fresh
  complete MPEG-2-to-UHD-BD conversion with the new source stability checks.
- The installed wheel completed the full generated MPEG-2-to-UHD-BD conversion
  outside the checkout, using packaged runners/author and the prepared external
  inspector/tools. A separate installed-author run passed native chapters,
  seek and pause/resume checks in stock VLC.
- The Java runtime source is unchanged from the prior ten-disc fidelity run.
  All nine installed Java files compiled against stock VLC's API; VM self-tests
  passed and all 23 runtime classes matched the tested author byte for byte.
  Artifact verification records exact source ZIP/wheel hashes, runs exported
  tests and checks fresh installed resources, GUI and CLI entry points.

## Scope and limitations

The 13 commercial-disc author corpus covers 1,871 clips, 249 titles and 1,044
PGCs. It is separate from the 117-image legacy HEVC DVD catalogue. Neither
count certifies every path or a full-film decoded comparison. See
[UHD_BD.md](UHD_BD.md) for per-disc counts, VM comparison and precise audio
payload/access-unit audit scopes.

Eight bonus-disc audio boundaries remain ambiguous/nonseamless. Whole-title
A/V continuity and timed artwork comparisons need more coverage. Taken 3's
extended playlist exposes 32 native chapter marks versus 33 DVD chapters;
the original independent final chapter/ending route is preserved. The Bourne
Supremacy similarly exposes 24 native movie marks versus 25 DVD chapters; its
end-cell branch and independent final chapter remain separate DVD targets. Multi-angle
and random/shuffle playback are rejected. Native player UOP enforcement is
incomplete. Subtitle artwork is painted in BD-J, with clear PGS slots for
native track selection. No hardware UHD conformance, 4K upscaling or HDR
creation is claimed. Windows is the complete conversion target; the real-tool
integration here used stock VLC 3.0.23/libbluray 1.4.0 and Java 11.0.29.
The three additional automatically discovered startup gates also passed with
Java 8.0.482; their selected interactive controls used Java 11.0.29.

## Distribution

GPL-3.0-only source, upstream VM attribution, Java source and Hadris MIT fork
source/binary provenance ship together. Media, captures, private reports,
external player/encoder binaries and credentials are excluded. See
[PUBLISHING.md](PUBLISHING.md) for the review/build/publication checklist.
Source and matching alpha downloads are published through
[DVD2HEVC on GitHub](https://github.com/nathanturk2/DVD2HEVC). The validation
above is local; hosted CI has its own separately recorded results.
