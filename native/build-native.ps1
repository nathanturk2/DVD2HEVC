param(
    [switch]$Clean,
    [string]$WorkRoot,
    [string]$Destination
)

$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSHOME "Modules\Microsoft.PowerShell.Utility") -Force
$root = Split-Path -Parent $PSScriptRoot
$state = if ($env:DVD2HEVC_STATE_DIR) { $env:DVD2HEVC_STATE_DIR } else { Join-Path $env:LOCALAPPDATA "DVD2HEVC" }
if (-not $WorkRoot) { $WorkRoot = Join-Path $state "native-build" }
if (-not $Destination) { $Destination = Join-Path $state "tools" }
$WorkRoot = [IO.Path]::GetFullPath($WorkRoot)
$Destination = [IO.Path]::GetFullPath($Destination)
$deps = Join-Path $WorkRoot "deps\libdvdread"
$build = Join-Path $WorkRoot "build\libdvdread"
$tools = $Destination
$probeBuild = Join-Path $WorkRoot "build\dvdinspect"
$commit = "980143e2192aded576c54a60dd230fa22937b637"
$repository = "https://code.videolan.org/videolan/libdvdread.git"
$patch = Join-Path $root "patches\libdvdread\0001-modern-msvc-stdio-and-close.patch"
$vcvars = "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"

if ($Clean) {
    $resolvedBuild = [IO.Path]::GetFullPath($build)
    if (-not $resolvedBuild.StartsWith($WorkRoot.TrimEnd("\") + "\", [StringComparison]::OrdinalIgnoreCase)) { throw "Unsafe build cleanup path" }
    Remove-Item -Recurse -Force -LiteralPath $resolvedBuild -ErrorAction SilentlyContinue
}
if (-not (Test-Path (Join-Path $deps ".git"))) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $deps) | Out-Null
    git clone $repository $deps
    if ($LASTEXITCODE -ne 0) { throw "Could not clone the pinned libdvdread source" }
    git -C $deps checkout $commit
    if ($LASTEXITCODE -ne 0) { throw "Could not check out the pinned libdvdread revision" }
}
$actualCommit = (& git -C $deps rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $actualCommit -ne $commit) { throw "Expected libdvdread revision $commit; found $actualCommit" }

$alreadyPatched = $false
try {
    # A failed reverse check means the clean source still needs the patch.
    $ErrorActionPreference = "Continue"
    git -C $deps apply --reverse --check $patch 2>$null
    $alreadyPatched = $LASTEXITCODE -eq 0
} finally {
    $ErrorActionPreference = "Stop"
}
if (-not $alreadyPatched) {
    git -C $deps apply --check $patch
    if ($LASTEXITCODE -ne 0) { throw "libdvdread compatibility patch does not apply cleanly" }
    git -C $deps apply $patch
}

if (-not (Test-Path $vcvars)) {
    $vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path $vswhere) {
        $installation = (& $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath | Select-Object -First 1)
        if ($installation) { $vcvars = Join-Path $installation "VC\Auxiliary\Build\vcvars64.bat" }
    }
    if (-not (Test-Path $vcvars)) { throw "Visual Studio C++ Build Tools were not found" }
}
python -c "import mesonbuild" 2>$null
if ($LASTEXITCODE -ne 0) { throw "Meson is required: python -m pip install --user meson" }

if (-not (Test-Path (Join-Path $build "build.ninja"))) {
    $setup = "call `"$vcvars`" >nul && python -m mesonbuild.mesonmain setup `"$build`" `"$deps`" --default-library=static -Dlibdvdcss=disabled"
    & cmd.exe /d /s /c $setup
    if ($LASTEXITCODE -ne 0) { throw "libdvdread Meson setup failed" }
}

$compileLibrary = "call `"$vcvars`" >nul && python -m mesonbuild.mesonmain compile -C `"$build`" dvdread"
& cmd.exe /d /s /c $compileLibrary
if ($LASTEXITCODE -ne 0) { throw "libdvdread build failed" }

New-Item -ItemType Directory -Force -Path $tools | Out-Null
New-Item -ItemType Directory -Force -Path $probeBuild | Out-Null
$main = Join-Path $root "native\dvdinspect\main.c"
$dlfcn = Join-Path $root "native\dvdinspect\dlfcn_win32.c"
$library = Join-Path $build "src\libdvdread.a"
$includeSource = Join-Path $deps "src"
$includeBuild = Join-Path $build "src"
$output = Join-Path $tools "dvdinspect.exe"
$compileProbe = "call `"$vcvars`" >nul && cd /d `"$probeBuild`" && cl /nologo /W4 /O2 /MD /I`"$includeSource`" /I`"$includeBuild`" `"$main`" `"$dlfcn`" `"$library`" /Fe:`"$output`""
& cmd.exe /d /s /c $compileProbe
if ($LASTEXITCODE -ne 0) { throw "dvdinspect build failed" }

Write-Output "Built $output"
[ordered]@{ schema="dvd2hevc-native-build-v1"; upstream_commit=$commit; patch_sha256=(Get-FileHash -LiteralPath $patch -Algorithm SHA256).Hash; inspector_source_sha256=(Get-FileHash -LiteralPath $main -Algorithm SHA256).Hash; platform_adapter_sha256=(Get-FileHash -LiteralPath $dlfcn -Algorithm SHA256).Hash; helper_sha256=(Get-FileHash -LiteralPath $output -Algorithm SHA256).Hash; compiler_flags="MSVC x64 /W4 /O2 /MD; static libdvdread; libdvdcss disabled" } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $tools "DVD2HEVC-NATIVE-BUILD.json") -Encoding UTF8
