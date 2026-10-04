> UHD-BD is the current default and uses stock VLC with Java; no VLC patch is needed. This document describes the legacy HEVC DVD output or the intermediate DVD encoding stage. See [UHD_BD.md](UHD_BD.md) and the [current README](../README.md).

# Preparing the patched VLC player

DVD2HEVC pins VLC `3.0.23`, source commit
`578d28f6c9f2379164516e689418f92ac74a3445`. Do not replace the system VLC
installation. Build a private copy so normal VLC remains untouched.

## GUI setup

Open **Presets & tools** and choose **Verify / set up HEVC VLC**. The button is
safe to use repeatedly:

- A verified player produces an "already ready" result and changes no files.
- A first-time setup automatically looks for VLC 3.0.23 and a prepared pinned
  source checkout, asking for either folder only when it cannot identify one.
- The GUI shows all three paths and asks for confirmation before a build.
- Copying, plugin building, and manifest creation happen in a sibling staging
  directory. Only a completely built player replaces an existing managed copy;
  a failure leaves that copy and the normal VLC installation untouched.
- An existing destination without a DVD2HEVC build manifest is refused rather
  than guessed at or overwritten.

The GUI cannot manufacture VLC's Windows build dependencies or source checkout,
so the requirements below still apply on a new machine. The source release does
not bundle a VLC binary.

## Requirements

- A VLC 3.0.23 Windows binary distribution to use as the private runtime base.
- A VLC source checkout at the pinned commit.
- The VLC Windows build dependencies prepared through its MSYS2/MINGW64 build
  workflow. The checked-in helper currently expects MSYS2 under `C:\msys64`.
- Git and PowerShell.

Configure/build the pinned VLC tree far enough that `win64/modules` exists.
The [official Windows build instructions](https://github.com/videolan/vlc/blob/master/doc/BUILD-win32.md)
explain the toolchain and dependency preparation. Use the pinned 3.0.23 source
for this project; current upstream instructions can describe newer versions.
Then run:

```powershell
powershell -ExecutionPolicy Bypass -File tools\prepare-vlc-dvdhevc.ps1 `
  -SourcePath C:\src\vlc-3.0.23 `
  -BaseVlcRoot C:\portable\vlc-3.0.23 `
  -Destination .\vlc-dvdhevc
```

The script verifies the exact commit and VLC version, rejects unrelated tracked
source changes, applies
`patches/vlc/0001-dvdnav-accept-hevc-program-stream-maps.patch`, rebuilds the
DVDNAV access plugin, stages a private distribution, and writes
`DVD2HEVC-VLC-BUILD.json` with SHA-256 values. It swaps the staged directory
into place only after those steps succeed.

For release verification, the manual `player-release` workflow runs this process
on a dedicated Windows build runner with the prepared pinned source and stock
runtime base. It forces a DVDNAV plugin rebuild and records the player hashes.
Run the generated-disc and private playback checks against that new player
before distributing it. The source release can be published independently.

Check discovery with:

```powershell
$env:DVD2HEVC_VLC_ROOT = "$PWD\vlc-dvdhevc"
python dvd2hevc.py tools
```

If you distribute the private VLC binary, comply with VLC and bundled-library
licenses: include the license texts and make the complete corresponding source,
including the DVD2HEVC patch and build instructions, available to recipients.
The DVD2HEVC source archive intentionally does not bundle VLC binaries.
