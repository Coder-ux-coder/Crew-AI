# Removes Crew's program files and shortcuts. Your projects, captures, settings and sign-ins
# (in your user folder, under .crew) are kept unless you choose to delete them too.

$ErrorActionPreference = 'Continue'
$Target = Join-Path $env:LOCALAPPDATA 'Programs\Crew'
$CrewHome = Join-Path $env:USERPROFILE '.crew'

Write-Host ''
Write-Host '  Crew - uninstall' -ForegroundColor White
Write-Host ''
Get-CimInstance Win32_Process -Filter "Name like 'python%.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -like '*crewlib*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
foreach ($folder in @('Desktop', 'Programs', 'Startup')) {
  $link = Join-Path ([Environment]::GetFolderPath($folder)) 'Crew.lnk'
  if (Test-Path $link) { Remove-Item $link -Force }
}
# Crew's own addresses leave Edge's and Chrome's microphone and clipboard lists; any other entries stay, numbered
# 1, 2, 3 ... as the browsers read them.
foreach ($base in @('HKCU:\Software\Policies\Microsoft\Edge', 'HKCU:\Software\Policies\Google\Chrome')) {
  foreach ($list in @('AudioCaptureAllowedUrls', 'ClipboardAllowedForUrls')) {
    $key = Join-Path $base $list
    if (-not (Test-Path $key)) { continue }
    $item = Get-Item $key
    $names = @($item.GetValueNames() | Where-Object { $_ -match '^\d+$' } | Sort-Object { [int]$_ })
    $keep = @($names | ForEach-Object { [string]$item.GetValue($_) } |
      Where-Object { $_ -notmatch '^http://(localhost|127\.0\.0\.1):87(6[5-9]|7[0-4])$' })
    if ($keep.Count -eq $names.Count) { continue }
    foreach ($name in $names) { Remove-ItemProperty -Path $key -Name $name }
    for ($i = 0; $i -lt $keep.Count; $i++) {
      New-ItemProperty -Path $key -Name ([string]($i + 1)) -Value $keep[$i] -PropertyType String | Out-Null
    }
    if ($keep.Count -eq 0) { Remove-Item $key -Force }
  }
}
if (Test-Path $Target) { Remove-Item $Target -Recurse -Force }
Write-Host '  Crew and its shortcuts were removed.' -ForegroundColor Green
$answer = Read-Host "  Also delete your Crew projects, captures, settings and sign-ins in $CrewHome ? [y/N]"
if ($answer -and $answer.Trim().ToLower().StartsWith('y')) {
  Remove-Item $CrewHome -Recurse -Force
  Write-Host '  Deleted.' -ForegroundColor Green
} else {
  Write-Host '  Kept. Reinstalling Crew later picks them up again.' -ForegroundColor Gray
}
Write-Host ''
Read-Host 'Press Enter to close' | Out-Null
