"""Verify that build/patch pins agree across the runtime and build helpers."""
from pathlib import Path
import hashlib
import json
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dvd2hevc_app.vlc_setup import EXPECTED_VLC_COMMIT

ROOT = Path(__file__).resolve().parents[1]
manifest = json.loads((ROOT / "docs/TOOL-PROVENANCE.json").read_text())
for entry in manifest["patches"]:
    assert hashlib.sha256((ROOT / entry["path"]).read_bytes()).hexdigest() == entry["sha256"], entry["path"]
assert manifest["vlc_commit"] == EXPECTED_VLC_COMMIT
assert manifest["vlc_commit"] in (ROOT / "tools/prepare-vlc-dvdhevc.ps1").read_text()
assert manifest["libdvdread_commit"] in (ROOT / "native/build-native.ps1").read_text()
print("Native library/player source pins and patch hashes verified")
directory = ROOT / 'tools/hadris-udf'
inventory=json.loads((directory/'BUILD-PROVENANCE.json').read_text())
for entry in inventory['files']:
    assert hashlib.sha256((directory/entry['path']).read_bytes()).hexdigest()==entry['sha256'],entry['path']
print('Bundled Hadris source/binary provenance verified')
