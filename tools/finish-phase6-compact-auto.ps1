[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$WorkRoot,

    [string]$Layout = "",
    [switch]$CompactStereoVts2,
    [string]$CompactAudioReport = "",
    [string]$OutputIso = "",
    [string]$VolumeLabel = "DVD2HEVC_COMPACT_AUTO",
    [string]$VlcRoot = "",
    [int]$PlaybackTitle = 1,
    [double]$PlaybackStart = 1160,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$cli = Join-Path $projectRoot "dvd2hevc.py"
. (Join-Path $PSScriptRoot "write-log.ps1")
$workPath = [IO.Path]::GetFullPath($WorkRoot)
$assemblyPath = Join-Path $workPath "automatic-final"
$statusPath = Join-Path $workPath "finalize-status.json"
$logPath = Join-Path $workPath "finalize.log"
$started = (Get-Date).ToString("o")

if (-not $Layout) {
    $runStatus = Join-Path $workPath "status.json"
    if (Test-Path -LiteralPath $runStatus -PathType Leaf) {
        $Layout = [string](Get-Content -LiteralPath $runStatus -Raw | ConvertFrom-Json).output
    }
}
if (-not $Layout) {
    $matches = @(Get-ChildItem -LiteralPath $workPath -File -Filter "*compact-auto-layout.json")
    if ($matches.Count -ne 1) {
        throw "Pass -Layout when the work root does not contain exactly one compact-auto layout"
    }
    $Layout = $matches[0].FullName
}
$layoutPath = [IO.Path]::GetFullPath($Layout)
if (-not $OutputIso) {
    $OutputIso = Join-Path $workPath "DVD2HEVC_COMPACT_AUTO_FINAL.iso"
}
$outputPath = [IO.Path]::GetFullPath($OutputIso)

if (Test-Path -LiteralPath $assemblyPath) {
    throw "Automatic assembly destination already exists: $assemblyPath"
}
if (Test-Path -LiteralPath $outputPath) {
    throw "Output ISO already exists: $outputPath"
}
New-Item -ItemType Directory -Path $assemblyPath -Force | Out-Null
[IO.File]::WriteAllText($logPath, "DVD2HEVC compact-auto finalization started $started`r`n")

function Write-FinalizeStatus {
    param([string]$State, [string]$Step, [string]$Message, [string]$Output = "")
    $value = [ordered]@{
        state = $State
        step = $Step
        message = $Message
        work_root = $workPath
        layout = $layoutPath
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
    param([string]$Step, [string]$Message, [string[]]$Arguments)
    Write-FinalizeStatus -State "running" -Step $Step -Message $Message
    "`r`n[$((Get-Date).ToString('o'))] $Step - $Message" | Tee-Object -FilePath $logPath -Append
    & $Python $cli @Arguments 2>&1 | Tee-Object -FilePath $logPath -Append
    if ($LASTEXITCODE -ne 0) {
        throw "Step '$Step' failed with exit code $LASTEXITCODE"
    }
}

try {
    if (-not (Test-Path -LiteralPath $layoutPath -PathType Leaf)) {
        throw "Compact layout is missing: $layoutPath"
    }
    $combinedVts1 = Join-Path $workPath "vts1-compact-auto-report.json"
    $title4 = Join-Path $workPath "title4\title-report.json"
    $vmgMenu = Join-Path $workPath "vmg-menu\menu-report.json"
    $vts1Menu = Join-Path $workPath "vts1-menu\menu-report.json"
    foreach ($required in @($combinedVts1, $title4, $vmgMenu, $vts1Menu)) {
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
            throw "Required conversion report is missing: $required"
        }
    }

    $vts2Layout = $layoutPath
    if ($CompactAudioReport) {
        $audioReportPath = [IO.Path]::GetFullPath($CompactAudioReport)
    }
    elseif ($CompactStereoVts2) {
        $title4Value = Get-Content -LiteralPath $title4 -Raw | ConvertFrom-Json
        $firstSector = ($title4Value.cells | ForEach-Object { [int]$_.cell.first_sector } | Measure-Object -Minimum).Minimum
        $lastSector = ($title4Value.cells | ForEach-Object { [int]$_.cell.last_sector } | Measure-Object -Maximum).Maximum
        $vts2Source = Join-Path $assemblyPath "vts2-source.vob"
        Invoke-Dvd2Hevc -Step "extract-vts2" -Message "Extracting the complete VTS 2 title domain" -Arguments @(
            "extract-cell", [string]$title4Value.source, "2", [string]$firstSector, [string]$lastSector, $vts2Source
        )
        $audioWorkspace = Join-Path $assemblyPath "vts2-compact-audio"
        Invoke-Dvd2Hevc -Step "compact-stereo" -Message "Creating DVD-compatible compact stereo for VTS 2" -Arguments @(
            "prototype-compact-audio", $vts2Source, $audioWorkspace
        )
        $audioReportPath = Join-Path $audioWorkspace "compact-audio-report.json"
    }
    else {
        $audioReportPath = ""
    }

    if ($audioReportPath) {
        if (-not (Test-Path -LiteralPath $audioReportPath -PathType Leaf)) {
            throw "Compact audio report is missing: $audioReportPath"
        }
        $vts2Layout = Join-Path $assemblyPath "vts2-stereo-layout.json"
        Invoke-Dvd2Hevc -Step "plan-stereo" -Message "Applying compact stereo packet counts to VTS 2" -Arguments @(
            "plan-compact-audio", $layoutPath, $audioReportPath, $vts2Layout, "title", "2"
        )
    }

    $sectorStage = Join-Path $assemblyPath "sector-stage"
    Invoke-Dvd2Hevc -Step "stage-disc" -Message "Staging every validated HEVC title and menu cell" -Arguments @(
        "stage-disc", $sectorStage, $combinedVts1, $title4, $vmgMenu, $vts1Menu
    )

    $vts1Vob = Join-Path $assemblyPath "vts1-compact.vob"
    $vts2Vob = Join-Path $assemblyPath "vts2-compact.vob"
    Invoke-Dvd2Hevc -Step "pack-vts1" -Message "Packing compact VTS 1 with one HEVC map per cell" -Arguments @(
        "prototype-compact-domain", $layoutPath, "title", "1", $vts1Vob
    )
    Invoke-Dvd2Hevc -Step "pack-vts2" -Message "Packing compact VTS 2 with one HEVC map per cell" -Arguments @(
        "prototype-compact-domain", $vts2Layout, "title", "2", $vts2Vob
    )

    $vts1Control = Join-Path $assemblyPath "control-vts1"
    $vts2Control = Join-Path $assemblyPath "control-vts2"
    Invoke-Dvd2Hevc -Step "control-vts1" -Message "Rewriting compact VTS 1 IFO and BUP" -Arguments @(
        "rewrite-compact-vts-ifo", $layoutPath, "1", (Join-Path $sectorStage "VIDEO_TS\VTS_01_0.IFO"),
        (Join-Path $vts1Control "VTS_01_0.IFO"), "--destination-bup", (Join-Path $vts1Control "VTS_01_0.BUP")
    )
    Invoke-Dvd2Hevc -Step "control-vts2" -Message "Rewriting compact VTS 2 IFO and BUP" -Arguments @(
        "rewrite-compact-vts-ifo", $vts2Layout, "2", (Join-Path $sectorStage "VIDEO_TS\VTS_02_0.IFO"),
        (Join-Path $vts2Control "VTS_02_0.IFO"), "--destination-bup", (Join-Path $vts2Control "VTS_02_0.BUP")
    )

    $vts1Stage = Join-Path $assemblyPath "vts1-stage"
    $finalStage = Join-Path $assemblyPath "final-stage"
    Invoke-Dvd2Hevc -Step "stage-vts1" -Message "Installing compact VTS 1" -Arguments @(
        "stage-compact-vts", $sectorStage, $layoutPath, "1", $vts1Vob,
        (Join-Path $vts1Control "VTS_01_0.IFO"), (Join-Path $vts1Control "VTS_01_0.BUP"), $vts1Stage
    )
    Invoke-Dvd2Hevc -Step "stage-vts2" -Message "Installing compact VTS 2" -Arguments @(
        "stage-compact-vts", $vts1Stage, $vts2Layout, "2", $vts2Vob,
        (Join-Path $vts2Control "VTS_02_0.IFO"), (Join-Path $vts2Control "VTS_02_0.BUP"), $finalStage
    )
    Invoke-Dvd2Hevc -Step "author" -Message "Authoring the final UDF ISO" -Arguments @(
        "author-iso", $finalStage, $outputPath, "--label", $VolumeLabel
    )
    Invoke-Dvd2Hevc -Step "verify" -Message "Verifying every ISO file and the physical graph" -Arguments @(
        "verify-iso", "$outputPath.json"
    )

    if ($VlcRoot) {
        $vlcPath = [IO.Path]::GetFullPath($VlcRoot)
        $testScript = Join-Path $PSScriptRoot "test-vlc-dvdhevc.ps1"
        $menuLog = Join-Path $assemblyPath "vlc-menu.log"
        $titleLog = Join-Path $assemblyPath "vlc-title.log"
        Write-FinalizeStatus -State "running" -Step "vlc" -Message "Running menu and title playback smoke tests"
        & powershell -NoProfile -ExecutionPolicy Bypass -File $testScript -VlcRoot $vlcPath -DvdPath $outputPath `
            -Menus -RunTime 20 -MaxWallTime 35 -SkipCssKeys -SkipValidation -LogPath $menuLog
        if ($LASTEXITCODE -ne 0) { throw "VLC menu smoke test failed" }
        & powershell -NoProfile -ExecutionPolicy Bypass -File $testScript -VlcRoot $vlcPath -DvdPath $outputPath `
            -Title $PlaybackTitle -StartTime $PlaybackStart -RunTime 12 -MaxWallTime 35 `
            -SkipCssKeys -SkipValidation -LogPath $titleLog
        if ($LASTEXITCODE -ne 0) { throw "VLC title smoke test failed" }
        if ((Select-String -LiteralPath $menuLog, $titleLog -SimpleMatch "corrupt").Count) {
            throw "VLC reported corrupt playback"
        }
        if ((Select-String -LiteralPath $menuLog -SimpleMatch "Output frame").Count -lt 1) {
            throw "VLC did not output the menu still frame"
        }
        if ((Select-String -LiteralPath $titleLog -SimpleMatch "Output frame").Count -lt 100) {
            throw "VLC did not output enough title frames"
        }
    }

    Write-FinalizeStatus -State "passed" -Step "complete" -Message "Compact disc authoring and verification passed" -Output $outputPath
    "[$((Get-Date).ToString('o'))] PASS - $outputPath" | Tee-Object -FilePath $logPath -Append
    exit 0
}
catch {
    $message = $_.Exception.Message
    Write-FinalizeStatus -State "failed" -Step "failed" -Message $message
    "[$((Get-Date).ToString('o'))] FAILED - $message`r`n$($_ | Out-String)" | Tee-Object -FilePath $logPath -Append
    exit 1
}
