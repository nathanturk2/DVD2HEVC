from __future__ import annotations

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Phase7ToolScriptTests(unittest.TestCase):
    def test_general_driver_derives_domains_and_runs_automatic_gates(self) -> None:
        script = (PROJECT_ROOT / "tools" / "run-phase7-general-disc.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn('$blockedPhysicalVts = @($planValue.title_tasks', script)
        self.assertIn('$unreferencedPhysicalVts = @($planValue.vts', script)
        self.assertIn('[int]$_.physical_cells -gt [int]$_.unique_referenced_cells', script)
        self.assertIn('$physicalGroupingCandidates = @(', script)
        self.assertIn('$planValue.vts | ForEach-Object { [int]$_.vts }', script)
        self.assertIn('$mandatoryPhysicalVts -contains $vts', script)
        self.assertIn('retaining the verified per-title fallback', script)
        self.assertIn('"dvd2hevc-grouped-vts-title-provenance-v1"', script)
        self.assertIn('scheduler = "physical-vts-grouped-v1"', script)
        self.assertIn('"plan-interleaved", $script:sourcePath', script)
        self.assertIn('"stage-disc", $sectorStage', script)
        self.assertIn('$titleDomains.Count -ne $expectedVts.Count', script)
        self.assertIn('Assert-FullClearScan', script)
        self.assertIn('"audit-css-safe-psm" $outputPath', script)
        self.assertIn('"verify-css-safe-psm", $outputPath, $repairTemporary', script)
        self.assertIn('$finalReportRoot = Join-Path $workPath "final-output-reports"', script)
        self.assertIn('"--report", $authorReport, "--log", $authorLog', script)
        self.assertIn('"--output-report", $verificationReport', script)
        self.assertNotIn('$authorReport = "$outputPath.json"', script)
        self.assertNotIn('$verificationReport = "$outputPath.verification.json"', script)
        self.assertNotIn('$cssAuditReport = "$outputPath.css-safe-audit.json"', script)
        self.assertIn('"title-hardware"', script)
        self.assertIn('"menu-hardware"', script)
        self.assertIn('$vlcAttemptLimit = if ($check.Name -eq "menu-hardware") { 2 } else { 1 }', script)
        self.assertIn('"$($check.Name)-attempt-$vlcAttempt.log"', script)
        self.assertIn('function Get-Dvd2HevcFileSha256', script)
        self.assertNotIn('Get-FileHash', script)
        self.assertIn('[string]$Cadence = "auto"', script)
        self.assertIn('[string]$AmbiguousCadence = "deinterlace50"', script)
        self.assertIn('[string]$Encoder = "hevc_nvenc"', script)
        self.assertIn('"--encoder", $Encoder', script)
        self.assertIn('"--prefer-compact-input", "--pipeline-depth", [string]$PipelineDepth', script)
        self.assertIn('"--audio-prefetch-root", $audioRoot', script)
        self.assertIn('intermediate_policy -eq "compact-input-v1"', script)
        self.assertIn('compactfirst1', script)
        self.assertIn('physicalgroup1', script)
        self.assertIn('"-Encoder", $Encoder', script)
        self.assertIn('"-Cadence", $Cadence', script)
        self.assertIn('"-AmbiguousCadence", $AmbiguousCadence', script)
        self.assertIn('[string]$AudioMode = "passthrough"', script)
        self.assertIn('"prototype-compact-audio-batch"', script)
        self.assertIn('Start-Job -Name "dvd2hevc-compact-audio"', script)
        self.assertIn('$script:audioJob | Receive-Job -Wait', script)
        self.assertIn('"plan-compact-audio", $layoutPath', script)
        self.assertIn('Write-ProgressEvent -Lane "audio"', script)
        self.assertIn('function Test-LayoutArtifact', script)
        self.assertIn('function Test-PassedVmgReports', script)
        self.assertIn('layout_sha256', script)
        self.assertIn('"compact-domains"', script)
        self.assertIn('$vtsMenuReports = @($menuReports | Where-Object', script)
        self.assertIn('$vmgMenuReports = @($menuReports | Where-Object', script)
        self.assertIn('"prototype-compact-domain", $layoutPath, "vts_menu"', script)
        self.assertIn('"prototype-compact-domain", $layoutPath, "vmg_menu"', script)
        self.assertIn('"rewrite-compact-vmgi", $layoutPath', script)
        self.assertIn('"stage-compact-vmg", $sectorStage, $layoutPath', script)
        self.assertIn('"--compact-menu-vob", $compactMenuVob', script)
        self.assertIn('menucompact1', script)
        self.assertIn('vmgcompact1', script)
        self.assertIn('$compactDomainTotalSectors', script)
        self.assertIn('$compactDomainWeights[$vts]', script)
        self.assertIn('[string]$_.domain -eq "vts_menu"', script)
        self.assertIn('"prototype-compact-domain-batch"', script)
        self.assertIn('unit = "weighted-sectors"', script)
        self.assertIn('. (Join-Path $PSScriptRoot "write-log.ps1")', script)
        self.assertIn('$ErrorActionPreference = "Continue"', script)
        self.assertNotIn("Add-Content -LiteralPath $logPath", script)
        self.assertNotIn('Expected four ordinary VTS reports', script)
        self.assertNotIn('Expected compact title domains VTS 1-5', script)

    def test_remaining_domain_driver_has_no_disc_specific_default_skip(self) -> None:
        script = (
            PROJECT_ROOT / "tools" / "run-phase7-remaining-domains.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn('[int[]]$SkipVts = @()', script)
        self.assertIn('[string]$SkipVtsCsv = ""', script)
        self.assertIn('$effectiveSkipVts', script)
        self.assertIn('"--encoder", $Encoder', script)
        self.assertIn('encoder = $Encoder', script)
        self.assertIn('$menuArguments += "--allow-compact-expansion"', script)
        self.assertIn('"--allow-compact-expansion", "--prefer-compact-input"', script)
        self.assertIn('$nonCompactManagedTitle', script)
        self.assertIn('"--audio-prefetch-root", $AudioPrefetchRoot', script)
        self.assertIn('$audioProcessingRequired', script)
        self.assertIn('[bool]$_.processing_required', script)
        self.assertNotIn('$unsupportedVmgExpansion', script)
        self.assertIn('. (Join-Path $PSScriptRoot "write-log.ps1")', script)
        self.assertIn('$ErrorActionPreference = "Continue"', script)
        self.assertNotIn("Add-Content -LiteralPath $logPath", script)
        # PowerShell variables are case-insensitive: the parsed document must
        # not reuse the typed string parameter name `$QualityPolicy`.
        self.assertIn('$script:qualityPolicyDocument = if ($QualityPolicy)', script)
        self.assertIn('$script:qualityPolicyDocument.quality_by_vts', script)
        self.assertNotIn('$script:qualityPolicy = if ($QualityPolicy)', script)
        self.assertNotIn('[int[]]$SkipVts = @(5)', script)

    def test_shared_log_writer_allows_gui_reads_and_retries_exclusive_locks(self) -> None:
        script = (PROJECT_ROOT / "tools" / "write-log.ps1").read_text(encoding="utf-8")
        self.assertIn("[IO.FileShare]::ReadWrite", script)
        self.assertIn("catch [IO.IOException]", script)
        self.assertIn("Start-Sleep -Milliseconds", script)
        self.assertIn("function Move-Dvd2HevcAtomicFile", script)
        self.assertIn("[IO.File]::Replace", script)

        status_runners = (
            "finish-phase6-compact-auto.ps1",
            "run-phase6-compact-auto.ps1",
            "run-phase7-compact-vts-test.ps1",
            "run-phase7-full-disc.ps1",
            "run-phase7-general-disc.ps1",
            "run-phase7-remaining-domains.ps1",
            "run-phase7-title-gate.ps1",
        )
        for name in status_runners:
            with self.subTest(script=name):
                runner = (PROJECT_ROOT / "tools" / name).read_text(encoding="utf-8")
                self.assertIn('. (Join-Path $PSScriptRoot "write-log.ps1")', runner)
                self.assertIn("Move-Dvd2HevcAtomicFile", runner)
                self.assertNotIn("Move-Item -LiteralPath $temporary", runner)

    def test_first_grouped_job_monitor_has_distinct_start_and_terminal_waits(self) -> None:
        script = (
            PROJECT_ROOT / "tools" / "monitor-first-grouped-job.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn('state = "waiting-for-grouped-job-start"', script)
        self.assertIn(
            '[string]$status.title_scheduler -eq "physical-vts-grouped-v1"',
            script,
        )
        self.assertIn('state = "grouped-job-started"', script)
        self.assertIn(
            'state = "waiting-for-grouped-job-terminal-state"', script
        )
        self.assertIn('$terminalStates -contains [string]$selectedJob.status', script)
        self.assertIn('"random-access-and-full-decode-per-physical-cell"', script)
        self.assertIn('"encode-validation-overlap-v1"', script)
        self.assertIn('state = if ($verified)', script)

    def test_full_disc_status_label_cannot_replace_compact_stage_path(self) -> None:
        script = (PROJECT_ROOT / "tools" / "run-phase7-full-disc.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn("$script:currentStage = $Stage", script)
        self.assertIn("$currentCompactStage = $sectorStage", script)
        self.assertIn(
            '"stage-compact-vts", $currentCompactStage, $layoutPath', script
        )
        self.assertNotIn("$currentStage = $sectorStage", script)

    def test_full_disc_driver_audits_and_repairs_css_ambiguous_psms(self) -> None:
        script = (PROJECT_ROOT / "tools" / "run-phase7-full-disc.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn('"audit-css-safe-psm" $outputPath', script)
        self.assertIn('"repair-css-safe-psm", $outputPath, $repairTemporary', script)
        self.assertIn('Require-Report $cssAuditReport "passed" "True"', script)

        compact_script = (
            PROJECT_ROOT / "tools" / "run-phase7-compact-vts-test.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn('"audit-css-safe-psm" $outputPath', compact_script)
        self.assertIn('"repair-css-safe-psm", $outputPath, $repairTemporary', compact_script)


if __name__ == "__main__":
    unittest.main()
