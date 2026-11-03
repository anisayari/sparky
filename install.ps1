# Sparky - installer (Windows 10/11, x64 or ARM64)
#
#   From the project folder : powershell -ExecutionPolicy Bypass -File install.ps1
#   One line                : irm https://raw.githubusercontent.com/anisayari/sparky/main/install.ps1 | iex
#
# Options (or environment variables for the one-line install):
#   -Lang en|fr      / $env:SPARKY_LANG      interface and installer language
#   -Dir <folder>    / $env:SPARKY_DIR       install folder (default: %LOCALAPPDATA%\Sparky)
#   -Models          / $env:SPARKY_MODELS    also download the recommended models from the terminal
#                                            (by default they are chosen in the browser at first launch)
#   -Yes             / $env:SPARKY_YES       answer yes to every question (English unless -Lang)
#   -NoLaunch        / $env:SPARKY_NOLAUNCH  do not open Sparky at the end
#   -NoShortcuts                              leave the desktop and Start menu unchanged
#   -ChatOnly        / $env:SPARKY_CHAT_ONLY  skip PyTorch, ComfyUI and Studio model downloads
#
# This file is pure ASCII on purpose (a BOM would break "irm | iex"): accents and symbols are
# written as \uXXXX and decoded at runtime.
param([string]$Dir = "", [string]$Lang = "", [switch]$Models, [switch]$NoModels, [switch]$Yes, [switch]$NoLaunch,
      [switch]$ChatOnly, [switch]$NoShortcuts)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
$Repo = 'https://github.com/anisayari/sparky'
if (-not $Lang -and $env:SPARKY_LANG) { $Lang = $env:SPARKY_LANG }
if (-not $Dir -and $env:SPARKY_DIR) { $Dir = $env:SPARKY_DIR }
if ($env:SPARKY_MODELS) { $Models = $true }
if ($env:SPARKY_NOMODELS) { $NoModels = $true }   # old option: models are no longer downloaded by default
if ($env:SPARKY_NOLAUNCH) { $NoLaunch = $true }
if ($env:SPARKY_CHAT_ONLY -eq '1') { $ChatOnly = $true }
if ($env:SPARKY_YES) { $Yes = $true }
$Interactive = -not $Yes

