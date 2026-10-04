[CmdletBinding()]
param(
    [string]$Destination = [Environment]::GetFolderPath("Desktop")
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$entry = Join-Path $projectRoot "dvd2hevc.py"
$icon = Join-Path $projectRoot "assets\DVD2HEVC.ico"
$launcher = Join-Path $projectRoot "DVD2HEVC.exe"
$launcherSource = Join-Path $PSScriptRoot "DVD2HEVCLauncher.cs"
$launcherBuild = Join-Path $PSScriptRoot "build-gui-launcher.ps1"
if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) { throw "DVD2HEVC entry point is missing: $entry" }
if (-not (Test-Path -LiteralPath $icon -PathType Leaf)) { throw "DVD2HEVC icon is missing: $icon" }
if (
    -not (Test-Path -LiteralPath $launcher -PathType Leaf) -or
    (Get-Item -LiteralPath $launcherSource).LastWriteTimeUtc -gt (Get-Item -LiteralPath $launcher).LastWriteTimeUtc -or
    (Get-Item -LiteralPath $icon).LastWriteTimeUtc -gt (Get-Item -LiteralPath $launcher).LastWriteTimeUtc
) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $launcherBuild -Destination $launcher
    if ($LASTEXITCODE -ne 0) { throw "DVD2HEVC launcher build failed" }
}
New-Item -ItemType Directory -Path $Destination -Force | Out-Null
$shortcutPath = Join-Path $Destination "DVD2HEVC.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $launcher
$shortcut.Arguments = ""
$shortcut.WorkingDirectory = $projectRoot
$shortcut.IconLocation = "$icon,0"
$shortcut.Description = "Convert complete DVD backups to compact HEVC while preserving menus"
$shortcut.Save()
Write-Output $shortcutPath
