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

    service.conf's components= names the folder the engine packages are installed in (usually
    %LOCALAPPDATA%\Strata\components). The service runs as LocalSystem, which has no profile of its own, so it
    cannot find the package of a model with its own engine (Inkling-2-Mini: strata-inkling, Hy4-Flash, a Qwen4
    shape) without this line. `strata launch` and the app run as the person, so they never need it.

        Strata_Engine.ps1                 what this PC has, and what Strata will use (changes nothing)
        Strata_Engine.ps1 -List           every engine installed for this user
        Strata_Engine.ps1 -Set <value>    write engine= into service.conf (a path, or component:<name>)
        Strata_Engine.ps1 -Clear          drop engine=, so the model config's own "exe" is used
        Strata_Engine.ps1 -Components <folder>   write components= (the engine package folder)
        Strata_Engine.ps1 -ClearComponents       drop components=
        Strata_Engine.ps1 -Set ... -Restart   ...and restart the service (asks for Administrator)

    Nothing is downloaded here: an engine that is not installed is `strata components update`'s job.
#>
param(
    [string]$Set,
    [switch]$Clear,
    [string]$Components,
    [switch]$ClearComponents,
    [switch]$List,
    [switch]$Restart,
    [string]$Root = 'D:\Strata'
)

$ErrorActionPreference = 'Stop'

$TERMINAL = if (Test-Path -LiteralPath (Join-Path $Root 'terminal\strata.py')) { Join-Path $Root 'terminal' } else { $Root }
$PY = Join-Path $TERMINAL '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $PY)) { $PY = Join-Path $Root '.venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $PY)) { $PY = 'python' }
$STRATA = Join-Path $TERMINAL 'strata.py'
$env:PYTHONPATH = $TERMINAL          # the probe below imports strata_control from here
$SERVICE = Join-Path $Root 'Scripts\Strata_Service.ps1'

function ConfPath {
    if ($env:STRATA_SERVICE_ROOT) { return (Join-Path $env:STRATA_SERVICE_ROOT 'service.conf') }
    $programData = if ($env:ProgramData) { $env:ProgramData } else { 'C:\ProgramData' }
    return (Join-Path $programData 'Strata\service\service.conf')
}

function Read-Key([string]$path, [string]$key) {
    if (-not (Test-Path -LiteralPath $path)) { return '' }
    foreach ($line in Get-Content -LiteralPath $path) {
        if ($line -match ('^\s*' + [regex]::Escape($key) + '\s*=\s*(.+?)\s*$')) { return $matches[1].Trim('"').Trim("'") }
    }
    return ''
}

function Set-Key([string]$path, [string]$key, [string]$value) {
    if (-not (Test-Path -LiteralPath $path)) { throw "$path does not exist. Install the service first." }
    $lines = @(Get-Content -LiteralPath $path)
    $found = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match ('^\s*' + [regex]::Escape($key) + '\s*=')) {
            if ($value) { $lines[$i] = "$key=$value" } else { $lines[$i] = "#$key=" }
            $found = $true
        }
    }
    if (-not $found -and $value) { $lines += "$key=$value" }
    Set-Content -LiteralPath $path -Value $lines -Encoding utf8
}

function Read-Engine([string]$path) { return (Read-Key $path 'engine') }
function Set-Engine([string]$path, [string]$value) { Set-Key $path 'engine' $value }

$conf = ConfPath

if ($List) {
    & $PY $STRATA components engines
    exit $LASTEXITCODE
}

if ($Set -or $Clear -or $Components -or $ClearComponents) {
    if ($Set -or $Clear) {
        $value = if ($Clear) { '' } else { $Set }
        Set-Engine $conf $value
        if ($value) { Write-Host "engine=$value" -ForegroundColor Cyan } else { Write-Host 'engine= removed: the model config names the engine' -ForegroundColor Cyan }
        Write-Host "written to $conf"
    }
    if ($Components -or $ClearComponents) {
        $folder = if ($ClearComponents) { '' } else { $Components }
        if ($folder) {
            if (-not (Test-Path -LiteralPath $folder)) { throw "$folder does not exist. That is the folder the engine packages are installed in, usually %LOCALAPPDATA%\Strata\components." }
            Set-Key $conf 'components' $folder
            Write-Host "components=$folder" -ForegroundColor Cyan
        } else {
            Set-Key $conf 'components' ''
            Write-Host 'components= removed: the service uses a packaged engine= path, then its own state folder' -ForegroundColor Cyan
        }
        Write-Host "written to $conf"
    }
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

# A model with its own engine (Inkling-2-Mini, Hy4-Flash, a Qwen4 shape) runs a package from the components
# folder, and the service has no profile of its own: without components= it reads its own empty state folder
# and says "needs its own engine" in its log.
$folder = Read-Key $conf 'components'
$here = if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'Strata\components' } else { '' }
if ($folder) {
    Write-Host "The service finds engine packages in (service.conf): $folder" -ForegroundColor Green
} else {
    Write-Host 'The service finds engine packages in its own state folder (no components= in service.conf).'
    if ($here -and ((Test-Path -LiteralPath (Join-Path $here 'engine')) -or (Test-Path -LiteralPath (Join-Path $here 'shapes')))) {
        Write-Host ("  This PC's component folder is {0}: a model with its own engine needs -Components {0}" -f $here) -ForegroundColor Yellow
    }
}
Write-Host "Settings file: $conf"
Write-Host 'Change it with -Set <path or component:strata-engine> or -Clear, or -Components <folder>; add -Restart to restart the service.'
