[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Plan,

    [Parameter(Mandatory = $true)]
    [string]$WorkRoot,

    [int[]]$SkipVts = @(),
    [string]$SkipVtsCsv = "",
    [string]$QualityValues = "cq:27",
    [string]$QualityPolicy = "",
    [ValidateSet("hevc_nvenc", "hevc_qsv", "hevc_amf", "libx265")]
    [string]$Encoder = "hevc_nvenc",
    [string]$Preset = "p6",
    [ValidateSet("auto", "progressive", "deinterlace50")]
    [string]$Cadence = "auto",
    [ValidateSet("fail", "progressive", "deinterlace50")]
    [string]$AmbiguousCadence = "deinterlace50",
    [string]$AudioPrefetchRoot = "",
    [string]$AudioPolicy = "",
    [string]$StereoAudioBitrate = "256000",
    [string]$MonoAudioBitrate = "128000",
    [ValidateRange(1, 8)]
    [int]$AudioWorkers = 2,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$env:DVD2HEVC_PROGRESS_STDOUT = "1"
$projectRoot = Split-Path -Parent $PSScriptRoot
$cli = Join-Path $projectRoot "dvd2hevc.py"
. (Join-Path $PSScriptRoot "write-log.ps1")
$planPath = [IO.Path]::GetFullPath($Plan)
$workPath = [IO.Path]::GetFullPath($WorkRoot)
$statusPath = Join-Path $workPath "status.json"
$logPath = Join-Path $workPath "run.log"
$cancelPath = Join-Path (Split-Path -Parent $workPath) "cancel.requested"
$started = (Get-Date).ToString("o")
$script:qualityPolicyDocument = if ($QualityPolicy) {
    Get-Content -LiteralPath ([IO.Path]::GetFullPath($QualityPolicy)) -Raw | ConvertFrom-Json
} else { $null }
$script:audioPolicyDocument = if ($AudioPolicy) {
    Get-Content -LiteralPath ([IO.Path]::GetFullPath($AudioPolicy)) -Raw | ConvertFrom-Json
} else { $null }

New-Item -ItemType Directory -Path $workPath -Force | Out-Null

function Write-Utf8Log {
    param([Parameter(ValueFromPipeline = $true)]$InputObject)
    process {
        $line = if ($null -eq $InputObject) { "" } else { [string]$InputObject }
        Add-Dvd2HevcLogLine -Path $logPath -Line $line
        Write-Output $InputObject
    }
}

function Write-Status {
    param([string]$State, [string]$Step, [string]$Message)
    $value = [ordered]@{
        schema = "dvd2hevc-phase7-remaining-status-v0"
        state = $State
        step = $Step
        message = $Message
        plan = $planPath
        source = $script:sourcePath
        work_root = $workPath
        encoder = $Encoder
        encoder_preset = $Preset
        quality_values = $QualityValues
        cadence = $Cadence
        ambiguous_cadence = $AmbiguousCadence
        title_reports = @($script:titleReports)
        combined_reports = @($script:combinedReports)
        menu_reports = @($script:menuReports)
        started = $started
        updated = (Get-Date).ToString("o")
        process_id = $PID
        completed_video_tasks = $script:completedVideoTasks
        total_video_tasks = $script:totalVideoTasks
    }
    $temporary = "$statusPath.$PID.tmp"
    $value | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Dvd2HevcAtomicFile -TemporaryPath $temporary -DestinationPath $statusPath
}

function Assert-NotCanceled {
    if (Test-Path -LiteralPath $cancelPath -PathType Leaf) {
        throw "DVD2HEVC cancellation requested; stopping at a safe task boundary"
    }
}

function Invoke-Dvd2Hevc {
    param([string]$Step, [string[]]$Arguments)
    Assert-NotCanceled
    $lane = if ($Step -match "^convert-") { "video" } else { "mux" }
    Write-Status -State "running" -Step $Step -Message ($Arguments -join " ")
    ("DVD2HEVC_PROGRESS " + (@{
        timestamp = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
        lane = $lane; event = "start"; task = $Step
    } | ConvertTo-Json -Compress)) | Write-Utf8Log
    "[$((Get-Date).ToString('o'))] $Step" | Write-Utf8Log
    $previousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $Python -u $cli @Arguments 2>&1 | Write-Utf8Log
        $nativeExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorAction
    }
    if ($nativeExitCode -ne 0) {
        throw "$Step failed with exit code $nativeExitCode"
    }
    Assert-NotCanceled
    ("DVD2HEVC_PROGRESS " + (@{
        timestamp = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
        lane = $lane; event = "done"; task = $Step
    } | ConvertTo-Json -Compress)) | Write-Utf8Log
}

