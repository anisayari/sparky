# Signal this installation's Sparky server. It owns and stops the engines it started.
$ErrorActionPreference = 'Stop'
$rootPath = [IO.Path]::GetFullPath($PSScriptRoot).ToLowerInvariant()
$sha = [Security.Cryptography.SHA256]::Create()
try {
  $digest = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($rootPath))).Replace('-', '').ToLowerInvariant()
} finally { $sha.Dispose() }
$name = "Local\Sparky-$digest"
$running = $null
$stopSignal = $null
function Wait-SparkyExit([int]$milliseconds) {
  try { $finished = $running.WaitOne($milliseconds) }
  catch [Threading.AbandonedMutexException] { $finished = $true }
  if ($finished) { $running.ReleaseMutex() }
  return $finished
}
try {
  try { $running = [Threading.Mutex]::OpenExisting("$name-running") }
  catch [Threading.WaitHandleCannotBeOpenedException] {
    Write-Host 'No managed Sparky instance is running from this folder.'
    return
  }
  # The server may be creating or closing its event as we acquire the mutex handle.
  $opening = [Diagnostics.Stopwatch]::StartNew()
  while (-not $stopSignal) {
    try { $stopSignal = [Threading.EventWaitHandle]::OpenExisting("$name-stop") }
    catch [Threading.WaitHandleCannotBeOpenedException] {
      if (Wait-SparkyExit 50) { Write-Host 'Sparky stopped.'; return }
      if ($opening.Elapsed.TotalSeconds -gt 5) { throw 'Sparky has not finished registering its stop signal.' }
    }
  }
  [void]$stopSignal.Set()
  if (-not (Wait-SparkyExit 30000)) { throw 'Stop requested. Sparky has not finished within 30 seconds.' }
  Write-Host 'Sparky stopped.'
} finally {
  if ($stopSignal) { $stopSignal.Dispose() }
  if ($running) { $running.Dispose() }
}
