# Starts Sparky (server + ComfyUI + assistant) and opens the UI in the browser.
param([switch]$NoBrowser, [switch]$NonInteractive)
$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
function Fail-Start([string]$message) {
  if (-not $NonInteractive) {
    Add-Type -AssemblyName PresentationFramework
    [void][System.Windows.MessageBox]::Show($message, 'Sparky')
  }
  throw $message
}
$cfg = @{}
$cfgPath = if ($env:SPARKY_CONFIG) { (Resolve-Path -LiteralPath $env:SPARKY_CONFIG).Path } else { Join-Path $Root 'config.json' }
if (Test-Path -LiteralPath $cfgPath) {
  (Get-Content -LiteralPath $cfgPath -Encoding UTF8 -Raw | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $cfg[$_.Name] = $_.Value }
  if ($env:SPARKY_CONFIG) { $env:SPARKY_CONFIG = $cfgPath }
}
$port = if ($cfg.app_port) { $cfg.app_port } else { 7860 }
$runtime = if ($cfg.runtime_dir) { [Environment]::ExpandEnvironmentVariables($cfg.runtime_dir) } else { 'runtime' }
if (-not [IO.Path]::IsPathRooted($runtime)) { $runtime = Join-Path $Root $runtime }
$py = if ($cfg.python) { [Environment]::ExpandEnvironmentVariables($cfg.python) } else { Join-Path $runtime 'venv\Scripts\python.exe' }
if (-not [IO.Path]::IsPathRooted($py)) { $py = Join-Path $Root $py }
if (-not (Test-Path $py)) {
  Fail-Start "Python not found ($py). Run install.ps1 first."
}
function Listening([int]$number) { try { (New-Object Net.Sockets.TcpClient('127.0.0.1', $number)).Close(); $true } catch { $false } }
function Ready {
  try {
    $expected = (Get-Content -LiteralPath (Join-Path $Root 'data/instance.json') -Raw | ConvertFrom-Json).instance
    if ($expected -notmatch '^[0-9a-f]{64}$') { return $false }
    $response = Invoke-RestMethod "http://127.0.0.1:$port/api/instance" -TimeoutSec 2 -MaximumRedirection 0
    return ($response.instance -ceq $expected)
  } catch { return $false }
}
if (-not (Ready)) {
  if ((Listening $port) -or (Listening ($port + 1))) {
    Fail-Start "Port $port or $($port + 1) is already occupied. Check the running service or choose another app_port."
  }
  New-Item -ItemType Directory -Force "$Root\data" | Out-Null
  if (Test-Path -LiteralPath (Join-Path $Root 'data/update-pending.json')) {
    # Recover an interrupted Git update before server.py imports its new dependencies.
    try {
      $repairLock = [IO.File]::Open((Join-Path $Root 'data/update-repair.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
      try {
        & $py (Join-Path $Root 'app/updater.py') --repair *> (Join-Path $Root 'data/update-repair.log')
        if ($LASTEXITCODE -ne 0) { throw "Update repair failed. See $Root\data\update-repair.log, then retry the shortcut." }
      } finally { $repairLock.Dispose() }
    } catch {
      Fail-Start $_.Exception.Message
    }
  }
  $server = Start-Process $py -PassThru -ArgumentList "app\server.py", "--port", $port -WorkingDirectory $Root -WindowStyle Hidden `
    -RedirectStandardOutput "$Root\data\server.log" -RedirectStandardError "$Root\data\server.err.log"
  $wait = [Diagnostics.Stopwatch]::StartNew()
  while (-not (Ready)) {
    if ($server.HasExited -or $wait.Elapsed.TotalSeconds -gt 60) {
      Fail-Start "Sparky did not become ready. See $Root\data\server.err.log."
    }
    Start-Sleep -Milliseconds 300
  }
}
if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$port" }
