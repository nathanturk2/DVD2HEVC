# Tool source and build inventory

## Native DVD inspector

- Upstream: [libdvdread](https://code.videolan.org/videolan/libdvdread), pinned revision `980143e2192aded576c54a60dd230fa22937b637` (7.1.0).
- Patch: `patches/libdvdread/0001-modern-msvc-stdio-and-close.patch`.
- First-party helper source: `native/dvdinspect/main.c`, `dlfcn_win32.c`.
- Build: `python dvd2hevc.py build-inspector`, using Git, Python Meson/Ninja and Visual Studio C++ Build Tools.
- Build flags: static libdvdread, libdvdcss disabled; helper `/W4 /O2 /MD`, Windows x64.
- Runtime provenance: per-user `tools/DVD2HEVC-NATIVE-BUILD.json` records pinned revision, source/patch hashes and executable hash.
- Licensing: upstream GPL-2.0-or-later, with LGPL utility files as noted by upstream; no upstream library/helper binary ships in the source release. See `THIRD_PARTY_NOTICES.md`.

The helper was built from a fresh pinned checkout on 30 September 2026 and used for the generated-disc integration run. CI repeats the native build from its documented inputs.

## Optional legacy private player

- Upstream: [VLC](https://code.videolan.org/videolan/vlc), 3.0.23, revision `578d28f6c9f2379164516e689418f92ac74a3445`.
- Patch: `patches/vlc/0001-dvdnav-accept-hevc-program-stream-maps.patch`.
- Build: prepared MSYS2/MINGW64 Windows contrib/build tree; `tools/prepare-vlc-dvdhevc.ps1` copies the pinned runtime and rebuilds the DVDNAV plugin. See [VLC_BUILD.md](VLC_BUILD.md).
- Provenance: `DVD2HEVC-VLC-BUILD.json` binds the upstream revision, patch SHA-256 and resulting plugin SHA-256. Runtime discovery verifies the manifest and expected plugin markers, or a known tested plugin hash.
- Licensing: VLC and its dependencies retain their upstream licences. The source release contains the patch and instructions, not a player binary.

The generated DVD test used an existing verified private player. A fresh complete VLC/contrib build was not performed in this preparation. The dedicated `player-release.yml` gate requires a prepared clean pinned source/build environment and records a rebuilt plugin's manifest; run that gate before claiming a fresh player build for a distributed binary.

## Default UHD-BD author and stock player

DVD2HEVC 0.2.0a1 integrates the GPL-3.0 DVD-to-BD-J author as `dvd2uhd`, including
its nine Java source files and upstream VM attribution in `dvd2uhd/NOTICE`.
The external stock VLC/libbluray BD-J API JAR supplies the compile-time API;
the player is unmodified. A Java 11 JDK and tsMuxeR supporting `--blu-ray-v3`
are external requirements. The new conversion and upgrade tests on 5 October
2026 used stock VLC 3.0.23/libbluray 1.4.0 and Java 11.0.29.

The included Windows x64 Hadris streaming author is the same MIT fork used by
BD2HEVC, `2.2.0-bd2hevc.5`. Its complete source, license, pinned Rust toolchain
and source/binary inventory are in `tools/hadris-udf`.
[BUILD-PROVENANCE.json](../tools/hadris-udf/BUILD-PROVENANCE.json) records the
upstream pin and exact SHA-256 values. `tools/check-provenance.py` verifies
that inventory as well as the native inspector and optional player pins.

## Release artifacts

[TOOL-PROVENANCE.json](TOOL-PROVENANCE.json) records source pins and patch hashes. `python tools/check-provenance.py` verifies agreement with the build/runtime scripts. FFmpeg/FFprobe, HandBrakeCLI, ISO authoring tools, MSYS2, Git and compiler distributions are external; record their versions with release playback evidence.

`tools/build-release.py` exports a positive source manifest with its Git revision and builds ZIP/wheel/sdist from that export. `tools/check-artifacts.py` verifies the ZIP, exported tests, Windows launcher and a fresh wheel install outside the checkout. The resulting receipt is `dist/artifact-check.json`.

The bundled UDF author was rebuilt on 5 October 2026 with local compiler paths
remapped to `/build`; its source and behavior are unchanged. Exact flags and
updated executable hashes are recorded in the bundled provenance manifest.
