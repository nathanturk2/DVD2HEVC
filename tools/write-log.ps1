function Add-Dvd2HevcLogLine {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$Line,
        [ValidateRange(1, 120)]
        [int]$RetryCount = 60
    )

    $encoding = New-Object System.Text.UTF8Encoding($false)
    for ($attempt = 0; $attempt -lt $RetryCount; $attempt++) {
        $stream = $null
        try {
            # Explicit ReadWrite sharing lets the GUI inspect progress while a
            # runner appends output. A retry still covers transient exclusive
            # opens from antivirus, indexing, or external log viewers.
            $stream = [IO.File]::Open(
                $Path,
                [IO.FileMode]::OpenOrCreate,
                [IO.FileAccess]::Write,
                [IO.FileShare]::ReadWrite
            )
            [void]$stream.Seek(0, [IO.SeekOrigin]::End)
            $bytes = $encoding.GetBytes($Line + [Environment]::NewLine)
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.Flush()
            return
        }
        catch [IO.IOException] {
            if ($attempt -ge $RetryCount - 1) { throw }
            Start-Sleep -Milliseconds ([Math]::Min(250, 25 + (25 * $attempt)))
        }
        finally {
            if ($null -ne $stream) { $stream.Dispose() }
        }
    }
}

function Move-Dvd2HevcAtomicFile {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$TemporaryPath,
        [Parameter(Mandatory = $true)]
        [string]$DestinationPath,
        [ValidateRange(1, 120)]
        [int]$RetryCount = 60
    )

    # Windows PowerShell 5 / .NET Framework rejects a null backup path in the
    # four-argument File.Replace overload. Keep the backup beside the temporary
    # file, remove it after a successful swap, and clear any stale copy before
    # a retry.
    $backupPath = "$TemporaryPath.replace-backup"
    for ($attempt = 0; $attempt -lt $RetryCount; $attempt++) {
        try {
            if ([IO.File]::Exists($DestinationPath)) {
                # File.Replace is atomic on the destination volume and avoids
                # PowerShell 5 Move-Item's intermittent "already exists"
                # failure when the GUI polls a status file at the same time.
                if ([IO.File]::Exists($backupPath)) {
                    [IO.File]::Delete($backupPath)
                }
                [IO.File]::Replace($TemporaryPath, $DestinationPath, $backupPath, $true)
                try { [IO.File]::Delete($backupPath) } catch { }
            }
            else {
                [IO.File]::Move($TemporaryPath, $DestinationPath)
            }
            return
        }
        catch {
            # PowerShell wraps .NET method-call failures in
            # MethodInvocationException, so inspect the innermost cause before
            # deciding whether a transient filesystem collision is retryable.
            $cause = $_.Exception
            while ($null -ne $cause.InnerException) {
                $cause = $cause.InnerException
            }
            $retryable = ($cause -is [IO.IOException]) -or
                ($cause -is [UnauthorizedAccessException])
            if (-not $retryable) { throw }
            if ($attempt -ge $RetryCount - 1) { throw }
            Start-Sleep -Milliseconds ([Math]::Min(250, 25 + (25 * $attempt)))
        }
    }
}
