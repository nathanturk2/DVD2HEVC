# DVD2HEVC

**Smaller DVD backups with the original disc experience.** DVD2HEVC converts
already-decrypted DVD ISOs from MPEG-2 to HEVC, preserving the original menus,
extras, chapters, shared footage and audio/subtitle choices.

**0.2.0a3 development: UHD-BD output with VLC-style navigation.** The DVD virtual machine runs
in BD-J inside a BDMV/UDF 2.50 image. **Stock VLC with Java can play it; patching
VLC is not necessary.** The explicit legacy HEVC DVD option remains available
for its private patched player.

The mouse update fixes scene-menu page arrows activating on hover, including
Gladiator. Hover highlights; a click activates once. Original shaped highlight
bitmaps and directional auto-action are preserved. Existing UHD-BD images need
their embedded BD-J runtime refreshed; updating the application alone does not
change an already authored ISO.

The current development update also fixes native VLC **Disc Menu** selection
leaving the movie running with its timeline hidden, and serializes asynchronous
menu/movie service changes. Menu return retains the DVD resume position. See
[release notes](docs/RELEASE_NOTES.md) for validation and remaining limits.
Short still menus also retain VLC's live highlight output after the video ends,
including Gladiator's audio setup and subtitle pages. This uses native Blu-ray
still metadata and does not expand or reencode the original media.
Repeated-picture stills now use the same native hold, avoiding video-output
restarts while navigating Voyager's language screen. Mouse and directional
navigation can alternate; stationary-pointer refreshes do not undo key input.

To rebuild a fixed UHD-BD from a retained HEVC-DVD backup, use a new output name:

```powershell
dvd2hevc author-uhd 'D:\Backups\Movie (DVD) (HEVC).iso' 'F:\Converted\Movie fixed (DVD) (UHD-BD).iso' --work-dir 'F:\Work\Movie-fixed'
```

The existing output is protected. This remuxes the existing HEVC video without
reencoding and verifies the new image before publication.

Video retains its DVD resolution and cadence policy. “UHD-BD” identifies the
HEVC Blu-ray structure used for VLC playback; it does not promise 4K upscaling,
HDR creation or hardware UHD player conformance. This remains experimental:
keep original backups and check the routes you use before relying on a result.

