[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Source,
    [Parameter(Mandatory = $true)][string]$Output,
    [ValidateRange(1, 20)][int]$Activations = 1,
    [ValidateRange(0, 36)][int]$Button = 0,
    [string]$Buttons = "",
    [string]$Distro = "Ubuntu-24.04",
    [string]$Workspace = "work\dvdnav-menu",
    [switch]$CompactRelocation
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$workspacePath = [IO.Path]::GetFullPath((Join-Path $root $Workspace))
New-Item -ItemType Directory -Force $workspacePath | Out-Null
$sourceCode = [IO.Path]::GetFullPath((Join-Path $root "native\dvdnavtrace\main.c"))
$binary = Join-Path $workspacePath "dvdnavtrace"

function Convert-ToWslPath([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    $portable = $full.Replace("\", "/")
    $value = & wsl.exe -d $Distro -- wslpath -a $portable
    if ($LASTEXITCODE -ne 0) { throw "wslpath failed for $full" }
    return $value.Trim()
}

$linuxSourceCode = Convert-ToWslPath $sourceCode
$linuxBinary = Convert-ToWslPath $binary
& wsl.exe -d $Distro -- gcc -std=c11 -Wall -Wextra -O2 -o $linuxBinary $linuxSourceCode -ldvdnav -ldvdread
if ($LASTEXITCODE -ne 0) { throw "dvdnavtrace build failed" }

$sourcePath = Convert-ToWslPath $Source
$outputPath = Convert-ToWslPath $Output
$buttonPlan = if ($Buttons) { $Buttons } else { [string]$Button }
$sourceAll = @(& wsl.exe -d $Distro -- $linuxBinary $sourcePath $Activations $buttonPlan 2>&1 | ForEach-Object { $_.ToString() })
$sourceExit = $LASTEXITCODE
$outputAll = @()
if ($sourceExit -eq 0) {
    $outputAll = @(& wsl.exe -d $Distro -- $linuxBinary $outputPath $Activations $buttonPlan 2>&1 | ForEach-Object { $_.ToString() })
}
$outputExit = $LASTEXITCODE
$sourceTrace = @($sourceAll | Where-Object { $_ -like "TRACE *" })
$outputTrace = @($outputAll | Where-Object { $_ -like "TRACE *" })

function Normalize-Trace([string]$Line) {
    if (-not $CompactRelocation) { return $Line }
    # These values count physical sectors/read iterations and necessarily
    # change when compact VOBUs are relocated. Keep every command, domain,
    # cell/program number, duration, stream, highlight, and activation field.
    return $Line `
        -replace ' cell_length=[0-9]+', ' cell_length=<relocated>' `
        -replace ' program_length=[0-9]+', ' program_length=<relocated>' `
        -replace ' calls=[0-9]+', ' calls=<relocated>'
}

$sourceCompared = @($sourceTrace | ForEach-Object { Normalize-Trace $_ })
$outputCompared = @($outputTrace | ForEach-Object { Normalize-Trace $_ })
[IO.File]::WriteAllLines((Join-Path $workspacePath "source.log"), [string[]]$sourceAll)
[IO.File]::WriteAllLines((Join-Path $workspacePath "output.log"), [string[]]$outputAll)
[IO.File]::WriteAllLines((Join-Path $workspacePath "source.trace"), [string[]]$sourceTrace)
[IO.File]::WriteAllLines((Join-Path $workspacePath "output.trace"), [string[]]$outputTrace)
if ($sourceExit -ne 0) { throw "Source dvdnavtrace failed; see source.log" }
if ($outputExit -ne 0) { throw "Output dvdnavtrace failed; see output.log" }

$equal = $sourceCompared.Count -eq $outputCompared.Count
if ($equal) {
    for ($index = 0; $index -lt $sourceCompared.Count; $index++) {
        if ($sourceCompared[$index] -cne $outputCompared[$index]) { $equal = $false; break }
    }
}
Write-Host "libdvdnav active-menu trace: $(if ($equal) {'PASS'} else {'FAIL'})"
Write-Host "  source lines=$($sourceTrace.Count) output lines=$($outputTrace.Count) activations=$Activations buttons=$buttonPlan compact-relocation=$CompactRelocation"
Write-Host "  workspace=$workspacePath"
if (-not $equal) { exit 2 }
