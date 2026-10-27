# Strata as a Windows service: install, start, stop, check, remove.
#
# A service is registered with Windows, so it starts at boot and keeps serving
# after the person who installed it signs out. Strata's own start/stop scripts
# still work; do not run both at once, or two servers will want the same port.
#
# Run this from PowerShell. The actions that change the system ask for elevation
# in the usual Windows way; Status, Logs and Edit do not.
#
#   powershell -ExecutionPolicy Bypass -File Scripts\Strata_Service.ps1 -Action Install
#   powershell -ExecutionPolicy Bypass -File Scripts\Strata_Service.ps1 -Action Status
#
# Actions:
#   Install    register the service (automatic start) and start it now.
#              The model is the service.conf line; the file is written on the
#              first install with every installed model listed as a comment.
#   Uninstall  stop the service and remove it. Settings and logs are kept
#              unless -Purge is given. Models and the Strata install are never
#              touched.
#   Start      start the service. The model loads in the background.
#   Stop       stop the service; the server is asked to drain first.
#   Restart    Stop, then Start. Use this after editing service.conf.
#   Status     say what the service and the model are doing.
#   Logs       show the end of the newest service log (-Tail N).
#   Edit       open service.conf in Notepad to change the model.
#
# Options:
#   -Config PATH   install only: the model config to serve.
#   -NoStart       install only: register but do not start.
#   -Force         install only: replace a same-named service not made by this script.
#   -Purge         uninstall only: also delete settings, logs and service state.
#   -Name NAME     service name (default Strata).
#   -Tail N        logs only: lines to show (default 60).

[CmdletBinding()]
param(
    [ValidateSet('Install', 'Uninstall', 'Start', 'Stop', 'Restart', 'Status', 'Logs', 'Edit')]
    [string]$Action = 'Status',
    [string]$Config,
    [switch]$NoStart,
    [switch]$Purge,
    [switch]$Force,
    [string]$Name = 'Strata',
    [int]$Tail = 60
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$serviceScript = Join-Path $PSScriptRoot 'Strata_Service.py'

if ($env:STRATA_SERVICE_ROOT) {
    $serviceRoot = $env:STRATA_SERVICE_ROOT
} elseif ($env:ProgramData) {
    $serviceRoot = Join-Path $env:ProgramData 'Strata\service'
} else {
    $serviceRoot = 'C:\ProgramData\Strata\service'
}
$confPath = Join-Path $serviceRoot 'service.conf'

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Quote-Text([string]$text) {
    return "'" + ($text -replace "'", "''") + "'"
}

function Find-Python {
    if ($env:STRATA_PYTHON -and (Test-Path -LiteralPath $env:STRATA_PYTHON)) {
        return @{ Exe = $env:STRATA_PYTHON; Args = @() }
    }
    $bundled = Join-Path $root 'python\python.exe'
    if (Test-Path -LiteralPath $bundled) {
        return @{ Exe = $bundled; Args = @() }
    }
    $venv = Join-Path $root '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venv) {
        return @{ Exe = $venv; Args = @() }
    }
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        return @{ Exe = $py.Source; Args = @('-3') }
    }
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) {
        return @{ Exe = $python.Source; Args = @() }
    }
    return $null
}

function Show-OldAutostart {
    try {
        $tasks = Get-ScheduledTask -ErrorAction Stop | Where-Object {
            $_.State -ne 'Disabled' -and ($_.Actions | Where-Object {
                $_.Arguments -and ($_.Arguments -match 'Strata_Start|strata_service|strata-iq3')
            })
        }
        foreach ($task in $tasks) {
            Write-Warning ("The scheduled task '{0}' also starts Strata. Disable it so two servers do not use the same port: Disable-ScheduledTask -TaskName '{0}'" -f $task.TaskName)
        }
    } catch {
        # Reading scheduled tasks is a courtesy; a locked-down machine may refuse it.
    }
    try {
        $run = Get-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -ErrorAction Stop
        foreach ($entry in $run.PSObject.Properties) {
            if ($entry.Value -and ($entry.Value -match 'Strata')) {
                Write-Warning ("The Run entry '{0}' also starts Strata at sign-in. Remove it if the service should be the only copy." -f $entry.Name)
            }
        }
    } catch {
        # No Run key is the normal case.
    }
}

