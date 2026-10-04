[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Plan,
    [Parameter(Mandatory = $true)]
    [string]$BaseStage,
    [Parameter(Mandatory = $true)]
    [string]$InterleavedReport,
    [Parameter(Mandatory = $true)]
    [string]$WorkRoot,
    [Parameter(Mandatory = $true)]
    [string]$OutputIso,
    [string]$QualityValues = "cq:27",
    [string]$CompactQuality = "cq:27",
    [ValidateSet("hevc_nvenc", "hevc_qsv", "hevc_amf", "libx265")]
    [string]$Encoder = "hevc_nvenc",
    [string]$Preset = "p6",
    [string]$Label = "TAKEN_2_HEVC",
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$cli = Join-Path $projectRoot "dvd2hevc.py"
. (Join-Path $PSScriptRoot "write-log.ps1")
$remainingRunner = Join-Path $PSScriptRoot "run-phase7-remaining-domains.ps1"
$planPath = [IO.Path]::GetFullPath($Plan)
$baseStagePath = [IO.Path]::GetFullPath($BaseStage)
$interleavedPath = [IO.Path]::GetFullPath($InterleavedReport)
$workPath = [IO.Path]::GetFullPath($WorkRoot)
$outputPath = [IO.Path]::GetFullPath($OutputIso)
$remainingPath = Join-Path $workPath "remaining"
$statusPath = Join-Path $workPath "status.json"
$logPath = Join-Path $workPath "run.log"
$started = (Get-Date).ToString("o")
$script:currentStage = "initializing"
$script:message = ""
$script:finalStage = $null

New-Item -ItemType Directory -Path $workPath -Force | Out-Null
New-Item -ItemType Directory -Path (Split-Path -Parent $outputPath) -Force | Out-Null

function Write-Status {
    param([string]$State, [string]$Stage, [string]$Message)
    $script:currentStage = $Stage
    $script:message = $Message
    $value = [ordered]@{
        schema = "dvd2hevc-phase7-full-disc-status-v0"
        state = $State
        stage = $Stage
        message = $Message
        plan = $planPath
        base_stage = $baseStagePath
        interleaved_report = $interleavedPath
        work_root = $workPath
        remaining_work = $remainingPath
        final_stage = $script:finalStage
        output_iso = $outputPath
        encoder = $Encoder
        started = $started
        updated = (Get-Date).ToString("o")
        process_id = $PID
    }
    $temporary = "$statusPath.$PID.tmp"
    $value | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Dvd2HevcAtomicFile -TemporaryPath $temporary -DestinationPath $statusPath
}

function Test-JsonValue {
    param([string]$Path, [string]$Property, [string]$Value)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    try {
        $document = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
        return [string]$document.$Property -eq $Value
    }
    catch { return $false }
}

function Invoke-Dvd2Hevc {
    param([string]$Stage, [string[]]$Arguments)
    Write-Status -State "running" -Stage $Stage -Message ($Arguments -join " ")
    "[$((Get-Date).ToString('o'))] $Stage" | Tee-Object -FilePath $logPath -Append
    & $Python -u $cli @Arguments 2>&1 | Tee-Object -FilePath $logPath -Append
    if ($LASTEXITCODE -ne 0) {
        throw "$Stage failed with exit code $LASTEXITCODE"
    }
}

function Require-Report {
    param([string]$Path, [string]$Property, [string]$Value)
    if (-not (Test-JsonValue $Path $Property $Value)) {
        throw "Required report is absent or incomplete: $Path"
    }
}

try {
    "DVD2HEVC full Phase 7 build started $started" | Set-Content -LiteralPath $logPath -Encoding UTF8
    Require-Report (Join-Path $baseStagePath "dvd2hevc-stage-report.json") "status" "passed"
    Require-Report $interleavedPath "status" "passed"

    $interleavedValue = Get-Content -LiteralPath $interleavedPath -Raw | ConvertFrom-Json
    if ([string]$interleavedValue.settings.encoder -ne $Encoder -or
        [string]$interleavedValue.settings.keyframe_policy -ne "dvd-vobu-idr-v1" -or
        [string]$interleavedValue.settings.program_stream_map_policy -ne "dvd-vobu-psm-v1") {
        $interleavedPlan = [IO.Path]::GetFullPath([string]$interleavedValue.plan)
        $interleavedWork = Split-Path -Parent $interleavedPath
        Invoke-Dvd2Hevc -Stage "rebuild-interleaved-vts-random-access" -Arguments @(
            "convert-interleaved-vts", $interleavedPlan, $interleavedWork,
            "--quality-values", $QualityValues, "--encoder", $Encoder, "--preset", $Preset
        )
        Require-Report $interleavedPath "status" "passed"
        $interleavedValue = Get-Content -LiteralPath $interleavedPath -Raw | ConvertFrom-Json
        if ([string]$interleavedValue.settings.encoder -ne $Encoder -or
            [string]$interleavedValue.settings.keyframe_policy -ne "dvd-vobu-idr-v1" -or
            [string]$interleavedValue.settings.program_stream_map_policy -ne "dvd-vobu-psm-v1") {
            throw "Interleaved VTS rebuild did not produce the required VOBU random-access policy"
        }
    }

    # The report path is stable across resumable rebuilds, so an older staged
    # folder can point at a newly rewritten report while still containing the
    # old cell-entry bytes. Compare the actual staged PSM count with the new
    # report before accepting it as the base.
    $effectiveBaseStage = $baseStagePath
    $baseStageReport = Join-Path $baseStagePath "dvd2hevc-stage-report.json"
    $baseStageValue = Get-Content -LiteralPath $baseStageReport -Raw | ConvertFrom-Json
    $expectedInterleavedMaps = 0
    foreach ($cell in @($interleavedValue.cells)) {
        $expectedInterleavedMaps += [int]$cell.validation.structure.program_stream_maps
    }
    if ([int]$baseStageValue.summary.program_stream_maps -ne $expectedInterleavedMaps) {
        $randomAccessBase = Join-Path $workPath "vts5-random-access-sector-stage"
        $randomAccessBaseReport = Join-Path $randomAccessBase "dvd2hevc-stage-report.json"
        if (-not (Test-JsonValue $randomAccessBaseReport "status" "passed")) {
            Invoke-Dvd2Hevc -Stage "stage-vts5-random-access" -Arguments @(
                "stage-disc", $randomAccessBase, $interleavedPath
            )
        }
        Require-Report $randomAccessBaseReport "status" "passed"
        $randomAccessVerification = Join-Path $randomAccessBase "dvd2hevc-stage-verification.json"
        if (-not (Test-JsonValue $randomAccessVerification "passed" "True")) {
            Invoke-Dvd2Hevc -Stage "verify-vts5-random-access-stage" -Arguments @(
                "verify-stage", $randomAccessBaseReport
            )
        }
        $effectiveBaseStage = $randomAccessBase
    }

    $remainingStatus = Join-Path $remainingPath "status.json"
    if (-not (
        (Test-JsonValue $remainingStatus "state" "passed") -and
        (Test-JsonValue $remainingStatus "encoder" $Encoder)
    )) {
        Write-Status -State "running" -Stage "remaining-domains" -Message "Encoding remaining ordinary titles and menus"
        "[$((Get-Date).ToString('o'))] remaining-domains" | Tee-Object -FilePath $logPath -Append
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $remainingRunner `
            -Plan $planPath -WorkRoot $remainingPath -QualityValues $QualityValues `
            -Encoder $Encoder -Preset $Preset -Python $Python 2>&1 | Tee-Object -FilePath $logPath -Append
        if ($LASTEXITCODE -ne 0) {
            throw "Remaining-domain conversion failed with exit code $LASTEXITCODE"
        }
    }
    Require-Report $remainingStatus "state" "passed"
    Require-Report $remainingStatus "encoder" $Encoder

    $combinedReports = @(Get-ChildItem -LiteralPath (Join-Path $remainingPath "combined") `
        -Filter "vts*-report.json" -File | Sort-Object Name | ForEach-Object FullName)
    $menuReports = @(Get-ChildItem -LiteralPath (Join-Path $remainingPath "menu-reports") `
        -Filter "*-report.json" -File | Sort-Object Name | ForEach-Object FullName)
    if ($combinedReports.Count -ne 4 -or $menuReports.Count -ne 2) {
        throw "Expected four ordinary VTS reports and two menu reports; found $($combinedReports.Count) and $($menuReports.Count)"
    }

    $sectorStage = Join-Path $workPath "all-hevc-vobu-random-access-sector-stage"
    $sectorReport = Join-Path $sectorStage "dvd2hevc-stage-report.json"
    if (-not (Test-JsonValue $sectorReport "status" "passed")) {
        $arguments = @("extend-stage", $effectiveBaseStage, $sectorStage)
        $arguments += $combinedReports
        $arguments += $menuReports
        Invoke-Dvd2Hevc -Stage "extend-all-hevc-stage" -Arguments $arguments
    }
    Require-Report $sectorReport "status" "passed"
    $sectorVerification = Join-Path $sectorStage "dvd2hevc-stage-verification.json"
    if (-not (Test-JsonValue $sectorVerification "passed" "True")) {
        Invoke-Dvd2Hevc -Stage "verify-all-hevc-stage" -Arguments @("verify-stage", $sectorReport)
    }

    $compactRoot = Join-Path $workPath "compact"
    New-Item -ItemType Directory -Path $compactRoot -Force | Out-Null
    $layoutPath = Join-Path $compactRoot "layout.json"
    $titleReports = @($combinedReports) + @($interleavedPath)
    if (-not (Test-JsonValue $layoutPath "status" "planned")) {
        $arguments = @("plan-compact", $layoutPath)
        $arguments += $titleReports
        $arguments += @("--quality", $CompactQuality)
        Invoke-Dvd2Hevc -Stage "plan-full-disc-compaction" -Arguments $arguments
    }
    Require-Report $layoutPath "status" "planned"
    $layout = Get-Content -LiteralPath $layoutPath -Raw | ConvertFrom-Json
    $titleDomains = @($layout.domains | Where-Object { [string]$_.domain -eq "title" } | Sort-Object { [int]$_.vts })
    if ($titleDomains.Count -ne 5) {
        throw "Expected compact title domains VTS 1-5, found $($titleDomains.Count)"
    }

    # Keep the compact-stage path separate from $script:currentStage, which is
    # the human-readable status label updated by Write-Status.  PowerShell
    # script variables are dynamically visible to functions, so using the
    # unqualified $currentStage name here allowed status updates such as
    # "relocate-vts01-ifo" to overwrite this filesystem path.
    $currentCompactStage = $sectorStage
    foreach ($domain in $titleDomains) {
        $vts = [int]$domain.vts
        $vtsName = "vts{0:d2}" -f $vts
        $vtsRoot = Join-Path $compactRoot $vtsName
        New-Item -ItemType Directory -Path $vtsRoot -Force | Out-Null
        $compactVob = Join-Path $vtsRoot ("VTS_{0:d2}_TITLE_COMPACT.vob" -f $vts)
        $compactVobReport = "$compactVob.json"
        if (-not (Test-JsonValue $compactVobReport "status" "passed")) {
            Invoke-Dvd2Hevc -Stage "compact-$vtsName" -Arguments @(
                "prototype-compact-domain", $layoutPath, "title", [string]$vts, $compactVob
            )
        }
        Require-Report $compactVobReport "status" "passed"

        $compactIfo = Join-Path $vtsRoot ("VTS_{0:d2}_0.IFO" -f $vts)
        $compactBup = Join-Path $vtsRoot ("VTS_{0:d2}_0.BUP" -f $vts)
        $compactIfoReport = "$compactIfo.json"
        if (-not (Test-JsonValue $compactIfoReport "status" "passed")) {
            $sourceIfo = Join-Path (Join-Path $sectorStage "VIDEO_TS") ("VTS_{0:d2}_0.IFO" -f $vts)
            Invoke-Dvd2Hevc -Stage "relocate-$vtsName-ifo" -Arguments @(
                "rewrite-compact-vts-ifo", $layoutPath, [string]$vts, $sourceIfo,
                $compactIfo, "--destination-bup", $compactBup
            )
        }
        Require-Report $compactIfoReport "status" "passed"

        $nextStage = Join-Path (Join-Path $compactRoot "stages") ("through-$vtsName")
        $nextReport = Join-Path $nextStage "dvd2hevc-stage-report.json"
        if (-not (Test-JsonValue $nextReport "status" "passed")) {
            Invoke-Dvd2Hevc -Stage "stage-$vtsName" -Arguments @(
                "stage-compact-vts", $currentCompactStage, $layoutPath, [string]$vts,
                $compactVob, $compactIfo, $compactBup, $nextStage
            )
        }
        Require-Report $nextReport "status" "passed"
        $currentCompactStage = $nextStage
        $script:finalStage = $currentCompactStage
    }

    $authorReport = "$outputPath.json"
    if (-not (Test-JsonValue $authorReport "status" "authored")) {
        Invoke-Dvd2Hevc -Stage "author-final-iso" -Arguments @(
            "author-iso", $currentCompactStage, $outputPath, "--label", $Label
        )
    }
    Require-Report $authorReport "status" "authored"
    $verificationReport = "$outputPath.verification.json"
    if (-not (Test-JsonValue $verificationReport "passed" "True")) {
        Invoke-Dvd2Hevc -Stage "verify-final-iso" -Arguments @("verify-iso", $authorReport)
    }
    Require-Report $verificationReport "passed" "True"

    # libdvdcss checks fixed sector byte 0x14 for the legacy PES scrambling
    # bits. Early DVD2HEVC PSMs could occupy that byte and be falsely
    # decrypted even though the backup was clear. Audit every final image and
    # migrate older cached output without changing a single sector offset.
    $cssAuditReport = "$outputPath.css-safe-audit.json"
    Write-Status -State "running" -Stage "audit-css-safe-psm" -Message "Checking final HEVC maps against false CSS decryption"
    & $Python -u $cli "audit-css-safe-psm" $outputPath "--report" $cssAuditReport 2>&1 |
        Tee-Object -FilePath $logPath -Append
    $cssAuditExit = $LASTEXITCODE
    if ($cssAuditExit -eq 2) {
        $repairTemporary = "$outputPath.css-safe.tmp.iso"
        Remove-Item -LiteralPath $repairTemporary -Force -ErrorAction SilentlyContinue
        $repairReport = "$outputPath.css-safe-repair.json"
        Invoke-Dvd2Hevc -Stage "repair-css-safe-psm" -Arguments @(
            "repair-css-safe-psm", $outputPath, $repairTemporary, "--report", $repairReport
        )
        Move-Item -LiteralPath $repairTemporary -Destination $outputPath -Force
        Invoke-Dvd2Hevc -Stage "verify-css-safe-final-iso" -Arguments @(
            "audit-css-safe-psm", $outputPath, "--report", $cssAuditReport
        )
    }
    elseif ($cssAuditExit -ne 0) {
        throw "CSS-safe final-image audit failed with exit code $cssAuditExit"
    }
    Require-Report $cssAuditReport "passed" "True"

    Write-Status -State "passed" -Stage "complete" -Message "Full all-HEVC compact DVD ISO passed graph, CSS-safe map, and playback-gate preparation"
    "[$((Get-Date).ToString('o'))] PASS - $outputPath" | Tee-Object -FilePath $logPath -Append
    exit 0
}
catch {
    Write-Status -State "failed" -Stage $script:currentStage -Message $_.Exception.Message
    "[$((Get-Date).ToString('o'))] FAILED - $($_.Exception.Message)`r`n$($_ | Out-String)" |
        Tee-Object -FilePath $logPath -Append
    exit 1
}
