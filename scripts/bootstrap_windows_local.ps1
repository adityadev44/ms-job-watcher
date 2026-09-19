param(
    [switch]$InstallWatcherTask,
    [int]$WatcherIntervalMinutes = 10
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

function Require-Command([string]$Name, [string]$InstallHint) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "$Name is required. $InstallHint"
    }
}

Require-Command "git" "Install Git for Windows, then reopen PowerShell."
Require-Command "py" "Install Python 3.12 from python.org, then reopen PowerShell."

Write-Host "Setting up local job watcher in $RepoRoot"

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    try {
        & py -3.12 -m venv .venv
    }
    catch {
        & py -3 -m venv .venv
    }
}

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Pip = Join-Path $RepoRoot ".venv\Scripts\pip.exe"

& $Python -m pip install --upgrade pip
& $Pip install -r requirements.txt
& $Python -m playwright install firefox chromium

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host ""
    Write-Host "Created .env from .env.example."
    Write-Host "Fill GMAIL_USER, GMAIL_APP_PASSWORD and ALERT_RECIPIENT locally."
    Write-Host "Do not commit .env."
}

Write-Host ""
Write-Host "Validating company registry and configuration..."
& $Python src/run_all.py --validate
if ($LASTEXITCODE -ne 0) {
    throw "Validation failed. Fix the reported error before scheduling the watcher."
}

if ($InstallWatcherTask) {
    if ($WatcherIntervalMinutes -lt 5) {
        throw "WatcherIntervalMinutes must be at least 5."
    }

    $TaskName = "Local Job Watcher"
    $Runner = Join-Path $RepoRoot "scripts\run_local_watcher.ps1"
    $TaskCommand = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$Runner`""
    & schtasks.exe /Create /SC MINUTE /MO $WatcherIntervalMinutes /TN $TaskName /TR $TaskCommand /F | Out-Host

    if ($LASTEXITCODE -ne 0) {
        throw "Windows Task Scheduler registration failed."
    }

    Write-Host "Scheduled '$TaskName' every $WatcherIntervalMinutes minutes for the current Windows user."
}

Write-Host ""
Write-Host "Local setup complete."
Write-Host "Run a manual smoke test with:"
Write-Host "  powershell -ExecutionPolicy Bypass -File .\scripts\run_local_watcher.ps1"
