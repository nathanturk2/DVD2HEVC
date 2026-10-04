[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SourcePath,
    [Parameter(Mandatory = $true)]
    [string]$BaseVlcRoot,
    [string]$Destination,
    [switch]$UpdateExisting
)

$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSHOME "Modules\Microsoft.PowerShell.Utility") -Force
$projectRoot = Split-Path -Parent $PSScriptRoot
$source = [IO.Path]::GetFullPath($SourcePath)
$base = [IO.Path]::GetFullPath($BaseVlcRoot)
$destinationPath = if ($Destination) {
    [IO.Path]::GetFullPath($Destination)
}
else {
    $state = if ($env:DVD2HEVC_STATE_DIR) { $env:DVD2HEVC_STATE_DIR } else { Join-Path $env:LOCALAPPDATA "DVD2HEVC" }
    Join-Path $state "vlc-dvdhevc"
}
$patch = Join-Path $projectRoot "patches\vlc\0001-dvdnav-accept-hevc-program-stream-maps.patch"
$rebuilder = Join-Path $PSScriptRoot "rebuild-vlc-dvdnav.ps1"

if (-not (Test-Path -LiteralPath (Join-Path $source ".git"))) {
    throw "VLC source checkout was not found: $source"
}
if (-not (Test-Path -LiteralPath (Join-Path $base "vlc.exe") -PathType Leaf)) {
    throw "Base VLC binary distribution was not found: $base"
}
if (-not (Test-Path -LiteralPath $patch -PathType Leaf)) {
    throw "DVD2HEVC VLC patch was not found: $patch"
}
$version = (Get-Item -LiteralPath (Join-Path $base "vlc.exe")).VersionInfo.FileVersion
if ($version -notlike "3.0.23*") {
    throw "DVD2HEVC currently pins VLC 3.0.23; the supplied base is $version"
}

$sourceCommit = (& git -C $source rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw "Could not identify the VLC source revision" }
$expectedCommit = "578d28f6c9f2379164516e689418f92ac74a3445"
if ($sourceCommit -ne $expectedCommit) {
    throw "Expected VLC 3.0.23 commit $expectedCommit; found $sourceCommit"
}

try {
    $ErrorActionPreference = "Continue"
    & git -C $source apply --reverse --check $patch 2>$null
    $alreadyPatched = $LASTEXITCODE -eq 0
} finally {
    $ErrorActionPreference = "Stop"
}
if (-not $alreadyPatched) {
    $trackedChanges = @(& git -C $source diff --name-only)
    if ($LASTEXITCODE -ne 0) { throw "Could not inspect the VLC source checkout" }
    if ($trackedChanges.Count -ne 0) {
        throw "The VLC checkout has tracked local changes. Use a clean pinned checkout before applying the DVD2HEVC patch."
    }
    & git -C $source apply --check $patch
    if ($LASTEXITCODE -ne 0) { throw "The DVD2HEVC VLC patch does not apply cleanly" }
    & git -C $source apply $patch
    if ($LASTEXITCODE -ne 0) { throw "Applying the DVD2HEVC VLC patch failed" }
}
$trackedChanges = @(& git -C $source diff --name-only)
if ($LASTEXITCODE -ne 0) { throw "Could not verify the patched VLC source checkout" }
if ($trackedChanges.Count -ne 1 -or $trackedChanges[0].Replace("\", "/") -ne "modules/access/dvdnav.c") {
    throw "The VLC checkout contains tracked changes beyond the DVD2HEVC dvdnav patch."
}

$destinationParent = Split-Path -Parent $destinationPath
$destinationName = Split-Path -Leaf $destinationPath
if (-not $destinationParent -or -not $destinationName) {
    throw "The private VLC destination is not a safe directory path: $destinationPath"
}
if ([StringComparer]::OrdinalIgnoreCase.Equals(
        $destinationPath.TrimEnd('\'), $base.TrimEnd('\'))) {
    throw "The private VLC destination must not be the normal VLC installation."
}
if ((Test-Path -LiteralPath $destinationPath) -and -not $UpdateExisting) {
    throw "Destination already exists; pass -UpdateExisting to refresh it: $destinationPath"
}
if (-not (Test-Path -LiteralPath $destinationParent -PathType Container)) {
    New-Item -ItemType Directory -Path $destinationParent | Out-Null
}

# Build into a sibling staging directory. The existing verified player is only
# swapped out after the copy, build, and manifest have all succeeded.
$stagingPath = Join-Path $destinationParent (".{0}.prepare-{1}" -f $destinationName, $PID)
$backupPath = Join-Path $destinationParent (".{0}.previous-{1}" -f $destinationName, $PID)
foreach ($temporaryPath in @($stagingPath, $backupPath)) {
    $resolvedParent = [IO.Path]::GetFullPath((Split-Path -Parent $temporaryPath))
    if (-not [StringComparer]::OrdinalIgnoreCase.Equals($resolvedParent.TrimEnd('\'), $destinationParent.TrimEnd('\'))) {
        throw "Refusing to clean a temporary path outside the destination directory: $temporaryPath"
    }
    if (Test-Path -LiteralPath $temporaryPath) {
        Remove-Item -LiteralPath $temporaryPath -Recurse -Force
    }
}

try {
    New-Item -ItemType Directory -Path $stagingPath | Out-Null
    Get-ChildItem -LiteralPath $base -Force | Copy-Item -Destination $stagingPath -Recurse -Force

    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $rebuilder `
        -SourcePath $source -VlcRoot $stagingPath
    if ($LASTEXITCODE -ne 0) { throw "The patched DVDNAV plugin build failed" }

    $plugin = Join-Path $stagingPath "plugins\access\libdvdnav_plugin.dll"
    if (-not (Test-Path -LiteralPath $plugin -PathType Leaf)) {
        throw "The patched DVDNAV plugin was not produced."
    }
    $manifest = [ordered]@{
        schema = "dvd2hevc-vlc-build-v1"
        vlc_version = $version
        player_abi = "dvd2hevc-vlc3-dvdnav-hevc-v1"
        vlc_source_commit = $sourceCommit
        patch = "patches/vlc/0001-dvdnav-accept-hevc-program-stream-maps.patch"
        patch_sha256 = (Get-FileHash -LiteralPath $patch -Algorithm SHA256).Hash
        plugin_sha256 = (Get-FileHash -LiteralPath $plugin -Algorithm SHA256).Hash
        prepared_at = (Get-Date).ToUniversalTime().ToString("o")
    }
    $manifest | ConvertTo-Json | Set-Content -LiteralPath `
        (Join-Path $stagingPath "DVD2HEVC-VLC-BUILD.json") -Encoding UTF8

    $movedPrevious = $false
    try {
        if (Test-Path -LiteralPath $destinationPath) {
            Move-Item -LiteralPath $destinationPath -Destination $backupPath
            $movedPrevious = $true
        }
        Move-Item -LiteralPath $stagingPath -Destination $destinationPath
    }
    catch {
        if ($movedPrevious -and -not (Test-Path -LiteralPath $destinationPath)) {
            Move-Item -LiteralPath $backupPath -Destination $destinationPath
        }
        throw
    }
    if (Test-Path -LiteralPath $backupPath) {
        Remove-Item -LiteralPath $backupPath -Recurse -Force
    }
}
catch {
    if (Test-Path -LiteralPath $stagingPath) {
        Remove-Item -LiteralPath $stagingPath -Recurse -Force
    }
    throw
}

Write-Output "Prepared private DVD2HEVC VLC: $destinationPath"
Write-Output "Source commit: $sourceCommit"
Write-Output "Plugin SHA-256: $($manifest.plugin_sha256)"
