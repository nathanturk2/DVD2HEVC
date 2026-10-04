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
    [ValidateSet("auto", "progressive", "deinterlace50")]
    [string]$Cadence = "auto",
    [ValidateSet("fail", "progressive", "deinterlace50")]
    [string]$AmbiguousCadence = "deinterlace50",
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$cli = Join-Path $projectRoot "dvd2hevc.py"
$planPath = [IO.Path]::GetFullPath($Plan)
$workPath = [IO.Path]::GetFullPath($WorkRoot)
$logPath = Join-Path $workPath "run.log"

New-Item -ItemType Directory -Path $workPath -Force | Out-Null
"DVD2HEVC Phase 7 VTS run started $((Get-Date).ToString('o'))" | `
    Set-Content -LiteralPath $logPath -Encoding UTF8

& $Python -u $cli @(
    "convert-interleaved-vts", $planPath, $workPath,
    "--quality-values", $QualityValues,
    "--encoder", $Encoder,
    "--preset", $Preset,
    "--cadence", $Cadence,
    "--ambiguous-cadence", $AmbiguousCadence,
    "--allow-compact-expansion"
) 2>&1 | Tee-Object -FilePath $logPath -Append
$code = $LASTEXITCODE
if ($code -eq 0) {
    "[$((Get-Date).ToString('o'))] PASS" | Tee-Object -FilePath $logPath -Append
}
else {
    "[$((Get-Date).ToString('o'))] FAILED - exit $code" | Tee-Object -FilePath $logPath -Append
}
exit $code
