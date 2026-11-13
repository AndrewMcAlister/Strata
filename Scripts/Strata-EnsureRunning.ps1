<#
    If Strata is not running, start it - with the same batch file the Desktop shortcut runs.

    WHY THIS IS NOT DONE BY THE SENTIMENT WORKER

    SentimentWorker is a LocalSystem service in session 0. Anything it starts runs there: no
    window on your desktop, and no one to answer the model prompt that Strata_Start.bat asks.
    Worse, a model started that way would be running as SYSTEM and invisible - and then pressing
    the Strata shortcut would say "already running", which is exactly what must not happen when
    the point of the shortcut is to change model.

    So this check runs as a scheduled task in YOUR session instead, and starts the same file the
    shortcut starts. The .lnk itself cannot be used: it opens the model chooser. An automatic
    start has nobody to answer it, so it is started with /newest - model 1, the tuned 94k config
    the service used to serve - and /window, so you get a console you can see and close.

    THE STARTING FLAG

    The server binds 8080 only after the model is in VRAM: 40 s warm, minutes cold. In that
    window "nothing is listening" is also true of a Strata that is 20 s into a load, so a check
    that started on that answer would put two 15 GB models on one 16 GB card. Strata's two
    automated starters therefore share a flag file (C:\ProgramData\Strata\strata.starting):

        a fresh flag      a Strata is loading: wait for the port, do not start a second one
        no flag           nothing is starting: take the flag, start, hold it until the port
                          answers (or five minutes pass), then release it
        a stale flag      a crash left it behind (older than five minutes): ignored and cleared

    The sentiment worker starts the Strata *service* the same way and honours the same flag
    (sentiment-worker/strata.py: ensure()), so the two never compete.

        port 8080 answering     nothing happens (no second model, no fight over the port)
        a fresh flag            waits up to five minutes for the port, then does nothing
        nothing else            the batch is started with /newest /noopen /window
        what it did             C:\Strata-scratch\ensure-running.log

    Registered as the task "Strata_EnsureRunning": at logon, and every 15 minutes while this
    user is logged on. RDP disconnect keeps the session, so the 15-minute check keeps running;
    after a reboot it comes back at your next logon.
#>

$port = 8080
$batch = 'D:\Strata\Scripts\Strata_Start.bat'
$log = 'C:\Strata-scratch\ensure-running.log'
$flagTool = 'D:\Strata\Scripts\starting_flag.py'
$flag = 'C:\ProgramData\Strata\strata.starting'
$python = 'D:\Strata\.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python.exe' }
$staleSeconds = 300
$waitSeconds = 300
$pollSeconds = 15

function Write-Log([string]$text) {
    try { New-Item -ItemType Directory -Force -Path (Split-Path $log) -ErrorAction Stop | Out-Null } catch { }
    try {
        "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $text |
            Add-Content -LiteralPath $log -ErrorAction Stop
    } catch { }
}

function Test-Listening {
    return (@(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue).Count -gt 0)
}

function Test-FlagFresh {
    if (-not (Test-Path $flag)) { return $false }
    try {
        return (((Get-Date) - (Get-Item -LiteralPath $flag).LastWriteTime).TotalSeconds -lt $staleSeconds)
    } catch { return $false }
}

if (Test-Listening) { exit 0 }

# A Strata that is loading holds the flag and does not own the port yet.
if (Test-FlagFresh) {
    Write-Log "a Strata is starting (flag is fresh); waiting for port $port"
    $deadline = (Get-Date).AddSeconds($waitSeconds)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds $pollSeconds
        if (Test-Listening) { Write-Log "it answered on $port; nothing to do"; exit 0 }
        if (-not (Test-FlagFresh)) { break }
    }
    if (Test-Listening) { exit 0 }
}

# Take the flag before starting anything. O_EXCL means exactly one starter can hold it.
& $python $flagTool acquire --owner "ensure-running pid $PID" | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Log "another starter holds the flag; nothing to do"
    exit 0
}

try {
    Write-Log "nothing answering on $port; starting Strata with /newest"
    # /window hands the server to a console of its own and returns here, so this task finishes
    # instead of being the server's parent for the rest of the day.
    Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', "`"$batch`" /newest /noopen /window" -WindowStyle Hidden

    # Hold the flag until the port answers, so a second check during the load waits rather
    # than starting a second model.
    $deadline = (Get-Date).AddSeconds($waitSeconds)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds $pollSeconds
        if (Test-Listening) { Write-Log "strata answered on $port"; break }
    }
    if (-not (Test-Listening)) {
        Write-Log "port $port did not answer within $waitSeconds s; releasing the flag"
    }
}
finally {
    & $python $flagTool release | Out-Null
}

exit 0
