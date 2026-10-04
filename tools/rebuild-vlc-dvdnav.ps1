param(
    [Parameter(Mandatory = $true)]
    [string]$SourcePath,
    [string]$VlcRoot = '',
    [string]$LogPath,
    [string]$ExitCodePath,
    [switch]$FullBuild
)

$ErrorActionPreference = 'Continue'
Import-Module (Join-Path $PSHOME "Modules\Microsoft.PowerShell.Utility") -Force
$env:MSYSTEM = 'MINGW64'
$env:BUILDCC = 'gcc'

$bash = 'C:\msys64\usr\bin\bash.exe'
if (-not (Test-Path -LiteralPath $bash)) {
    throw "MSYS2 bash was not found at $bash"
}
if (-not (Test-Path -LiteralPath $SourcePath)) {
    throw "VLC source path was not found: $SourcePath"
}
if (-not $VlcRoot) {
    $state = if ($env:DVD2HEVC_STATE_DIR) { $env:DVD2HEVC_STATE_DIR } else { Join-Path $env:LOCALAPPDATA 'DVD2HEVC' }
    $VlcRoot = Join-Path $state 'vlc-dvdhevc'
}
$vlcPath = [IO.Path]::GetFullPath($VlcRoot)
if (-not $LogPath) { $LogPath = Join-Path $vlcPath 'dvdnav-build.log' }
if (-not $ExitCodePath) { $ExitCodePath = Join-Path $vlcPath 'dvdnav-build.exit' }

$msysSource = $SourcePath.Replace('\', '/') -replace '^([A-Za-z]):', '/$1'
$msysSource = $msysSource.Substring(0, 2).ToLowerInvariant() + $msysSource.Substring(2)
$msysSource = $msysSource.Replace("'", "'\''")

$command = if ($FullBuild) {
    "cd '$msysSource' && ./extras/package/win32/build.sh -p -z"
}
else {
    "cd '$msysSource/win64/modules' && make -B -j4 libdvdnav_plugin.la"
}

& $bash -lc $command *>&1 |
    Tee-Object -FilePath $LogPath
$exitCode = $LASTEXITCODE
Set-Content -LiteralPath $ExitCodePath -Value $exitCode
if ($exitCode -eq 0) {
    $built = Join-Path $SourcePath 'win64\modules\.libs\libdvdnav_plugin.dll'
    $destination = Join-Path $vlcPath 'plugins\access\libdvdnav_plugin.dll'
    if (-not (Test-Path -LiteralPath $built -PathType Leaf)) {
        throw "Plugin build passed but the DLL is missing: $built"
    }
    if (-not (Test-Path -LiteralPath (Split-Path -Parent $destination) -PathType Container)) {
        throw "Private VLC plugin directory is missing: $(Split-Path -Parent $destination)"
    }
    Copy-Item -LiteralPath $built -Destination $destination -Force
    Get-FileHash -LiteralPath $destination -Algorithm SHA256
}
exit $exitCode
