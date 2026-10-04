# DVD2HEVC 0.2.0a1 release checklist

The default is UHD-BD output for stock VLC with Java. Private VLC patching is
only for the explicit legacy `dvd-hevc` format. See [PUBLISHING.md](PUBLISHING.md).

- Review the new output option in Convert, presets, queue and watched folders.
  Verify the no-patch notice and generated UHD-BD filename preview.
- Verify measured physical-clip author/audit progress, payload-byte writer
  progress and ISO verification. Overall/mux bars must reserve 100% for success.
- Run the Python suite, Java self-tests, source/wheel artifact checks, shared
  module parity and native/Hadris provenance checks.
- Run a generated DVD through the complete new default without a patched
  player. Check BDMV/UDF payloads, the BD-J startup gate, chapters, seek/pause
  and final output protection/resume. Test legacy output separately when its
  path or player changes.
- Test a representative existing HEVC DVD through `author-uhd`. The 13-disc
  author corpus and the 117-image legacy catalogue must remain distinct in
  documentation; do not call all paths or whole films certified.
- Include Python/Java sources, assets, docs, runners, patches, inspector source,
  LICENSE/attribution and the complete Hadris fork/license/provenance. Include
  no commercial media, private reports, logs, screenshots of films, external
  VLC/FFmpeg/tsMuxeR/Java binaries or work directories.
- Install the built wheel outside the checkout. Verify bundled Java source,
  stock-player harness, native runner and Windows Hadris are present; compile
  the Java runtime against the public VLC API on a prepared test host.
- Review the clean source commit/public author identity, alpha version and
  artifact hashes before publishing. Do not create/reuse a tag prematurely.
- Publish the source ZIP and matching supported wheel/sdist only after review.
  The Reddit announcement stays a draft until the maintainer posts it.
