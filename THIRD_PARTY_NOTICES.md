# Third-party components

DVD2HEVC source is licensed under GPL-3.0-only. It interoperates with, builds
against, or invokes third-party projects that retain their own licenses.

- **libdvdread**: the native `dvdinspect` helper is built from a pinned upstream
  commit with the checked-in Windows compatibility patch. The relevant source
  files are GPL version 2 or later (some utility files use LGPL 2.1 or later).
  The release source does not include the upstream library or a prebuilt helper.
- **VLC media player**: the optional player patch targets VLC 3.0.23. VLC is
  distributed under GPL version 2 or later, with libraries under LGPL and other
  compatible licenses. DVD2HEVC does not include VLC binaries in its source
  package. Binary redistributors must provide the applicable licenses and
  complete corresponding source.
- **pycdlib**: Python ISO/UDF inspection dependency, LGPLv2 according to its
  installed package metadata.
- **FFmpeg, FFprobe, HandBrakeCLI, genisoimage/WSL tooling, Git, CMake, Meson,
  and MSYS2**: discovered or invoked as external programs and not included in
  the DVD2HEVC source archive. Their exact licenses and enabled components
  depend on the user's chosen distributions.
- **libdvdcss**: not bundled. DVD2HEVC accepts already-decrypted backups and does
  not distribute keys or decryption material.

This notice is an engineering inventory, not legal advice. Before distributing
a binary bundle, review the exact binaries and their corresponding licenses.

## Integrated DVD-to-BD-J author

The included `dvd2uhd` author is GPL-3.0-only. Its DVD VM adapts libdvdnav
`src/vm/decoder.c` (GPL-2.0-or-later, used under GPL v3), originally from Ogle:
Copyright 2000–2001 Martin Norbäck and Håkan Hjort; 2002–2004 the dvdnav project.
See `dvd2uhd/NOTICE`. The read-only UDF reader and descriptor validator are
adapted from BD2HEVC (GPL-3.0-only). The BD-J API is supplied by external VLC/
libbluray (LGPL-2.1-or-later) and is not bundled.

Clear PGS and MPLS parsing were independently implemented using the public
FFmpeg PGS decoder and libbluray MPLS parser as format references; no extra
source was copied. Pillow (HPND) and pycdlib (LGPL-2.1) are Python dependencies.

The Windows Hadris UDF executable and complete streaming fork source are in
`tools/hadris-udf`, with MIT notice, pinned Rust inputs and BUILD-PROVENANCE.json.
FFmpeg, tsMuxeR, VLC and Java binaries are installed separately. No film, menu,
subtitle, capture or private patched-player binaries are release assets.

The bundled Hadris author also carries the resolved Rust dependency licence
inventory and original licence/copyright texts in
[DEPENDENCY-LICENSES.json](tools/hadris-udf/DEPENDENCY-LICENSES.json) and
`tools/hadris-udf/dependency-licenses/`. Optional, development and other-platform
packages are included in that inventory; it is broader than the Windows binary.
