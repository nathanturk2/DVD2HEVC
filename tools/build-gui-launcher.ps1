[CmdletBinding()]
param(
    [string]$Destination
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$source = Join-Path $PSScriptRoot "DVD2HEVCLauncher.cs"
$icon = Join-Path $projectRoot "assets\DVD2HEVC.ico"
if (-not $Destination) { $Destination = Join-Path $projectRoot "DVD2HEVC.exe" }
$destinationPath = [IO.Path]::GetFullPath($Destination)
if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "Launcher source is missing: $source" }
if (-not (Test-Path -LiteralPath $icon -PathType Leaf)) { throw "Launcher icon is missing: $icon" }

$temporary = Join-Path ([IO.Path]::GetTempPath()) ("DVD2HEVC-launcher-" + [guid]::NewGuid() + ".exe")
try {
    $compilerCandidates = @(
        (Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"),
        (Join-Path $env:WINDIR "Microsoft.NET\Framework\v4.0.30319\csc.exe")
    )
    $compiler = $compilerCandidates | Where-Object {
        Test-Path -LiteralPath $_ -PathType Leaf
    } | Select-Object -First 1
    if (-not $compiler) { throw "The Windows .NET C# compiler (csc.exe) was not found" }
    & $compiler /nologo /target:winexe /optimize+ "/win32icon:$icon" `
        /reference:System.Windows.Forms.dll "/out:$temporary" $source
    if ($LASTEXITCODE -ne 0) { throw "The C# compiler failed with exit code $LASTEXITCODE" }
    if (-not (Test-Path -LiteralPath $temporary -PathType Leaf)) {
        throw "The C# compiler did not produce the DVD2HEVC launcher"
    }
    New-Item -ItemType Directory -Path (Split-Path -Parent $destinationPath) -Force | Out-Null
    if (Test-Path -LiteralPath $destinationPath) {
        $backup = "$destinationPath.previous"
        [IO.File]::Replace($temporary, $destinationPath, $backup, $true)
        Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
    }
    else {
        Move-Item -LiteralPath $temporary -Destination $destinationPath
    }
}
finally {
    Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
}
Get-Item -LiteralPath $destinationPath | Select-Object FullName, Length, LastWriteTime
