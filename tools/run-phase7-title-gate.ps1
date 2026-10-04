[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Plan,

    [string]$WorkRoot = "work\phase7\title-gate",
    [int]$Title = 0,
    [string]$QualityValues = "cq:27",
    [ValidateSet("hevc_nvenc", "hevc_qsv", "hevc_amf", "libx265")]
    [string]$Encoder = "hevc_nvenc",
    [string]$Preset = "p6",
    [ValidateSet("auto", "interlaced", "progressive", "deinterlace50")]
    [string]$Cadence = "auto",
    [ValidateSet("fail", "progressive", "deinterlace50")]
    [string]$AmbiguousCadence = "deinterlace50",
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$cli = Join-Path $projectRoot "dvd2hevc.py"
. (Join-Path $PSScriptRoot "write-log.ps1")
$planPath = [IO.Path]::GetFullPath($Plan)
$workPath = [IO.Path]::GetFullPath($WorkRoot)
$statusPath = Join-Path $workPath "status.json"
$logPath = Join-Path $workPath "run.log"
$started = (Get-Date).ToString("o")

New-Item -ItemType Directory -Path $workPath -Force | Out-Null

function Write-RunStatus {
    param(
        [string]$State,
        [string]$Step,
        [string]$Message,
        [string]$Output = ""
    )
    $value = [ordered]@{
        schema = "dvd2hevc-phase7-gate-status-v0"
        state = $State
        step = $Step
        message = $Message
        plan = $planPath
        source = if ($script:sourcePath) { $script:sourcePath } else { "" }
        title = $script:selectedTitle
        vts = $script:selectedVts
        work_root = $workPath
        output = $Output
        started = $started
        updated = (Get-Date).ToString("o")
        process_id = $PID
    }
    $temporary = "$statusPath.$PID.tmp"
    $value | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Dvd2HevcAtomicFile -TemporaryPath $temporary -DestinationPath $statusPath
}

try {
    if (-not (Test-Path -LiteralPath $planPath -PathType Leaf)) {
        throw "Compatibility plan is missing: $planPath"
    }
    $planValue = Get-Content -LiteralPath $planPath -Raw | ConvertFrom-Json
    if ([string]$planValue.schema -ne "dvd2hevc-compatibility-plan-v0") {
        throw "Unsupported compatibility-plan schema: $($planValue.schema)"
    }
    $script:sourcePath = [IO.Path]::GetFullPath([string]$planValue.source)
    if (-not (Test-Path -LiteralPath $script:sourcePath -PathType Leaf)) {
        throw "Source ISO from compatibility plan is missing: $script:sourcePath"
    }

    if ($Title -gt 0) {
        $task = @($planValue.title_tasks | Where-Object { [int]$_.title -eq $Title })
        if ($task.Count -ne 1) {
            throw "Title $Title is not uniquely present in the compatibility plan"
        }
        $task = $task[0]
    }
    else {
        $task = $planValue.recommended_first_gate
        if ($null -eq $task) {
            throw "Compatibility plan has no safe recommended title gate"
        }
    }

    $script:selectedTitle = [int]$task.title
    $script:selectedVts = [int]$task.vts
    if ([string]$task.status -ne "ready") {
        $reasons = @($task.blockers) -join ", "
        throw "Title $script:selectedTitle is blocked before encoding: $reasons"
    }
    if ($Encoder -ne "libx265" -and $Cadence -eq "interlaced") {
        throw "$Encoder cannot emit DVD2HEVC interlaced HEVC; use auto, progressive, or deinterlace50"
    }

    $workspace = Join-Path $workPath ([string]$task.workspace)
    $reportCopy = Join-Path $workPath ([string]$task.report_copy)
    New-Item -ItemType Directory -Path $workspace -Force | Out-Null
    New-Item -ItemType Directory -Path (Split-Path -Parent $reportCopy) -Force | Out-Null
    [IO.File]::WriteAllText(
        $logPath,
        "DVD2HEVC Phase 7 title gate started $started`r`n" +
        "Source: $script:sourcePath`r`nTitle: $script:selectedTitle`r`nVTS: $script:selectedVts`r`n"
    )

    Write-RunStatus -State "running" -Step "convert-title" `
        -Message "Encoding and validating safe title $script:selectedTitle with $Encoder"
    & $Python $cli @(
        "convert-title", $script:sourcePath, [string]$script:selectedTitle, $workspace,
        "--encoder", $Encoder,
        "--preset", $Preset,
        "--quality-values", $QualityValues,
        "--cadence", $Cadence,
        "--ambiguous-cadence", $AmbiguousCadence
    ) 2>&1 | Tee-Object -FilePath $logPath -Append
    if ($LASTEXITCODE -ne 0) {
        throw "Title gate failed with exit code $LASTEXITCODE"
    }

    $workspaceReport = Join-Path $workspace "title-report.json"
    if (-not (Test-Path -LiteralPath $workspaceReport -PathType Leaf)) {
        throw "Conversion completed without a title report: $workspaceReport"
    }
    $reportValue = Get-Content -LiteralPath $workspaceReport -Raw | ConvertFrom-Json
    if ([string]$reportValue.status -ne "passed") {
        throw "Title report did not pass: $workspaceReport"
    }
    if ([int]$reportValue.title.title -ne $script:selectedTitle -or [int]$reportValue.vts -ne $script:selectedVts) {
        throw "Title report identity does not match the selected compatibility-plan task"
    }
    Copy-Item -LiteralPath $workspaceReport -Destination $reportCopy -Force

    Write-RunStatus -State "passed" -Step "complete" `
        -Message "Phase 7 title gate passed" -Output $reportCopy
    "[$((Get-Date).ToString('o'))] PASS - $reportCopy" | Tee-Object -FilePath $logPath -Append
    exit 0
}
catch {
    $message = $_.Exception.Message
    Write-RunStatus -State "failed" -Step "failed" -Message $message
    "[$((Get-Date).ToString('o'))] FAILED - $message`r`n$($_ | Out-String)" | `
        Tee-Object -FilePath $logPath -Append
    exit 1
}
