# Stop the Strata Windows service.
#
# Strata_Start.bat calls this elevated: a start from the shortcut cannot bind port 8080 while
# the service's own server holds it, and the service's watchdog then retries against a port it
# will never get. Stopping the service first leaves exactly one owner of the port.
#
# Idempotent on purpose: an absent or already-stopped service exits 0, so a start on a machine
# that never installed the service costs nothing beyond the elevation its caller asked for.
$ErrorActionPreference = 'SilentlyContinue'

$service = Get-Service -Name Strata
if ($null -eq $service) { exit 0 }

if ($service.Status -ne 'Stopped') {
    Stop-Service -Name Strata -Force
    Start-Sleep -Milliseconds 500
}

$after = Get-Service -Name Strata
if ($null -ne $after -and $after.Status -ne 'Stopped') { exit 1 }
exit 0
