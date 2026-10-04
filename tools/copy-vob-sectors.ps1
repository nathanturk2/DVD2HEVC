[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Source,
    [Parameter(Mandatory = $true)][string]$Destination,
    [Parameter(Mandatory = $true)][long]$SourceSector,
    [Parameter(Mandatory = $true)][long]$DestinationSector,
    [Parameter(Mandatory = $true)][ValidateRange(1, 1048576)][int]$SectorCount
)

$ErrorActionPreference = 'Stop'
$sectorSize = 2048
$sourcePath = [IO.Path]::GetFullPath($Source)
$destinationPath = [IO.Path]::GetFullPath($Destination)
$byteCount = [long]$SectorCount * $sectorSize
$sourceOffset = $SourceSector * $sectorSize
$destinationOffset = $DestinationSector * $sectorSize

$sourceInfo = Get-Item -LiteralPath $sourcePath
$destinationInfo = Get-Item -LiteralPath $destinationPath
if ($sourceOffset + $byteCount -gt $sourceInfo.Length) {
    throw 'Source sector range extends beyond the source VOB'
}
if ($destinationOffset + $byteCount -gt $destinationInfo.Length) {
    throw 'Destination sector range extends beyond the destination VOB'
}

$buffer = [byte[]]::new($byteCount)
$sourceStream = [IO.File]::Open($sourcePath, 'Open', 'Read', 'Read')
try {
    $sourceStream.Position = $sourceOffset
    $read = 0
    while ($read -lt $buffer.Length) {
        $amount = $sourceStream.Read($buffer, $read, $buffer.Length - $read)
        if ($amount -eq 0) { throw 'Unexpected end of source VOB' }
        $read += $amount
    }
}
finally {
    $sourceStream.Dispose()
}

$destinationStream = [IO.File]::Open($destinationPath, 'Open', 'Write', 'None')
try {
    $destinationStream.Position = $destinationOffset
    $destinationStream.Write($buffer, 0, $buffer.Length)
    $destinationStream.Flush($true)
}
finally {
    $destinationStream.Dispose()
}

Write-Host "Copied $SectorCount VOB sectors: source=$SourceSector destination=$DestinationSector"
