"""Run from outside the checkout after installing the built wheel."""
from importlib import import_module
from pathlib import Path
import sys

app = sys.argv[1]
paths = import_module(app + ".paths")
required = ["README.md", "assets/" + ("BD2HEVC.ico" if app == "bd2hevc_app" else "DVD2HEVC.ico")]
required += (["tools/hadris-udf/streaming-src/hadris-udf-cli-2.2.0/Cargo.toml"] if app == "bd2hevc_app" else ["tools/run-phase7-general-disc.ps1", "native/build-native.ps1", "patches/vlc/0001-dvdnav-accept-hevc-program-stream-maps.patch"])
if app == 'dvd2hevc_app':
    required += ['tools/test-stock-bluray.py','tools/hadris-udf/BUILD-PROVENANCE.json',
                 'tools/hadris-udf/streaming-src/hadris-udf-cli-2.2.0/Cargo.toml']
    import dvd2uhd
    engine=Path(dvd2uhd.__file__).parent
    assert len(list((engine/'java/org/dvd2uhd').glob('*.java'))) == 9
    assert (engine/'NOTICE').is_file()
    from dvd2hevc_app.cli import build_parser
    from dvd2hevc_app.frontend import resolve_conversion_settings
    assert resolve_conversion_settings(build_parser().parse_args(['auto','example.iso']))['output_format']=='uhd-bd'
    if sys.platform == 'win32': required += ['tools/hadris-udf/bin/hadris-udf.exe']
for relative in required:
    assert (paths.ROOT / relative).is_file(), relative
assert not paths.STATE_ROOT.resolve().is_relative_to(paths.ROOT.resolve()), "Writable state points into installed resources"
if app == "bd2hevc_app":
    from bd2hevc_app.output import TITLE_ACRONYMS
    assert TITLE_ACRONYMS, "Naming data missing"
print("Installed resources and per-user state verified:", app)
