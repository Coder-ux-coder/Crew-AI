# Crew installer for Windows 10 and 11.
#
# Double-click "Install Crew.cmd" (next to this file). It is safe to run again at any time:
# it only adds what is missing and updates Crew itself. Your projects, settings and sign-ins
# (in your user folder, under .crew) are never touched.

param([switch]$Quiet)

$ErrorActionPreference = 'Continue'   # native programs write progress to stderr; we check results ourselves
$ProgressPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}

$Source   = Split-Path -Parent $PSScriptRoot                 # the "crew" folder this installer came with
$Target   = Join-Path $env:LOCALAPPDATA 'Programs\Crew'
$CrewHome = Join-Path $env:USERPROFILE '.crew'
$Total    = 8
$script:Notes = @()

function Say([string]$Text, [string]$Color = 'Gray') { Write-Host $Text -ForegroundColor $Color }
function Step([int]$N, [string]$Text) { Write-Host ''; Write-Host "[$N/$Total] $Text" -ForegroundColor Cyan }
function Good([string]$Text) { Write-Host "      OK   $Text" -ForegroundColor Green }
function Note([string]$Text) { Write-Host "      NOTE $Text" -ForegroundColor Yellow; $script:Notes += $Text }
function Stop-Install([string]$Text) {
  Write-Host ''
  Write-Host "Crew could not be installed: $Text" -ForegroundColor Red
  Write-Host ''
  if (-not $Quiet) { Read-Host 'Press Enter to close' | Out-Null }
  exit 1
}
function Refresh-Path {
  $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
  $user = [Environment]::GetEnvironmentVariable('Path', 'User')
  $env:Path = "$machine;$user;$env:USERPROFILE\.local\bin;$env:APPDATA\npm"
}
function Have([string]$Name) { [bool](Get-Command $Name -ErrorAction SilentlyContinue) }
function Ask-YesNo([string]$Question, [bool]$Default = $true) {
  if ($Quiet) { return $Default }
  $hint = if ($Default) { '[Y/n]' } else { '[y/N]' }
  $answer = Read-Host "      $Question $hint"
  if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
  return $answer.Trim().ToLower().StartsWith('y')
}
function Winget-Install([string]$Id, [string]$Name) {
  if (-not (Have 'winget')) {
    Stop-Install "Windows' 'App Installer' (winget) is missing. Install 'App Installer' from the Microsoft Store, then run this installer again."
  }
  Say "      Installing $Name. This can take a few minutes; if Windows asks for permission, choose Yes."
  & winget install --exact --id $Id --accept-package-agreements --accept-source-agreements --silent --disable-interactivity *> $null
  Refresh-Path
}
function Find-Python {
  $candidates = @()
  if (Have 'py') {
    $exe = & py -3 -c "import sys; print(sys.executable)" 2> $null
    if ($exe) { $candidates += "$exe".Trim() }
  }
  foreach ($name in @('python', 'python3')) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -notlike '*WindowsApps*') { $candidates += $cmd.Source }   # skip the Store stub
  }
  $candidates += Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue |
    Sort-Object FullName -Descending | ForEach-Object { $_.FullName }
  foreach ($exe in $candidates) {
    if (-not (Test-Path $exe)) { continue }
    $version = & $exe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2> $null
    if ($version -and ([version]"$version".Trim() -ge [version]'3.11')) { return $exe }
  }
  return $null
}

Write-Host ''
Write-Host '  Crew - installing your AI team' -ForegroundColor White
Write-Host '  ------------------------------' -ForegroundColor DarkGray
Say "  From: $Source"
Say "  To:   $Target"

if (-not (Test-Path (Join-Path $Source 'crewlib\cli.py'))) {
  Stop-Install "this installer must stay inside the Crew folder (next to 'crewlib' and 'crewapp'). Extract the whole download first."
}
Refresh-Path

# ------------------------------------------------------------------------------------------ 1
Step 1 'Python (runs Crew itself)'
$Py = Find-Python
if (-not $Py) {
  Winget-Install 'Python.Python.3.12' 'Python 3.12'
  $Py = Find-Python
}
if (-not $Py) {
  Stop-Install 'Python could not be installed. Install Python 3.12 from https://www.python.org/downloads/ (tick "Add python.exe to PATH"), then run this installer again.'
}
$PyW = Join-Path (Split-Path $Py) 'pythonw.exe'
if (-not (Test-Path $PyW)) { $PyW = $Py }
Good "Python: $Py"

