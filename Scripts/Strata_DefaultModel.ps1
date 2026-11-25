<#
    Which model an unattended start should choose.

    THE TUNED 94K CONFIG IS THE DEFAULT on the PC that has it, and it is the FIRST line of the menu
    too: Strata_Start.bat asks this script before it builds its list and puts that file at the top,
    so the default is always the first key. It is the configuration this machine was tuned for: the
    40 GiB pinned expert budget, the adaptive RAM tier that follows the conversation, and the expert
    profile learned from real work. An unattended start - the task at logon - is the one case where
    nobody has chosen anything, so it gets the model the day-to-day work actually runs on rather
    than the cheapest one installed.

    WHAT THIS PC'S SERVICE SERVES COMES SECOND. service.conf names the model the Windows service
    serves, and that file is per computer (ProgramData), so a folder of configs shared by two PCs
    can serve the 94K Q4 on one and the 20K IQ2_XS on the other - the one whose model files and card
    are there. It answers when the named default is not installed here, then the first model, then
    the first without vision. (Until 25 November 2026 those two rules were the other way round, so
    a service.conf naming the 15B Inkling took the default away from the named Q4. The owner asked
    for the Q4 first and by default.)

    Chosen by name, not by position. The list Strata_Start.bat shows is ordered by file
    write time, so editing any config reorders it: a hardcoded "2" was correct until the
    config meant to be 2 was edited, which made it newest, moved it to 1, and pointed the
    default at a vision model instead. See DEFAULT_MODEL below to change it.

    WHAT IT PRINTS is the config FILE NAME (strata-iq2_xs-20k.json), not a position in the
    list. Strata_Start.bat builds its own list and finds the name in it, so the menu's order
    and this answer cannot disagree - a position did, on 23-11: the batch listed the configs
    in a different order and the default opened the Radeon config on a NVIDIA PC.

    THE RADEON (gfx906) CONFIGS ARE NOT A CHOICE HERE, as in the menu: strata-*mi50*.json names
    D:\Strata\build-906-win\strata.exe and "backend": "hip", and this PC's engine is the CUDA
    component. They stay in the folder for the Radeon PC and are skipped below.

    Falls back to the first model in the list, then to the first without vision. Prints
    nothing when the folder holds no config, and the batch uses its first entry.
#>
param([string]$Root = 'D:\Strata', [string]$ServerDir = '')
if (-not $ServerDir) { $ServerDir = $Root }

# The default model, by model_name. One line to change.
# It names the config the Windows service serves on the tuned PC (strata-q4_k_xl_dflash2_94k_novision.json),
# so an unattended start and the service pick the same model instead of loading two different ones into the
# same RAM. A PC whose service.conf names another config uses that one instead (see above).
$DEFAULT_MODEL = 'qwen3.8-flash-next-q4_k_xl_dflash2_94k_novision'

$ErrorActionPreference = 'SilentlyContinue'

$rows = @()
$position = 0
$cfgs = @(); $seen = @{}
foreach ($d in @($ServerDir, $Root)) {
    if (-not $d) { continue }
    foreach ($g in @(Get-ChildItem -LiteralPath $d -Filter 'strata-*.json' -ErrorAction SilentlyContinue)) {
        if ($seen.ContainsKey($g.Name)) { continue }
        $seen[$g.Name] = 1; $cfgs += $g
    }
}
foreach ($file in ($cfgs |
                    Sort-Object @{Expression='LastWriteTime';Descending=$true}, @{Expression='Name';Descending=$false})) {
    if ($file.Name -like '*.shared-settings.json') { continue }
    # The Radeon (gfx906) hand build's configs: not a choice on this PC (see the note above).
    # Both names end in mi50.json (one -mi50, one _mi50).
    if ($file.Name -like '*mi50*.json') { continue }
    $config = $null
    try { $config = ConvertFrom-Json (Get-Content -Raw -LiteralPath $file.FullName) } catch { continue }
    if ($config -eq $null) { continue }
    if (-not $config.exe) { continue }
    if (-not $config.args) { continue }

    $position++
    $args = @($config.args)
    $rows += [pscustomobject]@{
        Position = $position
        Name     = [string]$config.model_name
        Config   = [string]$file.Name
        Vision   = ($args -contains '--vision')
    }
}

# 0. the named default, when set: the tuned 94K Q4, and the first line of the menu
if ($DEFAULT_MODEL -ne '') {
    $hit = $rows | Where-Object { $_.Name -eq $DEFAULT_MODEL } | Select-Object -First 1
    if ($hit) { Write-Output $hit.Config; exit 0 }
}

# 1. what this PC's Windows service serves: service.conf is per computer, so this is the model whose
#    files and card are here (the shared configs themselves never have to change between the two PCs).
$confPath = if ($env:STRATA_SERVICE_ROOT) { Join-Path $env:STRATA_SERVICE_ROOT 'service.conf' }
            elseif ($env:ProgramData) { Join-Path $env:ProgramData 'Strata\service\service.conf' }
            else { 'C:\ProgramData\Strata\service\service.conf' }
if (Test-Path -LiteralPath $confPath) {
    $served = ''
    foreach ($line in Get-Content -LiteralPath $confPath) {
        if ($line -match '^\s*config\s*=\s*(.+?)\s*$') { $served = $matches[1].Trim('"').Trim("'") }
    }
    if ($served) {
        $leaf = Split-Path $served -Leaf
        $served_row = $rows | Where-Object { $_.Config -eq $leaf } | Select-Object -First 1
        if ($served_row) { Write-Output $served_row.Config; exit 0 }
    }
}

# 2. the first model in the list: newest first, the filename as the tie-break so it cannot
#    depend on which of two second-apart writes finished last
if ($rows.Count -gt 0) { Write-Output $rows[0].Config; exit 0 }

# 3. otherwise the first without vision - still the cheaper of what is installed
$noVision = $rows | Where-Object { -not $_.Vision } | Select-Object -First 1
if ($noVision) { Write-Output $noVision.Config; exit 0 }

# 4. nothing installed: print nothing, and the batch uses its first entry
exit 0
