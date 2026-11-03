# Apply only the release-owned files of an already extracted official ZIP.
param([Parameter(Mandatory)][string]$Source, [Parameter(Mandatory)][string]$Destination, [string]$Baseline)
$ErrorActionPreference = 'Stop'
$sourceDirs = @('app','docs','tests','.github')
$sourceFiles = @('.gitignore','.gitattributes','LICENSE','README.md','VERSION','install.bat','install.ps1','sync-zip.ps1',
                 'start.bat','start.ps1','stop.bat','stop.ps1','requirements.txt','pyproject.toml')

function Get-ReleaseHashes([string]$folder) {
  $files = @()
  foreach ($dir in $sourceDirs) {
    $path = Join-Path $folder $dir
    if ([IO.Directory]::Exists($path)) { $files += @(Get-ChildItem -LiteralPath $path -File -Recurse -Force) }
  }
  foreach ($name in $sourceFiles) {
    $path = Join-Path $folder $name
    if ([IO.File]::Exists($path)) { $files += Get-Item -LiteralPath $path }
  }
  $hashes = @{}
  foreach ($file in $files) {
    $name = $file.FullName.Substring($folder.Length + 1).Replace('\', '/')
    $hashes[$name] = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
  }
  if (-not $hashes['app/server.py'] -or -not $hashes['VERSION']) { throw 'Incomplete Sparky source archive.' }
  return $hashes
}

function Get-Target([string]$name) {
  $parts = $name.Split('/')
  if ($name -notin $sourceFiles -and ($parts.Length -lt 2 -or $parts[0] -notin $sourceDirs)) { throw "Unexpected source path: $name" }
  $path = $Destination
  foreach ($part in $parts) {
    if (-not $part -or $part -match '[\\:*?"<>|]|[. ]$|^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.|$)') { throw "Invalid source path: $name" }
    $path = Join-Path $path $part
    if ((Test-Path -LiteralPath $path) -and ((Get-Item -Force -LiteralPath $path).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
      throw "Source path uses a link: $path"
    }
  }
  return $path
}

function Save-Hashes($hashes) {
  $stage = $manifest + '.' + [guid]::NewGuid().ToString('N')
  try {
    [IO.File]::WriteAllText($stage, ($hashes | ConvertTo-Json))
    if ([IO.File]::Exists($manifest)) { [IO.File]::Replace($stage, $manifest, [System.Management.Automation.Language.NullString]::Value) }
    else { [IO.File]::Move($stage, $manifest) }
  } finally { if ([IO.File]::Exists($stage)) { [IO.File]::Delete($stage) } }
}

$Source = (Get-Item -LiteralPath $Source).FullName.TrimEnd('\', '/')
$Destination = [IO.Directory]::CreateDirectory($Destination).FullName.TrimEnd('\', '/')
$manifest = Join-Path $Destination '.sparky-zip-sources.json'
$lockPath = Join-Path $Destination '.sparky-zip-update.lock'
$lock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
try {
  $next = Get-ReleaseHashes $Source
  $previous = @{}
  if ([IO.File]::Exists($manifest)) {
    $stored = Get-Content -LiteralPath $manifest -Raw | ConvertFrom-Json
    foreach ($p in $stored.PSObject.Properties) { $previous[$p.Name] = $p.Value }
  } elseif ($Baseline) {
    $baseFolder = (Get-Item -LiteralPath $Baseline).FullName.TrimEnd('\', '/')
    $previous = Get-ReleaseHashes $baseFolder
  } elseif ([IO.File]::Exists((Join-Path $Destination 'app/server.py'))) {
    throw 'No verified baseline for this ZIP installation. Source files were left intact.'
  }
  $changes = @()
  foreach ($name in @($next.Keys) + @($previous.Keys) | Sort-Object -Unique) {
    $target = Get-Target $name
    $actual = if (Test-Path -LiteralPath $target) { (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash }
    if ($actual -eq $next[$name] -or $previous[$name] -eq $next[$name]) { continue }
    if ($actual -ne $previous[$name]) { throw "Local source differs: $name. Back up or merge your change before updating." }
    $changes += $name
  }
  # Keep the old baseline until every file is published, allowing the same release to resume after interruption.
  if (-not [IO.File]::Exists($manifest)) { Save-Hashes $previous }
  foreach ($name in @($changes | Where-Object { $_ -ne 'VERSION' }) + @($changes | Where-Object { $_ -eq 'VERSION' })) {
    $target = Get-Target $name
    if (-not $next[$name]) { [IO.File]::Delete($target); continue }
    [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target))
    $stage = $target + '.' + [guid]::NewGuid().ToString('N')
    try {
      [IO.File]::Copy((Join-Path $Source $name), $stage)
      if ([IO.File]::Exists($target)) { [IO.File]::Replace($stage, $target, [System.Management.Automation.Language.NullString]::Value) }
      else { [IO.File]::Move($stage, $target) }
    } finally { if ([IO.File]::Exists($stage)) { [IO.File]::Delete($stage) } }
  }
  Save-Hashes $next
} finally { $lock.Dispose() }
