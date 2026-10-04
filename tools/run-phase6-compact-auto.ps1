[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Source,

    [Parameter(Mandatory = $true)]
    [string]$QualityEstimate,

    [string]$WorkRoot = "work\phase6\compact-auto-cq27",
    [switch]$FinishDisc,
    [switch]$CompactStereoVts2,
    [string]$CompactAudioReport = "",
    [string]$OutputIso = "",
    [string]$VolumeLabel = "DVD2HEVC_COMPACT_AUTO",
    [string]$VlcRoot = "",
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$cli = Join-Path $projectRoot "dvd2hevc.py"
. (Join-Path $PSScriptRoot "write-log.ps1")
$sourcePath = [IO.Path]::GetFullPath($Source)
$estimatePath = [IO.Path]::GetFullPath($QualityEstimate)
$workPath = [IO.Path]::GetFullPath($WorkRoot)
$statusPath = Join-Path $workPath "status.json"
$logPath = Join-Path $workPath "run.log"
$started = (Get-Date).ToString("o")

New-Item -ItemType Directory -Path $workPath -Force | Out-Null
[IO.File]::WriteAllText($logPath, "DVD2HEVC compact-auto run started $started`r`n")

function Write-RunStatus {
    param(
        [string]$State,
        [string]$Step,
        [string]$Message,
        [string]$Output = ""
    )
    $value = [ordered]@{
        state = $State
        step = $Step
        message = $Message
        source = $sourcePath
        quality_estimate = $estimatePath
        work_root = $workPath
        output = $Output
        started = $started
        updated = (Get-Date).ToString("o")
        process_id = $PID
    }
    $temporary = "$statusPath.$PID.tmp"
    $value | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Dvd2HevcAtomicFile -TemporaryPath $temporary -DestinationPath $statusPath
}

function Invoke-Dvd2Hevc {
    param(
        [string]$Step,
        [string]$Message,
        [string[]]$Arguments
    )
    Write-RunStatus -State "running" -Step $Step -Message $Message
    "`r`n[$((Get-Date).ToString('o'))] $Step - $Message" | Tee-Object -FilePath $logPath -Append
    & $Python $cli @Arguments 2>&1 | Tee-Object -FilePath $logPath -Append
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        throw "Step '$Step' failed with exit code $code"
    }
}

try {
    if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
        throw "Source ISO is missing: $sourcePath"
    }
    if (-not (Test-Path -LiteralPath $estimatePath -PathType Leaf)) {
        throw "Quality estimate is missing: $estimatePath"
    }

    $vts1Workspace = Join-Path $workPath "vts1-shared"
    $titleReports = Join-Path $workPath "title-reports"
    New-Item -ItemType Directory -Path $titleReports -Force | Out-Null

    foreach ($title in 1..3) {
        Invoke-Dvd2Hevc `
            -Step "title-$title" `
            -Message "Encoding and validating title $title at compact-auto CQ" `
            -Arguments @(
                "convert-title", $sourcePath, [string]$title, $vts1Workspace,
                "--quality-preset", "compact-auto",
                "--quality-estimate", $estimatePath,
                "--cadence", "auto"
            )
        Copy-Item -LiteralPath (Join-Path $vts1Workspace "title-report.json") `
            -Destination (Join-Path $titleReports "title$title-report.json") -Force
    }

    $combinedVts1 = Join-Path $workPath "vts1-compact-auto-report.json"
    Invoke-Dvd2Hevc `
        -Step "combine-vts1" `
        -Message "Combining shared physical coverage for VTS 1" `
        -Arguments @(
            "combine-titles", $combinedVts1,
            (Join-Path $titleReports "title1-report.json"),
            (Join-Path $titleReports "title2-report.json"),
            (Join-Path $titleReports "title3-report.json")
        )

    $title4Workspace = Join-Path $workPath "title4"
    Invoke-Dvd2Hevc `
        -Step "title-4" `
        -Message "Encoding and validating VTS 2 title 4" `
        -Arguments @(
            "convert-title", $sourcePath, "4", $title4Workspace,
            "--quality-preset", "compact-auto",
            "--quality-estimate", $estimatePath,
            "--cadence", "auto"
        )

    $vmgWorkspace = Join-Path $workPath "vmg-menu"
    Invoke-Dvd2Hevc `
        -Step "vmg-menu" `
        -Message "Encoding and validating the VMG menu domain" `
        -Arguments @(
            "convert-menu", $sourcePath, "vmg_menu", "0", $vmgWorkspace,
            "--quality-preset", "compact-auto",
            "--quality-estimate", $estimatePath,
            "--cadence", "auto"
        )

    $vtsMenuWorkspace = Join-Path $workPath "vts1-menu"
    Invoke-Dvd2Hevc `
        -Step "vts1-menu" `
        -Message "Encoding and validating the VTS 1 menu domain" `
        -Arguments @(
            "convert-menu", $sourcePath, "vts_menu", "1", $vtsMenuWorkspace,
            "--quality-preset", "compact-auto",
            "--quality-estimate", $estimatePath,
            "--cadence", "auto"
        )

    $layout = Join-Path $workPath "the-intern-compact-auto-layout.json"
    Invoke-Dvd2Hevc `
        -Step "compact-layout" `
        -Message "Planning the exact compact-auto CQ layout" `
        -Arguments @(
            "plan-compact", $layout,
            $combinedVts1,
            (Join-Path $title4Workspace "title-report.json"),
            (Join-Path $vmgWorkspace "menu-report.json"),
            (Join-Path $vtsMenuWorkspace "menu-report.json"),
            "--quality", "compact-auto",
            "--quality-estimate", $estimatePath,
            "--audio-mode", "passthrough"
        )

    $finalOutput = $layout
    if ($FinishDisc) {
        Write-RunStatus -State "running" -Step "finalize" -Message "Packing, authoring, and verifying the compact disc"
        $finishScript = Join-Path $PSScriptRoot "finish-phase6-compact-auto.ps1"
        $finishArguments = @(
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $finishScript,
            "-WorkRoot", $workPath, "-Layout", $layout,
            "-VolumeLabel", $VolumeLabel, "-Python", $Python
        )
        if ($CompactStereoVts2) { $finishArguments += "-CompactStereoVts2" }
        if ($CompactAudioReport) { $finishArguments += @("-CompactAudioReport", $CompactAudioReport) }
        if ($OutputIso) { $finishArguments += @("-OutputIso", $OutputIso) }
        if ($VlcRoot) { $finishArguments += @("-VlcRoot", $VlcRoot) }
        & powershell @finishArguments 2>&1 | Tee-Object -FilePath $logPath -Append
        if ($LASTEXITCODE -ne 0) {
            throw "Compact-disc finalization failed with exit code $LASTEXITCODE"
        }
        $finalizeStatus = Get-Content -LiteralPath (Join-Path $workPath "finalize-status.json") -Raw | ConvertFrom-Json
        $finalOutput = [string]$finalizeStatus.output
    }

    Write-RunStatus `
        -State "passed" `
        -Step "complete" `
        -Message $(if ($FinishDisc) { "Compact-auto encode, authoring, and verification passed" } else { "All compact-auto CQ encodes and the exact layout passed" }) `
        -Output $finalOutput
    "[$((Get-Date).ToString('o'))] PASS - $finalOutput" | Tee-Object -FilePath $logPath -Append
    exit 0
}
catch {
    $message = $_.Exception.Message
    Write-RunStatus -State "failed" -Step "failed" -Message $message
    "[$((Get-Date).ToString('o'))] FAILED - $message`r`n$($_ | Out-String)" | `
        Tee-Object -FilePath $logPath -Append
    exit 1
}
