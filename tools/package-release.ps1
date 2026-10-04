[CmdletBinding()]
param([string]$Destination, [switch]$SkipTests)
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    if (-not $SkipTests) {
        & python -m unittest discover -s tests
        if ($LASTEXITCODE -ne 0) { throw "Unit tests failed" }
    }
    $outputDirectory = if ($Destination) { Split-Path -Parent ([IO.Path]::GetFullPath($Destination)) } else { Join-Path $projectRoot "dist" }
    & python (Join-Path $PSScriptRoot "build-release.py") --output-dir $outputDirectory
    if ($LASTEXITCODE -ne 0) { throw "Release build failed" }
    if ($Destination) {
        $version = (& python -c "from dvd2hevc_app import __version__; print(__version__)").Trim()
        $built = Join-Path $outputDirectory "DVD2HEVC-$version-source.zip"
        if ([IO.Path]::GetFullPath($built) -ne [IO.Path]::GetFullPath($Destination)) { Copy-Item -LiteralPath $built -Destination $Destination -Force }
    }
}
finally { Pop-Location }
