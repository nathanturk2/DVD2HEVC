from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dvd2hevc_app import frontend
from dvd2hevc_app.gui import FIELD_HELP
from dvd2hevc_app.pipeline import PipelineError
from dvd2hevc_app.vlc_setup import (
    EXPECTED_VLC_COMMIT,
    EXPECTED_VLC_VERSION,
    MANIFEST_NAME,
    PATCH_PATH,
    PLUGIN_MARKERS,
    PLUGIN_RELATIVE,
    find_ready_vlc_root,
    inspect_private_vlc,
    prepare_private_vlc,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


class VlcSetupTests(unittest.TestCase):
    def _make_verified_player(self, root: Path) -> Path:
        root.mkdir(parents=True)
        (root / "vlc.exe").write_bytes(b"portable VLC " + EXPECTED_VLC_VERSION.encode("ascii"))
        plugin = root / PLUGIN_RELATIVE
        plugin.parent.mkdir(parents=True)
        plugin.write_bytes(b"\0".join(PLUGIN_MARKERS))
        manifest = {
            "schema": "dvd2hevc-vlc-build-v1",
            "vlc_version": EXPECTED_VLC_VERSION,
            "vlc_source_commit": EXPECTED_VLC_COMMIT,
            "patch_sha256": _sha256(PATCH_PATH),
            "plugin_sha256": _sha256(plugin),
        }
        (root / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
        return root

    def test_matching_manifest_and_patch_markers_are_required(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            player = self._make_verified_player(Path(temporary) / "player")
            status = inspect_private_vlc(player)
            self.assertTrue(status["ready"])
            self.assertEqual(status["verified_by"], "manifest")

            (player / PLUGIN_RELATIVE).write_bytes(b"ordinary dvdnav plugin")
            broken = inspect_private_vlc(player)
            self.assertFalse(broken["ready"])
            self.assertTrue(any("playback changes" in reason for reason in broken["reasons"]))

    def test_repeated_setup_is_a_verified_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            player = self._make_verified_player(Path(temporary) / "player")
            before = _sha256(player / PLUGIN_RELATIVE)
            with patch("dvd2hevc_app.vlc_setup.subprocess.run") as run:
                result = prepare_private_vlc("unused-source", "unused-base", player)
            run.assert_not_called()
            self.assertFalse(result["changed"])
            self.assertIn("already ready", result["message"])
            self.assertEqual(before, _sha256(player / PLUGIN_RELATIVE))

    def test_unrecognized_destination_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "some-existing-folder"
            destination.mkdir()
            (destination / "personal-file.txt").write_text("keep", encoding="utf-8")
            with self.assertRaises(PipelineError) as raised:
                prepare_private_vlc("unused-source", "unused-base", destination)
            self.assertIn("Refusing to overwrite", str(raised.exception))
            self.assertEqual((destination / "personal-file.txt").read_text(encoding="utf-8"), "keep")

    def test_discovery_and_frontend_playback_use_strict_verifier(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            player = self._make_verified_player(Path(temporary) / "player")
            with patch("dvd2hevc_app.vlc_setup.private_vlc_candidates", return_value=[player]):
                self.assertEqual(find_ready_vlc_root(), player)
            with patch("dvd2hevc_app.frontend.find_ready_vlc_root", return_value=player) as find:
                self.assertEqual(frontend.find_patched_vlc_root(), player)
                find.assert_called_once_with(None)

    def test_frontend_playback_uses_conservative_disc_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            player = root / "player"
            player.mkdir()
            (player / "vlc.exe").write_bytes(b"test")
            iso = root / "Seek Heavy (DVD) (HEVC).iso"
            iso.write_bytes(b"test")
            args = argparse.Namespace(target=str(iso), vlc_root=None)
            with (
                patch("dvd2hevc_app.frontend.find_patched_vlc_root", return_value=player),
                patch("dvd2hevc_app.frontend.subprocess.Popen") as popen,
            ):
                self.assertEqual(frontend.cmd_play(args), 0)
            command = popen.call_args.args[0]
            self.assertIn("--disc-caching=1000", command)
            self.assertTrue(command[-1].startswith("dvd:///"))
            self.assertEqual(popen.call_args.kwargs["cwd"], str(player))

    def test_gui_help_covers_core_settings_and_every_button_uses_helper(self) -> None:
        for label in (
            "Video mode", "Target multiplier", "Bitrate control", "HEVC encoder", "Deinterlace",
            "General audio", "Audio workers", "Pipeline depth",
        ):
            self.assertIn(label, FIELD_HELP)
            self.assertGreater(len(FIELD_HELP[label]), 30)
        gui_source = (Path(__file__).parents[1] / "dvd2hevc_app" / "gui.py").read_text(encoding="utf-8")
        self.assertEqual(gui_source.count("ttk.Button("), 1)
        self.assertIn('bind_all("<F1>"', gui_source)
        self.assertIn("Verify / set up HEVC VLC", gui_source)


if __name__ == "__main__":
    unittest.main()
