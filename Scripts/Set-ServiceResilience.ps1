<#
    Make the two Windows services survive a reboot and a crash without anyone remembering them.

        Strata AI Server          (Strata)
        Strata Sentiment Worker   (SentimentWorker)

    What this changes

      * Start type: SentimentWorker -> Automatic (Delayed Start), so the machine's daily reboot
        brings the worker back unattended. Strata itself stays **demand**, because it is no
        longer supposed to be up unless something needs it: the worker starts it only when it
        has fresh work and nothing is listening, and the desktop shortcut starts it for
        interactive use. An automatic Strata would put a 15 GB model on the card at every boot
        whether or not anything asked for one.
      * The worker declares **no dependency** on Strata. It used to, and that dependency is a
        trap twice over: Windows then starts Strata before the worker on every start (including
        a start from Task Manager), and the SCM refuses to stop Strata while the worker runs.
        Worse, a dependency removed from the registry stays live in the SCM's memory until the
        next reboot, so "sc qc says empty" is not proof. This script clears it outright.
      * Recovery actions re-asserted: restart after 5 s, then 30 s, then 60 s, reset after a day.
        A crash is restarted; a deliberate stop is not.
      * failureflag 1 on the worker only: an exit with a non-zero code counts as a failure too,
        so a worker that dies of its own error comes back. Strata gets failureflag 0, so a
        deliberate stop stays stopped instead of being undone five seconds later.

    Nothing else is touched: the model, service.conf, the configs, the logs and the data folder
    stay as they are. To go back to manual starts:

        sc.exe config Strata start= demand
        sc.exe config SentimentWorker start= demand

    Run it (it asks for Administrator rights):

        powershell -NoProfile -ExecutionPolicy Bypass -File D:\Strata\Scripts\Set-ServiceResilience.ps1
#>

[CmdletBinding()]
param([switch]$Force)

$ErrorActionPreference = 'Continue'

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Admin)) {
    if ($Force) {
        Write-Host 'Administrator rights are required to change a service.' -ForegroundColor Red
        exit 1
    }
    Write-Host 'Asking for Administrator rights...' -ForegroundColor Cyan
    $launch = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $MyInvocation.MyCommand.Path, '-Force')
    $p = Start-Process -FilePath 'powershell.exe' -ArgumentList $launch -Verb RunAs -Wait -PassThru
    exit $p.ExitCode
}

$settings = @(
    @{ Name = 'Strata';          Start = 'demand';       FailureFlag = 0 },
    @{ Name = 'SentimentWorker'; Start = 'delayed-auto'; FailureFlag = 1 }
)

foreach ($setting in $settings) {
    $name = $setting.Name
    $svc = Get-Service -Name $name -ErrorAction SilentlyContinue
    if (-not $svc) {
        Write-Host "$name is not installed here; skipped." -ForegroundColor Yellow
        continue
    }
    Write-Host ''
    Write-Host "$name" -ForegroundColor Cyan
    sc.exe config $name start= $setting.Start | Out-Null
    sc.exe failure $name reset= 86400 actions= restart/5000/restart/30000/restart/60000 | Out-Null
    sc.exe failureflag $name $setting.FailureFlag | Out-Null
    sc.exe qc $name | Select-String 'START_TYPE|DEPENDENCIES|SERVICE_START_NAME' | ForEach-Object { "   " + $_.Line.Trim() }
    sc.exe qfailure $name | Select-String 'RESET_PERIOD|FAILURE_ACTIONS|RESTART --' | ForEach-Object { "   " + $_.Line.Trim() }
}

# Clear a dependency the worker must not have. It has to go through cmd: PowerShell drops the
# empty argument, so `sc.exe config SentimentWorker depend= ""` from here changes nothing.
Write-Host ''
Write-Host 'SentimentWorker dependency' -ForegroundColor Cyan
& cmd.exe /c 'sc config SentimentWorker depend= ""' | Out-Null
sc.exe qc SentimentWorker | Select-String 'DEPENDENCIES' | ForEach-Object { "   " + $_.Line.Trim() }

Write-Host ''
Write-Host 'Done. The worker starts at the next boot, two minutes after it; Strata stays demand,' -ForegroundColor Green
Write-Host 'so it comes up only when the worker has fresh work or a shortcut asks for it.'
Write-Host 'A dependency removed just now takes effect for service starts only after a reboot:'
Write-Host 'the SCM keeps the old graph in memory until then.'
Write-Host 'The services are still running now; no restart was needed for these settings.'
