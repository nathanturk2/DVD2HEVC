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
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$runner = Join-Path $PSScriptRoot "run-phase7-remaining-domains.ps1"
$planPath = [IO.Path]::GetFullPath($Plan)
$workPath = [IO.Path]::GetFullPath($WorkRoot)
New-Item -ItemType Directory -Path $workPath -Force | Out-Null

$statusPath = Join-Path $workPath "status.json"
if (Test-Path -LiteralPath $statusPath -PathType Leaf) {
    $previous = Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json
    if ([string]$previous.state -eq "running" -and
        $null -ne (Get-Process -Id ([int]$previous.process_id) -ErrorAction SilentlyContinue)) {
        throw "A remaining-domain conversion is already running with PID $($previous.process_id)"
    }
}

$arguments = @(
    "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$runner`"",
    "-Plan", "`"$planPath`"", "-WorkRoot", "`"$workPath`"",
    "-QualityValues", "`"$QualityValues`"", "-Encoder", "`"$Encoder`"", "-Preset", "`"$Preset`"",
    "-Python", "`"$Python`""
)
$process = Start-Process powershell.exe -ArgumentList $arguments -WindowStyle Hidden -PassThru
[ordered]@{
    schema = "dvd2hevc-phase7-remaining-launcher-v0"
    process_id = $process.Id
    plan = $planPath
    work_root = $workPath
    status = $statusPath
    log = Join-Path $workPath "run.log"
    started = (Get-Date).ToString("o")
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $workPath "launcher.json") -Encoding UTF8

Write-Output "Started remaining Phase 7 title/menu conversion (PID $($process.Id))."
Write-Output "Status: Get-Content -LiteralPath `"$statusPath`""
Write-Output "Log:    Get-Content -LiteralPath `"$(Join-Path $workPath 'run.log')`" -Tail 30"
