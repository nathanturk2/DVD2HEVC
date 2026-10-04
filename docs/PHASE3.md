# Phase 3: automated multi-cell title conversion

> Historical custom-DVD implementation notes. For the current default UHD-BD output and stock-VLC requirements, see [UHD_BD.md](UHD_BD.md).

Phase 3 turns the single-cell proof into a repeatable, failure-contained title
pipeline. The first completed target is The Intern, global title 3: a 276.12
second, two-chapter PGC containing four referenced physical cells. Its final
cell is shared with title 2 and lies earlier in the title-domain sector layout,
making it a useful test of physical-cell identity rather than playlist order.

## Pipeline

`convert-title` now performs the following transaction for every unique physical
cell referenced by a single-PGC title:

1. Read the authoritative title, PGC, cell, and VOBU graph from `dvdinspect`.
2. Extract only the VOB file portions overlapping the physical cell.
3. Encode HEVC Main 8-bit with bounded x265 worker pools and a cell-start random
   access point.
4. Assign timestamped video PES packets to VOBUs and reject any local capacity
   overflow before writing output.
5. Repack sequentially into the original video sectors, inserting HEVC program
   stream maps and retaining every sector at 2,048 bytes.
6. Independently verify non-video sectors and video-sector prefixes, rescan pack
   structure, compare source/output frame counts and audio hashes, and fully
   decode the HEVC cell.
7. Save per-attempt logs and JSON reports so a failed cell is a resumable,
   reproducible support bundle.

The configured CRF ladder is tried only when a genuine VOBU or packet-overhead
limit is exceeded. No lower-quality attempt is committed merely because a prior
attempt failed.

`stage-title` then extracts a new DVD folder, retaining all original IFO, BUP,
menu, and selected VTS title files. It overlays only passed physical-cell ranges,
rescans the complete title domain, and refuses an existing destination. The
separate `verify-stage` gate reads the overlaid ranges back and compares SHA-256
hashes with the validated cell files.

## Title 3 result

- Physical cells converted: 4
- VOBUs converted: 555
- Source/output frames: 6,892 / 6,892
- Replacement bytes: 132,173,824
- Complete staged title domain: 6,864,805,888 bytes
- Invalid or scrambled staged sectors: 0
- HEVC program stream maps: 555
- Encoder result: every cell fit at the first configured setting, CRF 20
- Audio: source/output elementary packet hashes matched for every cell
- Staged read-back: all four SHA-256 comparisons matched

The largest cell contains 6,700 frames and 539 VOBUs. Its HEVC payload used
66,695,224 of 117,484,768 available video-payload bytes; its smallest local VOBU
still retained 9,233 bytes of headroom.

An early run exposed a repacker defect: timestamp-proportional sector selection
could strand usable slots earlier in a VOBU and falsely report overflow even at
CRF 30. The saved measurements showed ample total and local capacity, so the
packer was changed to sequential first-fit within each VOBU. PES timestamps
continue to control presentation. The same cell then passed at CRF 20, avoiding
an unnecessary quality compromise.

## Navigation and player result

The untouched source and staged output were both played from title start to end
at 8x through the isolated VLC 3.0.23 runtime. The staged output selected the
HEVC packetizer and decoder and retained the AC-3 path. Normalized source/output
DVDNAV traces matched exactly: 8 ordered events and 23 associated navigation
fields, including VTS, domain, cell/program lengths, and PGC duration.

The four authored cells are seamless; libdvdnav reports them as one combined
cell-change unit during playback. The feedback gate therefore compares the
source trace instead of assuming one event per physical cell.

```powershell
python dvd2hevc.py convert-title "D:\DVD backups\THE_INTERN.iso" 3 work\phase3\title3
python dvd2hevc.py stage-title work\phase3\title3\title-report.json work\phase3\title3-dvd
python dvd2hevc.py verify-stage work\phase3\title3-dvd\dvd2hevc-stage-report.json
python dvd2hevc.py compare-navigation work\phase3\source-vlc.log work\phase3\output-vlc.log
```

## Remaining limits

The current pipeline deliberately supports one PGC per selected title and emits
a DVD folder rather than a UDF ISO. It converts referenced title cells only, so
the staged VTS may remain mixed MPEG-2/HEVC outside the selected title. Menu
video, the two-hour main feature, alternate cuts, angles, and whole-disc output
remain later gates. Encoding is conservative—no B-frames and frequent random
access headers—until navigation coverage is broader.
