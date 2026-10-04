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
$runner = Join-Path $PSScriptRoot "run-phase7-full-disc.ps1"
$planPath = [IO.Path]::GetFullPath($Plan)
$baseStagePath = [IO.Path]::GetFullPath($BaseStage)
$interleavedPath = [IO.Path]::GetFullPath($InterleavedReport)
$workPath = [IO.Path]::GetFullPath($WorkRoot)
$outputPath = [IO.Path]::GetFullPath($OutputIso)
New-Item -ItemType Directory -Path $workPath -Force | Out-Null

$statusPath = Join-Path $workPath "status.json"
if (Test-Path -LiteralPath $statusPath -PathType Leaf) {
    $previous = Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json
    if ([string]$previous.state -eq "running" -and
        $null -ne (Get-Process -Id ([int]$previous.process_id) -ErrorAction SilentlyContinue)) {
        throw "A full Phase 7 build is already running with PID $($previous.process_id)"
    }
}

$arguments = @(
    "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$runner`"",
    "-Plan", "`"$planPath`"", "-BaseStage", "`"$baseStagePath`"",
    "-InterleavedReport", "`"$interleavedPath`"", "-WorkRoot", "`"$workPath`"",
    "-OutputIso", "`"$outputPath`"", "-QualityValues", "`"$QualityValues`"",
    "-CompactQuality", "`"$CompactQuality`"", "-Encoder", "`"$Encoder`"", "-Preset", "`"$Preset`"",
    "-Label", "`"$Label`"", "-Python", "`"$Python`""
)
$process = Start-Process powershell.exe -ArgumentList $arguments -WindowStyle Hidden -PassThru
[ordered]@{
    schema = "dvd2hevc-phase7-full-disc-launcher-v0"
    process_id = $process.Id
    plan = $planPath
    base_stage = $baseStagePath
    interleaved_report = $interleavedPath
    work_root = $workPath
    output_iso = $outputPath
    status = $statusPath
    log = Join-Path $workPath "run.log"
    started = (Get-Date).ToString("o")
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $workPath "launcher.json") -Encoding UTF8

Write-Output "Started full Phase 7 build (PID $($process.Id))."
Write-Output "Status: python `"$(Join-Path (Split-Path -Parent $PSScriptRoot) 'dvd2hevc.py')`" status `"$workPath`""
Write-Output "Log:    Get-Content -LiteralPath `"$(Join-Path $workPath 'run.log')`" -Tail 30"
