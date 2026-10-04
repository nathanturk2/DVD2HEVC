[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Plan,

    [Parameter(Mandatory = $true)]
    [string]$WorkRoot,

    [string]$QualityValues = "cq:27",
    [ValidateSet("hevc_nvenc", "hevc_qsv", "hevc_amf", "libx265")]
    [string]$Encoder = "hevc_nvenc",
    [string]$Preset = "p6",
    [ValidateSet("auto", "progressive", "deinterlace50")]
    [string]$Cadence = "auto",
    [ValidateSet("fail", "progressive", "deinterlace50")]
    [string]$AmbiguousCadence = "deinterlace50",
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$runner = Join-Path $PSScriptRoot "run-phase7-interleaved-vts.ps1"
$planPath = [IO.Path]::GetFullPath($Plan)
$workPath = [IO.Path]::GetFullPath($WorkRoot)
$statusPath = Join-Path $workPath "status.json"
$launcherPath = Join-Path $workPath "launcher.json"

if (-not (Test-Path -LiteralPath $planPath -PathType Leaf)) {
    throw "Interleaved VTS plan is missing: $planPath"
}
New-Item -ItemType Directory -Path $workPath -Force | Out-Null
if (Test-Path -LiteralPath $statusPath -PathType Leaf) {
    $previous = Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json
    if ([string]$previous.state -eq "running") {
        $previousProcess = Get-Process -Id ([int]$previous.process_id) -ErrorAction SilentlyContinue
        if ($null -ne $previousProcess) {
            throw "A Phase 7 VTS run is already active with PID $($previous.process_id)"
        }
    }
}

$arguments = @(
    "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$runner`"",
    "-Plan", "`"$planPath`"",
    "-WorkRoot", "`"$workPath`"",
    "-QualityValues", "`"$QualityValues`"",
    "-Encoder", "`"$Encoder`"",
    "-Preset", "`"$Preset`"",
    "-Cadence", "`"$Cadence`"",
    "-AmbiguousCadence", "`"$AmbiguousCadence`"",
    "-Python", "`"$Python`""
)
$process = Start-Process -FilePath "powershell.exe" -ArgumentList $arguments `
    -WindowStyle Hidden -PassThru

[ordered]@{
    schema = "dvd2hevc-phase7-launcher-v0"
    state = "started"
    process_id = $process.Id
    plan = $planPath
    work_root = $workPath
    status = $statusPath
    log = Join-Path $workPath "run.log"
    started = (Get-Date).ToString("o")
} | ConvertTo-Json | Set-Content -LiteralPath $launcherPath -Encoding UTF8

Write-Output "Started hidden Phase 7 VTS conversion (PID $($process.Id))."
Write-Output "Status: python dvd2hevc.py status `"$workPath`""
Write-Output "Log:    Get-Content -LiteralPath `"$(Join-Path $workPath 'run.log')`" -Tail 30"
