# DVD navigation in UHD-BD output

UHD-BD is the default end-user output in DVD2HEVC 0.2.0a1. Its container is a
BDMV disc image at UDF 2.50; DVD VM execution and original graphics run in a
BD-J Xlet. Stock VLC 3.0.23 with libbluray BD-J and Java 11 is the tested player.
Patching VLC is unnecessary. Resolution remains that of the source DVD.

## Pipeline and progress

The existing conversion encodes each physical DVD video region, compacts the
HEVC DVD stage and validates its original graph/sector/payload invariants.
`dvd2hevc_app.uhd` then invokes the included `dvd2uhd` author. No intermediate
custom ISO or private-player gate is needed for this output.

The final 20% of overall progress covers source fingerprinting (80–81%),
BDMV/BD-J authoring (81–90%), full media audit (90–94%), ISO writing (94–96%),
payload verification (96–98%), stock-VLC startup (98–99%) and a final source
fingerprint check (99–99.5%). The mux lane
also includes these stages. Author/audit counts come from physical clips,
writer counts from actual Hadris payload bytes, and verification counts from
individually compared files. Completion is reserved for a passed job.

DVD menu video, SPU masks, palettes, button geometry/neighbour commands,
logical stream maps, title/PGC/program/cell commands and chapter targets are
retained. Physical clips can be shared by separate logical editions. Compatible
seamless cell runs become native joined playlists. BD-J clocks follow the DVD
presentation independently of still padding and pause in the appropriate states.

VLC's native subtitle menu synchronizes with DVD SPRM 2 through the public
BD-J SubtitlingControl. Clear-only PGS channels name the logical slots; the
original subtitle artwork, forced flags and timing are painted by BD-J.
Native audio/subtitle changes obey PGC and timed-PCI UOP restrictions, and
unavailable subtitle slots are rejected. Other native-control UOP coverage is
incomplete.

At an unambiguous compatible seamless AC-3 boundary, a successor's original
prefix completes the preceding cell's final access unit. Every occurrence of
a shared clip must agree on that successor. Provenance records both source
fragments and the complete frame hash. Ambiguous branches and unsupported
boundary formats remain explicit limitations.

Source and runtime fingerprints bind reusable completed authoring to the
original input and implementation. Reuse also requires a completed author report
belonging to that source; a completed disc in the current working directory is
never implicitly selected. The source fingerprint is checked again before
publication, and changed inputs leave the staging image unpublished. Still
padding must contain the complete original compressed-picture sequence before
any repetition. Incomplete authoring attempts are retained
and restarted in a new attempt directory. A final output is published only
after independent UDF/file hashes and stock-VLC BD-J/video startup pass.
Cancellation uses the existing worker-tree policy. The default startup gate
does not substitute for interactive whole-disc playback.

## Commercial-disc corpus

| Disc | Physical clips | Titles | PGCs |
| --- | ---: | ---: | ---: |
| Home Alone | 54 | 6 | 36 |
| Taken 2 | 157 | 10 | 78 |
| The Bourne Identity | 140 | 30 | 95 |
| Mission: Impossible | 58 | 3 | 128 |
| Moonlighting disc 1 | 93 | 19 | 200 |
| Benjamin Button feature | 69 | 3 | 22 |
| Gladiator | 86 | 20 | 39 |
| Taken 3 | 186 | 9 | 76 |
| Benjamin Button bonus | 497 | 20 | 54 |
| The Intern | 73 | 4 | 28 |
| I, Robot | 110 | 11 | 44 |
| The Bourne Supremacy | 164 | 24 | 112 |
| I, Robot bonus | 184 | 90 | 132 |
| Total | **1,871** | **249** | **1,044** |

Retained full media audits cover 4,453,387 original HEVC picture NALs and
1,696 audio tracks. Of those tracks, 1,630 match the full compressed payload
and A/V start offset; 22 use the narrower complete-access-unit scope with
boundary fragments recorded; 44 include a verified reconstructed original
AC-3 access unit. Static-picture repetition is separately checked. The repaired
Benjamin Button main-title tracks and the affected Intern extra match their
complete original logical elementary audio byte for byte.

The independent VM comparison uses unmodified libdvdnav as its C oracle:
24,584 cases, 2,582 unique source instructions, zero unexplained mismatches and
92 explicitly recorded upstream signed-multiplication overflow differences.
The corpus audits establish payload/navigation evidence, not exhaustive decoded
presentation or all menu paths. Commercial assets/logs stay private.

Nine current images received full media/UDF/payload checks and selected
original-startup ISO playback tests: Mission: Impossible, Benjamin Button
feature/bonus, Gladiator, Taken 3, The Intern, I, Robot feature/bonus and The
Bourne Supremacy. The other four retain matching
earlier full audits and selected stock-VLC playback evidence. Moonlighting's
historical bounded-run report has a conservative incomplete flag; its audited
93 clips exactly cover all 93 unique source physical cells.

The three additional integrated upgrades verify all 458 physical clips,
1,294,343 original picture NALs, 482 full compressed-audio payloads and 2,380
ISO files. Selected I, Robot movie controls include all 39 native chapter marks,
a chapter jump, native audio/subtitle changes, pause/resume and menu return.
Its bonus disc passed a selected feature/menu-return route. The Bourne Supremacy
received selected movie chapter/seek, audio/subtitle, pause and menu checks.
These callback tests establish navigation and decode, not overlay pixel accuracy.

Selected live window checks compared original highlights and mouse navigation
against the custom-DVD reference player, and observed original caption artwork
in stock VLC. Memory callbacks omit BD-J overlays, so those callbacks prove
decode/navigation rather than subtitle pixel accuracy.

## Known limits

- Eight Benjamin Button bonus-disc AC-3 boundaries have conflicting or
  nonseamless successors. DTS/E-AC-3 boundary reconstruction is unfinished.
- Taken 3 shares 53 physical clips between theatrical and extended editions.
  Both original DVD graphs have 33 chapters. The extended movie playlist has
  32 native chapter marks because its short tail has mismatched source timing;
  original ending tests reach the independent final cells/chapter, copyright
  screen and motion-menu return. The theatrical playlist exposes all 33.
- The Bourne Supremacy has 25 DVD chapters and 24 native marks in its main
  joined playlist. An original end-cell command branches before the independent
  final black chapter, so that chapter remains a separate DVD target. Native
  chapter menus currently enumerate only the active joined playlist.
- Angle blocks and random/shuffle PGCs are rejected during UHD-BD planning.
- Whole-title decoded A/V continuity, branch preroll, all subtitle/audio/menu
  routes and timed visual comparison need broader tests. No full commercial
  film was compared end-to-end.
- Caption artwork export to native PGS and real-disc regional colour-change
  coverage remain work items. Native seek/pause/chapter UOP enforcement is
  incomplete.
- Gallery still padding can increase output size substantially. HEVC savings
  do not guarantee that every UHD-BD image is smaller than its custom DVD.
- VLC acceptance does not certify a physical UHD-BD player. The current target
  is unmodified VLC with Java, not hardware certification.

The encoding catalogue's 117 legacy images are separate evidence. This change
does not relabel them as 117 tested UHD-BD outputs.
