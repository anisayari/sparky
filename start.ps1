# Starts Sparky (server + ComfyUI + assistant) and opens the UI in the browser.
$Root = $PSScriptRoot
$cfg = @{}
if (Test-Path "$Root\config.json") { (Get-Content "$Root\config.json" -Raw | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $cfg[$_.Name] = $_.Value } }
$port = if ($cfg.app_port) { $cfg.app_port } else { 7860 }
$py = if ($cfg.python) { $cfg.python } else { "$Root\runtime\venv\Scripts\python.exe" }
if (-not (Test-Path $py)) {
  Add-Type -AssemblyName PresentationFramework
  [System.Windows.MessageBox]::Show("Python not found ($py). Run install.ps1 first.", 'Sparky') | Out-Null
  exit 1
}
function Up { try { (New-Object Net.Sockets.TcpClient('127.0.0.1', $port)).Close(); $true } catch { $false } }
if (-not (Up)) {
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
      Add-Type -AssemblyName PresentationFramework
      [System.Windows.MessageBox]::Show($_.Exception.Message, 'Sparky') | Out-Null
      exit 1
    }
  }
  Start-Process $py -ArgumentList "app\server.py", "--port", $port -WorkingDirectory $Root -WindowStyle Hidden `
    -RedirectStandardOutput "$Root\data\server.log" -RedirectStandardError "$Root\data\server.err.log"
  for ($i = 0; $i -lt 60 -and -not (Up); $i++) { Start-Sleep -Milliseconds 500 }
}
Start-Process "http://127.0.0.1:$port"
