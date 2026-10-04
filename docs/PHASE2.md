# Phase 2: one-cell HEVC feasibility proof

> Historical custom-DVD implementation notes. For the current default UHD-BD output and stock-VLC requirements, see [UHD_BD.md](UHD_BD.md).

Phase 2 answers the narrowest high-risk question: can DVD title navigation remain
in place while the MPEG-2 video occupying its original sectors is replaced by
HEVC and decoded through VLC's DVDNAV path? The answer is yes for the first
controlled fixture.

## Fixture and result

The test uses title-domain cells 1 and 2 from The Intern, VTS 2, PGC 1. Cell 1
contains 3,623 sectors, 21 VOBUs, and 10.08 seconds of PAL 720x576 interlaced
video. Cell 2 contains 34 sectors, one VOBU, and 0.72 seconds.

The prototype:

- extracts exact title-domain sector ranges without mounting or changing the ISO;
- encodes HEVC Main 8-bit, top-field-first, with conservative no-B-frame settings;
- assigns timestamped HEVC PES packets to the original VOBU time windows;
- inserts an MPEG program stream map declaring stream `0xe0` as HEVC type `0x24`;
- rewrites only original video-bearing sectors and pads every sector back to 2,048 bytes;
- leaves NAV, audio, and other non-video sectors byte-identical.

Cell 1 retained all 3,623 sectors and its exact 7,419,904-byte size. The HEVC
payload used 1,965,885 bytes of 6,697,108 available video-payload bytes; all 21
VOBU budgets passed. A structural rescan found 3,623 valid packs, 21 NAV packs,
21 program stream maps, and no scrambled or invalid sectors. FFmpeg decoded all
252 video frames. The source and output AC-3 packet streams had the same MD5,
`e28e316f46ee6f679503a1f4802c5f07`.

Cell 2 also retained its exact 34-sector size. Its 18 HEVC frames used 6,706
bytes of 26,040 available video-payload bytes.

## VLC result

Stock VLC 3.0.23 followed the DVD navigation graph but discarded the program
stream map and incorrectly selected its MPEG-2 video packetizer. The patch in
`patches/vlc/0001-dvdnav-accept-hevc-program-stream-maps.patch` makes the DVDNAV
demux retain program stream maps, use them when creating tracks, and emit the
appropriate end-of-sequence data for an HEVC still.

A Windows build of that plugin was tested in a private copy of VLC; the installed
VLC was not changed. The headless regression reached VTS 2, cell 1 with the
expected 3,623-sector cell length, accepted the HEVC map, selected the `hevc`
packetizer, started FFmpeg's HEVC decoder, and selected the unchanged AC-3 stream.

Run the same feedback gate against a private VLC tree and authored fixture with:

```powershell
powershell -ExecutionPolicy Bypass -File tools\test-vlc-dvdhevc.ps1 `
  -VlcRoot work\phase2\vlc-dvdhevc `
  -DvdPath work\phase2\dvdnav-fixture `
  -Title 4
```

The command runs VLC with dummy interfaces and outputs, so it does not take over
the Windows desktop. It then checks the log for successful DVDNAV opening, a cell
change, HEVC map acceptance, the HEVC packetizer, and decoder startup.

## Deliberate limits

This is not yet a whole-disc converter. The encoder settings prioritize simple,
deterministic timestamp and VOBU placement rather than final compression quality.
Only title cells in a small staging fixture have been rewritten. Menu video,
multi-cell continuity, shared cells, angles, seamless branching, still frames,
subpictures, and authored ISO output remain later regression gates.

Phase 3 should expand the same transaction and validation loop to a complete
multi-cell title before attempting menus or the difficult branching discs.