function Test-PassedReport {
    param(
        [string]$Path,
        [string]$Schema,
        [string]$ExpectedAmbiguousCadence = $AmbiguousCadence,
        [string]$ExpectedQualitySetting = ""
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    try {
        $value = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
        $expectedSetting = if ($ExpectedQualitySetting) {
            $ExpectedQualitySetting
        } else { Get-VtsQuality ([int]$value.vts) }
        $expectedQuality = (@($expectedSetting -split ",") | ForEach-Object {
            ([string]$_).Trim().ToLowerInvariant() -replace '^cq:', ''
        }) -join ","
        $actualQuality = (@($value.settings.quality_values) | ForEach-Object {
            [string]$_
        }) -join ","
        $nonCompactManagedTitle = (
            [string]$value.schema -eq "dvd2hevc-title-conversion-v0" -and
            (
                [string]$value.settings.intermediate_policy -ne "compact-input-v1" -or
                @($value.cells | Where-Object {
                    [string]$_.layout_mode -ne "compact-input"
                }).Count -gt 0
            )
        )
        return (
            [string]$value.status -eq "passed" -and
            [string]$value.schema -eq $Schema -and
            [string]$value.settings.encoder -eq $Encoder -and
            [string]$value.settings.preset -eq $Preset -and
            [string]$value.settings.cadence -eq $Cadence -and
            [string]$value.settings.ambiguous_cadence -eq $ExpectedAmbiguousCadence -and
            $actualQuality -eq $expectedQuality -and
            -not $nonCompactManagedTitle
        )
    }
    catch { return $false }
}

function Get-VtsQuality {
    param([int]$Vts)
    if ($null -ne $script:qualityPolicyDocument -and $Vts -gt 0) {
        $qualityMap = $script:qualityPolicyDocument.quality_by_vts
        if ($null -eq $qualityMap) {
            throw "Quality policy is missing its per-VTS map: $QualityPolicy"
        }
        $property = $qualityMap.PSObject.Properties[[string]$Vts]
        if ($null -eq $property) {
            throw "Quality policy has no VTS $Vts entry: $QualityPolicy"
        }
        return [string]$property.Value.resolved
    }
    return $QualityValues
}

$script:titleReports = @()
$script:combinedReports = @()
$script:menuReports = @()
$script:completedVideoTasks = 0
$script:totalVideoTasks = 0
$effectiveSkipVts = @($SkipVts)
if ($SkipVtsCsv) {
    $effectiveSkipVts += @($SkipVtsCsv.Split(",", [StringSplitOptions]::RemoveEmptyEntries) |
        ForEach-Object { [int]$_.Trim() })
}
$effectiveSkipVts = @($effectiveSkipVts | Sort-Object -Unique)

try {
    $planValue = Get-Content -LiteralPath $planPath -Raw | ConvertFrom-Json
    if ([string]$planValue.schema -ne "dvd2hevc-compatibility-plan-v0") {
        throw "Unsupported compatibility plan: $($planValue.schema)"
    }
    $script:sourcePath = [IO.Path]::GetFullPath([string]$planValue.source)
    if (-not (Test-Path -LiteralPath $script:sourcePath -PathType Leaf)) {
        throw "Source ISO is missing: $script:sourcePath"
    }
    "DVD2HEVC Phase 7 remaining-domain conversion started $started`r`nSource: $script:sourcePath" |
        Set-Content -LiteralPath $logPath -Encoding UTF8

    $tasks = @($planValue.title_tasks | Where-Object {
        [string]$_.status -eq "ready" -and $effectiveSkipVts -notcontains [int]$_.vts
    })
    $script:totalVideoTasks = $tasks.Count + @($planValue.menu_tasks).Count
    foreach ($task in $tasks) {
        Assert-NotCanceled
        $title = [int]$task.title
        $vts = [int]$task.vts
        $workspace = Join-Path $workPath ([string]$task.workspace)
        $reportCopy = Join-Path $workPath ([string]$task.report_copy)
        New-Item -ItemType Directory -Path $workspace -Force | Out-Null
        New-Item -ItemType Directory -Path (Split-Path -Parent $reportCopy) -Force | Out-Null
        if (-not (Test-PassedReport $reportCopy "dvd2hevc-title-conversion-v0")) {
            $titleArguments = @(
                "convert-title", $script:sourcePath, [string]$title, $workspace,
                "--encoder", $Encoder, "--preset", $Preset,
                "--quality-values", (Get-VtsQuality $vts),
                "--cadence", $Cadence, "--ambiguous-cadence", $AmbiguousCadence,
                "--allow-compact-expansion", "--prefer-compact-input"
            )
            $audioProcessingRequired = @($script:audioPolicyDocument.title_sets | Where-Object {
                [int]$_.vts -eq $vts -and [bool]$_.processing_required
            }).Count -gt 0
            if ($AudioPrefetchRoot -and $AudioPolicy -and $audioProcessingRequired) {
                $titleArguments += @(
                    "--audio-prefetch-root", $AudioPrefetchRoot,
                    "--audio-policy", $AudioPolicy,
                    "--stereo-audio-bitrate", $StereoAudioBitrate,
                    "--mono-audio-bitrate", $MonoAudioBitrate,
                    "--audio-workers", [string]$AudioWorkers
                )
            }
            Invoke-Dvd2Hevc -Step "convert-title-$title" -Arguments $titleArguments
            $workspaceReport = Join-Path $workspace "title-report.json"
            if (-not (Test-PassedReport $workspaceReport "dvd2hevc-title-conversion-v0")) {
                throw "Title $title did not produce a passed report"
            }
            Copy-Item -LiteralPath $workspaceReport -Destination $reportCopy -Force
        }
        $script:titleReports += $reportCopy
        $script:completedVideoTasks += 1
        ("DVD2HEVC_PROGRESS " + (@{
            timestamp = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
            lane = "video"; event = "progress"; task = "title-$title"
            current = $script:completedVideoTasks; total = $script:totalVideoTasks
        } | ConvertTo-Json -Compress)) | Write-Utf8Log
        Write-Status -State "running" -Step "titles" -Message "Completed title $title (VTS $vts)"
    }

    foreach ($vtsGroup in @($tasks | Group-Object vts)) {
        Assert-NotCanceled
        $vts = [int]$vtsGroup.Name
        $reports = @($vtsGroup.Group | ForEach-Object {
            Join-Path $workPath ([string]$_.report_copy)
        })
        $combined = Join-Path $workPath ("combined\vts{0:d2}-report.json" -f $vts)
        New-Item -ItemType Directory -Path (Split-Path -Parent $combined) -Force | Out-Null
        if (-not (Test-PassedReport $combined "dvd2hevc-vts-conversion-v0")) {
            $combineArguments = @("combine-titles", $combined)
            $combineArguments += $reports
            Invoke-Dvd2Hevc -Step "combine-vts-$vts" -Arguments $combineArguments
        }
        $script:combinedReports += $combined
    }

    foreach ($task in @($planValue.menu_tasks)) {
        Assert-NotCanceled
        $domain = [string]$task.domain
        $vts = [int]$task.vts
        $workspace = Join-Path $workPath ([string]$task.workspace)
        $name = if ($domain -eq "vmg_menu") { "vmg-menu-report.json" } else { "vts{0:d2}-menu-report.json" -f $vts }
        $reportCopy = Join-Path $workPath ("menu-reports\$name")
        New-Item -ItemType Directory -Path $workspace -Force | Out-Null
        New-Item -ItemType Directory -Path (Split-Path -Parent $reportCopy) -Force | Out-Null
        if (-not (Test-PassedReport $reportCopy "dvd2hevc-menu-conversion-v0" "progressive" $QualityValues)) {
            $menuArguments = @(
                "convert-menu", $script:sourcePath, $domain, [string]$vts, $workspace,
                "--encoder", $Encoder, "--preset", $Preset, "--quality-values", $QualityValues,
                "--cadence", $Cadence, "--ambiguous-cadence", "progressive"
            )
            # Both VTS and VMG menu IFO tables are relocated by compact
            # authoring, so an exact requested encode may grow a tight VOBU.
            $menuArguments += "--allow-compact-expansion"
            Invoke-Dvd2Hevc -Step "convert-$domain-$vts" -Arguments $menuArguments
            $workspaceReport = Join-Path $workspace "menu-report.json"
            if (-not (Test-PassedReport $workspaceReport "dvd2hevc-menu-conversion-v0" "progressive" $QualityValues)) {
                throw "$domain VTS $vts did not produce a passed report"
            }
            Copy-Item -LiteralPath $workspaceReport -Destination $reportCopy -Force
        }
        $script:menuReports += $reportCopy
        $script:completedVideoTasks += 1
        ("DVD2HEVC_PROGRESS " + (@{
            timestamp = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
            lane = "video"; event = "progress"; task = "$domain-$vts"
            current = $script:completedVideoTasks; total = $script:totalVideoTasks
        } | ConvertTo-Json -Compress)) | Write-Utf8Log
        Write-Status -State "running" -Step "menus" -Message "Completed $domain VTS $vts"
    }

    Write-Status -State "passed" -Step "complete" -Message "Every remaining title and menu domain passed"
    "[$((Get-Date).ToString('o'))] PASS" | Write-Utf8Log
    exit 0
}
catch {
    if (Test-Path -LiteralPath $cancelPath -PathType Leaf) {
        Write-Status -State "canceled" -Step "canceled" -Message $_.Exception.Message
        "[$((Get-Date).ToString('o'))] CANCELED - $($_.Exception.Message)" | Write-Utf8Log
        exit 3
    }
    else {
        Write-Status -State "failed" -Step "failed" -Message $_.Exception.Message
        "[$((Get-Date).ToString('o'))] FAILED - $($_.Exception.Message)`r`n$($_ | Out-String)" |
            Write-Utf8Log
        exit 1
    }
}