# ------------------------------------------------------------------------------------------ 2
Step 2 "Git (keeps every version of the team's work)"
if (-not (Have 'git')) { Winget-Install 'Git.Git' 'Git' }
if (-not (Have 'git')) {
  Stop-Install 'Git could not be installed. Install it from https://git-scm.com/download/win, then run this installer again.'
}
Good ((& git --version) -join ' ')

# ------------------------------------------------------------------------------------------ 3
Step 3 'Claude Code (runs your Claude subscriptions)'
$ClaudeExe = Join-Path $env:USERPROFILE '.local\bin\claude.exe'
if (-not (Have 'claude') -and -not (Test-Path $ClaudeExe)) {
  Say "      Installing Claude Code with Anthropic's official installer..."
  & powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://claude.ai/install.ps1 | iex" *> $null
  Refresh-Path
}
if ((Have 'claude') -or (Test-Path $ClaudeExe)) {
  Good 'Claude Code is installed. You sign in from inside Crew (Home page or Settings > Subscriptions).'
} else {
  Note 'Claude Code did not install. Install it from https://claude.ai/download, then run this installer again.'
}

# ------------------------------------------------------------------------------------------ 4
Step 4 'ChatGPT through Codex (optional)'
if (Ask-YesNo 'Will you use a ChatGPT subscription with Crew too?' $true) {
  if (-not (Have 'npm')) { Winget-Install 'OpenJS.NodeJS.LTS' 'Node.js' }
  if (Have 'npm') {
    Say '      Installing Codex...'
    & npm install -g '@openai/codex' --no-fund --no-audit --loglevel=error *> $null
    Refresh-Path
  }
  if ((Have 'codex') -or (Test-Path (Join-Path $env:APPDATA 'npm\codex.cmd'))) {
    Good 'Codex is installed. Add your ChatGPT subscription in Crew: Settings > Subscriptions.'
  } else {
    Note 'Codex did not install. Crew works fully with your Claude subscriptions; you can add ChatGPT later.'
  }
} else {
  Say '      Skipped.'
}

# ------------------------------------------------------------------------------------------ 5
Step 5 'Browser and picture add-ons'
& $Py -m pip install --user --upgrade --disable-pip-version-check --quiet playwright pillow *> $null
if ($LASTEXITCODE -eq 0) {
  Good 'Installed. The side-panel browser uses Microsoft Edge, which is already on your PC.'
} else {
  Note 'The browser add-on did not install. Crew still works; the Browser page will explain what is missing.'
}

# ------------------------------------------------------------------------------------------ 6
Step 6 'Phone connector (to see and control your Samsung)'
$Tools = Join-Path $CrewHome 'tools'
$Adb = Join-Path $Tools 'platform-tools\adb.exe'
if (-not (Test-Path $Adb) -and -not (Have 'adb')) {
  try {
    New-Item -ItemType Directory -Force -Path $Tools | Out-Null
    $zip = Join-Path $env:TEMP 'crew-platform-tools.zip'
    Say "      Downloading Google's Android platform tools (by installing them you accept Google's licence)..."
    Invoke-WebRequest 'https://dl.google.com/android/repository/platform-tools-latest-windows.zip' -OutFile $zip -UseBasicParsing -ErrorAction Stop
    Expand-Archive -Path $zip -DestinationPath $Tools -Force -ErrorAction Stop
    Remove-Item $zip -Force -ErrorAction SilentlyContinue
  } catch {
    Note "The phone connector did not download ($($_.Exception.Message)). Everything else works."
  }
}
if ((Test-Path $Adb) -or (Have 'adb')) { Good 'Ready. Connect your phone from the Phone page in Crew.' }

