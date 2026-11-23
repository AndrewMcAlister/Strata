<#
    The four Desktop Strata shortcuts in one script, so start and stop always agree.

        -Action Start [-Model <config.json>]   enable the 15-minute watchdog, then start Strata
                                              (the model menu, or -Model straight away) in this
                                              console window. Closing that window stops it.
        -Action Stop                          stop Strata whatever it is: the Windows service if
                                              that is what runs, or the console server and its
                                              python - Strata_Stop.bat handles both, drains the
                                              engine and releases the pinned memory. Then stop
                                              the sentiment worker and disable the watchdog, so
                                              it stays stopped instead of coming back in 15 min.
        -Action Status                        what is running. No elevation needed.

    Why the watchdog is toggled here: it restarts Strata when nothing answers on 8080 for fifteen
    minutes. A stop that it undoes fifteen minutes later is not a stop, so Stop disables it and
    every Start enables it again.

        powershell -NoProfile -ExecutionPolicy Bypass -File D:\Strata\Scripts\StrataStack.ps1 -Action Start
        powershell -NoProfile -ExecutionPolicy Bypass -File D:\Strata\Scripts\StrataStack.ps1 -Action Start -Model D:\Strata\strata-iq3_xxs-dflash2.json
        powershell -NoProfile -ExecutionPolicy Bypass -File D:\Strata\Scripts\StrataStack.ps1 -Action Stop
        powershell -NoProfile -ExecutionPolicy Bypass -File D:\Strata\Scripts\StrataStack.ps1 -Action Status
#>

[CmdletBinding()]
param(
    [ValidateSet('Start', 'Stop', 'Status')]
    [string]$Action = 'Status',
    [string]$Model,
    [switch]$Force
)

$ErrorActionPreference = 'Continue'

$BATCH_START = 'D:\Strata\Scripts\Strata_Start.bat'
$BATCH_STOP = 'D:\Strata\Scripts\Strata_Stop.bat'
$PORT_HELPER = 'D:\Strata\Scripts\Strata_Port.ps1'
$TASK = 'Strata_EnsureRunning'
$WORKER = 'SentimentWorker'

function Say([string]$text, [string]$colour = 'Gray') {
    Write-Host $text -ForegroundColor $colour
}

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function StrataPort {
    # One place knows the port (Scripts\Strata_Port.ps1: service.conf, the model config, then 8080):
    # another program can own 8080 - a llama.cpp service does on this computer - and Strata then
    # serves on the port its own service.conf names.
    $port = 0
    if (Test-Path -LiteralPath $PORT_HELPER) {
        $text = & powershell -NoProfile -ExecutionPolicy Bypass -File $PORT_HELPER 2>$null
        if ("$text".Trim() -match '^\d+$') { $port = [int]"$text".Trim() }
    }
    if ($port -le 0) { $port = 8080 }
    return $port
}

function StrataAnswering([int]$port) {
    return (@(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue).Count -gt 0)
}

function WatchdogState {
    $task = Get-ScheduledTask -TaskName $TASK -ErrorAction SilentlyContinue
    if (-not $task) { return 'not installed' }
    return [string]$task.State
}

function SetWatchdog([bool]$enabled) {
    # $true: changed. $false: the task exists and could not be changed. 'absent': no task was ever
    # registered here (the Windows service restarts itself then, so there is nothing to warn about).
    $task = Get-ScheduledTask -TaskName $TASK -ErrorAction SilentlyContinue
    if (-not $task) { return 'absent' }
    $flag = if ($enabled) { '/ENABLE' } else { '/DISABLE' }
    & schtasks.exe /Change /TN $TASK $flag *> $null
    return ($LASTEXITCODE -eq 0)
}

switch ($Action) {

    'Status' {
        $port = StrataPort
        $worker = Get-Service -Name $WORKER -ErrorAction SilentlyContinue
        Say ("  {0,-16} {1}" -f 'SentimentWorker', $(if ($worker) { $worker.Status } else { 'not installed' }))
        Say ("  {0,-16} {1}" -f 'Strata', $(if (StrataAnswering $port) { "answering on $port" } else { "not answering on $port" }))
        Say ("  {0,-16} {1}" -f 'watchdog task', (WatchdogState))
        exit 0
    }

    'Start' {
        $port = StrataPort
        if ((SetWatchdog $true) -eq $false) { Say ("watchdog: could not be enabled (task '{0}')" -f $TASK) 'Yellow' }

        # The stop happens HERE in process mode, not through the batch's /restart. Strata_Start.bat
        # treats a running server as "this is the model you meant" and /restart reloads that same
        # config - it never asks which model. Stopping first means the batch starts with nothing
        # running, so it offers the menu again. That is the whole point: one click to change model.
        #
        # With the Windows service installed there is nothing to stop here: the batch switches the
        # service (Strata_Service.ps1 -Action Serve stops it, writes the choice and starts it again),
        # and a stop first would only add a second Administrator prompt to the click.
        $serviceInstalled = @(Get-Service -Name 'Strata' -ErrorAction SilentlyContinue).Count -gt 0
        if (-not $serviceInstalled -and (StrataAnswering $port)) {
            Say 'stopping the running model first...' 'Cyan'
            & cmd.exe /c "$BATCH_STOP /quiet" | Out-Null
            Start-Sleep -Seconds 2
        }

        if ($Model) {
            $runArgs = @('--config', $Model, '/noopen', '/nopause')
            Say ("starting {0}" -f (Split-Path $Model -Leaf)) 'Cyan'
        }
        else {
            # /nopause: in service mode the batch hands the model to the service and returns, so this
            # window has nothing left to watch and closes once the pages have opened. (/fg would ask
            # for a process of the batch's own instead of the service - not what the shortcut wants.)
            $runArgs = @('/nopause')
            Say 'asking which model to start' 'Cyan'
        }

        # Runs in THIS console: with the service installed the model is the service's, and the
        # window closes when the menu is answered. Without it the server lives in this window,
        # and closing the window stops it.
        & cmd.exe /c ("$BATCH_START " + ($runArgs -join ' '))
        exit $LASTEXITCODE
    }

    'Stop' {
        $watchdog = SetWatchdog $false
        if ($watchdog -eq $false) { Say ("watchdog: could not be disabled (task '{0}')" -f $TASK) 'Yellow' }
        elseif ($watchdog -eq $true) { Say 'watchdog: disabled, so it will not start Strata again' }

        Say 'stopping Strata (service or console server, whichever is running)...' 'Cyan'
        & cmd.exe /c "$BATCH_STOP /quiet"
        $stopCode = $LASTEXITCODE

        $worker = Get-Service -Name $WORKER -ErrorAction SilentlyContinue
        if ($worker -and $worker.Status -ne 'Stopped') {
            try {
                Stop-Service -Name $WORKER -ErrorAction Stop
                Say 'SentimentWorker: stopped'
            }
            catch {
                Say ('SentimentWorker: not stopped - {0}' -f $_.Exception.Message.Trim()) 'Yellow'
                Say '  (stopping a service needs Administrator: StopMcTrader does it elevated)' 'Yellow'
            }
        }
        elseif ($worker) {
            Say 'SentimentWorker: already stopped'
        }

        Say ''
        Say ("  {0,-16} {1}" -f 'Strata', $(if (StrataAnswering) { 'STILL answering on 8080' } else { 'stopped' })) $(if (StrataAnswering) { 'Red' } else { 'Green' })
        Say ("  {0,-16} {1}" -f 'watchdog task', (WatchdogState))
        if (StrataAnswering) { exit 1 }
        exit 0
    }
}
