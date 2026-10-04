[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [datetimeoffset]$After,
    [string]$OutputReport
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$jobRoot = Join-Path $projectRoot "reports\jobs"
if (-not $OutputReport) {
    $OutputReport = Join-Path $projectRoot "reports\first-grouped-job-monitor.json"
}
$reportPath = [IO.Path]::GetFullPath($OutputReport)
$terminalStates = @("passed", "failed", "canceled")

function Read-JsonRetry {
    param([string]$Path)
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        try {
            if (Test-Path -LiteralPath $Path -PathType Leaf) {
                return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
            }
        }
        catch {
            if ($attempt -ge 19) { throw }
        }
        Start-Sleep -Milliseconds 250
    }
    return $null
}

function Write-Monitor {
    param([hashtable]$Value)
    $Value.schema = "dvd2hevc-first-grouped-job-monitor-v1"
    $Value.updated_at = (Get-Date).ToString("o")
    $temporary = "$reportPath.$PID.$([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()).tmp"
    New-Item -ItemType Directory -Path (Split-Path -Parent $reportPath) -Force | Out-Null
    $Value | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $temporary -Encoding UTF8
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        try {
            Move-Item -LiteralPath $temporary -Destination $reportPath -Force
            return
        }
        catch [IO.IOException] {
            if ($attempt -ge 39) { throw }
            Start-Sleep -Milliseconds ([Math]::Min(500, 25 * ($attempt + 1)))
        }
        catch [UnauthorizedAccessException] {
            if ($attempt -ge 39) { throw }
            Start-Sleep -Milliseconds ([Math]::Min(500, 25 * ($attempt + 1)))
        }
    }
}

Write-Monitor @{
    state = "waiting-for-grouped-job-start"
    after = $After.ToString("o")
    monitor_pid = $PID
}

$selectedJobPath = $null
$selectedJob = $null
$selectedStatus = $null
while ($null -eq $selectedJob) {
    $candidates = @(Get-ChildItem -LiteralPath $jobRoot -Directory -ErrorAction SilentlyContinue |
        ForEach-Object {
            $jobPath = Join-Path $_.FullName "job.json"
            $job = Read-JsonRetry $jobPath
            if ($null -ne $job -and $job.started_at) {
                [pscustomobject]@{
                    Path = $jobPath
                    Job = $job
                    Started = [datetimeoffset]$job.started_at
                }
            }
        } | Where-Object { $_.Started -gt $After } | Sort-Object Started)
    foreach ($candidate in $candidates) {
        $statusPath = Join-Path ([string]$candidate.Job.work_root) "status.json"
        $status = Read-JsonRetry $statusPath
        if ([string]$status.title_scheduler -eq "physical-vts-grouped-v1") {
            $selectedJobPath = $candidate.Path
            $selectedJob = $candidate.Job
            $selectedStatus = $status
            break
        }
    }
    if ($null -eq $selectedJob) {
        Start-Sleep -Seconds 15
    }
}

Write-Monitor @{
    state = "grouped-job-started"
    after = $After.ToString("o")
    monitor_pid = $PID
    job_id = [string]$selectedJob.id
    source = [string]$selectedJob.source
    output = [string]$selectedJob.output
    job_started_at = [string]$selectedJob.started_at
    scheduler = [string]$selectedStatus.title_scheduler
    physical_vts = @($selectedStatus.physical_vts)
}

# Second explicit wait: follow the selected job only until it reaches a
# terminal state. A later watched-batch job cannot accidentally satisfy it.
while ($true) {
    $selectedJob = Read-JsonRetry $selectedJobPath
    $selectedStatus = Read-JsonRetry (
        Join-Path ([string]$selectedJob.work_root) "status.json"
    )
    if ($terminalStates -contains [string]$selectedJob.status) {
        break
    }
    Write-Monitor @{
        state = "waiting-for-grouped-job-terminal-state"
        after = $After.ToString("o")
        monitor_pid = $PID
        job_id = [string]$selectedJob.id
        source = [string]$selectedJob.source
        output = [string]$selectedJob.output
        job_status = [string]$selectedJob.status
        pipeline_stage = [string]$selectedStatus.stage
        physical_vts = @($selectedStatus.physical_vts)
    }
    Start-Sleep -Seconds 30
}

$workRoot = [string]$selectedJob.work_root
$groupedReports = @(Get-ChildItem -LiteralPath (Join-Path $workRoot "physical") `
    -Recurse -Filter "vts*-conversion-report.json" -File -ErrorAction SilentlyContinue |
    ForEach-Object { Read-JsonRetry $_.FullName })
$provenance = @(Get-ChildItem -LiteralPath (Join-Path $workRoot "physical") `
    -Recurse -Filter "vts*-title-provenance.json" -File -ErrorAction SilentlyContinue |
    ForEach-Object { Read-JsonRetry $_.FullName })
$expectedGrouped = @($selectedStatus.physical_vts).Count
$badValidationPolicy = @($groupedReports | Where-Object {
    [string]$_.pipeline.validation_policy -ne
        "random-access-and-full-decode-per-physical-cell"
})
$multiTaskReports = @($groupedReports | Where-Object {
    [int]$_.summary.unique_physical_tasks -gt 1
})
$missingOverlap = @($multiTaskReports | Where-Object {
    -not [bool]$_.pipeline.validation_overlap -or
    [string]$_.pipeline.mode -ne "encode-validation-overlap-v1"
})
$outputExists = Test-Path -LiteralPath ([string]$selectedJob.output) -PathType Leaf
$verified = (
    [string]$selectedJob.status -eq "passed" -and
    [string]$selectedStatus.state -eq "passed" -and
    [string]$selectedStatus.title_scheduler -eq "physical-vts-grouped-v1" -and
    $expectedGrouped -gt 0 -and
    $groupedReports.Count -eq $expectedGrouped -and
    $provenance.Count -eq $expectedGrouped -and
    $badValidationPolicy.Count -eq 0 -and
    $missingOverlap.Count -eq 0 -and
    $outputExists
)

Write-Monitor @{
    state = if ($verified) { "verified-passed" } else { "verification-failed" }
    verified = $verified
    monitor_pid = $PID
    job_id = [string]$selectedJob.id
    source = [string]$selectedJob.source
    output = [string]$selectedJob.output
    output_exists = $outputExists
    job_status = [string]$selectedJob.status
    job_started_at = [string]$selectedJob.started_at
    job_ended_at = [string]$selectedJob.ended_at
    pipeline_state = [string]$selectedStatus.state
    pipeline_stage = [string]$selectedStatus.stage
    scheduler = [string]$selectedStatus.title_scheduler
    expected_grouped_vts = $expectedGrouped
    grouped_reports = $groupedReports.Count
    provenance_reports = $provenance.Count
    multi_task_grouped_vts = $multiTaskReports.Count
    overlap_failures = $missingOverlap.Count
    validation_policy_failures = $badValidationPolicy.Count
    error = [string]$selectedJob.error
}