# ------------------------------------------------------------------ look & feel
function U([string]$s) { [regex]::Unescape($s) }
$Fancy = [bool]$env:WT_SESSION -or $env:TERM_PROGRAM -eq 'vscode'   # Windows Terminal / VS Code draw every glyph
if ($Fancy) {
  $G = @{ tl = U '\u256d'; tr = U '\u256e'; bl = U '\u2570'; br = U '\u256f'; h = U '\u2500'; v = U '\u2502'
          ok = U '\u2714'; ko = U '\u2716'; dot = U '\u25cf'; star = U '\u2726'; arrow = U '\u203a'; full = U '\u2588'; empty = U '\u2591'
          spin = @((U '\u280b'), (U '\u2819'), (U '\u2839'), (U '\u2838'), (U '\u283c'), (U '\u2834'), (U '\u2826'), (U '\u2827'), (U '\u2807'), (U '\u280f')) }
} else {
  $G = @{ tl = U '\u250c'; tr = U '\u2510'; bl = U '\u2514'; br = U '\u2518'; h = U '\u2500'; v = U '\u2502'
          ok = U '\u221a'; ko = 'x'; dot = '*'; star = '*'; arrow = '>'; full = U '\u2588'; empty = U '\u2591'
          spin = @('|', '/', '-', '\') }
}
$BoxW = 58       # inner width of the boxes
$Mid = [char]0xB7

function Box([string[]]$lines, [string]$color = 'Magenta') {
  Write-Host ("   " + $G.tl + ($G.h * $BoxW) + $G.tr) -ForegroundColor $color
  foreach ($l in $lines) {
    $txt = "  " + $l
    if ($txt.Length -gt $BoxW) { $txt = $txt.Substring(0, $BoxW) }
    Write-Host ("   " + $G.v) -ForegroundColor $color -NoNewline
    Write-Host $txt.PadRight($BoxW) -NoNewline
    Write-Host $G.v -ForegroundColor $color
  }
  Write-Host ("   " + $G.bl + ($G.h * $BoxW) + $G.br) -ForegroundColor $color
}
$script:StepN = 0
$Total = 7
function Step([string]$title) {
  $script:StepN++
  Write-Host ""
  Write-Host ("  " + $G.dot + " ") -ForegroundColor Magenta -NoNewline
  Write-Host ((T 'step') + " $($script:StepN)/$Total   ") -ForegroundColor DarkGray -NoNewline
  Write-Host $title -ForegroundColor White
}
function Ok([string]$t) { Write-Host ("    " + $G.ok + " ") -ForegroundColor Green -NoNewline; Write-Host $t }
function Info([string]$t) { Write-Host ("      " + $t) -ForegroundColor DarkGray }
function Warn([string]$t) { Write-Host ("    ! " + $t) -ForegroundColor Yellow }
function Fail([string]$t) {
  Write-Host ("    " + $G.ko + " " + $t) -ForegroundColor Red
  if (Test-Path $script:Log) {
    Write-Host ""; Get-Content $script:Log -Tail 12 | ForEach-Object { Write-Host ("      " + $_) -ForegroundColor DarkGray }
    Write-Host ""; Write-Host ("      " + (T 'logat') + " " + $script:Log) -ForegroundColor DarkGray
  }
  exit 1
}
function Ask([string]$q, [bool]$def = $true) {
  if (-not $Interactive) { return $def }
  $hint = if ($def) { T 'yn' } else { T 'ny' }
  Write-Host ("    " + $G.arrow + " " + $q + " ") -ForegroundColor Cyan -NoNewline
  Write-Host ($hint + " ") -ForegroundColor DarkGray -NoNewline
  $r = ([string](Read-Host)).Trim().ToLower()
  if ($r -eq '') { return $def }
  return ($r -match '^(y|yes|o|oui)$')
}
function Fmt([double]$s) { if ($s -lt 60) { "{0:N0} s" -f $s } else { "{0:N0} min {1:N0} s" -f [math]::Floor($s / 60), ($s % 60) } }
function Cols { try { [Math]::Max(40, [Math]::Min(79, [Console]::WindowWidth - 2)) } catch { 79 } }
function Fit([string]$s) { $n = Cols; if ($s.Length -gt $n) { $s.Substring(0, $n) } else { $s.PadRight($n) } }
function Clear-Line { Write-Host ("`r" + (" " * (Cols)) + "`r") -NoNewline }

# run a program in the background with a spinner; its output goes to the log
$script:Log = Join-Path $env:TEMP 'sparky-install.log'
function Run([string]$label, [string]$exe, [string[]]$argv, [string]$hint = '') {
  $quoted = $argv | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }
  $out = "$script:Log.out"; $err = "$script:Log.err"
  $p = Start-Process -FilePath $exe -ArgumentList $quoted -PassThru -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err
  $null = $p.Handle
  $t0 = Get-Date; $i = 0
  while (-not $p.HasExited) {
    $line = "    " + $G.spin[$i % $G.spin.Count] + " " + $label + "   " + (Fmt ((Get-Date) - $t0).TotalSeconds)
    if ($hint) { $line += "   " + $hint }
    Write-Host ("`r" + (Fit $line)) -ForegroundColor Cyan -NoNewline
    Start-Sleep -Milliseconds 100; $i++
  }
  $p.WaitForExit()
  foreach ($f in $out, $err) { if (Test-Path $f) { Get-Content $f | Add-Content $script:Log; Remove-Item $f -ErrorAction SilentlyContinue } }
  Clear-Line
  if ($p.ExitCode -ne 0) { Fail ($label + " (code " + $p.ExitCode + ")") }
  Write-Host ("    " + $G.ok + " ") -ForegroundColor Green -NoNewline; Write-Host ($label + "   ") -NoNewline
  Write-Host (Fmt ((Get-Date) - $t0).TotalSeconds) -ForegroundColor DarkGray
}
# download with a progress bar (curl.exe is built into Windows 10+)
function Fetch([string]$label, [string]$url, [string]$file, [double]$size = 0, [string]$sha256 = '') {
  if (-not $size) { try { $size = [double](Invoke-WebRequest $url -Method Head -UseBasicParsing).Headers['Content-Length'] } catch { $size = 0 } }
  if (Test-Path $file) { Remove-Item $file -Force }
  $p = Start-Process curl.exe -ArgumentList '-L', '-s', '--fail', '--retry', '5', '-o', ('"' + $file + '"'), $url -PassThru -WindowStyle Hidden
  $null = $p.Handle
  while (-not $p.HasExited) {
    $done = 0; if (Test-Path $file) { $done = (Get-Item $file).Length }
    if ($size -gt 0) {
      $pc = [math]::Min(100, $done / $size * 100); $n = [int]($pc / 100 * 24)
      $bar = ($G.full * $n) + ($G.empty * (24 - $n))
      $line = "    {0}  {1}  {2,5:N1} %  {3:N0} / {4:N0} MB" -f $label, $bar, $pc, ($done / 1MB), ($size / 1MB)
    } else { $line = "    {0}   {1:N0} MB" -f $label, ($done / 1MB) }
    Write-Host ("`r" + (Fit $line)) -ForegroundColor Cyan -NoNewline
    Start-Sleep -Milliseconds 200
  }
  $p.WaitForExit()
  Clear-Line
  if ($p.ExitCode -ne 0 -or -not (Test-Path $file)) { Fail ((T 'dlfail') + " " + $url) }
  if ($sha256 -and (($sha256 -notmatch '^[a-f0-9]{64}$') -or (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -ne $sha256)) {
    Fail ("SHA-256 mismatch: " + $label)
  }
  Ok ($label + "   " + ("{0:N0} MB" -f ((Get-Item $file).Length / 1MB)))
}

# ------------------------------------------------------------------ texts
$TX = @{
  en = @{ step = 'Step'; yn = '[Y/n]'; ny = '[y/N]'; logat = 'Full log:'; dlfail = 'Download failed:'
          welcome1 = 'Chat, vision, images, code and voice dictation,'; welcome2 = 'running entirely on this PC. Nothing leaves it.'
          s1 = 'Checking your machine'; s2 = 'Sparky files'; s3 = 'Python'; s4 = 'Image engine (PyTorch + ComfyUI)'; s5 = 'AI engine (llama.cpp)'
          s6 = 'System scan'; s7 = 'Shortcuts'
          nogpu = 'No NVIDIA GPU: the assistant runs on the processor (slower), no image generation.'
          lowdisk = 'Less than 25 GB free: the tools take ~8 GB, plus 3 to 20 GB per model.'
          folder = 'Install folder'; folderq = 'Press Enter to accept, or type another folder:'; dlcode = 'Downloading Sparky'; updated = 'Updating Sparky'
          free = 'free'; pyinst = 'Installing Python 3.12 (winget)'; venv = 'Creating the Python environment'
          torch = 'Installing PyTorch'; torchhint = '(~2 GB, a few minutes)'; comfy = 'ComfyUI'; ipa = 'IP-Adapter'; deps = 'Installing dependencies'
          gpuok = 'GPU acceleration'; nogpuacc = 'no GPU acceleration'; scan = 'Analysing your hardware'
          level = 'Level:'; usable = 'GB usable for models'; recommended = 'Recommended for this PC'; assistant = 'Assistant'; images = 'Images'; voice = 'Voice'
          later = 'You will choose and install your models in the browser, at first launch.'; total = 'Total'
          shortcut = 'Desktop and Start menu shortcuts: Sparky'; ready = 'Sparky is ready!'; launchhow = 'Launch it with the "Sparky" shortcut on your desktop,'
          launchhow2 = 'or start.bat in the install folder.'; opening = 'Opening Sparky in your browser...'; bye = 'Enjoy!'; already = 'already installed'; existing = 'Using' }
  fr = @{ step = (U '\u00c9tape'); yn = '[O/n]'; ny = '[o/N]'; logat = 'Journal complet :'; dlfail = (U 'T\u00e9l\u00e9chargement impossible :')
          welcome1 = (U 'Chat, vision, images, code et dict\u00e9e vocale,'); welcome2 = (U 'enti\u00e8rement sur ce PC. Rien n''en sort.')
          s1 = (U 'V\u00e9rification de ta machine'); s2 = 'Fichiers de Sparky'; s3 = 'Python'; s4 = (U 'Moteur d''images (PyTorch + ComfyUI)'); s5 = 'Moteur IA (llama.cpp)'
          s6 = 'Diagnostic de ta machine'; s7 = 'Raccourcis'
          nogpu = (U 'Pas de GPU NVIDIA : l''assistant tourne sur le processeur (plus lent), pas d''images.')
          lowdisk = (U 'Moins de 25 Go libres : les outils prennent ~8 Go, plus 3 \u00e0 20 Go par mod\u00e8le.')
          folder = (U 'Dossier d''installation'); folderq = (U 'Entr\u00e9e pour accepter, ou tape un autre dossier :'); dlcode = (U 'T\u00e9l\u00e9chargement de Sparky'); updated = (U 'Mise \u00e0 jour de Sparky')
          free = 'libres'; pyinst = 'Installation de Python 3.12 (winget)'; venv = (U 'Cr\u00e9ation de l''environnement Python')
          torch = 'Installation de PyTorch'; torchhint = '(~2 Go, quelques minutes)'; comfy = 'ComfyUI'; ipa = 'IP-Adapter'; deps = (U 'Installation des d\u00e9pendances')
          gpuok = (U 'Acc\u00e9l\u00e9ration GPU'); nogpuacc = (U 'sans acc\u00e9l\u00e9ration GPU'); scan = (U 'Analyse du mat\u00e9riel')
          level = 'Niveau :'; usable = (U 'Go utilisables pour les mod\u00e8les'); recommended = (U 'Recommand\u00e9 pour ce PC'); assistant = 'Assistant'; images = 'Images'; voice = 'Voix'
          later = (U 'Tu choisiras et installeras tes mod\u00e8les dans le navigateur, au premier lancement.'); total = 'Total'
          shortcut = (U 'Raccourcis Bureau et menu D\u00e9marrer : Sparky'); ready = (U 'Sparky est pr\u00eat !'); launchhow = 'Lance-le avec le raccourci "Sparky" sur ton Bureau,'
          launchhow2 = 'ou start.bat dans le dossier d''installation.'; opening = 'Ouverture de Sparky dans ton navigateur...'; bye = 'Amuse-toi bien !'; already = (U 'd\u00e9j\u00e0 install\u00e9'); existing = 'Dossier' }
}
function T([string]$k) { $v = $TX[$script:L][$k]; if ($v) { $v } else { $TX['en'][$k] } }

# ------------------------------------------------------------------ welcome + language
try { Clear-Host } catch {}
Write-Host ""
Box @('', ("$($G.star)  S P A R K Y"), '', 'Your private AI assistant  -  100% local', '') 'Magenta'
$script:L = 'en'
if ($Lang -in @('en', 'fr')) { $script:L = $Lang }
elseif ($Interactive) {
  Write-Host ""
  Write-Host "    Choose your language / Choisis ta langue" -ForegroundColor White
  Write-Host "      1  English"
  Write-Host ("      2  " + (U 'Fran\u00e7ais'))
  Write-Host ("    " + $G.arrow + " ") -ForegroundColor Cyan -NoNewline
  $c = ([string](Read-Host)).Trim()
  if ($c -eq '2' -or $c -match '^f') { $script:L = 'fr' }
}
$gbu = if ($script:L -eq 'fr') { 'Go' } else { 'GB' }
Write-Host ""
Write-Host ("    " + (T 'welcome1')) -ForegroundColor Gray
Write-Host ("    " + (T 'welcome2')) -ForegroundColor Gray

# ------------------------------------------------------------------ 1. machine
Step (T 's1')
$arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64' -or $env:PROCESSOR_ARCHITEW6432 -eq 'ARM64') { 'arm64' } else { 'x64' }
$nvidia = $null
try {
  $line = (& nvidia-smi '--query-gpu=name,memory.total,driver_version' '--format=csv,noheader,nounits' 2>$null | Select-Object -First 1)
  if ($line) { $p = $line.Split(','); $nvidia = @{ name = $p[0].Trim(); vram = [int]$p[1].Trim(); driver = [double]($p[2].Trim().Split('.')[0]) } }
} catch {}
$os = (Get-CimInstance Win32_OperatingSystem).Caption -replace 'Microsoft ', ''
$ram = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
Ok ("$os  $Mid  $arch  $Mid  $ram $gbu RAM")
if ($nvidia) { Ok ("{0}  {1}  {2:N0} {3}" -f $nvidia.name, $Mid, ($nvidia.vram / 1024), $gbu) } else { Warn (T 'nogpu') }

# ------------------------------------------------------------------ 2. Sparky files
function Get-SourceArchive([string]$url, [string]$folder) {
  $archive = $folder + '.zip'
  Fetch (T 'dlcode') $url $archive
  Expand-Archive -LiteralPath $archive -DestinationPath $folder
  $roots = @(Get-ChildItem -Directory -LiteralPath $folder)
  if ($roots.Count -ne 1) { throw 'Unexpected source archive layout.' }
  return $roots[0].FullName
}

function Install-ZipSource([string]$folder) {
  $tempRoot = [IO.Path]::GetFullPath($env:TEMP).TrimEnd('\')
  $tmp = Join-Path $tempRoot ('sparky-zip-' + [guid]::NewGuid().ToString('N'))
  [void][IO.Directory]::CreateDirectory($tmp)
  try {
    $next = Get-SourceArchive "$Repo/archive/refs/heads/main.zip" (Join-Path $tmp 'release')
    $base = ''
    if ((Test-Path -LiteralPath (Join-Path $folder 'app/server.py')) -and
        -not (Test-Path -LiteralPath (Join-Path $folder '.sparky-zip-sources.json'))) {
      # Older ZIPs have no manifest. Compare against their official release before touching local files.
      $version = (Get-Content -LiteralPath (Join-Path $folder 'VERSION') -Raw).Trim()
      if ($version -notmatch '^\d+\.\d+\.\d+$') { throw 'Cannot identify the installed ZIP version.' }
      # These releases predate both source manifests and Git tags. Pin their last release revisions.
      $legacy = @{
        '1.6.1'='7f8e9fca0077e1a6199e1495f5c0dfa7a3e0af94'; '1.6.0'='642e5a3f89c5149ba9b3595ecd5c299d0f9e4f0c'
        '1.5.0'='c99067a336653c5df828c375ee2fe002ef7e5981'; '1.4.0'='0884388fdcde8a4da7865a3118ea1848e422c333'
        '1.3.0'='36762f542e86fc7305adc6a06ea89070bf4c597b'; '1.2.0'='14ae66847f717c66f2a704d082df466bbed25795'
        '1.1.2'='633323d31e193a232ad3b422a661bd515aace98e'; '1.1.1'='cfd9e80c0eb8fddb3a439554a44d26cc4b8e696a'
        '1.1.0'='da8761525d9bc5e7c803789a9c89a3600c9510f9'
      }
      $ref = if ($legacy.ContainsKey($version)) { $legacy[$version] } else { "refs/tags/v$version" }
      $base = Get-SourceArchive "$Repo/archive/$ref.zip" (Join-Path $tmp 'baseline')
    }
    & (Join-Path $next 'sync-zip.ps1') -Source $next -Destination $folder -Baseline $base
  } finally {
    if ([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($tmp)) -eq $tempRoot) {
      Remove-Item -LiteralPath $tmp -Recurse -Force
    }
  }
}

Step (T 's2')
$here = if ($PSScriptRoot) { $PSScriptRoot } else { '' }
if (-not $Dir -and $here -and (Test-Path (Join-Path $here 'app\server.py'))) {
  $Root = $here
  if (-not (Test-Path -LiteralPath (Join-Path $Root '.git')) -and (Test-Path -LiteralPath (Join-Path $Root 'data/install.log'))) {
    Install-ZipSource $Root
  }
} else {
  if (-not $Dir) {
    $Dir = Join-Path $env:LOCALAPPDATA 'Sparky'
    if ($Interactive) {
      Info ((T 'folder') + ": " + $Dir)
      Write-Host ("    " + $G.arrow + " " + (T 'folderq') + " ") -ForegroundColor Cyan -NoNewline
      $d = ([string](Read-Host)).Trim().Trim('"')
      if ($d) { $Dir = $d }
    }
  }
  $hasGit = [bool](Get-Command git -ErrorAction SilentlyContinue)
  if (Test-Path -LiteralPath (Join-Path $Dir '.git')) {
    if (-not $hasGit) { throw 'This is a Git installation. Install Git to update it safely.' }
    Run (T 'updated') 'git' @('-C', $Dir, 'pull', '--ff-only')
  } elseif ($hasGit -and -not (Test-Path -LiteralPath (Join-Path $Dir 'app\server.py'))) {
    Run (T 'dlcode') 'git' @('clone', '--depth', '1', "$Repo.git", $Dir)
  } else {
    Install-ZipSource $Dir
  }
  $Root = $Dir
}
$Runtime = Join-Path $Root 'runtime'
New-Item -ItemType Directory -Force $Runtime, (Join-Path $Root 'data') | Out-Null
$script:Log = Join-Path $Root 'data\install.log'
"=== Sparky install $(Get-Date -Format s)" | Set-Content $script:Log
$free = [math]::Round((Get-PSDrive ($Root.Substring(0, 1))).Free / 1GB)
Ok ("$Root   $Mid   $free $gbu " + (T 'free'))
if ($free -lt 25) { Warn (T 'lowdisk') }

# ------------------------------------------------------------------ 3. Python
Step (T 's3')
$py = $null
foreach ($c in @(@('py', '-3.12'), @('py', '-3.11'), @('python'))) {
  try {
    $v = & $c[0] $c[1..9] -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
    if ($v -match '^3\.(1[0-3])$') { $py = $c; break }
  } catch {}
}
if (-not $py) {
  Run (T 'pyinst') 'winget' @('install', '-e', '--id', 'Python.Python.3.12', '--scope', 'user', '--accept-package-agreements', '--accept-source-agreements', '--disable-interactivity')
  $env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine')
  $py = @('py', '-3.12')
}
$venv = Join-Path $Runtime 'venv'
$vpy = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path $vpy)) {
  $pyexe = ([string](& $py[0] $py[1..9] -c "import sys;print(sys.executable)")).Trim()
  Run (T 'venv') $pyexe @('-m', 'venv', $venv)
}
Run 'pip' $vpy @('-m', 'pip', 'install', '--upgrade', 'pip', '--quiet')
Ok (([string](& $vpy --version)).Trim())

# ------------------------------------------------------------------ 4. PyTorch + ComfyUI
Step (T 's4')
if ($ChatOnly) {
  Info 'Chat only: skipping PyTorch and ComfyUI.'
  Run (T 'deps') $vpy @('-m', 'pip', 'install', '-r', (Join-Path $Root 'requirements.txt'))
} else {
  $hasTorch = (& $vpy -c "import importlib.util;print(importlib.util.find_spec('torch') is not None)") -eq 'True'
  if (-not $hasTorch) {
    if ($nvidia -and $arch -eq 'arm64') {
      Run (T 'torch') $vpy @('-m', 'pip', 'install', '--pre', '--only-binary=:all:', '--extra-index-url', 'https://pypi.nvidia.com/nvtorch_oot_nightly', 'torch', 'torchvision', 'torchaudio') (T 'torchhint')
    } elseif ($nvidia) {
      Run (T 'torch') $vpy @('-m', 'pip', 'install', 'torch', 'torchvision', 'torchaudio', '--index-url', 'https://download.pytorch.org/whl/cu128') (T 'torchhint')
    } else {
      Run (T 'torch') $vpy @('-m', 'pip', 'install', 'torch', 'torchvision', 'torchaudio') (T 'torchhint')
    }
  } else { Ok ("PyTorch   " + (T 'already')) }
  $tinfo = ([string](& $vpy -c "import torch;print(torch.__version__+'|'+(torch.cuda.get_device_name(0) if torch.cuda.is_available() else ''))")).Trim().Split('|')
  if ($tinfo[1]) { Ok ((T 'gpuok') + "   " + $tinfo[1]) } else { Warn ("PyTorch " + $tinfo[0] + "   " + (T 'nogpuacc')) }

  $comfy = Join-Path $Runtime 'ComfyUI'
  function GetRepo([string]$label, [string]$url, [string]$dest, [string]$revision) {
    if (Test-Path $dest) { Ok ($label + "   " + (T 'already')); return }
    $parent = [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($dest)).FullName
    $tmp = Join-Path $parent ('sparky-stage-' + [guid]::NewGuid().ToString('N'))
    [void][IO.Directory]::CreateDirectory($tmp)
    try {
      if (Get-Command git -ErrorAction SilentlyContinue) {
        $stage = Join-Path $tmp 'repo'
        Run $label 'git' @('init', '-q', $stage)
        Run $label 'git' @('-C', $stage, 'remote', 'add', 'origin', $url)
        Run $label 'git' @('-C', $stage, 'fetch', '--depth=1', 'origin', $revision)
        Run $label 'git' @('-C', $stage, 'checkout', '-q', '--detach', $revision)
      } else {
        $stage = Get-SourceArchive "$url/archive/$revision.zip" (Join-Path $tmp 'extract')
      }
      [IO.Directory]::Move($stage, $dest)
    } finally {
      if ([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($tmp)) -eq $parent) {
        Remove-Item -LiteralPath $tmp -Recurse -Force
      }
    }
  }
  GetRepo (T 'comfy') 'https://github.com/Comfy-Org/ComfyUI' $comfy '08ff3c11b3a85eb07c2c06f21cb378ac2e086840'
  GetRepo (T 'ipa') 'https://github.com/cubiq/ComfyUI_IPAdapter_plus' (Join-Path $comfy 'custom_nodes\ComfyUI_IPAdapter_plus') 'a0f451a5113cf9becb0847b92884cb10cbdec0ef'
  Run (T 'deps') $vpy @('-m', 'pip', 'install', '-r', (Join-Path $comfy 'requirements.txt'), '-r', (Join-Path $Root 'requirements.txt'))
}

# ------------------------------------------------------------------ 5. llama.cpp
Step (T 's5')
$llama = Join-Path $Runtime 'llama'
if (-not (Test-Path (Join-Path $llama 'llama-server.exe'))) {
  $release = Invoke-RestMethod 'https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/b11382' -Headers @{ 'User-Agent' = 'sparky' }
  # CUDA 13 needs a recent NVIDIA driver (>= 580); otherwise CUDA 12.4 (x64); without NVIDIA: CPU build
  $want = @()
  if ($nvidia -and $nvidia.driver -ge 580) { $want += "bin-win-cuda-13\.[\d.]+-$arch\.zip$" }
  if ($nvidia) { $want += "bin-win-cuda-12\.[\d.]+-$arch\.zip$" }
  $want += "bin-win-cpu-$arch\.zip$"
  $pick = $null
  foreach ($pat in $want) {
    $a = $release.assets | Where-Object { $_.name -match "^llama-.*$pat" -and $_.digest -match '^sha256:[a-f0-9]{64}$' } | Select-Object -First 1
    if ($a) { $pick = @{ rel = $release; asset = $a }; break }
  }
  if (-not $pick) { Fail "llama.cpp: no build for Windows $arch" }
  $zip = Join-Path $env:TEMP 'llama.zip'
  Fetch ("llama.cpp " + $pick.rel.tag_name) $pick.asset.browser_download_url $zip $pick.asset.size $pick.asset.digest.Substring(7)
  New-Item -ItemType Directory -Force $llama | Out-Null
  Expand-Archive $zip $llama -Force
  if ($pick.asset.name -match 'cuda-([\d.]+)-') {   # CUDA runtime libraries ship separately
    $cv = $Matches[1]
    $rt = $pick.rel.assets | Where-Object { $_.name -eq "cudart-llama-bin-win-cuda-$cv-$arch.zip" } | Select-Object -First 1
    if ($rt) {
      if ($rt.digest -notmatch '^sha256:[a-f0-9]{64}$') { Fail 'Missing CUDA archive SHA-256.' }
      Fetch ("CUDA runtime " + $cv) $rt.browser_download_url $zip $rt.size $rt.digest.Substring(7)
      Expand-Archive $zip $llama -Force
    }
  }
  $exe = Get-ChildItem $llama -Recurse -Filter 'llama-server.exe' | Select-Object -First 1
  if ($exe -and $exe.DirectoryName -ne $llama) { Get-ChildItem $exe.DirectoryName | Move-Item -Destination $llama -Force }
  $kind = if ($pick.asset.name -match 'cuda-([\d.]+)') { "CUDA $($Matches[1])" } else { 'CPU' }
  Ok ("llama.cpp " + $pick.rel.tag_name + "   $Mid   $kind $arch")
} else { Ok ("llama.cpp   " + (T 'already')) }
# Node.js (portable): runs and tests the JavaScript projects the assistant builds
$nodeDir = Join-Path $Runtime 'node'
if (-not (Test-Path (Join-Path $nodeDir 'node.exe'))) {
  $version = 'v24.21.0'
  $hashes = @{ arm64 = '8779b1bde1d39f8d420e3b57aa657b39891af434d3de44a919044cec06785921';
               x64 = '158f7685b44de51f6c0df1d153526cbcd3e1bc739a8dfc607721cef75de9e541' }
  if (-not $hashes[$arch]) { Fail ("Node.js: unsupported platform " + $arch) }
  $zip = Join-Path $env:TEMP ('sparky-node-' + [guid]::NewGuid().ToString('N') + '.zip')
  try {
    Fetch ("Node.js " + $version) "https://nodejs.org/dist/$version/node-$version-win-$arch.zip" $zip 0 $hashes[$arch]
    Expand-Archive -LiteralPath $zip -DestinationPath $Runtime -Force
    [IO.Directory]::Move((Join-Path $Runtime "node-$version-win-$arch"), $nodeDir)
  } finally { if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force } }
} else { Ok ("Node.js   " + (T 'already')) }

