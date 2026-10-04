[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Plan,
    [Parameter(Mandatory = $true)]
    [string]$WorkRoot,
    [Parameter(Mandatory = $true)]
    [string]$OutputIso,
    [string]$FullScanReport,
    [string]$QualityValues = "cq:27",
    [string]$CompactQuality = "cq:27",
    [ValidateSet("hevc_nvenc", "hevc_qsv", "hevc_amf", "libx265")]
    [string]$Encoder = "hevc_nvenc",
    [string]$Preset = "p6",
    [ValidateSet("auto", "progressive", "deinterlace50")]
    [string]$Cadence = "auto",
    [ValidateSet("fail", "progressive", "deinterlace50")]
    [string]$AmbiguousCadence = "deinterlace50",
    [string]$Label,
    [string]$VlcRoot,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$runner = Join-Path $PSScriptRoot "run-phase7-general-disc.ps1"
$planPath = [IO.Path]::GetFullPath($Plan)
$workPath = [IO.Path]::GetFullPath($WorkRoot)
$outputPath = [IO.Path]::GetFullPath($OutputIso)
New-Item -ItemType Directory -Path $workPath -Force | Out-Null

$statusPath = Join-Path $workPath "status.json"
if (Test-Path -LiteralPath $statusPath -PathType Leaf) {
    $previous = Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json
    if ([string]$previous.state -eq "running" -and
        $null -ne (Get-Process -Id ([int]$previous.process_id) -ErrorAction SilentlyContinue)) {
        throw "A generalized Phase 7 build is already running with PID $($previous.process_id)"
    }
}

$arguments = @(
    "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$runner`"",
    "-Plan", "`"$planPath`"", "-WorkRoot", "`"$workPath`"",
    "-OutputIso", "`"$outputPath`"", "-QualityValues", "`"$QualityValues`"",
    "-CompactQuality", "`"$CompactQuality`"", "-Encoder", "`"$Encoder`"", "-Preset", "`"$Preset`"",
    "-Cadence", "`"$Cadence`"", "-AmbiguousCadence", "`"$AmbiguousCadence`"",
    "-Python", "`"$Python`""
)
if ($FullScanReport) {
    $scanPath = [IO.Path]::GetFullPath($FullScanReport)
    $arguments += @("-FullScanReport", "`"$scanPath`"")
}
if ($Label) { $arguments += @("-Label", "`"$Label`"") }
if ($VlcRoot) {
    $vlcPath = [IO.Path]::GetFullPath($VlcRoot)
    $arguments += @("-VlcRoot", "`"$vlcPath`"")
}

$process = Start-Process powershell.exe -ArgumentList $arguments -WindowStyle Hidden -PassThru
[ordered]@{
    schema = "dvd2hevc-phase7-general-disc-launcher-v0"
    process_id = $process.Id
    plan = $planPath
    work_root = $workPath
    output_iso = $outputPath
    status = $statusPath
    log = Join-Path $workPath "run.log"
    started = (Get-Date).ToString("o")
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $workPath "launcher.json") -Encoding UTF8

Write-Output "Started generalized hidden Phase 7 build (PID $($process.Id))."
Write-Output "Status: python dvd2hevc.py status `"$workPath`""
Write-Output "Log:    Get-Content -LiteralPath `"$(Join-Path $workPath 'run.log')`" -Tail 30"
