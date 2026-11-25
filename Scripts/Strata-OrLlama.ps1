<#
    Which model server answers on http://127.0.0.1:8080: Strata or llama.cpp - never both.

    WHY THIS FILE

    Both servers are set up for the same address on purpose, so a client (an editor, a chat app,
    a script) keeps one base URL whatever is serving those minutes: http://127.0.0.1:8080/v1.

        Strata          the "Strata" Windows service (Automatic, starts at boot), port= in
                        C:\ProgramData\Strata\service\service.conf
        llama.cpp       the "llama-server" service (Manual, D:\llama-service), started with
                        --host 127.0.0.1 --port 8080

    One port means one of them at a time. Starting the second while the first holds 8080 does not
    "just work": the new server finds the port taken and stops, or Strata moves to another port and
    the client's URL silently stops answering. So the switch is written down here instead of being
    remembered: stop the one that runs, wait for the port to close, start the other, then wait
    until it answers.

    Strata is stopped through Strata_Service.ps1, which asks the server to drain first; the model
    finishes the answer it is writing before the port closes. llama.cpp is stopped through its own
    service, whose nssm wrapper closes the process.

    HOW TO USE IT

        powershell -ExecutionPolicy Bypass -File D:\Strata\Scripts\Strata-OrLlama.ps1 -Use Strata
        powershell -ExecutionPolicy Bypass -File D:\Strata\Scripts\Strata-OrLlama.ps1 -Use Llama
        powershell -ExecutionPolicy Bypass -File D:\Strata\Scripts\Strata-OrLlama.ps1 -Use Status
        powershell -ExecutionPolicy Bypass -File D:\Strata\Scripts\Strata-OrLlama.ps1 -Use Stop

    Stopping and starting services needs Administrator rights; the script asks for them the usual
    Windows way. -Wait N is seconds to wait for the wanted server to answer (default 300: a large
    model loads cold in minutes).

    Exit codes: 0 the wanted server answers, 1 something failed (the message says what),
    2 the port is held by a program that is neither service.
#>

[CmdletBinding()]
param(
    [ValidateSet('Strata', 'Llama', 'Status', 'Stop')]
    [string]$Use = 'Status',
    [int]$Wait = 300
)

$ErrorActionPreference = 'Stop'

$strataService = 'Strata'
$llamaService = 'llama-server'
$strataScript = 'D:\Strata\Scripts\Strata_Service.ps1'
$portTool = 'D:\Strata\Scripts\Strata_Port.ps1'

# The port is read, not written here: service.conf owns it (Scripts\Strata_Port.ps1), and this
# script must check the same number that Strata serves on.
$port = 8080
if (Test-Path -LiteralPath $portTool) {
    $asked = & powershell -NoProfile -ExecutionPolicy Bypass -File $portTool 2>$null
    if ("$asked".Trim() -match '^\d+$') { $port = [int]"$asked".Trim() }
}
$base = "http://127.0.0.1:$port"

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Confirm-Admin {
    if (Test-Admin) { return $true }
    Write-Host "Asking for Administrator rights..." -ForegroundColor Cyan
    $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath, '-Use', $Use, '-Wait', [string]$Wait)
    try {
        $process = Start-Process -FilePath 'powershell.exe' -ArgumentList $arguments -Verb RunAs -Wait -PassThru
    } catch {
        Write-Host "Elevation was not given, so nothing was changed." -ForegroundColor Yellow
        exit 1
    }
    exit $process.ExitCode
}

function Get-State([string]$name) {
    $service = Get-Service -Name $name -ErrorAction SilentlyContinue
    if ($null -eq $service) { return 'not installed' }
    return [string]$service.Status
}

# What answers on the port, asked of the server itself: Strata says "strata" and names its model.
function Get-Answer {
    try {
        $health = Invoke-RestMethod -Uri "$base/health" -TimeoutSec 3 -ErrorAction Stop
        if ($health.status -eq 'ok') {
            $who = if ($health.service) { $health.service } else { 'unknown' }
            $model = if ($health.model) { $health.model } else { '?' }
            return ("{0} (model {1})" -f $who, $model)
        }
    } catch { }
    return $null
}

function Test-PortOpen {
    return (@(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue).Count -gt 0)
}

function Wait-PortClosed([int]$seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        if (-not (Test-PortOpen)) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return -not (Test-PortOpen)
}