# ------------------------------------------------------------------------------------------ 7
Step 7 'Crew itself'
# Close the Crew window's background program so the new version starts fresh. Team projects that are
# running keep going (they are separate programs), and can always be continued later.
Get-CimInstance Win32_Process -Filter "Name like 'python%.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -like '*crewlib app*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
$sameFolder = (Resolve-Path $Source).Path.TrimEnd('\') -eq $Target.TrimEnd('\')
if (-not $sameFolder) {
  New-Item -ItemType Directory -Force -Path $Target | Out-Null
  & robocopy $Source $Target /MIR /XD __pycache__ .git tests /XF *.pyc /R:2 /W:2 /NFL /NDL /NJH /NJS /NP *> $null
  if ($LASTEXITCODE -ge 8) { Stop-Install "copying Crew to $Target failed (robocopy code $LASTEXITCODE). Close Crew if it is open, then try again." }
}
New-Item -ItemType Directory -Force -Path $CrewHome | Out-Null
Good "Installed in $Target"

# ------------------------------------------------------------------------------------------ 8
Step 8 'Shortcuts'
$Icon = Join-Path $Target 'crewapp\static\icons\crew.ico'
$Shell = New-Object -ComObject WScript.Shell
function Make-Shortcut([string]$Path, [string]$Arguments) {
  $link = $Shell.CreateShortcut($Path)
  $link.TargetPath = $PyW
  $link.Arguments = $Arguments
  $link.WorkingDirectory = $Target
  $link.IconLocation = "$Icon,0"
  $link.Description = 'Crew - your AI team'
  $link.Save()
}
Make-Shortcut (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Crew.lnk') '-X utf8 -m crewlib app'
Make-Shortcut (Join-Path ([Environment]::GetFolderPath('Programs')) 'Crew.lnk') '-X utf8 -m crewlib app'
$StartupLink = Join-Path ([Environment]::GetFolderPath('Startup')) 'Crew.lnk'
if (Ask-YesNo 'Start Crew quietly when you sign in to Windows (so your phone can always reach it)?' $true) {
  Make-Shortcut $StartupLink '-X utf8 -m crewlib app --no-open'
} elseif (Test-Path $StartupLink) {
  Remove-Item $StartupLink -Force
}
Good 'Crew is on your desktop and in the Start menu.'

# ------------------------------------------------------------------------------------------ check
Write-Host ''
Write-Host 'Checking that Crew starts...' -ForegroundColor Cyan
Push-Location $Target
$check = & $Py -X utf8 -c "import crewlib.cli, crewapp.server; print('CREW-READY')" 2>&1
Pop-Location
if ("$check" -notmatch 'CREW-READY') {
  Write-Host ''
  Write-Host 'Crew could not start. Please send a screenshot of this window:' -ForegroundColor Red
  $check | Select-Object -Last 25 | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
  if (-not $Quiet) { Read-Host 'Press Enter to close' | Out-Null }
  exit 1
}
Start-Process -FilePath $PyW -ArgumentList @('-X', 'utf8', '-m', 'crewlib', 'app') -WorkingDirectory $Target
$running = $false
for ($i = 0; $i -lt 45 -and -not $running; $i++) {
  Start-Sleep -Seconds 1
  foreach ($port in 8765..8774) {
    try {
      $client = New-Object System.Net.WebClient
      $client.Proxy = $null
      if ($client.DownloadString("http://127.0.0.1:$port/api/health") -match '"version"') { $running = $true; break }
    } catch {}
  }
}

# ------------------------------------------------------------------------------------------ done
Write-Host ''
if ($script:Notes.Count -gt 0) {
  Write-Host 'Finished, with these notes:' -ForegroundColor Yellow
  foreach ($n in $script:Notes) { Write-Host "  - $n" -ForegroundColor Yellow }
  Write-Host ''
}
if ($running) {
  Write-Host 'All done: Crew is running and its window is opening.' -ForegroundColor Green
  Write-Host ''
  Write-Host 'On its Home page, press "Sign in" to connect your Claude subscription.' -ForegroundColor White
  Write-Host 'Add your other subscriptions in Settings > Subscriptions. To use Crew on your Samsung,' -ForegroundColor White
  Write-Host 'open Settings > Use on your phone and scan the code.' -ForegroundColor White
  Write-Host 'Next time, open Crew with the Crew icon on your desktop.' -ForegroundColor White
} else {
  Write-Host 'Crew was installed but did not answer. Please send a screenshot of this window.' -ForegroundColor Red
  $log = Join-Path $CrewHome 'app.log'
  if (Test-Path $log) {
    Write-Host 'Last lines of its log:' -ForegroundColor Red
    Get-Content $log -Tail 25 | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
  }
}
if (-not $Quiet) {
  Write-Host ''
  Read-Host 'Press Enter to close this window' | Out-Null
}
