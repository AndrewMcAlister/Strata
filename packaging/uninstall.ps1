# Strata Terminal: uninstaller for Windows (PowerShell 5.1 or newer).
#
#   irm -TimeoutSec 120 https://github.com/Niko1221/Strata/releases/latest/download/uninstall.ps1 -OutFile "$env:TEMP\strata-uninstall.ps1"
#   & "$env:TEMP\strata-uninstall.ps1"            # remove the Terminal, keep your data
#   & "$env:TEMP\strata-uninstall.ps1" -Purge     # also remove Strata's engine, saved state, settings and chats (asks first)
#   & "$env:TEMP\strata-uninstall.ps1" -Plan      # show what would be removed, change nothing
#
# What it removes, in order (and nothing else):
#   1. stops a Strata server that is still running (`strata launch --stop`), so no file is in use;
#   2. removes  %LOCALAPPDATA%\Strata\terminal : the installed copies and the `current` link;
#   3. removes  %LOCALAPPDATA%\Strata\terminal\current\bin  from your user PATH. The machine PATH is never touched.
#
# It keeps your data. A model folder is never touched, and Strata's engine, settings and chats are kept unless -Purge is given.
#
# Options: -Plan, -Purge, -Yes, -DataDir <folder> (the same choice install.ps1 has), -NoPath.
# The settings install.ps1 reads work here too: $env:STRATA_DATA_DIR, $env:STRATA_NO_PATH='1', $env:STRATA_HOME.
param(
    [switch]$Plan,
    [switch]$Purge,
    [switch]$Yes,
    [string]$DataDir = '',
    [switch]$NoPath
)
# The switches sit outside the child scope, so a file run can pass them and `irm ... | iex` can still run this; everything
# else stays inside the child scope, because `iex` runs in the caller's scope and must not have its variables overwritten.
& {
$ErrorActionPreference = 'Stop'

function Die([string]$msg) { [Console]::Error.WriteLine("strata uninstaller: $msg"); throw 'strata uninstaller stopped' }
function Say([string]$msg) { Write-Host $msg }

# One function, so a test can run it on its own. It drops the folder install.ps1 added (and an older
# <data>\terminal\<version>\bin), and keeps every other entry as it was. $null means "nothing to remove".
function Remove-StrataPathEntry([string]$Value, [string]$Entry, [string]$Terminal) {
    if (-not $Value) { return $null }
    $entryFull = ([IO.Path]::GetFullPath($Entry)).TrimEnd('\')
    $termPrefix = ([IO.Path]::GetFullPath($Terminal)).TrimEnd('\') + '\'
    $keep = New-Object System.Collections.Generic.List[string]
    $dropped = $false
    foreach ($part in ($Value -split ';')) {
        $p = $part.Trim().Trim('"')
        if (-not $p) { continue }
        $full = $p
        try { $full = ([IO.Path]::GetFullPath($p)).TrimEnd('\') } catch { }
        $ours = [string]::Equals($full, $entryFull, [StringComparison]::OrdinalIgnoreCase) -or
            ($full.StartsWith($termPrefix, [StringComparison]::OrdinalIgnoreCase) -and $full.EndsWith('\bin', [StringComparison]::OrdinalIgnoreCase))
        if ($ours) { $dropped = $true; continue }
        $keep.Add($p)
    }
    if (-not $dropped) { return $null }
    return ($keep -join ';')
}

function Remove-Strata([string]$path, [switch]$Link) {
    if ($Plan) { Say "  would remove $path"; return }
    try {
        if ($Link) {
            & cmd /c rmdir "$path" | Out-Null     # a junction: the link goes, never what it points at
            if ($LASTEXITCODE -ne 0) { throw "cmd rmdir exited $LASTEXITCODE" }
        } else {
            Remove-Item -LiteralPath $path -Recurse -Force
        }
    } catch { Die "could not remove $path ($($_.Exception.Message)). If Strata is running, run 'strata launch --stop', then run this again." }
    Say "  removed $path"
}

if (-not $DataDir -and -not $env:STRATA_DATA_DIR -and -not $env:LOCALAPPDATA) {
    Die 'Windows did not say where your user folder is. Set $env:STRATA_DATA_DIR to the folder Strata is in, for example $env:STRATA_DATA_DIR=''C:\Strata'', and run this again.'
}
$data = if ($DataDir) { $DataDir } elseif ($env:STRATA_DATA_DIR) { $env:STRATA_DATA_DIR } else { Join-Path $env:LOCALAPPDATA 'Strata' }
try { $data = [IO.Path]::GetFullPath($data) } catch { Die "this is not a folder path: $data" }
if ($data.Length -gt 3) { $data = $data.TrimEnd('\') }
$isDefault = -not $DataDir -and -not $env:STRATA_DATA_DIR

$terminal = Join-Path $data 'terminal'
$current = Join-Path $terminal 'current'
$bin = Join-Path $current 'bin'
$strata = Join-Path $bin 'strata.cmd'

Say 'Strata Terminal uninstaller'
Say "  Strata files: $terminal"
Say "  The strata command: $strata"

$currentItem = Get-Item -LiteralPath $current -Force -ErrorAction SilentlyContinue
$currentIsLink = [bool]($currentItem -and ($currentItem.Attributes -band [IO.FileAttributes]::ReparsePoint))
$currentIsOurs = $false
if ($currentIsLink) {
    $target = @($currentItem.Target)[0]
    if ($target) {
        if ($target.StartsWith('\\?\')) { $target = $target.Substring(4) }
        try { $target = [IO.Path]::GetFullPath($target) } catch { }
        if ($target.StartsWith($terminal + '\', [StringComparison]::OrdinalIgnoreCase)) { $currentIsOurs = $true }
    }
} elseif ($currentItem -and $currentItem.PSIsContainer) {
    # the small launcher install.ps1 writes when a drive cannot hold a junction
    if ((Test-Path -LiteralPath (Join-Path $current 'bin\strata.cmd')) -and (Test-Path -LiteralPath (Join-Path $current 'current.txt'))) { $currentIsOurs = $true }
}
$copies = New-Object System.Collections.Generic.List[string]
if (Test-Path -LiteralPath $terminal) {
    foreach ($d in @(Get-ChildItem -LiteralPath $terminal -Directory -Force -ErrorAction SilentlyContinue)) {
        if ($d.FullName -eq $current) { continue }
        if ($d.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            Say "  left $($d.FullName) alone (it is a link, not a copy the installer made)"
        } elseif (Test-Path -LiteralPath (Join-Path $d.FullName 'bin\strata.cmd')) {
            $copies.Add($d.FullName)
        } else {
            Say "  left $($d.FullName) alone (it does not look like a Strata copy)"
        }
    }
}

$installed = [bool](($currentItem -and $currentIsOurs) -or $copies.Count -gt 0)
if (-not $installed -and -not $Purge) {
    Say 'Strata is not installed for this user. Nothing was changed.'
    if ($isDefault) { Say '  (the folder above is the default; set $env:STRATA_DATA_DIR or pass -DataDir if Strata is elsewhere)' }
    Say 'Add -Purge to remove Strata data that is left over.'
    return
}

# ---- what -Purge would remove: Strata's own folders, never a model folder
$purgeItems = New-Object System.Collections.Generic.List[string]
foreach ($sub in 'components', 'runtime', 'tuning', 'time-skip', 'draft-costs') {
    $p = Join-Path $data $sub
    if (Test-Path -LiteralPath $p) { $purgeItems.Add($p) }
}
$winHome = $env:USERPROFILE
if (-not $winHome) { try { $winHome = [Environment]::GetFolderPath('UserProfile') } catch { } }
if (-not $winHome) { Die 'Windows did not say where your user profile is. Set $env:USERPROFILE and run this again.' }
$homeDir = Join-Path $winHome '.strata'
if ($env:STRATA_HOME) {
    Say "  kept $homeDir (STRATA_HOME is set; it is your own folder)"
} elseif (Test-Path -LiteralPath $homeDir) {
    $purgeItems.Add($homeDir)
}
$configDir = Join-Path $winHome '.config\strata'
if (Test-Path -LiteralPath $configDir) { $purgeItems.Add($configDir) }
if ($Purge) {
    if ($purgeItems.Count -gt 0) {
        Say 'These Strata folders would be removed as well:'
        foreach ($p in $purgeItems) { Say "  $p" }
    } else { Say 'There is no Strata data to remove.' }
    if ($purgeItems.Count -gt 0 -and -not $Plan -and -not $Yes) {
        if (-not [Environment]::UserInteractive -or [Console]::IsInputRedirected) {
            Die 'nothing can answer here, so -Purge needs -Yes. Add -Yes to remove the data, or leave -Purge out to keep it.'
        }
        $reply = Read-Host 'Remove them? Model folders are not touched. [y/N]'
        if ($reply -notmatch '^(?i)\s*(y|yes)\s*$') { $Purge = $false; Say 'Your data will be kept.' }
    }
}

# ---- 1. a running server first: its engine keeps files open
if ($Plan) {
    Say '  a running Strata server would be stopped (strata launch --stop)'
} elseif (Test-Path -LiteralPath $strata) {
    Say '  asking a running Strata server to stop (strata launch --stop)'
    $was = $ErrorActionPreference; $ErrorActionPreference = 'Continue'   # a native program's stderr note must not stop the script
    try { & $strata launch --stop 2>&1 | Out-Null } catch { } finally { $ErrorActionPreference = $was }
}

# ---- 2 and 3. the `current` link, the copies, and the PATH entry
if ($currentIsLink) {
    if ($currentIsOurs) { Remove-Strata $current -Link }
    else { Say "  left $current alone (it is not the link the installer makes)" }
} elseif ($currentItem) {
    if ($currentIsOurs) { Remove-Strata $current }
    else { Say "  left $current alone (it does not look like the installer's launcher)" }
}
foreach ($p in $copies) { Remove-Strata $p }

if ($NoPath -or $env:STRATA_NO_PATH -eq '1') {
    Say "  (your user PATH was not changed; the entry would be $bin)"
} else {
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $newPath = Remove-StrataPathEntry -Value $userPath -Entry $bin -Terminal $terminal
    if ($null -ne $newPath) {
        if ($Plan) { Say "  would remove $bin from your user PATH" }
        else {
            [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
            Say "  removed $bin from your user PATH"
        }
    } else { Say "  $bin was not in your user PATH" }
}

if ($Purge) {
    foreach ($p in $purgeItems) { Remove-Strata $p }
}

# An empty folder Strata made is removed; a folder with anything left in it stays.
if (-not $Plan) {
    foreach ($d in @($terminal, $data)) {
        if (-not $d -or $d -match '^[A-Za-z]:\\$') { continue }
        if (-not (Test-Path -LiteralPath $d)) { continue }
        $first = @(Get-ChildItem -LiteralPath $d -Force -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($first.Count -eq 0) { Remove-Item -LiteralPath $d -Force -ErrorAction SilentlyContinue }
    }
}

if ($Plan) { Say 'Nothing was changed (Plan).'; return }
if ($installed) { Say 'Strata Terminal was removed.' }
else { Say 'Strata Terminal was not installed, so there was nothing of it to remove.' }
if ($Purge) { Say '  Strata data: engine components, saved state, settings and chats were removed.' }
else { Say "  Kept: Strata data under $data (add -Purge to remove it)" }
Say '  Kept: every model folder. No model or downloaded model file was touched.'
if ($env:APPDATA) {
    $appData = Join-Path $env:APPDATA 'Strata'
    if (Test-Path -LiteralPath $appData) { Say "  The desktop app keeps its own files in $appData; remove the app in Settings > Apps if it is installed." }
}
Say 'To install Strata again:'
Say '  irm -TimeoutSec 120 https://github.com/Niko1221/Strata/releases/latest/download/install.ps1 | iex'
}
