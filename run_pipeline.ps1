<#
.SYNOPSIS
    Runs the NFL 2.0 weekly pipeline, for both the hourly scheduled task and the
    "Start NFL 2.0.cmd" double-click.

.DESCRIPTION
    Both entry points go through this one script so they share the same
    interpreter, the same log, and -- the reason it exists -- the same lock.
    nfl_2_0.db is a single SQLite file; a scheduled run firing while you have a
    manual one open would have two writers on it.

    Always uses .venv\Scripts\python.exe when present. The system "py -3" is
    missing tzdata and can't verify TLS against half the upstream hosts, so it
    is only a last-resort fallback and says so loudly in the log.

.PARAMETER Open
    Open the rendered dashboard in a browser afterwards. Off for scheduled runs.

.PARAMETER ExtraArgs
    Passed straight through to main.py:

        & .\run_pipeline.ps1 -ExtraArgs '--skip-scrape','--no-publish'

    Call the script in-process like that, not via "powershell.exe -File",
    which flattens an array parameter into one comma-joined string. Splitting
    it back apart here isn't an option -- "--ingest-seasons 2023,2024,2025"
    is a single argument that legitimately contains commas.
#>
[CmdletBinding()]
param(
    [switch] $Open,
    [string[]] $ExtraArgs = @()
)

$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location -LiteralPath $AppDir

$LogDir = Join-Path $AppDir 'logs'
if (-not (Test-Path -LiteralPath $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}
$LogFile = Join-Path $LogDir 'pipeline.log'
$LockFile = Join-Path $AppDir '.pipeline.lock'
$MaxLogBytes = 5MB

function Write-Both {
    param([string] $Message)
    Write-Output $Message
    Add-Content -LiteralPath $LogFile -Value $Message -Encoding utf8
}

# Rotate before writing, so one run never straddles the cut.
if ((Test-Path -LiteralPath $LogFile) -and ((Get-Item -LiteralPath $LogFile).Length -gt $MaxLogBytes)) {
    Move-Item -LiteralPath $LogFile -Destination (Join-Path $LogDir 'pipeline.prev.log') -Force
}

# --- Lock ------------------------------------------------------------------
# A file rather than a mutex: a scheduled run and a double-click land in
# different sessions, and a Local\ mutex isn't shared across those.
#
# The lock holds this wrapper's PID plus its start time. The PID alone isn't
# enough to decide a lock is live -- Windows recycles PIDs, so a lock orphaned
# by a killed run could name a PID that now belongs to something unrelated, and
# the hourly task would skip forever. Matching the start time too settles it.
function Get-LockHolder {
    if (-not (Test-Path -LiteralPath $LockFile)) { return $null }
    $raw = Get-Content -LiteralPath $LockFile -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $raw) { return $null }
    $parts = [string]$raw -split '\|'
    $holderPid = 0
    if (-not [int]::TryParse($parts[0], [ref] $holderPid)) { return $null }
    $proc = Get-Process -Id $holderPid -ErrorAction SilentlyContinue
    if (-not $proc) { return $null }
    if ($parts.Count -lt 2 -or $parts[1] -ne $proc.StartTime.Ticks.ToString()) { return $null }
    return $holderPid
}

$holderPid = Get-LockHolder
if ($null -ne $holderPid) {
    Write-Both "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') SKIPPED: a pipeline run (PID $holderPid) is already going."
    exit 0
}
Remove-Item -LiteralPath $LockFile -Force -ErrorAction SilentlyContinue

# --- Interpreter -----------------------------------------------------------
$VenvPython = Join-Path $AppDir '.venv\Scripts\python.exe'
$usingVenv = Test-Path -LiteralPath $VenvPython
if ($usingVenv) {
    $exe = $VenvPython
    $exeArgs = @()
} else {
    $exe = 'py'
    $exeArgs = @('-3')
}

$pyArgs = @('main.py')
if (-not $Open) { $pyArgs += '--no-open' }
$pyArgs += $ExtraArgs

$started = Get-Date
Write-Both ''
Write-Both "===== $($started.ToString('yyyy-MM-dd HH:mm:ss')) ===== $(if ($usingVenv) { '.venv' } else { 'py -3' }) $($pyArgs -join ' ')"
if (-not $usingVenv) {
    Write-Both 'WARNING: .venv is missing, falling back to the system Python. Expect tzdata / TLS failures.'
    Write-Both '         Rebuild it:  py -3 -m venv .venv; .venv\Scripts\python.exe -m pip install -r requirements-local.txt'
}

# Native stderr redirected inside PowerShell 5.1 arrives as ErrorRecord objects
# and would terminate the script under 'Stop', so keep it at Continue and judge
# success by $LASTEXITCODE instead.
$ErrorActionPreference = 'Continue'
$exitCode = 1
try {
    Set-Content -LiteralPath $LockFile -Encoding utf8 `
        -Value "$PID|$((Get-Process -Id $PID).StartTime.Ticks)"
    # Not Tee-Object: in PowerShell 5.1 it writes UTF-16 with no -Encoding to
    # override, which turns the log into mojibake next to the UTF-8 headers.
    & $exe @exeArgs @pyArgs 2>&1 | ForEach-Object {
        if ($_ -is [System.Management.Automation.ErrorRecord]) { Write-Both $_.ToString() }
        else { Write-Both ([string] $_) }
    }
    # $LASTEXITCODE is $null when the exe never launched; `exit $null` is 0,
    # which would show Task Scheduler a success for a run that did nothing.
    $exitCode = if ($null -eq $LASTEXITCODE) { 1 } else { $LASTEXITCODE }
} finally {
    Remove-Item -LiteralPath $LockFile -Force -ErrorAction SilentlyContinue
}

$elapsed = [math]::Round(((Get-Date) - $started).TotalSeconds, 1)
Write-Both "----- exit $exitCode after ${elapsed}s"

# Surfaces as Task Scheduler's "Last Run Result", so a broken run is visible
# there without opening the log.
exit $exitCode
