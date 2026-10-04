# Phase 4: progressive NVENC and complete VTS 1

> Historical custom-DVD implementation notes. For the current default UHD-BD output and stock-VLC requirements, see [UHD_BD.md](UHD_BD.md).

Phase 4 establishes a scalable hardware path and validates every physical title
cell in The Intern VTS 1. The original MPEG-2 streams report top-field-first,
but pixel-based `idet` sampling showed the film content is progressive beneath
that signalling. Automatic mode therefore clears the false field flag and
encodes native progressive 25 fps HEVC; it does not deinterlace already-
progressive pictures.

For genuinely interlaced material, the hardware path uses motion-adaptive
`bwdif` output at progressive 50 fps. A synthetic 50-field motion source was
classified as 100% TFF, and the forced 50p path passed the two-cell title with
the expected doubled frame count, unchanged duration, and all VOBU checks.

## Hardware and quality result

The tested GPU is an NVIDIA GeForce RTX 3070 Laptop GPU. FFmpeg's NVENC HEVC
encoder exposes no interlaced mode, so both automatic paths deliberately produce
progressive output.

The 116.57-minute main feature completed with:

- 16/16 cells, 14,127 VOBUs, and 174,849 source/output frames;
- all cells classified as progressive 25p, with one very short cell inheriting
  its preceding confident classification;
- 14,125 VOBUs at CQ 20, one at CQ 22, and one at CQ 24;
- matching source/output audio hashes for every cell;
- zero invalid or scrambled sectors and complete HEVC decode for every cell.

The remaining VTS 1 extra/shared cells added 555 VOBUs and 6,892 frames. The
combined VTS gate matched libdvdread exactly: 20/20 physical cells and
14,682/14,682 title VOBUs, with shared content represented once.

NVENC spent 452.44 seconds encoding approximately 121 minutes of unique VTS 1
physical content, including adaptive retry candidates. The first visual sample
scored VMAF 97.23 and SSIM 0.99198 at NVENC CQ 20, compared with VMAF 96.59 and
SSIM 0.99080 for the earlier x265 CRF 20 proof. NVENC used more payload, but
still fit the original DVD allocation comfortably after per-VOBU retries.

## Staging and behavior result

The staged title domain retained its exact 6,864,805,888-byte size. All 20
replacement ranges matched their validated files by SHA-256. A full rescan found
14,682 HEVC program stream maps, zero invalid sectors, and zero scrambled PES.

Patched VLC passed:

- title-start playback for VTS 1 titles 1, 2, and 3;
- the complete title 3 path and its shared-cell jump;
- chapter probes inside early, middle, and late main-feature chapters, landing
  on authored cell/program pairs 2/2, 9/7, and 15/13;
- the complete 6,994.04-second main feature at 32x in 232.6 seconds;
- an exact complete source/output trace: 78 DVDNAV events and 247 associated
  fields in the same order.

The VLC harness now has an external wall-clock limit because VLC's internal
`--run-time` is not reliable when combined with a DVDNAV time seek.

## Resumption and resource behavior

Cell reports are keyed by source-edge hash, physical cell/VOBU graph, encoder,
cadence policy, preset, quality ladder, and pipeline revision. A restarted
hardware run reused eight passed cells without re-encoding them. Encoding remains
one cell at a time. With NVENC, structural scans and full-decode validation are
now a material part of elapsed time rather than HEVC encoding dominating the run.

## Remaining scope

Phase 4 covers the complete VTS 1 title domain, not menu video or the VMG title
menu. Phase 5 converts menu domains while preserving buttons, still behavior,
commands, audio, and subpictures, then builds the first complete sector-
preserving folder and UDF ISO. Sector-preserving output remains the same size;
Phase 6 adds the separate compact profile.
