[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Report,
    [Parameter(Mandatory = $true)]
    [int]$Vts,
    [Parameter(Mandatory = $true)]
    [string]$WorkRoot,
    [Parameter(Mandatory = $true)]
    [string]$OutputIso,
    [string]$Quality = "cq:27",
    [string]$Label = "DVD2HEVC_TEST",
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$cli = Join-Path $projectRoot "dvd2hevc.py"
. (Join-Path $PSScriptRoot "write-log.ps1")
$reportPath = [IO.Path]::GetFullPath($Report)
$workPath = [IO.Path]::GetFullPath($WorkRoot)
$outputPath = [IO.Path]::GetFullPath($OutputIso)
$statusPath = Join-Path $workPath "status.json"
$logPath = Join-Path $workPath "run.log"
$started = (Get-Date).ToString("o")
$script:step = "initializing"

New-Item -ItemType Directory -Path $workPath -Force | Out-Null
New-Item -ItemType Directory -Path (Split-Path -Parent $outputPath) -Force | Out-Null

function Write-Status {
    param([string]$State, [string]$Step, [string]$Message)
    $script:step = $Step
    $value = [ordered]@{
        schema = "dvd2hevc-phase7-compact-vts-test-status-v0"
        state = $State
        step = $Step
        message = $Message
        report = $reportPath
        vts = $Vts
        work_root = $workPath
        output_iso = $outputPath
        started = $started
        updated = (Get-Date).ToString("o")
        process_id = $PID
    }
    $temporary = "$statusPath.$PID.tmp"
    $value | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $temporary -Encoding UTF8
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
    param([string]$Step, [string[]]$Arguments)
    Write-Status -State "running" -Step $Step -Message ($Arguments -join " ")
    "[$((Get-Date).ToString('o'))] $Step" | Tee-Object -FilePath $logPath -Append
    & $Python -u $cli @Arguments 2>&1 | Tee-Object -FilePath $logPath -Append
    if ($LASTEXITCODE -ne 0) { throw "$Step failed with exit code $LASTEXITCODE" }
}

function Require-JsonValue {
    param([string]$Path, [string]$Property, [string]$Value)
    if (-not (Test-JsonValue $Path $Property $Value)) {
        throw "Required report is absent or incomplete: $Path"
    }
}

try {
    "DVD2HEVC compact VTS test build started $started" | Set-Content -LiteralPath $logPath -Encoding UTF8
    Require-JsonValue $reportPath "status" "passed"
    $conversion = Get-Content -LiteralPath $reportPath -Raw | ConvertFrom-Json
    if ([int]$conversion.vts -ne $Vts -or [string]$conversion.domain -ne "title") {
        throw "Conversion report does not cover title-domain VTS $Vts"
    }
    if ([string]$conversion.settings.keyframe_policy -ne "dvd-vobu-idr-v1" -or
        [string]$conversion.settings.program_stream_map_policy -ne "dvd-vobu-psm-v1") {
        throw "Compact resume test requires the VOBU random-access conversion policy"
    }

    $sectorStage = Join-Path $workPath "sector-stage"
    $sectorReport = Join-Path $sectorStage "dvd2hevc-stage-report.json"
    if (-not (Test-JsonValue $sectorReport "status" "passed")) {
        Invoke-Dvd2Hevc -Step "stage-sector-preserving-vts" -Arguments @(
            "stage-disc", $sectorStage, $reportPath
        )
    }
    Require-JsonValue $sectorReport "status" "passed"
    $stageVerification = Join-Path $sectorStage "dvd2hevc-stage-verification.json"
    if (-not (Test-JsonValue $stageVerification "passed" "True")) {
        Invoke-Dvd2Hevc -Step "verify-sector-stage" -Arguments @("verify-stage", $sectorReport)
    }

    $layout = Join-Path $workPath "layout.json"
    if (-not (Test-JsonValue $layout "status" "planned")) {
        Invoke-Dvd2Hevc -Step "plan-compact-layout" -Arguments @(
            "plan-compact", $layout, $reportPath, "--quality", $Quality
        )
    }
    Require-JsonValue $layout "status" "planned"

    $compactVob = Join-Path $workPath ("VTS_{0:d2}_TITLE_COMPACT.vob" -f $Vts)
    if (-not (Test-JsonValue "$compactVob.json" "status" "passed")) {
        Invoke-Dvd2Hevc -Step "write-compact-vob" -Arguments @(
            "prototype-compact-domain", $layout, "title", [string]$Vts, $compactVob
        )
    }
    Require-JsonValue "$compactVob.json" "status" "passed"

    $compactIfo = Join-Path $workPath ("VTS_{0:d2}_0.IFO" -f $Vts)
    $compactBup = Join-Path $workPath ("VTS_{0:d2}_0.BUP" -f $Vts)
    if (-not (Test-JsonValue "$compactIfo.json" "status" "passed")) {
        $sourceIfo = Join-Path (Join-Path $sectorStage "VIDEO_TS") ("VTS_{0:d2}_0.IFO" -f $Vts)
        Invoke-Dvd2Hevc -Step "rewrite-compact-ifo" -Arguments @(
            "rewrite-compact-vts-ifo", $layout, [string]$Vts, $sourceIfo,
            $compactIfo, "--destination-bup", $compactBup
        )
    }
    Require-JsonValue "$compactIfo.json" "status" "passed"

    $compactStage = Join-Path $workPath "compact-stage"
    $compactStageReport = Join-Path $compactStage "dvd2hevc-stage-report.json"
    if (-not (Test-JsonValue $compactStageReport "status" "passed")) {
        Invoke-Dvd2Hevc -Step "stage-compact-vts" -Arguments @(
            "stage-compact-vts", $sectorStage, $layout, [string]$Vts,
            $compactVob, $compactIfo, $compactBup, $compactStage
        )
    }
    Require-JsonValue $compactStageReport "status" "passed"

    $authorReport = "$outputPath.json"
    if (-not (Test-JsonValue $authorReport "status" "authored")) {
        Invoke-Dvd2Hevc -Step "author-test-iso" -Arguments @(
            "author-iso", $compactStage, $outputPath, "--label", $Label
        )
    }
    Require-JsonValue $authorReport "status" "authored"
    $verification = "$outputPath.verification.json"
    if (-not (Test-JsonValue $verification "passed" "True")) {
        Invoke-Dvd2Hevc -Step "verify-test-iso" -Arguments @("verify-iso", $authorReport)
    }
    Require-JsonValue $verification "passed" "True"

    $cssAudit = "$outputPath.css-safe-audit.json"
    & $Python -u $cli "audit-css-safe-psm" $outputPath "--report" $cssAudit 2>&1 |
        Tee-Object -FilePath $logPath -Append
    $cssAuditExit = $LASTEXITCODE
    if ($cssAuditExit -eq 2) {
        $repairTemporary = "$outputPath.css-safe.tmp.iso"
        Remove-Item -LiteralPath $repairTemporary -Force -ErrorAction SilentlyContinue
        Invoke-Dvd2Hevc -Step "repair-css-safe-psm" -Arguments @(
            "repair-css-safe-psm", $outputPath, $repairTemporary,
            "--report", "$outputPath.css-safe-repair.json"
        )
        Move-Item -LiteralPath $repairTemporary -Destination $outputPath -Force
        Invoke-Dvd2Hevc -Step "verify-css-safe-test-iso" -Arguments @(
            "audit-css-safe-psm", $outputPath, "--report", $cssAudit
        )
    }
    elseif ($cssAuditExit -ne 0) {
        throw "CSS-safe test-image audit failed with exit code $cssAuditExit"
    }
    Require-JsonValue $cssAudit "passed" "True"

    Write-Status -State "passed" -Step "complete" -Message "Compact VTS test ISO passed graph and CSS-safe map verification"
    "[$((Get-Date).ToString('o'))] PASS - $outputPath" | Tee-Object -FilePath $logPath -Append
    exit 0
}
catch {
    Write-Status -State "failed" -Step $script:step -Message $_.Exception.Message
    "[$((Get-Date).ToString('o'))] FAILED - $($_.Exception.Message)`r`n$($_ | Out-String)" |
        Tee-Object -FilePath $logPath -Append
    exit 1
}
