# Phase 5: menu domains and complete UDF image

> Historical custom-DVD implementation notes. For the current default UHD-BD output and stock-VLC requirements, see [UHD_BD.md](UHD_BD.md).

Phase 5 has produced the first complete DVD2HEVC image of *The Intern*. Every
physical video cell in the disc is represented by a validated progressive HEVC
replacement. The result remains the sector-preserving profile: video payload
space that HEVC does not need is padding inside the original VOB extents.

## Coverage

- VTS 1 titles: 20 cells and 14,682 VOBUs.
- VTS 2 title: 2 cells and 22 VOBUs.
- VMG menus: 41 cells, 41 VOBUs, and 41 still frames.
- VTS 1 menus: 10 cells, 676 VOBUs, and 10 still frames.
- Total replacements: 73 physical cells covering 6,887,024,640 VOB bytes.

The menu VOBU count is larger than the menu frame count because the long menu
cells use one still image while NAV and AC-3 continue. Those audio/NAV-only
VOBUs correctly receive no invented video packet or program stream map. All
original non-video sectors and video-sector prefixes remain byte-identical.

## Complete-disc gates

The complete staged folder passes all of these checks:

- 73/73 replacement ranges hash back to their validated cell files;
- every VOB scans with zero invalid and zero scrambled sectors;
- all title and menu HEVC payloads decode completely;
- audio stream presence and hashes are preserved;
- all IFO, BUP, NAV, subpicture, highlight, command, and still data are copied.

The first UDF image was authored with open-source `genisoimage` in DVD-Video
ordering. Its measured results are:

- image size: 6,888,226,816 bytes;
- 17/17 `VIDEO_TS` files and 6,887,258,112 bytes hash-identical to staging;
- source/authored libdvdread physical graphs exactly equal;
- 3,362,805 VOB sectors scan with zero invalid or scrambled sectors;
- 14,704 title VOBUs and 717 menu VOBUs are present;
- patched VLC passes both title and menu startup directly from the ISO.

The ISO is smaller than the source backup because reauthoring omits original
filesystem slack. It does **not** yet reclaim the much larger padding inside the
sector-preserved VOBs; that is the distinct compact profile in Phase 6.

## Menu behavior feedback

A source/output menu-start trace matches exactly: 13 DVDNAV events and 41
normalized fields, including first-play commands, VMG/VTS changes, cell
selection, the default highlighted button, audio selection, subpicture streams,
and CLUT changes. The output also starts patched VLC's HEVC decoder and AC-3
path.

The headless `dvdnavtrace` feedback driver then executes real menu commands
without decoding video or controlling Windows. All four top-level buttons have
exact source/output traces (28, 27, 27, and 28 lines respectively). Two
multi-action paths also pass exactly:

- top-level button 2 followed by the submenu's current button: 36 lines,
  including the resulting title-cell transition;
- top-level button 3 followed by the submenu's current button: 35 lines,
  including the return to the main menu.

These traces cover button enumeration, selection and activation status,
highlight updates, hop events, VTS/domain changes, title/menu cell lengths,
audio selection, subpicture selection, and CLUT changes. This closes the Phase
5 active-menu gate for the baseline disc. The difficult-disc phase will expand
the same matrix for branching and language variants.

Authored ISOs initially exposed a startup-performance issue. When libdvdcss is
available, libdvdread normally performs an eager all-title key pass for image
files. It completes immediately on the clear MPEG-2 source backup, but its
MPEG-oriented title-key search fails slowly against the custom clear HEVC VOBs.

No VLC modification is needed. libdvdread already supports `DVDREAD_NOKEYS=1`
to disable that eager pass. On identical three-second title probes, normal
startup took 22.27 seconds, including 12 seconds of failed key cracking; the
existing switch reduced it to 10.48 seconds and emitted no key-scan messages.
DVDNAV, HEVC map/packetizer/decoder, and AC-3 checks passed in both cases.

This switch is only safe for verified clear DVD2HEVC output. It must not be set
globally for encrypted source discs. `author-iso` records the required playback
environment in its report, and the VLC feedback script exposes `-SkipCssKeys`.

## Repeatable commands

```powershell
python dvd2hevc.py convert-menu "D:\DVD backups\THE_INTERN.iso" vmg_menu 0 work\phase5\vmg-menu-nvenc
python dvd2hevc.py convert-menu "D:\DVD backups\THE_INTERN.iso" vts_menu 1 work\phase5\vts1-menu-nvenc
python dvd2hevc.py stage-disc work\phase5\complete-nvenc-dvd <title reports...> <menu reports...>
python dvd2hevc.py verify-stage work\phase5\complete-nvenc-dvd\dvd2hevc-stage-report.json
python dvd2hevc.py author-iso work\phase5\complete-nvenc-dvd work\phase5\THE_INTERN_DVD2HEVC.iso --label THE_INTERN_HEVC
python dvd2hevc.py verify-iso work\phase5\THE_INTERN_DVD2HEVC.iso.json
powershell -ExecutionPolicy Bypass -File tools\test-vlc-dvdhevc.ps1 -VlcRoot work\phase2\vlc-dvdhevc -DvdPath work\phase5\THE_INTERN_DVD2HEVC.iso -SkipCssKeys
powershell -ExecutionPolicy Bypass -File tools\test-dvdnav-menu.ps1 -Source "D:\DVD backups\THE_INTERN.iso" -Output work\phase5\THE_INTERN_DVD2HEVC.iso -Button 4
```

On this development machine `author-iso` uses `genisoimage` from Ubuntu 24.04
under WSL. A native `genisoimage` or `mkisofs` installation is used when found.
The active-menu tool uses WSL's `gcc`, `libdvdnav-dev`, and `libdvdread-dev`.
