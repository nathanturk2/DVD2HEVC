[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Plan,
    [Parameter(Mandatory = $true)]
    [string]$WorkRoot,
    [Parameter(Mandatory = $true)]
    [string]$OutputIso,
    [string]$FullScanReport,
    [string]$QualityValues = "cq:27",
    [string]$CompactQuality = "cq:27",
    [string]$QualityMode = "",
    [Alias("AutoCqMultiplier")]
    [ValidateRange(0.25, 4.0)]
    [double]$TargetBitrateMultiplier = 1.0,
    [ValidateSet("vbr", "cbr")]
    [string]$BitrateMode = "vbr",
    [string]$MainTitleQuality = "",
    [string]$TopNQuality = "",
    [int]$TopNCount = 0,
    [ValidateSet("hevc_nvenc", "hevc_qsv", "hevc_amf", "libx265")]
    [string]$Encoder = "hevc_nvenc",
    [string]$Preset = "p6",
    [ValidateSet("auto", "progressive", "deinterlace50")]
    [string]$Cadence = "auto",
    [ValidateSet("fail", "progressive", "deinterlace50")]
    [string]$AmbiguousCadence = "deinterlace50",
    [ValidateSet("passthrough", "compact-stereo")]
    [string]$AudioMode = "passthrough",
    [string]$AudioLanguageOverridesJson = "{}",
    [string]$StereoAudioBitrate = "256000",
    [string]$MonoAudioBitrate = "128000",
    [ValidateRange(1, 8)]
    [int]$AudioWorkers = 2,
    [ValidateRange(1, 8)]
    [int]$PipelineDepth = 2,
    [string]$Label,
    [string]$VlcRoot,
    [ValidateSet("uhd-bd", "dvd-hevc")]
    [string]$OutputFormat = "uhd-bd",
    [string]$Tsmuxer,
    [string]$UdfTool,
    [string]$JavaHome,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$env:DVD2HEVC_PROGRESS_STDOUT = "1"
$projectRoot = Split-Path -Parent $PSScriptRoot
$cli = Join-Path $projectRoot "dvd2hevc.py"
. (Join-Path $PSScriptRoot "write-log.ps1")
$remainingRunner = Join-Path $PSScriptRoot "run-phase7-remaining-domains.ps1"
$vlcRunner = Join-Path $PSScriptRoot "test-vlc-dvdhevc.ps1"
$planPath = [IO.Path]::GetFullPath($Plan)
$workPath = [IO.Path]::GetFullPath($WorkRoot)
$outputPath = [IO.Path]::GetFullPath($OutputIso)
$statusPath = Join-Path $workPath "status.json"
$logPath = Join-Path $workPath "run.log"
$cancelPath = Join-Path $workPath "cancel.requested"
$started = (Get-Date).ToString("o")
$script:currentStage = "initializing"
$script:finalStage = $null
$script:sourcePath = $null
$script:scanPath = $null
$script:physicalVts = @()
$script:physicalReports = @()
$script:audioJob = $null
$script:qualityPolicy = $null
$script:qualityPolicyPath = Join-Path $workPath "quality-policy.json"
$script:audioPolicyPath = Join-Path $workPath "audio-policy.json"
$script:audioPolicy = $null
$finalReportRoot = Join-Path $workPath "final-output-reports"
$authorReport = Join-Path $finalReportRoot "author.json"
$authorLog = Join-Path $finalReportRoot "author.log"
$verificationReport = Join-Path $finalReportRoot "verification.json"
$cssAuditReport = Join-Path $finalReportRoot "css-safe-audit.json"
$repairReport = Join-Path $finalReportRoot "css-safe-repair.json"
$repairVerification = Join-Path $finalReportRoot "css-safe-verification.json"

New-Item -ItemType Directory -Path $workPath -Force | Out-Null
New-Item -ItemType Directory -Path $finalReportRoot -Force | Out-Null
New-Item -ItemType Directory -Path (Split-Path -Parent $outputPath) -Force | Out-Null

function Write-Utf8Log {
    param([Parameter(ValueFromPipeline = $true)]$InputObject)
    process {
        $line = if ($null -eq $InputObject) { "" } else { [string]$InputObject }
        Add-Dvd2HevcLogLine -Path $logPath -Line $line
        Write-Output $InputObject
    }
}

function Write-ProgressEvent {
    param([string]$Lane, [string]$Event, [string]$Task, [hashtable]$Fields = @{})
    $value = [ordered]@{
        timestamp = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
        lane = $Lane
        event = $Event
        task = $Task
    }
    foreach ($key in $Fields.Keys) { $value[$key] = $Fields[$key] }
    $line = "DVD2HEVC_PROGRESS " + ($value | ConvertTo-Json -Compress)
    Add-Dvd2HevcLogLine -Path $logPath -Line $line
}

function Get-TaskLane {
    param([string]$Stage)
    if ($Stage -match "compact-audio") { return "audio" }
    if ($Stage -match "^(convert-|ordinary-and-menu)") { return "video" }
    return "mux"
}

function Assert-NotCanceled {
    if (Test-Path -LiteralPath $cancelPath -PathType Leaf) {
        throw "DVD2HEVC cancellation requested; stopping at a safe task boundary"
    }
}

function Write-Status {
    param([string]$State, [string]$Stage, [string]$Message)
    $script:currentStage = $Stage
    $value = [ordered]@{
        schema = "dvd2hevc-phase7-general-disc-status-v0"
        state = $State
        stage = $Stage
        message = $Message
        plan = $planPath
        source = $script:sourcePath
        full_scan = $script:scanPath
        physical_vts = @($script:physicalVts)
        physical_reports = @($script:physicalReports)
        work_root = $workPath
        final_stage = $script:finalStage
        output_iso = $outputPath
        output_format = $OutputFormat
        encoder = $Encoder
        audio_mode = $AudioMode
        stereo_audio_bitrate = $StereoAudioBitrate
        mono_audio_bitrate = $MonoAudioBitrate
        audio_workers = $AudioWorkers
        pipeline_depth = $PipelineDepth
        title_scheduler = "physical-vts-grouped-v1"
        started = $started
        updated = (Get-Date).ToString("o")
        process_id = $PID
    }
    $temporary = "$statusPath.$PID.tmp"
    $value | ConvertTo-Json -Depth 7 | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Dvd2HevcAtomicFile -TemporaryPath $temporary -DestinationPath $statusPath
}

function Test-JsonValue {
    param([string]$Path, [string]$Property, [string]$Value)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    try {
        $document = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
        return [string]$document.$Property -eq $Value
    }
    catch { return $false }
}

function Require-Report {
    param([string]$Path, [string]$Property, [string]$Value)
    if (-not (Test-JsonValue $Path $Property $Value)) {
        throw "Required report is absent or incomplete: $Path"
    }
}

function Get-Dvd2HevcFileSha256 {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)
    $stream = [IO.File]::OpenRead([IO.Path]::GetFullPath($LiteralPath))
    try {
        $sha256 = [Security.Cryptography.SHA256]::Create()
        try {
            $digest = $sha256.ComputeHash($stream)
        }
        finally {
            $sha256.Dispose()
        }
    }
    finally {
        $stream.Dispose()
    }
    return ([BitConverter]::ToString($digest).Replace("-", "")).ToLowerInvariant()
}

