<#
    Which engine Strata runs, and the one line that changes it.

    THE RULE

    The engine family is the one belonging to the card with the most *dedicated* memory: NVIDIA -> cuda,
    AMD -> hip, Intel -> sycl. A card that shares the system memory (an APU's Radeon, an Intel iGPU) reports
    only its small BIOS carve-out, so it never outvotes a card that holds its own memory. The rule lives in
    strata_control/engine_component.py (detect_backend); this script asks it instead of repeating it, and
    prints the cards it decided from.

    THE SETTING

    service.conf's engine= names the engine the Windows service runs, instead of the run config's own "exe":
    a path (D:\Strata\build-906-win\strata.exe) or component:strata-engine (the engine installed for this
    PC's card). One folder of model configs, two PCs, two engines - that is what the line is for.

        Strata_Engine.ps1                 what this PC has, and what Strata will use (changes nothing)
        Strata_Engine.ps1 -List           every engine installed for this user
        Strata_Engine.ps1 -Set <value>    write engine= into service.conf (a path, or component:<name>)
        Strata_Engine.ps1 -Clear          drop engine=, so the model config's own "exe" is used
        Strata_Engine.ps1 -Set ... -Restart   ...and restart the service (asks for Administrator)

    Nothing is downloaded here: an engine that is not installed is `strata components update`'s job.
#>
param(
    [string]$Set,
    [switch]$Clear,
    [switch]$List,
    [switch]$Restart,
    [string]$Root = 'D:\Strata'
)

$ErrorActionPreference = 'Stop'

$PY = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $PY)) { $PY = 'python' }
$STRATA = Join-Path $Root 'strata.py'
$SERVICE = Join-Path $Root 'Scripts\Strata_Service.ps1'

function ConfPath {
    if ($env:STRATA_SERVICE_ROOT) { return (Join-Path $env:STRATA_SERVICE_ROOT 'service.conf') }
    $programData = if ($env:ProgramData) { $env:ProgramData } else { 'C:\ProgramData' }
    return (Join-Path $programData 'Strata\service\service.conf')
}

function Read-Engine([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return '' }
    foreach ($line in Get-Content -LiteralPath $path) {
        if ($line -match '^\s*engine\s*=\s*(.+?)\s*$') { return $matches[1].Trim('"').Trim("'") }
    }
    return ''
}

function Set-Engine([string]$path, [string]$value) {
    if (-not (Test-Path -LiteralPath $path)) { throw "$path does not exist. Install the service first." }
    $lines = @(Get-Content -LiteralPath $path)
    $found = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match '^\s*engine\s*=') {
            if ($value) { $lines[$i] = "engine=$value" } else { $lines[$i] = "#engine=" }
            $found = $true
        }
    }
    if (-not $found -and $value) { $lines += "engine=$value" }
    Set-Content -LiteralPath $path -Value $lines -Encoding utf8
}

$conf = ConfPath

if ($List) {
    & $PY $STRATA components engines
    exit $LASTEXITCODE
}

if ($Set -or $Clear) {
    $value = if ($Clear) { '' } else { $Set }
    Set-Engine $conf $value
    if ($value) { Write-Host "engine=$value" -ForegroundColor Cyan } else { Write-Host 'engine= removed: the model config names the engine' -ForegroundColor Cyan }
    Write-Host "written to $conf"
    if ($Restart) {
        & powershell -NoProfile -ExecutionPolicy Bypass -File $SERVICE -Action Restart
        exit $LASTEXITCODE
    }
    Write-Host "Restart the service to use it:  $SERVICE -Action Restart" -ForegroundColor Yellow
    exit 0
}

# --- what this PC has, and what Strata would start -------------------------------------------------
$probe = @'
import json
from strata_control import engine_component as ec
backend, how = ec.detect_backend()
cards = sorted(({"name": a.get("name") or "?", "vram_gb": round(float(a.get("vram_gb") or 0), 1),
                 "vendor": a.get("vendor")} for a in ec.windows_display_adapters()), key=lambda c: -c["vram_gb"])
print(json.dumps({"backend": backend, "how": how, "cards": cards}))
'@
$info = $probe | & $PY - | ConvertFrom-Json

Write-Host ''
Write-Host "Cards on this PC, most dedicated memory first:" -ForegroundColor Cyan
foreach ($card in $info.cards) {
    $make = switch ($card.vendor) { '10de' { 'cuda' } '1002' { 'hip' } '8086' { 'sycl' } default { '?' } }
    Write-Host ("  {0,-32} {1,6:N1} GB  ({2})" -f $card.name, $card.vram_gb, $make)
}
Write-Host ("The engine family follows the largest dedicated memory here: {0} ({1})." -f $info.backend, $info.how)

$report = (& $PY $STRATA components engine --output json 2>$null | ConvertFrom-Json)
if ($report -and $report.engine) {
    Write-Host ("The engine Strata would start: {0} {1}-{2}" -f $report.engine.version, $report.engine.platform, $report.engine.backend)
    Write-Host ("  {0}" -f $report.engine.exe)
    if ($report.update_notice) { Write-Host ("  {0}" -f $report.update_notice) -ForegroundColor Yellow }
} else {
    Write-Host 'No engine is installed for this PC: run  strata components update' -ForegroundColor Yellow
    if ($report -and $report.notice) { Write-Host ("  {0}" -f $report.notice) -ForegroundColor Yellow }
}

$named = Read-Engine $conf
Write-Host ''
if ($named) {
    Write-Host "The Windows service runs this engine (service.conf): $named" -ForegroundColor Green
} else {
    Write-Host 'The Windows service runs the engine its model config names (no engine= in service.conf).'
}
Write-Host "Settings file: $conf"
Write-Host 'Change it with -Set <path or component:strata-engine> or -Clear; add -Restart to restart the service.'