function Wait-Answer([int]$seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    $said = 0
    while ((Get-Date) -lt $deadline) {
        $answer = Get-Answer
        if ($answer) { return $answer }
        # llama.cpp answers /health with 503 while it loads a model; Strata's server is not up
        # until the model is in memory, so "not answering" covers both.
        $spent = [int]($seconds - ($deadline - (Get-Date)).TotalSeconds)
        if ($spent -ge $said + 15) {
            $said = $spent
            Write-Host "  still loading ($spent s)..." -ForegroundColor DarkGray
        }
        Start-Sleep -Seconds 2
    }
    return $null
}

function Stop-Strata {
    if ((Get-State $strataService) -ne 'Running') { return }
    Write-Host "Stopping Strata (it finishes the answer it is writing first)..." -ForegroundColor Cyan
    & powershell -NoProfile -ExecutionPolicy Bypass -File $strataScript -Action Stop
    if (-not (Wait-PortClosed 120)) {
        Write-Host "Strata still holds port $port after 120 s. Run Strata_Service.ps1 -Action Status and read its log." -ForegroundColor Red
        exit 1
    }
}

function Stop-Llama {
    if ((Get-State $llamaService) -ne 'Running') { return }
    Write-Host "Stopping llama.cpp..." -ForegroundColor Cyan
    Stop-Service -Name $llamaService -Force
    if (-not (Wait-PortClosed 120)) {
        Write-Host "llama.cpp still holds port $port after 120 s. Stop it in services.msc and try again." -ForegroundColor Red
        exit 1
    }
}

function Start-Strata {
    Write-Host "Starting Strata (the model loads in the background)..." -ForegroundColor Cyan
    & powershell -NoProfile -ExecutionPolicy Bypass -File $strataScript -Action Start
}

function Start-Llama {
    Write-Host "Starting llama.cpp..." -ForegroundColor Cyan
    Start-Service -Name $llamaService
}

# A program that is neither service must not be killed: it belongs to someone else, and the
# message names the process so the person can decide.
function Test-Stranger {
    $listener = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) | Select-Object -First 1
    if ($null -eq $listener) { return $false }
    $owner = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
    if ($null -eq $owner) { return $false }
    $ours = @('python', 'strata', 'strata-inkling', 'llama-server')
    if ($ours -contains $owner.ProcessName) { return $false }
    Write-Host ("Port {0} is held by {1} (process {2}), which is neither service. Nothing was changed." -f $port, $owner.ProcessName, $owner.Id) -ForegroundColor Red
    return $true
}

function Show-Status {
    $who = Get-Answer
    Write-Host ""
    Write-Host ("Address:    {0}   (OpenAI base: {0}/v1)" -f $base) -ForegroundColor White
    Write-Host ("Strata:     service {0}" -f (Get-State $strataService))
    Write-Host ("llama.cpp:  service {0}" -f (Get-State $llamaService))
    if ($who) { Write-Host ("Answering:  {0}" -f $who) -ForegroundColor Green }
    elseif (Test-PortOpen) { Write-Host "Answering:  the port is open, but /health does not answer yet (still loading)" -ForegroundColor Yellow }
    else { Write-Host "Answering:  nothing" -ForegroundColor Yellow }
    Write-Host ""
}

switch ($Use) {
    'Status' {
        Show-Status
        exit 0
    }
    'Stop' {
        Confirm-Admin | Out-Null
        Stop-Strata
        Stop-Llama
        if (Test-PortOpen) {
            Write-Host "Port $port is still open." -ForegroundColor Red
            exit 1
        }
        Write-Host "Both are stopped; port $port is free." -ForegroundColor Green
        exit 0
    }
    'Strata' {
        Confirm-Admin | Out-Null
        if (Test-Stranger) { exit 2 }
        Stop-Llama
        if ((Get-State $strataService) -ne 'Running') { Start-Strata }
        else { Write-Host "Strata is already running." }
    }
    'Llama' {
        Confirm-Admin | Out-Null
        if (Test-Stranger) { exit 2 }
        Stop-Strata
        if ((Get-State $llamaService) -ne 'Running') { Start-Llama }
        else { Write-Host "llama.cpp is already running." }
    }
}

Write-Host "Waiting up to $Wait s for $base to answer..." -ForegroundColor Cyan
$answer = Wait-Answer $Wait
if (-not $answer) {
    Write-Host ("{0} did not answer on {1} within {2} s. Strata: Strata_Service.ps1 -Action Logs. llama.cpp: D:\llama-service\logs." -f $Use, $base, $Wait) -ForegroundColor Red
    exit 1
}

Write-Host ("Ready: {0} - {1}/v1  (OpenAI: /v1/chat/completions, Anthropic: /v1/messages)" -f $answer, $base) -ForegroundColor Green
Write-Host ("Open {0}/ in a browser to chat." -f $base) -ForegroundColor Green
exit 0
