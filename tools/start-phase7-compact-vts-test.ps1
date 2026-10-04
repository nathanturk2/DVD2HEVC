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
$runner = Join-Path $PSScriptRoot "run-phase7-compact-vts-test.ps1"
$reportPath = [IO.Path]::GetFullPath($Report)
$workPath = [IO.Path]::GetFullPath($WorkRoot)
$outputPath = [IO.Path]::GetFullPath($OutputIso)
New-Item -ItemType Directory -Path $workPath -Force | Out-Null
$statusPath = Join-Path $workPath "status.json"
if (Test-Path -LiteralPath $statusPath -PathType Leaf) {
    $previous = Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json
    if ([string]$previous.state -eq "running" -and
        $null -ne (Get-Process -Id ([int]$previous.process_id) -ErrorAction SilentlyContinue)) {
        throw "A compact VTS test build is already running with PID $($previous.process_id)"
    }
}

$arguments = @(
    "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$runner`"",
    "-Report", "`"$reportPath`"", "-Vts", [string]$Vts,
    "-WorkRoot", "`"$workPath`"", "-OutputIso", "`"$outputPath`"",
    "-Quality", "`"$Quality`"", "-Label", "`"$Label`"", "-Python", "`"$Python`""
)
$process = Start-Process powershell.exe -ArgumentList $arguments -WindowStyle Hidden -PassThru
[ordered]@{
    schema = "dvd2hevc-phase7-compact-vts-test-launcher-v0"
    process_id = $process.Id
    report = $reportPath
    vts = $Vts
    work_root = $workPath
    output_iso = $outputPath
    status = $statusPath
    log = Join-Path $workPath "run.log"
    started = (Get-Date).ToString("o")
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $workPath "launcher.json") -Encoding UTF8

Write-Output "Started compact VTS $Vts test build (PID $($process.Id))."
Write-Output "Status: python dvd2hevc.py status `"$workPath`""
Write-Output "Log:    Get-Content -LiteralPath `"$(Join-Path $workPath 'run.log')`" -Tail 30"