function Test-ReportSettings {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    try {
        $document = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
        $expectedSetting = Get-VtsQuality ([int]$document.vts)
        $expectedQuality = (@($expectedSetting -split ",") | ForEach-Object {
            ([string]$_).Trim().ToLowerInvariant() -replace '^cq:', ''
        }) -join ","
        $actualQuality = (@($document.settings.quality_values) | ForEach-Object {
            [string]$_
        }) -join ","
        return (
            [string]$document.settings.encoder -eq $Encoder -and
            [string]$document.settings.preset -eq $Preset -and
            [string]$document.settings.cadence -eq $Cadence -and
            [string]$document.settings.ambiguous_cadence -eq $AmbiguousCadence -and
            [string]$document.settings.intermediate_policy -eq "compact-input-v1" -and
            $actualQuality -eq $expectedQuality
        )
    }
    catch { return $false }
}

function Test-PassedVmgReports {
    param([string]$Root, [int]$ExpectedCount)
    $menuRoot = Join-Path $Root "menu-reports"
    if (-not (Test-Path -LiteralPath $menuRoot -PathType Container)) {
        return $ExpectedCount -eq 0
    }
    try {
        $reports = @(Get-ChildItem -LiteralPath $menuRoot -Filter "*-report.json" -File |
            ForEach-Object {
                $document = Get-Content -LiteralPath $_.FullName -Raw | ConvertFrom-Json
                if ([string]$document.domain -eq "vmg_menu") {
                    [pscustomobject]@{ Path = $_.FullName; Document = $document }
                }
            })
        if ($reports.Count -ne $ExpectedCount) { return $false }
        foreach ($report in $reports) {
            if ([string]$report.Document.status -ne "passed") { return $false }
            foreach ($cell in @($report.Document.cells)) {
                if (@($cell.attempts).Count -eq 0) { return $false }
            }
        }
        return $true
    }
    catch { return $false }
}

function Get-VtsQuality {
    param([int]$Vts)
    if ($null -ne $script:qualityPolicy -and $Vts -gt 0) {
        $row = $script:qualityPolicy.quality_by_vts.PSObject.Properties[[string]$Vts].Value
        if ($null -ne $row) { return [string]$row.resolved }
    }
    return $QualityValues
}

function Test-CompactAudioLayout {
    param([string]$Path, [string]$AudioReport, [int]$Vts, [string]$BaseLayout)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    try {
        $document = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
        $audio = Get-Content -LiteralPath $AudioReport -Raw | ConvertFrom-Json
        $row = @($document.domains | Where-Object {
            [string]$_.domain -eq "title" -and [int]$_.vts -eq $Vts
        })[0]
        if (-not (
            [string]$document.status -eq "planned" -and
            [string]$document.audio_policy.mode -eq "compact-stereo" -and
            [string]$document.audio_policy.layout_policy -eq "source-local-vobu-sector-map-v2" -and
            [string]$document.base_layout_sha256 -eq (Get-Dvd2HevcFileSha256 -LiteralPath $BaseLayout) -and
            [string]$row.compact_audio.processing_policy -eq [string]$audio.processing_policy -and
            [IO.Path]::GetFullPath([string]$row.compact_audio.audio_report) -eq
                [IO.Path]::GetFullPath($AudioReport)
        )) { return $false }
        if (@($row.compact_audio.tracks).Count -ne @($audio.tracks).Count) { return $false }
        foreach ($track in @($audio.tracks)) {
            $planned = @($row.compact_audio.tracks | Where-Object {
                [int]$_.ordinal -eq [int]$track.ordinal
            })[0]
            if (
                [int]$planned.target_channels -ne [int]$track.target_channels -or
                [int]$planned.target_bitrate -ne [int]$track.target_bitrate -or
                [string]$planned.processing -ne [string]$track.processing
            ) { return $false }
        }
        return $true
    }
    catch { return $false }
}

function Test-LayoutArtifact {
    param([string]$ReportPath, [string]$LayoutPath, [string]$StatusProperty = "status", [string]$StatusValue = "passed")
    if (-not (Test-Path -LiteralPath $ReportPath -PathType Leaf)) { return $false }
    try {
        $report = Get-Content -LiteralPath $ReportPath -Raw | ConvertFrom-Json
        $layoutHash = Get-Dvd2HevcFileSha256 -LiteralPath $LayoutPath
        return (
            [string]$report.$StatusProperty -eq $StatusValue -and
            [string]$report.layout_sha256 -eq $layoutHash
        )
    }
    catch { return $false }
}

function Invoke-Dvd2Hevc {
    param([string]$Stage, [string[]]$Arguments)
    Assert-NotCanceled
    $lane = Get-TaskLane $Stage
    Write-Status -State "running" -Stage $Stage -Message ($Arguments -join " ")
    Write-ProgressEvent -Lane $lane -Event "start" -Task $Stage
    "[$((Get-Date).ToString('o'))] $Stage" | Write-Utf8Log
    $previousErrorAction = $ErrorActionPreference
    try {
        # Windows PowerShell wraps native stderr as NativeCommandError.  Keep
        # streaming every traceback line, then decide success by the process
        # exit code instead of truncating the useful diagnostic at line one.
        $ErrorActionPreference = "Continue"
        & $Python -u $cli @Arguments 2>&1 | Write-Utf8Log
        $nativeExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorAction
    }
    if ($nativeExitCode -ne 0) {
        Write-ProgressEvent -Lane $lane -Event "failed" -Task $Stage
        throw "$Stage failed with exit code $nativeExitCode"
    }
    Write-ProgressEvent -Lane $lane -Event "done" -Task $Stage
}

function Assert-FullClearScan {
    param([string]$Path, [string]$ExpectedSource)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    try {
        $scan = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
        $scanSource = [IO.Path]::GetFullPath([string]$scan.source)
        return (
            $scanSource -eq $ExpectedSource -and
            [string]$scan.scan_depth -eq "full" -and
            [bool]$scan.content_decrypted -and
            [int64]$scan.video_ts.totals.scrambled_pes_packets -eq 0 -and
            [int64]$scan.video_ts.totals.invalid_sectors -eq 0
        )
    }
    catch { return $false }
}

