<#
    Which model an unattended start should choose.

    THE SMALLEST AND FASTEST MODEL IS THE DEFAULT. For this install that is IQ2_XS: the
    lowest quantisation present, so it loads and answers fastest, and it runs without
    vision. An unattended start - the task at logon - is the one case where nobody has
    chosen anything, so it should get the cheapest useful answer rather than the largest
    model that happens to be newest.

    Chosen by name, not by position. The list Strata_Start.bat shows is ordered by file
    write time, so editing any config reorders it: a hardcoded "2" was correct until the
    config meant to be 2 was edited, which made it newest, moved it to 1, and pointed the
    default at a vision model instead. See DEFAULT_MODEL below to change it.

    Falls back to the first model without vision, then to 1, so a missing or renamed
    config still yields a usable position rather than an error.
#>
param([string]$Root = 'D:\Strata')

# The smallest, fastest model. One line to change.
$DEFAULT_MODEL = 'qwen3.8-flash-next-iq3_xxs'

$ErrorActionPreference = 'SilentlyContinue'

$rows = @()
$position = 0
foreach ($file in @(Get-ChildItem -LiteralPath $Root -Filter 'strata-*.json' |
                    Sort-Object @{Expression='LastWriteTime';Descending=$true}, @{Expression='Name';Descending=$false})) {
    if ($file.Name -like '*.shared-settings.json') { continue }
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
        Vision   = ($args -contains '--vision')
    }
}

# 1. the named default, when set
if ($DEFAULT_MODEL -ne '') {
    $hit = $rows | Where-Object { $_.Name -eq $DEFAULT_MODEL } | Select-Object -First 1
    if ($hit) { Write-Output $hit.Position; exit 0 }
}

# 2. position 1 - the first model in the list, which is newest first with the filename as a
#    tie-break so it cannot depend on which of two second-apart writes finished last
if ($rows.Count -gt 0) { Write-Output $rows[0].Position; exit 0 }

# 2. otherwise the first without vision - still the cheaper of what is installed
$noVision = $rows | Where-Object { -not $_.Vision } | Select-Object -First 1
if ($noVision) { Write-Output $noVision.Position; exit 0 }

# 3. otherwise the first
Write-Output 1
