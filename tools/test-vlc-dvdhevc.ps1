[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$VlcRoot,

    [Parameter(Mandatory = $true)]
    [string]$DvdPath,

    [int]$Title = 1,
    [int]$RunTime = 8,
    [double]$Rate = 1.0,
    [double]$StartTime = 0.0,
    [ValidateRange(1, 3)]
    [int]$Verbosity = 3,
    [ValidateSet("any", "d3d11va", "dxva2", "none")]
    [string]$AvcodecHardware = "any",
    [int]$DiscCaching = 1000,
    [int]$MaxWallTime = 0,
    [int]$ExpectedCells = 1,
    [switch]$Menus,
    [switch]$SkipValidation,
    [switch]$SkipCssKeys,
    [string]$LogPath
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$vlc = Join-Path ([IO.Path]::GetFullPath($VlcRoot)) "vlc.exe"
$dvd = [IO.Path]::GetFullPath($DvdPath)

if (-not (Test-Path -LiteralPath $vlc -PathType Leaf)) {
    throw "vlc.exe was not found under $VlcRoot"
}
if (-not (Test-Path -LiteralPath $dvd)) {
    throw "DVD folder or image was not found: $dvd"
}
if (-not $LogPath) {
    $LogPath = Join-Path $projectRoot "work\vlc-dvdhevc-validation.log"
}
$LogPath = [IO.Path]::GetFullPath($LogPath)
$logDirectory = Split-Path -Parent $LogPath
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null

$uriPath = $dvd.Replace("\", "/").Replace("%", "%25").Replace(" ", "%20")
$uri = if ($Menus) { "dvd:///$uriPath" } else { "dvd:///$uriPath#$Title" }
$arguments = @(
    "-I", "dummy",
    ("-" + ("v" * $Verbosity)),
    "--no-video-title-show",
    "--vout=dummy",
    "--aout=dummy",
    "--avcodec-hw=$AvcodecHardware",
    "--disc-caching=$DiscCaching",
    "--run-time=$RunTime",
    "--rate=$Rate",
    "--start-time=$StartTime",
    "--play-and-exit",
    $uri
)
if (-not $Menus) {
    $arguments = $arguments[0..5] + "--no-dvdnav-menu" + $arguments[6..($arguments.Count - 1)]
}

$stdoutPath = "$LogPath.stdout.tmp"
$stderrPath = "$LogPath.stderr.tmp"
$previousNoKeys = [Environment]::GetEnvironmentVariable("DVDREAD_NOKEYS", "Process")
if ($SkipCssKeys) { [Environment]::SetEnvironmentVariable("DVDREAD_NOKEYS", "1", "Process") }
try {
    $process = Start-Process -FilePath $vlc -ArgumentList $arguments -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru
    $wallSeconds = if ($MaxWallTime -gt 0) { $MaxWallTime } else { [Math]::Max(30, $RunTime + 20) }
    if (-not $process.WaitForExit($wallSeconds * 1000)) {
        Stop-Process -Id $process.Id -Force
        $process.WaitForExit()
        Write-Host "VLC reached external wall-clock limit after $wallSeconds seconds"
    }
    $lines = @()
    if (Test-Path -LiteralPath $stdoutPath) { $lines += Get-Content -LiteralPath $stdoutPath }
    if (Test-Path -LiteralPath $stderrPath) { $lines += Get-Content -LiteralPath $stderrPath }
    [IO.File]::WriteAllLines($LogPath, [string[]]$lines)
}
finally {
    [Environment]::SetEnvironmentVariable("DVDREAD_NOKEYS", $previousNoKeys, "Process")
    Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
}
Write-Host "VLC log: $LogPath"

if ($SkipCssKeys -and (Select-String -LiteralPath $LogPath -SimpleMatch "Attempting to retrieve all CSS keys" -Quiet)) {
    throw "DVDREAD_NOKEYS was requested but libdvdread still attempted its eager key scan"
}

if ($SkipValidation) { exit 0 }
& python (Join-Path $projectRoot "dvd2hevc.py") validate-vlc-log $LogPath --minimum-cell-changes $ExpectedCells
exit $LASTEXITCODE
