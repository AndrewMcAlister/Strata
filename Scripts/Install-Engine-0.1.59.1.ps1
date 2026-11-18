# Install the engine rebuilt from the merged source (0.1.59.1) and prove the stack serves.
#
# Stops the Strata service, keeps the running engine's binaries, installs engine-next\, starts the
# service and waits for /v1/models to answer. If it does not answer, it puts the old binaries back
# and starts them again, so a bad engine cannot leave Strata down.
#
# Needs Administrator (the service runs as SYSTEM, so its engine binary is locked from here):
#   powershell -ExecutionPolicy Bypass -File "D:\Strata\Scripts\Install-Engine-0.1.59.1.ps1"
#
# The run config already carries "undeclared_tool_calls": "call" and the merged server code, so this
# restart is what makes points 2 and 3 live together.

$ErrorActionPreference = 'Stop'
$root   = 'D:\Strata'
$live   = Join-Path $root 'engine'
$new    = Join-Path $root 'engine-next'
$stamp  = Get-Date -Format 'yyyyMMdd-HHmmss'
$backup = Join-Path $root "engine-backup-$stamp"
$files  = @('strata.exe', 'strata_kq.dll', 'strata.pdb', 'BUILD.json')
$port   = 8080

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'This needs Administrator rights.'
    Write-Host 'Right-click PowerShell, Run as administrator, then:'
    Write-Host ('  powershell -ExecutionPolicy Bypass -File "' + $PSCommandPath + '"')
    exit 1
}

function Test-Serving {
    for ($i = 0; $i -lt 120; $i++) {
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:$port/v1/models" -TimeoutSec 4 -UseBasicParsing
            if ($r.StatusCode -eq 200) { return $true }
        } catch { }
        Start-Sleep -Seconds 2
    }
    return $false
}

Write-Host 'Stopping the Strata service ...'
Stop-Service Strata -Force
for ($i = 0; $i -lt 90; $i++) {
    if (-not (Get-Process strata -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Seconds 1
}
if (Get-Process strata -ErrorAction SilentlyContinue) {
    Write-Host 'The engine is still running: not touching the binaries.'
    Start-Service Strata
    exit 3
}

Write-Host "Keeping the current engine in $backup ..."
New-Item -ItemType Directory -Force -Path $backup | Out-Null
foreach ($f in $files) {
    $p = Join-Path $live $f
    if (Test-Path $p) { Copy-Item $p (Join-Path $backup $f) -Force }
}

Write-Host 'Installing the rebuilt engine ...'
foreach ($f in $files) {
    $p = Join-Path $new $f
    if (Test-Path $p) { Copy-Item $p (Join-Path $live $f) -Force }
}

Write-Host 'Starting the service (the engine copy is 32 GiB: this takes a minute) ...'
Start-Service Strata

if (Test-Serving) {
    $v = (Get-Content (Join-Path $live 'BUILD.json') -Raw | ConvertFrom-Json).version
    Write-Host "OK: serving on http://127.0.0.1:$port with engine $v"
    Write-Host "The previous binaries are in $backup"
    exit 0
}

Write-Host 'No answer on the port after four minutes: putting the old engine back ...'
Stop-Service Strata -Force
for ($i = 0; $i -lt 90; $i++) {
    if (-not (Get-Process strata -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Seconds 1
}
foreach ($f in $files) {
    $p = Join-Path $backup $f
    if (Test-Path $p) { Copy-Item $p (Join-Path $live $f) -Force }
}
Start-Service Strata
if (Test-Serving) {
    Write-Host 'Rolled back: the engine that was running is serving again.'
} else {
    Write-Host 'Rolled back, but nothing answers on the port. Check the service log:'
    Write-Host '  C:\ProgramData\Strata\service\logs\service-<date>.log'
}
exit 2
