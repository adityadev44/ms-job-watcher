param(
    [int]$Workers = 20
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    throw "Local environment not found. Run scripts\bootstrap_windows_local.ps1 first."
}

Set-Location $RepoRoot

# Keep local runs serialized so two overlapping Task Scheduler invocations
# cannot send duplicate alerts or race on seen-job state.
$MutexName = "Global\MSJobWatcherLocalPrimary"
$CreatedNew = $false
$Mutex = New-Object System.Threading.Mutex($true, $MutexName, [ref]$CreatedNew)

if (-not $CreatedNew) {
    Write-Host "Another local watcher run is already active. Exiting."
    $Mutex.Dispose()
    exit 0
}

try {
    & $Python -u src/run_all.py --workers $Workers
    exit $LASTEXITCODE
}
finally {
    try { $Mutex.ReleaseMutex() } catch {}
    $Mutex.Dispose()
}