Source and alpha downloads: [DVD2HEVC on GitHub](https://github.com/nathanturk2/DVD2HEVC).

![DVD2HEVC Windows conversion interface](docs/images/convert-uhd.png)

*The real GUI with generated backup names. Documentation queue/progress examples
are labelled simulated; conversion evidence is recorded separately.*

## Install and check tools

The complete conversion workflow targets Windows 10/11 and accepts local,
already-decrypted DVD ISO backups. DVD2HEVC does not decrypt discs.

| Requirement | Purpose |
| --- | --- |
| Python 3.10+ with Tkinter | CLI and desktop GUI |
| FFmpeg / FFprobe and HandBrakeCLI | Encoding, source inspection and validation |
| A working HEVC encoder | NVENC, QSV, AMF or software x265; available hardware is probed |
| Native DVD inspector | Built from pinned libdvdread source; needs Git, Visual Studio C++ tools, Meson and Ninja |
| tsMuxeR with `--blu-ray-v3` | UHD-BD transport/playlist authoring |
| Stock VLC with libbluray BD-J and a Java 11 JDK | Compile DVD navigation and play/validate it |
| Hadris streaming UDF author | Included for Windows x64, with fork source and MIT notice |
| Temporary storage | Encoding stage, BDMV media and verified ISO coexist during authoring |

Extract the source release into a permanent directory:

```powershell
python -m pip install .
python -m pip install meson ninja
python dvd2hevc.py build-inspector
$env:JAVA_HOME = 'C:\Program Files\Eclipse Adoptium\YOUR_JAVA_11_JDK'
python dvd2hevc.py tools
python dvd2hevc.py gui
```

Install external tools separately. Put FFmpeg, FFprobe, HandBrakeCLI and
tsMuxeR on `PATH`; `DVD2HEVC_TSMUXER` can identify a tsMuxeR executable.
`DVD2HEVC_STOCK_VLC_ROOT` can identify a stock VLC directory. Authoring also
accepts `--tsmuxer`, `--udf-tool`, `--java-home` and `--vlc-root` overrides.
The BD-J API JAR comes from VLC's `plugins/access`; it is not bundled here.
No private VLC build is required for the default output. Menu streams with audio use a
4 Mbit/s minimum transport rate so stock VLC services BD-J navigation promptly.
This adds transport stuffing to low-bitrate menus, without re-encoding their
pictures or audio or changing the original transitions. Noninteractive titles
retain their normal variable bitrate. Low-bitrate menus become logically larger;
the padding is highly compressible on filesystems with transparent compression.

The current VLC-targeted output uses reserved CLPI application type `0` for
ordinary main-path video clips. Stock VLC/libbluray accepts this value and keeps
normal video playback, avoiding a VLC 3.0.23 playlist/stream lock inversion that
can freeze feature selection or clip changes. This is an intentional departure
from standard Blu-ray metadata, not a claim of hardware-player conformance.
Slideshow types, media bytes, seek indexes, timestamps and DVD navigation are
retained. Existing images can receive this metadata-only repair without remuxing.

For a desktop shortcut:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\install-gui-shortcut.ps1
```

Wheels include the GUI assets, documentation, runners, Java source and Windows
UDF author. Jobs/reports live under `%LOCALAPPDATA%\DVD2HEVC`, or
`DVD2HEVC_STATE_DIR`; larger workspaces are separately recorded in each job.
See [tool provenance](docs/TOOL_PROVENANCE.md) and
[third-party notices](THIRD_PARTY_NOTICES.md).

A wheel installation also provides `dvd2hevc-gui`, which launches the desktop
interface without a console window on Windows.

## Convert

1. Choose the source ISO and destination in **Convert**.
2. Leave **Disc output** at **UHD-BD (stock VLC)**. The GUI explains that VLC
   patching is unnecessary. Generated filenames use `(DVD) (UHD-BD)` tags.
3. Choose quality, encoder, deinterlacing and audio policy. The balanced default
   uses VBR at 25% of measured source MPEG-2 video bitrate; passthrough audio is
   the default. CQ values trade quality against size.
4. Use **Plan only**, then **Add to queue**. Unsupported angle/random structures
   in the current BD-J implementation fail during UHD-BD planning.
5. Watch **Jobs & progress**. The overall and mux bars include BDMV authoring,
   the full media audit, measured ISO writing, payload verification and the
   stock-VLC startup gate. They reach 100% only when the job passes.
6. Use **Play output**. It opens UHD-BD through VLC's Blu-ray/menu mode with
   Java, or chooses the private player for a legacy HEVC DVD.

![DVD2HEVC UHD-BD progress example, explicitly simulated](docs/images/jobs-uhd.png)

```powershell
dvd2hevc auto 'D:\Backups\Movie.iso' --dry-run
dvd2hevc start 'D:\Backups\Movie.iso'
dvd2hevc status --watch
dvd2hevc queue 'D:\Backups' --output-dir 'F:\Converted'
dvd2hevc watch-folder 'D:\Incoming' --output-dir 'F:\Converted' --recursive
```

Batch queues and watched folders retain their chosen output format and settings.
Previously queued legacy jobs/watches keep their original format. Presets save
the new choice. Pause-after-current, cancellation and safe resume remain
available. Sources are opened read-only, and existing outputs are protected.
The new final ISO is published after payload and player checks pass; failed
authoring attempts retain diagnostics. A failed partial BD-J build currently
restarts that authoring attempt, while completed encoding work can be reused.

## Existing HEVC DVD backups and legacy output

Upgrade an existing DVD2HEVC backup without reencoding its HEVC pictures:

```powershell
dvd2hevc author-uhd 'D:\Backups\Movie (DVD) (HEVC).iso' 'F:\Converted\Movie (DVD) (UHD-BD).iso' --work-dir 'F:\Work\Movie'
```

For the older custom DVD format, select **HEVC DVD (legacy player)** or use
`--output-format dvd-hevc`. That option requires the private player described
in [VLC_BUILD.md](docs/VLC_BUILD.md), plus genisoimage/mkisofs. UHD-BD output
uses stock VLC and the included UDF writer instead.

## Work files and disk space

Choose **Work folder** in the GUI to keep temporary files on a drive of your
choice. The setting is saved and applies to new single-disc, batch and watched
jobs. Existing jobs retain their original paths. An SSD is recommended for
temporary work; the size of the improvement depends on the disc and hardware.
One full 297-cell HEVC-DVD upgrade on 23 October 2026 took **15:37** with an
external Toshiba HDD workspace and **11:41** with an internal Acer NVMe SSD
workspace, including verified copy-back, cleanup and diagnostic archiving: **25% less time (1.34×
throughput)**. Both runs passed full media and stock-VLC gates, and all 297
transport-stream files matched byte for byte. The SSD run peaked at **22.3 GiB**
of work files including its ISO. This is one same-disc comparison with normal
OS caching, not a universal performance claim; CPU/GPU encoding may dominate
other workflows. That drive comparison predates the compact still/scratch
changes described below.
The final ISO can remain on a different HDD. UHD-BD images are authored and
checked on the work drive, then copied to a private destination file, read back
and SHA-256 checked before the final filename appears.

The CLI accepts `--work-root 'D:\DVD2HEVC work'` for `auto`, `start`, `queue`
and `watch-folder`. This creates separate managed job directories and prunes
reproducible files after success. `--work-dir` remains the exact-path option for
an explicit resumable workspace. `DVD2HEVC_WORK_ROOT` overrides the saved GUI
preference when no command-line work root is given.

An existing mounted RAM disk can be selected as the work folder. The GUI shows
a starting allowance of **the larger of 16 GiB or 6 times the source ISO
size**, plus **8 GiB of additional free RAM** for the converter and background
work. For example, a 4 GiB ISO suggests 24 GiB of usable RAM-disk space and roughly 32 GiB
free RAM before allocating it; an 8 GiB ISO suggests roughly 56 GiB free RAM.
These are planning estimates, not allocations or measured exact requirements;
allow filesystem/driver overhead as well. Unusual discs can exceed this
size-based estimate. Temporary media is retired as each physical clip completes;
the exact source cells remain until the full compressed-picture/audio audit
finishes, and original AC-3 fragments remain until cross-cell repair planning.
Static stills use a short native asset and a native Blu-ray still hold,
retaining the original DVD wait and audio instead of writing the wait as a
minute of repeated pictures on every gallery page. No repeated seeking is needed.
The 404-clip Mission: Impossible bonus-disc test on 23 October 2026 peaked at
**7.5 GiB**, including the ISO; its final image fell from **21.9 GiB to 3.0 GiB**.
The older run had retained **86.5 GiB** of intermediate media. The new run passed
the full source-picture/audio, navigation, shared-clip and ISO payload audits,
and all 404 graphics resources and 23 runtime classes matched the prior output.
For that measured case, a **16 GiB usable RAM disk plus 8 GiB free for processing**
is a reasonable starting allowance, about **24 GiB free RAM** before allocation,
rather than the previous roughly 128 GiB estimate. This is one measured disc;
the generic estimate is not a guaranteed upper bound.
Measure the peak on an SSD before using a RAM disk for unfamiliar structures.
No RAM-disk driver is installed. Volatile work is lost when the disk is
unmounted or power is lost, so an SSD is preferable for reliable resume.

UHD-BD writer logs, manifests and progress counters stay in a single
`Conversion files` folder beside the output. Unpublished images use the work
volume when it differs from the output volume; only a verified transfer uses
temporary space on the destination. For `author-uhd`, `--support-dir` selects
one shared diagnostics folder for a whole library; it must be on the output volume.
Successful clip muxes retire duplicate MKV/elementary/transport work immediately.
After a verified conversion, cached source VOBs and authored transport files
are removed from that attempt's workspace. Verification records, navigation,
logs and failed clips' scratch remain. `author-uhd --keep-intermediates` retains
all mux work for debugging. Resume rechecks the ISO against its recorded
payload hashes, so it does not require those temporary media copies.
Use `author-uhd --keep-intermediates` when retaining them for investigation.

For one shared support/work folder when upgrading a collection:

```powershell
dvd2hevc author-uhd 'D:\Backups\Movie (DVD) (HEVC).iso' 'F:\Converted\Movie (DVD) (UHD-BD).iso' --work-dir 'F:\Converted\Conversion files\work\Movie' --support-dir 'F:\Converted\Conversion files\images'
```

## What has been tested

The UHD-BD author/navigation work has a **13-disc commercial DVD backup corpus**:
Home Alone, Taken 2, The Bourne Identity, Mission: Impossible, Moonlighting
disc 1, The Curious Case of Benjamin Button feature and bonus discs, Gladiator,
Taken 3, The Intern, I, Robot feature and bonus discs, and The Bourne Supremacy.
It covers **1,871 physical clips, 249 titles and 1,044 PGCs**.
Taken 3's two cuts share 53 physical clips. The full retained media audits cover
4,453,387 original HEVC picture NALs and 1,696 audio tracks, with the precise
full-payload versus complete-access-unit scopes recorded in
[the UHD-BD notes](docs/UHD_BD.md).

The Java VM comparison passes 24,584 real/generated cases with zero unexplained
mismatches; 92 upstream signed-multiplication overflow differences are explicit.
Nine current final images pass UDF/payload checks and selected original-startup
stock-VLC tests. Selected menus, extras, subtitle/audio changes, chapters,
seeks, pause/resume, stills, galleries and both Taken 3 editions were exercised.
The corpus does not represent full films watched end-to-end or every path tested.

The older conversion catalogue contains **117 legacy HEVC DVD images** in its
30 September 2026 snapshot. [TESTED_DISCS.md](docs/TESTED_DISCS.md) lists those
examples; catalogue presence is not fresh certification of the UHD-BD path.
The generated **Colour Lab** DVD also passes the integrated MPEG-2 → HEVC →
BD-J/UDF workflow using software x265 and stock VLC. Run
`python tools/make-demo.py NEW_DIRECTORY --convert` to reproduce it with the
external tools installed. Its pictures and audio are generated by FFmpeg.

Current gaps include eight ambiguous/nonseamless AC-3 boundaries on the bonus
disc, broader whole-title A/V continuity and timed visual comparisons,
multi-angle and random/shuffle playback, and complete native VLC UOP handling.
Taken 3's extended movie run exposes 32 native chapters versus 33 DVD chapters;
its independent final chapter and original post-play menu route work.
Original caption artwork is rendered in BD-J; native clear-only PGS channels
provide selection, not standalone exported caption artwork.

## Support and development

Read the retained job error/log first. To prepare a support bundle:

```powershell
dvd2hevc diagnose JOB_ID --output 'F:\Support\diagnostics.zip'
```

Known private paths are redacted and media is omitted; review the bundle before
sharing. Include versions, encoder/settings and the exact failed menu route.
Do not upload copyrighted disc media or keys.

- [UHD-BD implementation, evidence and limits](docs/UHD_BD.md)
- [Supported scope](docs/SUPPORTED.md), [capabilities](docs/CAPABILITIES.md)
- [Publishing](docs/PUBLISHING.md), [release checklist](docs/RELEASE_CHECKLIST.md)
- [Legacy workflow reference](docs/WORKFLOW_REFERENCE.md)
- [Contributing](CONTRIBUTING.md), [changelog](CHANGELOG.md)

The combined source is GPL-3.0-only. The DVD VM retains libdvdnav attribution;
the imported BD2HEVC UDF reader/validator and bundled Hadris source have their
notices. External VLC, Java, FFmpeg and tsMuxeR binaries and film/menu/subtitle
assets are not included in the source release.

Release details and local validation: [RELEASE_NOTES.md](docs/RELEASE_NOTES.md).