if (-not (Test-Path -LiteralPath $serviceScript)) {
    Write-Error "Cannot find $serviceScript. Run this from a Strata source checkout."
    exit 1
}

if ($Action -eq 'Edit') {
    if (-not (Test-Path -LiteralPath $confPath)) {
        Write-Host "There is no service.conf yet at $confPath." -ForegroundColor Yellow
        Write-Host "Install the service first: .\Scripts\Strata_Service.ps1 -Action Install" -ForegroundColor Yellow
        exit 1
    }
    Start-Process -FilePath 'notepad.exe' -ArgumentList ('"{0}"' -f $confPath) | Out-Null
    Write-Host "Opened $confPath. Save, then restart the service: -Action Restart" -ForegroundColor Green
    exit 0
}

$needsAdmin = @('Install', 'Uninstall', 'Start', 'Stop', 'Restart') -contains $Action
$python = Find-Python
if (-not $python) {
    Write-Error "No Python was found. Set STRATA_PYTHON to the python.exe that runs Strata, or install the Strata Terminal."
    exit 1
}

$pythonArgs = @()
foreach ($extra in $python.Args) { $pythonArgs += $extra }
$pythonArgs += @($serviceScript, ($Action.ToLowerInvariant()), '--name', $Name)
if ($Action -eq 'Install') {
    if ($Config) { $pythonArgs += @('--config', $Config) }
    if ($NoStart) { $pythonArgs += '--no-start' }
    if ($Force) { $pythonArgs += '--force' }
}
if ($Action -eq 'Uninstall' -and $Purge) { $pythonArgs += '--purge' }
if ($Action -eq 'Logs') { $pythonArgs += @('--tail', [string]$Tail) }

if ($needsAdmin -and -not (Test-Admin)) {
    # Start-Process -Verb RunAs uses ShellExecute, which cannot redirect. The
    # elevated copy writes its own output to a temp file, and that file is shown
    # here after it exits, so the result stays in this window.
    $temp = Join-Path $env:TEMP ('strata-service-{0}.log' -f [guid]::NewGuid().ToString('N'))
    $parts = @('-Action', (Quote-Text $Action), '-Name', (Quote-Text $Name))
    if ($Action -eq 'Install') {
        if ($Config) { $parts += @('-Config', (Quote-Text $Config)) }
        if ($NoStart) { $parts += '-NoStart' }
        if ($Force) { $parts += '-Force' }
    }
    if ($Action -eq 'Uninstall' -and $Purge) { $parts += '-Purge' }
    $inner = "& {0} {1} *>&1 | Tee-Object -FilePath {2}; exit `$LASTEXITCODE" -f `
        (Quote-Text $serviceScript), ($parts -join ' '), (Quote-Text $temp)
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($inner))
    Write-Host "Asking for Administrator rights..." -ForegroundColor Cyan
    try {
        $process = Start-Process -FilePath 'powershell.exe' `
            -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -EncodedCommand {0}' -f $encoded) `
            -Verb RunAs -WindowStyle Hidden -WorkingDirectory $root -Wait -PassThru
    } catch {
        Write-Host "Elevation was not given, so nothing was changed." -ForegroundColor Yellow
        exit 1
    }
    if (Test-Path -LiteralPath $temp) {
        Get-Content -LiteralPath $temp | ForEach-Object { Write-Host $_ }
        Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
    }
    if ($process.ExitCode -ne 0) {
        Write-Host "That did not finish (exit code $($process.ExitCode))." -ForegroundColor Red
    }
    exit $process.ExitCode
}

if ($Action -eq 'Install') {
    Show-OldAutostart
}

& $python.Exe @pythonArgs
exit $LASTEXITCODE
