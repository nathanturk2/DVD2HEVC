from __future__ import annotations

import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from dvd2hevc_app.compat_registry import (
    classify_outcome,
    record_job_outcome,
)
from dvd2hevc_app.compact import prototype_compact_domain_batch
from dvd2hevc_app.contracts import (
    FORMAT_PROFILE,
    PLAYER_ABI,
    capability_contract,
    contract_compatible,
)
from dvd2hevc_app.frontend import _compact_domain_fraction, managed_job_work_root


class ReleaseHardeningTests(unittest.TestCase):
    def test_capability_contract_names_format_and_player_abi(self) -> None:
        contract = capability_contract()
        self.assertEqual(contract["format_profile"], FORMAT_PROFILE)
        self.assertEqual(contract["player_abi"], PLAYER_ABI)
        self.assertTrue(contract["compaction"]["vmg_menu_vob"])
        self.assertTrue(contract_compatible(contract))
        self.assertFalse(contract_compatible({**contract, "player_abi": "other-player"}))

    def test_outcome_classification_distinguishes_media_runtime_and_converter(self) -> None:
        self.assertEqual(
            classify_outcome({"status": "failed", "error": "scrambled video packets"})["verdict"],
            "source-invalid",
        )
        self.assertEqual(
            classify_outcome({"status": "failed", "error": "access is denied"})["verdict"],
            "environment-interrupted",
        )
        self.assertEqual(
            classify_outcome({"status": "failed", "error": "decode gate failed"})["verdict"],
            "conversion-failed",
        )
        self.assertEqual(
            classify_outcome({"status": "passed", "warnings": ["manual menu check recommended"]})["verdict"],
            "passed-with-warnings",
        )

    def test_registry_retains_sampled_identity_after_source_is_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "renamable-source.iso"
            output = root / "converted.iso"
            source.write_bytes(b"source-disc" * 200)
            output.write_bytes(b"output-disc" * 200)
            work = root / "work"
            (work / "final-output-reports").mkdir(parents=True)
            (work / "status.json").write_text(
                json.dumps({"schema": "status", "state": "passed"}), encoding="utf-8"
            )
            job_path = root / "job" / "job.json"
            job_path.parent.mkdir()
            job = {
                "id": "fixture-job",
                "status": "passed",
                "version": "test",
                "source": str(source),
                "output": str(output),
                "work_root": str(work),
                "settings": {"quality": "target-bitrate"},
                "plan_summary": {"title_sets": 2},
                "started_at": "2026-07-19T10:00:00+12:00",
                "ended_at": "2026-07-19T10:30:00+12:00",
            }
            registry = root / "registry.json"
            first = record_job_outcome(job_path, job, registry_path=registry)
            retained = first["disc"]["source_fingerprint"]["sample_sha256"]
            source.unlink()
            record_job_outcome(job_path, job, registry_path=registry)
            rebuilt = json.loads(registry.read_text(encoding="utf-8"))
            fingerprint = rebuilt["records"]["fixture-job"]["disc"]["source_fingerprint"]
            self.assertEqual(fingerprint["sample_sha256"], retained)
            self.assertEqual(
                rebuilt["records"]["fixture-job"]["duration_seconds"], 1800.0
            )

    def test_compact_domain_batch_reuses_one_loaded_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layout = root / "layout.json"
            layout.write_text(
                json.dumps({
                    "status": "planned",
                    "domains": [
                        {"domain": "title", "vts": 1, "compact_sectors": 10},
                        {"domain": "vts_menu", "vts": 1, "compact_sectors": 2},
                    ],
                }),
                encoding="utf-8",
            )
            tasks = [
                {"domain": "title", "vts": 1, "destination": str(root / "one.vob")},
                {"domain": "vts_menu", "vts": 1, "destination": str(root / "menu.vob")},
            ]
            report = root / "batch.json"
            with patch(
                "dvd2hevc_app.compact.prototype_compact_domain",
                side_effect=[
                    {"summary": {"compact_sectors": 10}},
                    {"summary": {"compact_sectors": 2}},
                ],
            ) as compact, patch("dvd2hevc_app.compact.progress_event") as progress:
                result = prototype_compact_domain_batch(layout, tasks, report)
            self.assertEqual(compact.call_count, 2)
            first_layout = compact.call_args_list[0].kwargs["_loaded_layout"]
            second_layout = compact.call_args_list[1].kwargs["_loaded_layout"]
            self.assertIs(first_layout, second_layout)
            self.assertEqual(result["summary"]["layout_reads"], 1)
            self.assertEqual(result["summary"]["compact_sectors"], 12)
            self.assertTrue(report.is_file())
            measurements = [
                (call.kwargs["current"], call.kwargs["total"])
                for call in progress.call_args_list
                if call.args[2] == "compact-domain-batch"
            ]
            self.assertEqual(measurements, [(0, 12), (10, 12), (12, 12), (12, 12)])

    def test_compact_progress_bridges_batch_and_staging_without_decreasing(self) -> None:
        events = [
            {"lane": "mux", "task": "compact-domain-batch", "current": 1, "total": 2},
            {"lane": "mux", "task": "compact-domain-batch", "current": 2, "total": 2},
            {"lane": "mux", "task": "compact-domains", "current": 75, "total": 100},
            {"lane": "mux", "task": "compact-domains", "current": 90, "total": 100},
        ]
        fractions = [_compact_domain_fraction(events[:index]) for index in range(1, 5)]
        self.assertEqual(fractions, [0.375, 0.75, 0.75, 0.9])

    def test_managed_workspace_is_short_and_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job_id = "20260719-200753-An Extremely Long Disc Name That Must Not Reach Cell Paths"
            with patch("dvd2hevc_app.frontend.managed_work_base", return_value=root / "work"):
                first = managed_job_work_root(job_id)
                second = managed_job_work_root(job_id)
            self.assertEqual(first, second)
            self.assertEqual(first.parent, root / "work")
            self.assertLessEqual(len(first.name), 28)
            self.assertNotIn("Extremely", first.name)

    def test_shortcut_installer_rebuilds_reproducible_launcher(self) -> None:
        script = (
            Path(__file__).resolve().parents[1] / "tools" / "install-gui-shortcut.ps1"
        ).read_text(encoding="utf-8")
        builder = (
            Path(__file__).resolve().parents[1] / "tools" / "build-gui-launcher.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("build-gui-launcher.ps1", script)
        self.assertIn("DVD2HEVCLauncher.cs", builder)
        self.assertIn("target:winexe", builder)
        self.assertIn("csc.exe", builder)
        self.assertIn("win32icon", builder)

    def test_authoritative_capability_document_matches_code_contract(self) -> None:
        root = Path(__file__).resolve().parents[1]
        capabilities = (root / "docs" / "CAPABILITIES.md").read_text(encoding="utf-8")
        supported = (root / "docs" / "SUPPORTED.md").read_text(encoding="utf-8")
        phase8 = (root / "docs" / "PHASE8.md").read_text(encoding="utf-8")
        self.assertIn(FORMAT_PROFILE, capabilities)
        self.assertIn(PLAYER_ABI, capabilities)
        self.assertIn("VTS and VMG menu VOBs participate", supported)
        self.assertIn("VTS-menu and VMG-menu", phase8)
        self.assertNotIn("VMG menu remains\nsector-preserved", supported)


if __name__ == "__main__":
    unittest.main()
