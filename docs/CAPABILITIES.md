# Capability matrix

DVD2HEVC 0.2.0a1 defaults to profile `dvd-vm-bdj-hevc-v1`, with public
stock-VLC/libbluray BD-J runtime model 6 (`stock-vlc-libbluray-bdj-v6`). The retained
legacy profile is `dvd-hevc-compact-v1` and requires the private DVDNAV HEVC
player ABI `dvd2hevc-vlc3-dvdnav-hevc-v1`.

| Area | Default UHD-BD | Legacy HEVC DVD |
| --- | --- | --- |
| Container | BDMV/M2TS, UDF 2.50 | Custom HEVC DVD program streams |
| Video | Shared compressed HEVC pictures, original DVD resolution | HEVC Main 8-bit, type 0x24 |
| DVD navigation | Java VM in BD-J | Patched DVDNAV |
| Menus/highlights | Original SPU masks, palettes, button geometry/commands | Original DVD navigation |
| Extras and editions | Original graph and shared physical clips | Original graph/physical cells |
| Subtitle artwork | BD-J rendering | Private player's DVD SPU rendering |
| Native subtitle menu | Logical selection through clear PGS control streams | Private DVD controls |
| Audio | Original logical maps, native/DVD synchronization | Original selection metadata |
| Chapters | DVD targets and native marks within compatible runs | Original DVD programs |
| UOP restrictions | BD-J controls plus native audio/subtitle bridge; other native controls incomplete | Private DVD player contract |
| Angles/random/shuffle | Rejected for now | Existing custom-format path |
| Player | Stock VLC with Java; no patch | Private patched VLC 3.0.23 |
| Hardware UHD conformance | Not certified | Not a hardware DVD format |

Both outputs use the existing source scan, encoder/random-access/decode and
compact-stage graph checks. UHD-BD adds independent complete media audit,
UDF/file payload verification and original-first-play BD-J/decoded-video startup.
The final image appears after these checks pass. New jobs record the selected
format/player contract; existing legacy job contracts remain recognized.

[UHD_BD.md](UHD_BD.md) describes the 13-disc corpus, precise audio audit scopes
and remaining presentation limits. Historical phase notes describe the custom
DVD stage or legacy output rather than changing the current default contract.
