param(
  [ValidateSet("start", "retry-failed", "status", "logs", "stop")]
  [string]$Action = "status"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Data = Join-Path $Root "data"
$PidFile = Join-Path $Data "backfill.pid"
$LogFile = Join-Path $Data "backfill.log"
$ErrorLogFile = Join-Path $Data "backfill.error.log"
$Provider = if ($env:FININTEL_BACKFILL_PROVIDER) { $env:FININTEL_BACKFILL_PROVIDER } else { "alpaca_sip" }
$StateFile = if ($Provider -eq "alpaca_sip") { Join-Path $Data "finintel.backfill.json" } else { Join-Path $Data "finintel.$Provider.backfill.json" }

New-Item -ItemType Directory -Force -Path $Data | Out-Null

function Get-Worker {
  if (-not (Test-Path $PidFile)) { return $null }
  $workerPid = [int](Get-Content $PidFile -Raw)
  try { return Get-Process -Id $workerPid -ErrorAction Stop } catch { return $null }
}

switch ($Action) {
  "start" { $retry = $false }
  "retry-failed" { $retry = $true }
  "status" {
    $worker = Get-Worker
    if ($worker) { Write-Output "RUNNING (PID $($worker.Id))" } else { Write-Output "NOT RUNNING" }
    if (Test-Path $StateFile) { Get-Content $StateFile }
    return
  }
  "logs" {
    if (Test-Path $LogFile) { Get-Content $LogFile -Wait }
    elseif (Test-Path $ErrorLogFile) { Get-Content $ErrorLogFile -Wait }
    else { Write-Output "No log yet." }
    return
  }
  "stop" {
    $worker = Get-Worker
    if ($worker) { Stop-Process -Id $worker.Id; Write-Output "Stop requested for PID $($worker.Id)." }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    return
  }
  default { throw "Unsupported backfill action: $Action" }
}

if (Get-Worker) { Write-Output "A backfill is already running."; exit 0 }
if (-not (Test-Path $Python)) { throw "Missing Python environment: $Python" }
$args = @("-u", "-m", "finintel.market_data.backfill_worker", "--provider", $Provider,
  "--period", $(if ($env:FININTEL_BACKFILL_PERIOD) { $env:FININTEL_BACKFILL_PERIOD } else { "10y" }),
  "--max-requests-per-day", $(if ($env:FININTEL_BACKFILL_MAX_REQUESTS_PER_DAY) { $env:FININTEL_BACKFILL_MAX_REQUESTS_PER_DAY } else { "0" }),
  "--delay-seconds", $(if ($env:FININTEL_BACKFILL_DELAY_SECONDS) { $env:FININTEL_BACKFILL_DELAY_SECONDS } else { "0" }),
  "--state-path", $StateFile)
if ($retry) { $args += "--retry-failed" }
$env:PYTHONPATH = Join-Path $Root "backend"
$env:FININTEL_DB_PATH = if ($env:FININTEL_DB_PATH) { $env:FININTEL_DB_PATH } else { Join-Path $Data "finintel.sqlite3" }
$process = Start-Process -FilePath $Python -ArgumentList $args -WorkingDirectory $Root -RedirectStandardOutput $LogFile -RedirectStandardError $ErrorLogFile -PassThru
$process.Id | Set-Content $PidFile
Write-Output "Running as PID $($process.Id). Logs: $LogFile"
