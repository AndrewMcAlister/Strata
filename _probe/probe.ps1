$ErrorActionPreference = 'Continue'
$out = @()
$out += "=== shortcut"
$p = 'C:\Users\AndrewBot\Desktop\Strata.lnk'
$out += "exists=" + (Test-Path $p)
if (Test-Path $p) {
  $s = (New-Object -ComObject WScript.Shell).CreateShortcut($p)
  $out += "TargetPath=" + $s.TargetPath
  $out += "Arguments=" + $s.Arguments
  $out += "WorkingDirectory=" + $s.WorkingDirectory
}
$out += "=== stop shortcut"
$p2 = 'C:\Users\AndrewBot\Desktop\Strata (stop).lnk'
if (Test-Path $p2) {
  $s2 = (New-Object -ComObject WScript.Shell).CreateShortcut($p2)
  $out += "TargetPath=" + $s2.TargetPath
  $out += "Arguments=" + $s2.Arguments
  $out += "WorkingDirectory=" + $s2.WorkingDirectory
}
$out += "=== startup folder"
Get-ChildItem "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup" -ErrorAction SilentlyContinue | ForEach-Object { $out += $_.Name }
$out += "=== scheduled tasks with strata"
Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object { $_.TaskName -match 'trata' } | ForEach-Object { $out += $_.TaskName + " " + $_.TaskPath + " " + $_.State }
$out += "=== engine dir"
if (Test-Path 'd:\Strata\engine') { Get-ChildItem 'd:\Strata\engine' | ForEach-Object { $out += $_.Name + " " + $_.Length } } else { $out += "(missing)" }
$out += "=== data root"
if (Test-Path 'D:\Strata-data') {
  Get-ChildItem 'D:\Strata-data' | ForEach-Object { $out += $_.Name + " " + $_.Length }
  $out += "-- models"
  Get-ChildItem 'D:\Strata-data\models' -ErrorAction SilentlyContinue | ForEach-Object { $out += $_.Name }
  $out += "-- packs"
  Get-ChildItem 'D:\Strata-data\packs' -ErrorAction SilentlyContinue | ForEach-Object { $out += $_.Name }
} else { $out += "(missing)" }
$out += "=== nvidia-smi"
try { & nvidia-smi --query-gpu=name,memory.total,compute_mode --format=csv } catch { $out += "nvidia-smi unavailable" }
$out += "=== running strata processes"
Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match 'strata' } | ForEach-Object { $out += $_.Id.ToString() + " " + $_.ProcessName + " " + $_.Path }
[IO.File]::WriteAllText('d:\Strata\_probe\ps.txt', ($out -join "`r`n"), [Text.Encoding]::UTF8)