try {
    "DVD2HEVC generalized Phase 7 build started $started" |
        Set-Content -LiteralPath $logPath -Encoding UTF8
    $planValue = Get-Content -LiteralPath $planPath -Raw | ConvertFrom-Json
    if ([string]$planValue.schema -ne "dvd2hevc-compatibility-plan-v0") {
        throw "Unsupported compatibility plan: $($planValue.schema)"
    }
    $script:sourcePath = [IO.Path]::GetFullPath([string]$planValue.source)
    if (-not (Test-Path -LiteralPath $script:sourcePath -PathType Leaf)) {
        throw "Source ISO is missing: $script:sourcePath"
    }
    if (-not $Label) { $Label = "$([string]$planValue.label)_HEVC" }
    if (-not $VlcRoot) { $VlcRoot = Join-Path $projectRoot "work\phase2\vlc-dvdhevc" }

    $script:scanPath = if ($FullScanReport) {
        [IO.Path]::GetFullPath($FullScanReport)
    }
    else {
        Join-Path $workPath "source-full-scan.json"
    }
    if (-not (Assert-FullClearScan $script:scanPath $script:sourcePath)) {
        Invoke-Dvd2Hevc -Stage "full-source-decryption-scan" -Arguments @(
            "scan", $script:sourcePath, "--no-handbrake", "--report", $script:scanPath
        )
    }
    if (-not (Assert-FullClearScan $script:scanPath $script:sourcePath)) {
        throw "The source did not pass the full clear-content and sector-integrity gate"
    }

    $requestedQuality = if ($QualityMode) { $QualityMode } else { $QualityValues }
    $qualityArguments = @(
        "resolve-quality-policy", $planPath, $script:scanPath, $script:qualityPolicyPath,
        "--quality", $requestedQuality,
        "--target-bitrate-multiplier", [string]$TargetBitrateMultiplier,
        "--bitrate-mode", $BitrateMode
    )
    if ($MainTitleQuality) { $qualityArguments += @("--main-title-quality", $MainTitleQuality) }
    if ($TopNQuality) {
        $qualityArguments += @("--top-n-quality", $TopNQuality, "--top-n-count", [string]$TopNCount)
    }
    Invoke-Dvd2Hevc -Stage "resolve-quality-policy" -Arguments $qualityArguments
    $script:qualityPolicy = Get-Content -LiteralPath $script:qualityPolicyPath -Raw | ConvertFrom-Json
    $QualityValues = [string]$script:qualityPolicy.general.resolved
    $CompactQuality = $QualityValues

    $audioPolicyArguments = @(
        "resolve-audio-policy", $script:sourcePath, $script:audioPolicyPath,
        "--default-mode", $AudioMode
    )
    $audioOverrides = $AudioLanguageOverridesJson | ConvertFrom-Json
    foreach ($property in @($audioOverrides.PSObject.Properties)) {
        $audioPolicyArguments += @("--language", "$($property.Name)=$($property.Value)")
    }
    Invoke-Dvd2Hevc -Stage "resolve-audio-policy" -Arguments $audioPolicyArguments
    $script:audioPolicy = Get-Content -LiteralPath $script:audioPolicyPath -Raw | ConvertFrom-Json
    $AudioMode = [string]$script:audioPolicy.resolved_pipeline_mode

    # The final compact layout is the first sector-preserving artifact.  Keep
    # the much smaller exact HEVC intermediates beside their reusable audio
    # cache, instead of building and then rereading obsolete DVD-sized VOBs.
    $variantKey = (
        "$Encoder-$Preset-$((Get-Dvd2HevcFileSha256 -LiteralPath $script:qualityPolicyPath).Substring(0,12))-$Cadence-$AmbiguousCadence-vmgcompact1-menucompact1-compactfirst1-physicalgroup1" -replace '[^A-Za-z0-9._-]', '_'
    )
    $compactRoot = Join-Path $workPath "compact\$variantKey"
    $audioRoot = Join-Path $compactRoot "audio"
    New-Item -ItemType Directory -Path $compactRoot -Force | Out-Null

    $supportedBlockers = @("interleaved-branching-cells", "title-does-not-map-to-one-pgc")
    $unsupportedBlockers = @($planValue.title_tasks | ForEach-Object {
        @($_.blockers) | Where-Object { $supportedBlockers -notcontains [string]$_ }
    } | Sort-Object -Unique)
    if ($unsupportedBlockers.Count -gt 0) {
        throw "Unsupported compatibility blockers: $($unsupportedBlockers -join ', ')"
    }
    $blockedPhysicalVts = @($planValue.title_tasks | Where-Object {
        [string]$_.status -eq "blocked"
    } | ForEach-Object { [int]$_.vts } | Sort-Object -Unique)
    $unreferencedPhysicalVts = @($planValue.vts | Where-Object {
        [int]$_.physical_cells -gt [int]$_.unique_referenced_cells
    } | ForEach-Object { [int]$_.vts } | Sort-Object -Unique)
    $mandatoryPhysicalVts = @(
        @($blockedPhysicalVts) + @($unreferencedPhysicalVts) | Sort-Object -Unique
    )
    # The complete physical converter is also the efficient ordinary-title
    # scheduler. It reads each VTS once, encodes shared cells once, and overlaps
    # one hardware encode with one full-decode validation. Keep the old
    # per-title runner as a safe fallback if a novel VTS cannot prove complete
    # physical coverage.
    $physicalGroupingCandidates = @(
        $planValue.vts | ForEach-Object { [int]$_.vts } | Sort-Object -Unique
    )
    $physicalPlans = @{}
    foreach ($vts in $physicalGroupingCandidates) {
        $vtsName = "vts{0:d2}" -f $vts
        $physicalPlan = Join-Path $workPath "physical-plans\$vtsName.json"
        New-Item -ItemType Directory -Path (Split-Path -Parent $physicalPlan) -Force | Out-Null
        if (-not (Test-JsonValue $physicalPlan "conversion_ready" "True")) {
            Invoke-Dvd2Hevc -Stage "plan-$vtsName-physical" -Arguments @(
                "plan-interleaved", $script:sourcePath, [string]$vts, $physicalPlan,
                "--scan-report", $script:scanPath
            )
        }
        if (Test-JsonValue $physicalPlan "conversion_ready" "True") {
            $script:physicalVts += $vts
            $physicalPlans[$vts] = $physicalPlan
        }
        elseif ($mandatoryPhysicalVts -contains $vts) {
            throw "Mandatory physical VTS $vts did not prove complete conversion coverage"
        }
        else {
            "VTS $vts cannot use grouped physical scheduling; retaining the verified per-title fallback" |
                Write-Utf8Log
        }
    }
    $script:physicalVts = @($script:physicalVts | Sort-Object -Unique)

    foreach ($vts in $script:physicalVts) {
        $vtsName = "vts{0:d2}" -f $vts
        $physicalPlan = [string]$physicalPlans[$vts]
        Require-Report $physicalPlan "conversion_ready" "True"

        $physicalWork = Join-Path $workPath "physical\$vtsName"
        $physicalReport = Join-Path $physicalWork "$vtsName-conversion-report.json"
        if (-not (
            (Test-JsonValue $physicalReport "status" "passed") -and
            (Test-ReportSettings $physicalReport)
        )) {
            $physicalArguments = @(
                "convert-interleaved-vts", $physicalPlan, $physicalWork,
                "--quality-values", (Get-VtsQuality $vts), "--encoder", $Encoder, "--preset", $Preset,
                "--cadence", $Cadence, "--ambiguous-cadence", $AmbiguousCadence,
                "--prefer-compact-input", "--pipeline-depth", [string]$PipelineDepth
            )
            $audioPolicyRow = @($script:audioPolicy.title_sets | Where-Object {
                [int]$_.vts -eq [int]$vts -and [bool]$_.processing_required
            })
            if ($AudioMode -eq "compact-stereo" -and $audioPolicyRow.Count -gt 0) {
                New-Item -ItemType Directory -Path $audioRoot -Force | Out-Null
                $physicalArguments += @(
                    "--audio-prefetch-root", $audioRoot,
                    "--audio-policy", $script:audioPolicyPath,
                    "--stereo-audio-bitrate", $StereoAudioBitrate,
                    "--mono-audio-bitrate", $MonoAudioBitrate,
                    "--audio-workers", [string]$AudioWorkers
                )
            }
            Invoke-Dvd2Hevc -Stage "convert-$vtsName-physical" -Arguments $physicalArguments
        }
        Require-Report $physicalReport "status" "passed"
        if (-not (Test-ReportSettings $physicalReport)) {
            throw "Physical VTS report does not use the requested encoder settings: $physicalReport"
        }
        $provenancePath = Join-Path $physicalWork "$vtsName-title-provenance.json"
        $provenance = [ordered]@{
            schema = "dvd2hevc-grouped-vts-title-provenance-v1"
            status = "passed"
            scheduler = "physical-vts-grouped-v1"
            source = $script:sourcePath
            vts = $vts
            compatibility_plan = $planPath
            physical_plan = $physicalPlan
            conversion_report = $physicalReport
            titles = @($planValue.title_tasks | Where-Object {
                [int]$_.vts -eq $vts
            } | ForEach-Object {
                [ordered]@{
                    title = [int]$_.title
                    vts_title_number = [int]$_.vts_title_number
                    pgcs = @($_.pgcs)
                    duration_seconds = [double]$_.duration_seconds
                    status = [string]$_.status
                }
            })
        }
        $provenanceTemporary = "$provenancePath.$PID.tmp"
        $provenance | ConvertTo-Json -Depth 8 |
            Set-Content -LiteralPath $provenanceTemporary -Encoding UTF8
        Move-Dvd2HevcAtomicFile -TemporaryPath $provenanceTemporary -DestinationPath $provenancePath
        $script:physicalReports += $physicalReport
    }

    $remainingPath = Join-Path $workPath "ordinary-and-menus"
    $remainingStatus = Join-Path $remainingPath "status.json"
    $expectedVmgMenus = @($planValue.menu_tasks | Where-Object {
        [string]$_.domain -eq "vmg_menu"
    }).Count
    if (-not (
        (Test-JsonValue $remainingStatus "state" "passed") -and
        (Test-JsonValue $remainingStatus "encoder" $Encoder) -and
        (Test-JsonValue $remainingStatus "encoder_preset" $Preset) -and
        (Test-JsonValue $remainingStatus "quality_values" $QualityValues) -and
        (Test-JsonValue $remainingStatus "cadence" $Cadence) -and
        (Test-JsonValue $remainingStatus "ambiguous_cadence" $AmbiguousCadence) -and
        (Test-PassedVmgReports $remainingPath $expectedVmgMenus)
    )) {
        Assert-NotCanceled
        Write-Status -State "running" -Stage "ordinary-and-menu-domains" `
            -Message "Encoding fallback title sets and all menu domains"
        Write-ProgressEvent -Lane "video" -Event "start" -Task "ordinary-and-menu-domains"
        $remainingArguments = @(
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $remainingRunner,
            "-Plan", $planPath, "-WorkRoot", $remainingPath,
            "-QualityValues", $QualityValues, "-Encoder", $Encoder, "-Preset", $Preset,
            "-QualityPolicy", $script:qualityPolicyPath,
            "-Cadence", $Cadence, "-AmbiguousCadence", $AmbiguousCadence,
            "-Python", $Python
        )
        if ($script:physicalVts.Count -gt 0) {
            # Windows PowerShell -File binds only the first token supplied to
            # an array parameter. Pass a single CSV value so every physical VTS
            # is excluded from the title-oriented child runner.
            $remainingArguments += @("-SkipVtsCsv", ($script:physicalVts -join ","))
        }
        if ($AudioMode -eq "compact-stereo") {
            New-Item -ItemType Directory -Path $audioRoot -Force | Out-Null
            $remainingArguments += @(
                "-AudioPrefetchRoot", $audioRoot,
                "-AudioPolicy", $script:audioPolicyPath,
                "-StereoAudioBitrate", $StereoAudioBitrate,
                "-MonoAudioBitrate", $MonoAudioBitrate,
                "-AudioWorkers", [string]$AudioWorkers
            )
        }
        & powershell.exe @remainingArguments 2>&1 | Write-Utf8Log
        if ($LASTEXITCODE -ne 0) {
            Write-ProgressEvent -Lane "video" -Event "failed" -Task "ordinary-and-menu-domains"
            throw "Ordinary/menu conversion failed with exit code $LASTEXITCODE"
        }
        Write-ProgressEvent -Lane "video" -Event "done" -Task "ordinary-and-menu-domains"
    }
    Require-Report $remainingStatus "state" "passed"
    Require-Report $remainingStatus "encoder" $Encoder
    Require-Report $remainingStatus "encoder_preset" $Preset
    Require-Report $remainingStatus "quality_values" $QualityValues
    Require-Report $remainingStatus "cadence" $Cadence
    Require-Report $remainingStatus "ambiguous_cadence" $AmbiguousCadence

    $combinedRoot = Join-Path $remainingPath "combined"
    $menuRoot = Join-Path $remainingPath "menu-reports"
    $ordinaryReports = if (Test-Path -LiteralPath $combinedRoot) {
        @(Get-ChildItem -LiteralPath $combinedRoot -Filter "vts*-report.json" -File |
            Sort-Object Name | Where-Object {
                $reportVts = [int](Get-Content -LiteralPath $_.FullName -Raw | ConvertFrom-Json).vts
                $script:physicalVts -notcontains $reportVts
            } | ForEach-Object FullName)
    }
    else { @() }
    $menuReports = if (Test-Path -LiteralPath $menuRoot) {
        @(Get-ChildItem -LiteralPath $menuRoot -Filter "*-report.json" -File |
            Sort-Object Name | ForEach-Object FullName)
    }
    else { @() }
    $vmgMenuReports = @($menuReports | Where-Object {
        [string](Get-Content -LiteralPath $_ -Raw | ConvertFrom-Json).domain -eq "vmg_menu"
    })
    $vtsMenuReports = @($menuReports | Where-Object {
        [string](Get-Content -LiteralPath $_ -Raw | ConvertFrom-Json).domain -eq "vts_menu"
    })
    $titleReports = @($ordinaryReports) + @($script:physicalReports)

    $expectedVts = @($planValue.vts | ForEach-Object { [int]$_.vts } | Sort-Object)
    $actualVts = @($titleReports | ForEach-Object {
        [int](Get-Content -LiteralPath $_ -Raw | ConvertFrom-Json).vts
    } | Sort-Object)
    if (($expectedVts -join ",") -ne ($actualVts -join ",")) {
        throw "Title VTS coverage mismatch: expected $($expectedVts -join ','); actual $($actualVts -join ',')"
    }
    $expectedMenus = @($planValue.menu_tasks).Count
    if ($menuReports.Count -ne $expectedMenus) {
        throw "Menu report count mismatch: expected $expectedMenus; actual $($menuReports.Count)"
    }

    # The exact video layout depends only on completed title reports, so plan it
    # before staging. Compact audio can then run in a bounded background lane
    # while the main lane assembles and verifies the all-HEVC sector stage.
    $videoLayoutPath = if ($AudioMode -eq "compact-stereo") {
        Join-Path $compactRoot "layout-video.json"
    }
    else {
        Join-Path $compactRoot "layout.json"
    }
    if (-not (Test-JsonValue $videoLayoutPath "status" "planned")) {
        $layoutArguments = @("plan-compact", $videoLayoutPath)
        $layoutArguments += @($titleReports) + @($vtsMenuReports) + @($vmgMenuReports)
        $layoutArguments += @(
            "--quality", $CompactQuality,
            "--disc-quality-policy", $script:qualityPolicyPath,
            "--audio-mode", $AudioMode,
            "--stereo-audio-bitrate", $StereoAudioBitrate,
            "--mono-audio-bitrate", $MonoAudioBitrate
        )
        Invoke-Dvd2Hevc -Stage "plan-full-disc-compaction" -Arguments $layoutArguments
    }
    Require-Report $videoLayoutPath "status" "planned"
    $videoLayout = Get-Content -LiteralPath $videoLayoutPath -Raw | ConvertFrom-Json
    $videoTitleDomains = @($videoLayout.domains | Where-Object {
        [string]$_.domain -eq "title"
    } | Sort-Object { [int]$_.vts })
    if ($videoTitleDomains.Count -ne $expectedVts.Count) {
        throw "Compact plan contains $($videoTitleDomains.Count) title domains; expected $($expectedVts.Count)"
    }
    $videoVmgDomains = @($videoLayout.domains | Where-Object {
        [string]$_.domain -eq "vmg_menu" -and [int]$_.vts -eq 0
    })
    if ($videoVmgDomains.Count -ne $expectedVmgMenus) {
        throw "Compact plan contains $($videoVmgDomains.Count) VMG menu domains; expected $expectedVmgMenus"
    }

    $audioBatchReport = Join-Path $audioRoot "compact-audio-batch-report.json"
    $audioProgressPath = Join-Path $audioRoot "progress.jsonl"
    if ($AudioMode -eq "compact-stereo") {
        New-Item -ItemType Directory -Path $audioRoot -Force | Out-Null
        $audioVts = @($script:audioPolicy.title_sets | Where-Object {
            [bool]$_.processing_required
        } | ForEach-Object { [int]$_.vts } | Sort-Object -Unique)
        $vtsCsv = ($audioVts -join ",")
        # The foreground runner deliberately uses pythonw.exe on Windows so
        # no console flashes appear. A PowerShell background job already has
        # no visible window, and needs python.exe so exit status and output are
        # observable instead of returning a null $LASTEXITCODE immediately.
        $audioPython = $Python
        if ([IO.Path]::GetFileName($audioPython) -ieq "pythonw.exe") {
            $consolePython = Join-Path (Split-Path -Parent $audioPython) "python.exe"
            if (Test-Path -LiteralPath $consolePython -PathType Leaf) {
                $audioPython = $consolePython
            }
        }
        Write-ProgressEvent -Lane "audio" -Event "start" -Task "compact-stereo-batch"
        $script:audioJob = Start-Job -Name "dvd2hevc-compact-audio" -ScriptBlock {
            param(
                [string]$PythonExe, [string]$CliPath, [string]$LayoutPath,
                [string]$AudioRoot, [string]$ProgressPath, [string]$VtsCsv,
                [string]$StereoRate, [string]$MonoRate,
                [int]$TrackWorkers, [int]$DomainWorkers, [string]$AudioPolicyPath
            )
            $env:DVD2HEVC_PROGRESS_FILE = $ProgressPath
            $arguments = @(
                "-u", $CliPath, "prototype-compact-audio-batch", $LayoutPath, $AudioRoot,
                "--stereo-audio-bitrate", $StereoRate,
                "--mono-audio-bitrate", $MonoRate,
                "--audio-workers", [string]$TrackWorkers,
                "--domain-workers", [string]$DomainWorkers,
                "--audio-policy", $AudioPolicyPath,
                "--vts"
            ) + @($VtsCsv -split ",")
            & $PythonExe @arguments 2>&1
            $exitCode = $LASTEXITCODE
            if ($null -eq $exitCode) {
                throw "Compact audio batch did not return an observable process exit code"
            }
            if ($exitCode -ne 0) {
                throw "Compact audio batch failed with exit code $exitCode"
            }
        } -ArgumentList @(
            $audioPython, $cli, $videoLayoutPath, $audioRoot, $audioProgressPath, $vtsCsv,
            $StereoAudioBitrate, $MonoAudioBitrate, $AudioWorkers, $PipelineDepth,
            $script:audioPolicyPath
        )
    }

    # Every video domain is rebuilt from the compact layout. The base stage is
    # therefore an unmodified extraction used only as the hardlink source.
    $sectorStage = Join-Path $workPath "compact-base-sector-stages\$variantKey"
    $sectorReport = Join-Path $sectorStage "dvd2hevc-stage-report.json"
    if (-not (Test-JsonValue $sectorReport "status" "passed")) {
        $stageArguments = @(
            "stage-disc", $sectorStage, "--source", $script:sourcePath
        )
        Invoke-Dvd2Hevc -Stage "stage-all-compact-base" -Arguments $stageArguments
    }
    Require-Report $sectorReport "status" "passed"
    $sectorVerification = Join-Path $sectorStage "dvd2hevc-stage-verification.json"
    if (-not (Test-JsonValue $sectorVerification "passed" "True")) {
        Invoke-Dvd2Hevc -Stage "verify-all-compact-base" -Arguments @("verify-stage", $sectorReport)
    }
    Require-Report $sectorVerification "passed" "True"

    $layoutPath = $videoLayoutPath
    if ($AudioMode -eq "compact-stereo") {
        Write-Status -State "running" -Stage "compact-audio-batch" `
            -Message "Waiting for the bounded audio lane after overlapping sector staging"
        $audioOutput = @($script:audioJob | Receive-Job -Wait -ErrorAction Continue)
        $audioOutput | Write-Utf8Log
        if ($script:audioJob.State -ne "Completed") {
            Write-ProgressEvent -Lane "audio" -Event "failed" -Task "compact-stereo-batch"
            $reason = [string]$script:audioJob.ChildJobs[0].JobStateInfo.Reason
            throw "Compact audio background lane failed: $reason"
        }
        Remove-Job -Job $script:audioJob -Force
        $script:audioJob = $null
        Require-Report $audioBatchReport "status" "passed"
        Write-ProgressEvent -Lane "audio" -Event "done" -Task "compact-stereo-batch"

        $layoutRoot = Join-Path $compactRoot "audio-layouts"
        New-Item -ItemType Directory -Path $layoutRoot -Force | Out-Null
        foreach ($domain in @($videoTitleDomains | Where-Object {
            $audioVts -contains [int]$_.vts
        })) {
            $vts = [int]$domain.vts
            $vtsName = "vts{0:d2}" -f $vts
            $audioReport = Join-Path $audioRoot "$vtsName\compact-audio-domain-report.json"
            Require-Report $audioReport "status" "passed"
            $nextLayout = Join-Path $layoutRoot "through-$vtsName.json"
            if (-not (Test-CompactAudioLayout $nextLayout $audioReport $vts $layoutPath)) {
                Invoke-Dvd2Hevc -Stage "plan-compact-audio-$vtsName" -Arguments @(
                    "plan-compact-audio", $layoutPath, $audioReport, $nextLayout, "title", [string]$vts
                )
            }
            $layoutPath = $nextLayout
        }
    }
    Require-Report $layoutPath "status" "planned"
    $layout = Get-Content -LiteralPath $layoutPath -Raw | ConvertFrom-Json
    $titleDomains = @($layout.domains | Where-Object {
        [string]$_.domain -eq "title"
    } | Sort-Object { [int]$_.vts })
    if ($titleDomains.Count -ne $expectedVts.Count) {
        throw "Compact plan contains $($titleDomains.Count) title domains; expected $($expectedVts.Count)"
    }

    $vmgDomains = @($layout.domains | Where-Object {
        [string]$_.domain -eq "vmg_menu" -and [int]$_.vts -eq 0
    })
    if ($vmgDomains.Count -ne $expectedVmgMenus) {
        throw "Final compact layout contains $($vmgDomains.Count) VMG menu domains; expected $expectedVmgMenus"
    }

    $currentCompactStage = $sectorStage
    $compactVariant = if ($AudioMode -eq "compact-stereo") { "compact-stereo" } else { "passthrough" }
    $artifactSuffix = if ($AudioMode -eq "compact-stereo") { "_COMPACT_STEREO" } else { "" }

    # Write every missing raw compact VOB in one Python process. The compact
    # layout can be tens of MiB; previously each VMG/VTS title/menu command
    # parsed it again. Standalone per-domain reports remain the resume keys.
    $compactDomainTasks = @()
    if ($vmgDomains.Count -eq 1) {
        $vmgRoot = Join-Path $compactRoot "vmg"
        $compactVmgVob = Join-Path $vmgRoot "VIDEO_TS_COMPACT.vob"
        if (-not (Test-LayoutArtifact "$compactVmgVob.json" $layoutPath)) {
            $compactDomainTasks += [ordered]@{
                domain = "vmg_menu"; vts = 0; destination = $compactVmgVob
            }
        }
    }
    foreach ($domainRow in $titleDomains) {
        $batchVts = [int]$domainRow.vts
        $batchVtsName = "vts{0:d2}" -f $batchVts
        $batchVtsRoot = Join-Path $compactRoot $batchVtsName
        $batchCompactVob = Join-Path $batchVtsRoot (
            "VTS_{0:d2}_TITLE_COMPACT{1}.vob" -f $batchVts, $artifactSuffix
        )
        if (-not (Test-LayoutArtifact "$batchCompactVob.json" $layoutPath)) {
            $compactDomainTasks += [ordered]@{
                domain = "title"; vts = $batchVts; destination = $batchCompactVob
            }
        }
        $batchMenuDomains = @($layout.domains | Where-Object {
            [string]$_.domain -eq "vts_menu" -and [int]$_.vts -eq $batchVts
        })
        if ($batchMenuDomains.Count -gt 1) {
            throw "Compact layout duplicates the $batchVtsName menu domain"
        }
        if ($batchMenuDomains.Count -eq 1) {
            $batchCompactMenuVob = Join-Path $batchVtsRoot (
                "VTS_{0:d2}_MENU_COMPACT.vob" -f $batchVts
            )
            if (-not (Test-LayoutArtifact "$batchCompactMenuVob.json" $layoutPath)) {
                $compactDomainTasks += [ordered]@{
                    domain = "vts_menu"; vts = $batchVts; destination = $batchCompactMenuVob
                }
            }
        }
    }
    if ($compactDomainTasks.Count -gt 0) {
        $compactBatchTasks = Join-Path $compactRoot "compact-domain-batch-tasks.json"
        $compactBatchReport = Join-Path $compactRoot "compact-domain-batch-report.json"
        [IO.File]::WriteAllText(
            $compactBatchTasks,
            (ConvertTo-Json -InputObject @($compactDomainTasks) -Depth 5),
            [Text.UTF8Encoding]::new($false)
        )
        Invoke-Dvd2Hevc -Stage "compact-domain-batch" -Arguments @(
            "prototype-compact-domain-batch", $layoutPath, $compactBatchTasks, $compactBatchReport
        )
        Require-Report $compactBatchReport "status" "passed"
    }
    $compactDomainWeights = @{}
    $compactDomainTotalSectors = 0L
    foreach ($domain in $titleDomains) {
        $vts = [int]$domain.vts
        $menuSectors = [long](($layout.domains | Where-Object {
            [string]$_.domain -eq "vts_menu" -and [int]$_.vts -eq $vts
        } | Measure-Object -Property compact_sectors -Sum).Sum)
        $weight = [long]$domain.compact_sectors + $menuSectors
        if ($weight -le 0) {
            throw "Compact VTS $('{0:d2}' -f $vts) has no planned title/menu sectors"
        }
        $compactDomainWeights[$vts] = $weight
        $compactDomainTotalSectors += $weight
    }
    $vmgWeight = [long](($vmgDomains | Measure-Object -Property compact_sectors -Sum).Sum)
    $compactDomainTotalSectors += $vmgWeight
    $compactDomainCompletedSectors = 0L
    if ($compactDomainTotalSectors -le 0) {
        throw "Compact title domains have no planned sectors"
    }
    # Raw VOB writing is normally most of this phase.  It is completed by the
    # batch command above, while the remaining quarter represents IFO rewrites
    # and the successive compact staging passes below.
    $compactDomainProgressBase = [long][Math]::Floor($compactDomainTotalSectors * 0.75)
    $compactDomainProgressSpan = $compactDomainTotalSectors - $compactDomainProgressBase
    Write-ProgressEvent -Lane "mux" -Event "progress" -Task "compact-domains" -Fields @{
        current = $compactDomainProgressBase
        total = $compactDomainTotalSectors
        unit = "weighted-sectors"
    }

    if ($vmgDomains.Count -eq 1) {
        $vmgRoot = Join-Path $compactRoot "vmg"
        New-Item -ItemType Directory -Path $vmgRoot -Force | Out-Null
        $compactVmgVob = Join-Path $vmgRoot "VIDEO_TS_COMPACT.vob"
        if (-not (Test-LayoutArtifact "$compactVmgVob.json" $layoutPath)) {
            Invoke-Dvd2Hevc -Stage "compact-vmg-menu" -Arguments @(
                "prototype-compact-domain", $layoutPath, "vmg_menu", "0", $compactVmgVob
            )
        }
        Require-Report "$compactVmgVob.json" "status" "passed"

        $compactVmgi = Join-Path $vmgRoot "VIDEO_TS.IFO"
        $compactVmgiBup = Join-Path $vmgRoot "VIDEO_TS.BUP"
        if (-not (Test-LayoutArtifact "$compactVmgi.json" $layoutPath)) {
            $sourceVmgi = Join-Path $sectorStage "VIDEO_TS\VIDEO_TS.IFO"
            Invoke-Dvd2Hevc -Stage "relocate-vmgi" -Arguments @(
                "rewrite-compact-vmgi", $layoutPath, $sourceVmgi, $compactVmgi,
                "--destination-bup", $compactVmgiBup
            )
        }
        Require-Report "$compactVmgi.json" "status" "passed"

        $vmgStage = Join-Path $compactRoot "stages\$compactVariant\through-vmg"
        $vmgStageReport = Join-Path $vmgStage "dvd2hevc-stage-report.json"
        if (-not (Test-LayoutArtifact $vmgStageReport $layoutPath)) {
            if (Test-Path -LiteralPath $vmgStage) {
                $resolvedStage = [IO.Path]::GetFullPath($vmgStage)
                $resolvedCompactRoot = [IO.Path]::GetFullPath($compactRoot).TrimEnd('\') + '\'
                if (-not $resolvedStage.StartsWith($resolvedCompactRoot, [StringComparison]::OrdinalIgnoreCase)) {
                    throw "Refusing to replace compact VMG stage outside its workspace: $resolvedStage"
                }
                Remove-Item -LiteralPath $resolvedStage -Recurse -Force
            }
            Invoke-Dvd2Hevc -Stage "stage-compact-vmg" -Arguments @(
                "stage-compact-vmg", $sectorStage, $layoutPath, $compactVmgVob,
                $compactVmgi, $compactVmgiBup, $vmgStage
            )
        }
        Require-Report $vmgStageReport "status" "passed"
        $currentCompactStage = $vmgStage
        $script:finalStage = $currentCompactStage
        $compactDomainCompletedSectors += $vmgWeight
        $compactDomainProgressCurrent = $compactDomainProgressBase + [long][Math]::Round(
            $compactDomainProgressSpan * $compactDomainCompletedSectors / $compactDomainTotalSectors
        )
        Write-ProgressEvent -Lane "mux" -Event "progress" -Task "compact-domains" -Fields @{
            current = $compactDomainProgressCurrent
            total = $compactDomainTotalSectors
            unit = "weighted-sectors"
        }
    }

    foreach ($domain in $titleDomains) {
        $vts = [int]$domain.vts
        $vtsName = "vts{0:d2}" -f $vts
        $vtsRoot = Join-Path $compactRoot $vtsName
        New-Item -ItemType Directory -Path $vtsRoot -Force | Out-Null
        $compactVob = Join-Path $vtsRoot ("VTS_{0:d2}_TITLE_COMPACT{1}.vob" -f $vts, $artifactSuffix)
        if (-not (Test-LayoutArtifact "$compactVob.json" $layoutPath)) {
            Invoke-Dvd2Hevc -Stage "compact-$vtsName" -Arguments @(
                "prototype-compact-domain", $layoutPath, "title", [string]$vts, $compactVob
            )
        }
        Require-Report "$compactVob.json" "status" "passed"

        $menuDomain = @($layout.domains | Where-Object {
            [string]$_.domain -eq "vts_menu" -and [int]$_.vts -eq $vts
        })
        if ($menuDomain.Count -gt 1) {
            throw "Compact layout duplicates the $vtsName menu domain"
        }
        $compactMenuVob = $null
        if ($menuDomain.Count -eq 1) {
            $compactMenuVob = Join-Path $vtsRoot ("VTS_{0:d2}_MENU_COMPACT.vob" -f $vts)
            if (-not (Test-LayoutArtifact "$compactMenuVob.json" $layoutPath)) {
                Invoke-Dvd2Hevc -Stage "compact-$vtsName-menu" -Arguments @(
                    "prototype-compact-domain", $layoutPath, "vts_menu", [string]$vts,
                    $compactMenuVob
                )
            }
            Require-Report "$compactMenuVob.json" "status" "passed"
        }

        $compactIfo = Join-Path $vtsRoot ("VTS_{0:d2}_0{1}.IFO" -f $vts, $artifactSuffix)
        $compactBup = Join-Path $vtsRoot ("VTS_{0:d2}_0{1}.BUP" -f $vts, $artifactSuffix)
        if (-not (Test-LayoutArtifact "$compactIfo.json" $layoutPath)) {
            $sourceIfo = Join-Path $sectorStage "VIDEO_TS\VTS_$('{0:d2}' -f $vts)_0.IFO"
            Invoke-Dvd2Hevc -Stage "relocate-$vtsName-ifo" -Arguments @(
                "rewrite-compact-vts-ifo", $layoutPath, [string]$vts, $sourceIfo,
                $compactIfo, "--destination-bup", $compactBup
            )
        }
        Require-Report "$compactIfo.json" "status" "passed"

        $nextStage = Join-Path $compactRoot "stages\$compactVariant\through-$vtsName"
        $nextReport = Join-Path $nextStage "dvd2hevc-stage-report.json"
        if (-not (Test-LayoutArtifact $nextReport $layoutPath)) {
            if (Test-Path -LiteralPath $nextStage) {
                $resolvedStage = [IO.Path]::GetFullPath($nextStage)
                $resolvedCompactRoot = [IO.Path]::GetFullPath($compactRoot).TrimEnd('\') + '\'
                if (-not $resolvedStage.StartsWith($resolvedCompactRoot, [StringComparison]::OrdinalIgnoreCase)) {
                    throw "Refusing to replace compact stage outside its workspace: $resolvedStage"
                }
                Remove-Item -LiteralPath $resolvedStage -Recurse -Force
            }
            $stageCompactArguments = @(
                "stage-compact-vts", $currentCompactStage, $layoutPath, [string]$vts,
                $compactVob, $compactIfo, $compactBup, $nextStage
            )
            if ($compactMenuVob) {
                $stageCompactArguments += @("--compact-menu-vob", $compactMenuVob)
            }
            Invoke-Dvd2Hevc -Stage "stage-$vtsName" -Arguments $stageCompactArguments
        }
        Require-Report $nextReport "status" "passed"
        $currentCompactStage = $nextStage
        $script:finalStage = $currentCompactStage
        $compactDomainCompletedSectors += [long]$compactDomainWeights[$vts]
        $compactDomainProgressCurrent = $compactDomainProgressBase + [long][Math]::Round(
            $compactDomainProgressSpan * $compactDomainCompletedSectors / $compactDomainTotalSectors
        )
        Write-ProgressEvent -Lane "mux" -Event "progress" -Task "compact-domains" -Fields @{
            current = $compactDomainProgressCurrent
            total = $compactDomainTotalSectors
            unit = "weighted-sectors"
        }
    }

    # Builds before the ISO-only output policy placed diagnostics beside the
    # image.  Preserve and relocate those reports when resuming older work.
    $legacyFinalArtifacts = @(
        @{ Source = "$outputPath.json"; Destination = $authorReport },
        @{ Source = "$outputPath.author.log"; Destination = $authorLog },
        @{ Source = "$outputPath.verification.json"; Destination = $verificationReport },
        @{ Source = "$outputPath.css-safe-audit.json"; Destination = $cssAuditReport },
        @{ Source = "$outputPath.css-safe-repair.json"; Destination = $repairReport },
        @{ Source = "$outputPath.css-safe-verification.json"; Destination = $repairVerification }
    )
    foreach ($artifact in $legacyFinalArtifacts) {
        if ((Test-Path -LiteralPath $artifact.Source) -and
            -not (Test-Path -LiteralPath $artifact.Destination)) {
            Move-Item -LiteralPath $artifact.Source -Destination $artifact.Destination
        }
    }
    if (Test-Path -LiteralPath $authorReport) {
        $legacyAuthor = Get-Content -LiteralPath $authorReport -Raw | ConvertFrom-Json
        if ([string]$legacyAuthor.log -eq "$outputPath.author.log") {
            $legacyAuthor.log = $authorLog
            [IO.File]::WriteAllText(
                $authorReport, ($legacyAuthor | ConvertTo-Json -Depth 20),
                [Text.UTF8Encoding]::new($false)
            )
        }
    }
    if (Test-Path -LiteralPath $verificationReport) {
        $legacyVerification = Get-Content -LiteralPath $verificationReport -Raw | ConvertFrom-Json
        if ([string]$legacyVerification.author_report -eq "$outputPath.json") {
            $legacyVerification.author_report = $authorReport
            [IO.File]::WriteAllText(
                $verificationReport, ($legacyVerification | ConvertTo-Json -Depth 20),
                [Text.UTF8Encoding]::new($false)
            )
        }
    }

    if ($OutputFormat -eq "uhd-bd") {
        $uhdArguments = @("author-uhd", $currentCompactStage, $outputPath,
            "--work-dir", (Join-Path $workPath "uhd"), "--label", $Label, "--vlc-root", $VlcRoot)
        if ($Tsmuxer) { $uhdArguments += @("--tsmuxer", $Tsmuxer) }
        if ($UdfTool) { $uhdArguments += @("--udf-tool", $UdfTool) }
        if ($JavaHome) { $uhdArguments += @("--java-home", $JavaHome) }
        Invoke-Dvd2Hevc -Stage "uhd-author" -Arguments $uhdArguments
        Require-Report (Join-Path $workPath "uhd\uhd-output.json") "passed" "True"
        Write-Status -State "passed" -Stage "complete" `
            -Message "UHD-BD ISO passed navigation/media, UDF payload and stock-VLC BD-J startup checks"
        "[$((Get-Date).ToString('o'))] PASS - $outputPath" | Write-Utf8Log
        exit 0
    }

    if (-not (Test-JsonValue $authorReport "status" "authored")) {
        Invoke-Dvd2Hevc -Stage "author-final-iso" -Arguments @(
            "author-iso", $currentCompactStage, $outputPath, "--label", $Label,
            "--report", $authorReport, "--log", $authorLog
        )
    }
    Require-Report $authorReport "status" "authored"
    if (-not (Test-JsonValue $verificationReport "passed" "True")) {
        Invoke-Dvd2Hevc -Stage "verify-final-iso" -Arguments @(
            "verify-iso", $authorReport, "--output-report", $verificationReport
        )
    }
    Require-Report $verificationReport "passed" "True"

    Assert-NotCanceled
    Write-Status -State "running" -Stage "audit-css-safe-psm" `
        -Message "Checking final HEVC maps against false CSS decryption"
    Write-ProgressEvent -Lane "mux" -Event "start" -Task "audit-css-safe-psm"
    & $Python -u $cli "audit-css-safe-psm" $outputPath "--report" $cssAuditReport 2>&1 |
        Write-Utf8Log
    $cssAuditExit = $LASTEXITCODE
    if ($cssAuditExit -eq 2) {
        $repairTemporary = Join-Path $finalReportRoot "css-safe-repaired.iso"
        Remove-Item -LiteralPath $repairTemporary -Force -ErrorAction SilentlyContinue
        Invoke-Dvd2Hevc -Stage "repair-css-safe-psm" -Arguments @(
            "repair-css-safe-psm", $outputPath, $repairTemporary, "--report", $repairReport
        )
        Invoke-Dvd2Hevc -Stage "verify-css-safe-psm" -Arguments @(
            "verify-css-safe-psm", $outputPath, $repairTemporary, "--report", $repairVerification
        )
        Move-Item -LiteralPath $repairTemporary -Destination $outputPath -Force
        Invoke-Dvd2Hevc -Stage "audit-repaired-css-safe-psm" -Arguments @(
            "audit-css-safe-psm", $outputPath, "--report", $cssAuditReport
        )
    }
    elseif ($cssAuditExit -ne 0) {
        Write-ProgressEvent -Lane "mux" -Event "failed" -Task "audit-css-safe-psm"
        throw "CSS-safe final-image audit failed with exit code $cssAuditExit"
    }
    Require-Report $cssAuditReport "passed" "True"
    Write-ProgressEvent -Lane "mux" -Event "done" -Task "audit-css-safe-psm"

    $vlcLogRoot = Join-Path $workPath "vlc-gates"
    New-Item -ItemType Directory -Path $vlcLogRoot -Force | Out-Null
    $longestTitle = @($planValue.title_tasks | Sort-Object {
        [double]$_.duration_seconds
    } -Descending | Select-Object -First 1)[0]
    $titleNumber = [int]$longestTitle.title
    $vlcChecks = @(
        @{ Name = "title-hardware"; Arguments = @(
            "-VlcRoot", $VlcRoot, "-DvdPath", $outputPath, "-Title", [string]$titleNumber,
            "-RunTime", "12", "-AvcodecHardware", "any", "-ExpectedCells", "1",
            "-SkipCssKeys", "-LogPath", (Join-Path $vlcLogRoot "title-hardware.log")
        ) },
        @{ Name = "menu-hardware"; Arguments = @(
            "-VlcRoot", $VlcRoot, "-DvdPath", $outputPath, "-Menus", "-RunTime", "12",
            "-AvcodecHardware", "any", "-ExpectedCells", "1", "-SkipCssKeys",
            "-LogPath", (Join-Path $vlcLogRoot "menu-hardware.log")
        ) }
    )
    foreach ($check in $vlcChecks) {
        Assert-NotCanceled
        Write-Status -State "running" -Stage "vlc-$($check.Name)" `
            -Message "Running patched-VLC $($check.Name) gate"
        # VLC's frame-threaded HEVC decoder can very rarely report a transient
        # duplicate POC while it is simultaneously dropping late dummy-output
        # frames. A clean replay of the exact same authored bytes is sufficient
        # to distinguish that probe race from deterministic media corruption.
        # Keep the failed evidence and retry only the menu gate once; a repeated
        # failure still takes the source-relative fallback and then fails hard.
        $vlcAttemptLimit = if ($check.Name -eq "menu-hardware") { 2 } else { 1 }
        $vlcExit = 1
        for ($vlcAttempt = 1; $vlcAttempt -le $vlcAttemptLimit; $vlcAttempt++) {
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $vlcRunner @($check.Arguments) 2>&1 |
                Write-Utf8Log
            $vlcExit = $LASTEXITCODE
            if ($vlcExit -eq 0) {
                break
            }
            if ($vlcAttempt -lt $vlcAttemptLimit) {
                $failedVlcLog = Join-Path $vlcLogRoot "$($check.Name)-attempt-$vlcAttempt.log"
                Copy-Item -LiteralPath (Join-Path $vlcLogRoot "$($check.Name).log") `
                    -Destination $failedVlcLog -Force
                "Patched-VLC $($check.Name) attempt $vlcAttempt failed; retrying once" |
                    Write-Utf8Log
            }
        }
        if ($vlcExit -ne 0 -and $check.Name -eq "menu-hardware") {
            $sourceMenuLog = Join-Path $vlcLogRoot "source-menu-hardware.log"
            $menuEquivalence = Join-Path $vlcLogRoot "menu-navigation-equivalence.json"
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $vlcRunner `
                -VlcRoot $VlcRoot -DvdPath $script:sourcePath -Menus -RunTime 12 `
                -AvcodecHardware any -ExpectedCells 1 -SkipCssKeys -SkipValidation `
                -LogPath $sourceMenuLog 2>&1 | Write-Utf8Log
            if ($LASTEXITCODE -eq 0) {
                & $Python -u $cli "validate-vlc-menu-equivalence" $sourceMenuLog `
                    (Join-Path $vlcLogRoot "menu-hardware.log") `
                    "--minimum-cell-changes" "1" "--report" $menuEquivalence 2>&1 |
                    Write-Utf8Log
                if ($LASTEXITCODE -eq 0) {
                    $vlcExit = 0
                }
            }
        }
        if ($vlcExit -ne 0) {
            throw "Patched-VLC $($check.Name) gate failed with exit code $vlcExit"
        }
    }

    Write-Status -State "passed" -Stage "complete" `
        -Message "Generalized all-HEVC compact ISO passed source, graph, CSS-safe, and VLC gates"
    "[$((Get-Date).ToString('o'))] PASS - $outputPath" | Write-Utf8Log
    exit 0
}
catch {
    if ($null -ne $script:audioJob) {
        Stop-Job -Job $script:audioJob -ErrorAction SilentlyContinue
        Remove-Job -Job $script:audioJob -Force -ErrorAction SilentlyContinue
        $script:audioJob = $null
    }
    if (Test-Path -LiteralPath $cancelPath -PathType Leaf) {
        Write-Status -State "canceled" -Stage $script:currentStage -Message $_.Exception.Message
        "[$((Get-Date).ToString('o'))] CANCELED - $($_.Exception.Message)" | Write-Utf8Log
        exit 3
    }
    Write-Status -State "failed" -Stage $script:currentStage -Message $_.Exception.Message
    "[$((Get-Date).ToString('o'))] FAILED - $($_.Exception.Message)`r`n$($_ | Out-String)" |
        Write-Utf8Log
    exit 1
}