# ------------------------------------------------------------------ 6. scan + models
Step (T 's6')
Push-Location $Root
# interface language, saved for the first launch
& $vpy -c "import json,os,sys;p=os.path.join('data','settings.json');d=json.load(open(p,encoding='utf-8')) if os.path.exists(p) else {};d['ui_lang']=sys.argv[1];json.dump(d,open(p,'w',encoding='utf-8'),indent=1)" $script:L
$i = 0
$job = Start-Job -ScriptBlock { param($v, $r, $l) Set-Location $r; & $v app\syscheck.py --json "--lang=$l" } -ArgumentList $vpy, $Root, $script:L
while ($job.State -eq 'Running') { Write-Host ("`r" + (Fit ("    " + $G.spin[$i % $G.spin.Count] + " " + (T 'scan')))) -ForegroundColor Cyan -NoNewline; Start-Sleep -Milliseconds 100; $i++ }
$scan = ((Receive-Job $job) | Out-String).Trim() | ConvertFrom-Json
Remove-Job $job
Clear-Line
$a = $scan.assess
Ok ((T 'level') + " " + $a.tier_label + "   $Mid   " + [math]::Round($a.model_mem_gb) + " " + (T 'usable'))
foreach ($n in $scan.notes) { Warn $n }
$rec = $scan.recommended
$lines = @('')
$sum = 0
$labels = @{ llm = (T 'assistant'); image = (T 'images'); voice = (T 'voice') }
$modelKinds = if ($ChatOnly) { @('llm') } else { @('llm', 'image', 'voice') }
foreach ($k in $modelKinds) {
  $m = $rec.$k
  if ($m) {
    $lines += ("{0,-11}{1,-31}{2,6:N1} {3}" -f $labels[$k], $m.label, $m.size_gb, $gbu)
    $sum += $m.size_gb
  }
}
$lines += ''
$lines += ("{0,-42}{1,6:N1} {2}" -f (T 'total'), $sum, $gbu)
Write-Host ""
Write-Host ("    " + (T 'recommended')) -ForegroundColor White
Box $lines 'DarkCyan'
if ($Models -and -not $NoModels -and $sum -gt 0) {
  Write-Host ""
  $ids = @($modelKinds | ForEach-Object { if ($rec.$_) { $rec.$_.id } })
  & $vpy app\models_cli.py @ids "--lang=$script:L"
} else { Info (T 'later') }
Pop-Location

# ------------------------------------------------------------------ 7. shortcuts
Step (T 's7')
if (-not $NoShortcuts) {
  $ws = New-Object -ComObject WScript.Shell
  foreach ($folder in @([Environment]::GetFolderPath('Desktop'), (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'))) {
    $lnk = $ws.CreateShortcut((Join-Path $folder 'Sparky.lnk'))
    $lnk.TargetPath = 'powershell.exe'
    $lnk.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Root\start.ps1`""
    $lnk.WorkingDirectory = $Root
    $lnk.IconLocation = "$Root\app\logo.ico"
    $lnk.Description = 'Sparky - private AI assistant, 100% local'
    $lnk.Save()
  }
  Ok (T 'shortcut')
}

# ------------------------------------------------------------------ done
Write-Host ""
Box @('', ("$($G.ok)  " + (T 'ready')), '', (T 'launchhow'), (T 'launchhow2'), '') 'Green'
Write-Host ""
if (-not $NoLaunch) {
  Info (T 'opening')
  & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root 'start.ps1')
  Write-Host ("    " + (T 'bye')) -ForegroundColor Magenta
}
Write-Host ""
