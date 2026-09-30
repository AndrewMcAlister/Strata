@echo off
rem ==============================================================================================
rem  Strata_Stop.bat - stop Strata and leave nothing behind.
rem
rem    Strata_Stop.bat              stop the server and the model it loaded
rem    Strata_Stop.bat /status      say what is running (server, port, model, RAM) and change nothing
rem    Strata_Stop.bat /force       do not wait for the model: kill what is left at once
rem    Strata_Stop.bat /timeout N   wait up to N seconds for the model (default 60)
rem    Strata_Stop.bat /quiet       skip the header lines
rem    Strata_Stop.bat /?           this text
rem
rem  How a 47 GB model is stopped without corrupting anything: the server is killed first (it keeps no
rem  state in memory), which closes the stdin pipe of the engine.  The engine reads stdin on its own
rem  thread, so it sees the end of input, leaves its serve loop and exits by itself, releasing the
rem  pinned memory.  This is not the server's own shutdown path: serve\server.py writes QUIT to the
rem  engine only when it exits gracefully, and a kill does not do that, so a request in flight is cut
rem  short rather than finished.  The server still goes first, because killing the model first would
rem  break the same request and leave a server with nothing behind it.  The engine is waited for, and
rem  only killed if it is still there after the timeout, which is said out loud.
rem
rem  This file may sit anywhere in the Strata folder: next to START-HERE.bat, in Scripts\ or in
rem  .venv\Scripts\.  It finds the Strata folder by walking up from its own location until it sees
rem  serve\server.py, so it does not care where it was put.
rem
rem  Exit codes: 0 stopped (or nothing was running), 1 something is still running, 2 bad option,
rem              3 Strata was not found above this file.  /status answers with 0 when it reported
rem              what is running and 1 when nothing is, so it can be used as a test in a script.
rem ==============================================================================================

setlocal EnableExtensions
title Strata - stop

rem shift moves %0 as well as %1, which would break %~dp0 and %~f0, so take them before the loop
set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"

rem --- arguments -------------------------------------------------------------------------------
set "MODE=stop"
set "GRACE=60"
set "QUIET="
:parse
if "%~1"=="" goto parsed
if /i "%~1"=="/?"            (set "MODE=help" & shift & goto parse)
if /i "%~1"=="/h"            (set "MODE=help" & shift & goto parse)
if /i "%~1"=="/help"         (set "MODE=help" & shift & goto parse)
if /i "%~1"=="/status"       (set "MODE=status" & shift & goto parse)
if /i "%~1"=="/force"        (set "MODE=force" & shift & goto parse)
if /i "%~1"=="/quiet"        (set "QUIET=1" & shift & goto parse)
if /i "%~1"=="/timeout"      (set "GRACE=%~2" & shift & shift & goto parse)
echo  Unknown option: %~1
echo  Run Strata_Stop.bat /? for what this script understands.
exit /b 2
:parsed

rem a bad or missing number after /timeout falls back to the default
echo %GRACE%|findstr /r /c:"^[0-9][0-9]*$" >nul
if errorlevel 1 set "GRACE=60"
if "%MODE%"=="help" goto help

rem --- where is Strata? -------------------------------------------------------------------------
set "ROOT="
if exist "%HERE%\serve\server.py" set "ROOT=%HERE%"
if not defined ROOT for %%D in ("%HERE%\..") do if exist "%%~fD\serve\server.py" set "ROOT=%%~fD"
if not defined ROOT for %%D in ("%HERE%\..\..") do if exist "%%~fD\serve\server.py" set "ROOT=%%~fD"
if not defined ROOT goto noroot
if not defined QUIET echo  Strata folder: %ROOT%

rem --- the part that needs to look at other processes lives in PowerShell -----------------------
rem cmd cannot list processes by path, so the probe below is written to a temporary .ps1 file next
rem to the batch and run with -File.  Writing it as a file, instead of passing it on the command
rem line, keeps the quoting sane: cmd cannot continue a caret inside a quoted argument, so a
rem multi-line -Command string is not possible.  Endless input would also break it.
rem PowerShell is looked for before the file is made, so a machine without it leaves nothing
rem behind in %TEMP%.
where powershell >nul 2>nul
if errorlevel 1 goto nops
set "PSFILE=%TEMP%\strata-stop-%RANDOM%.ps1"

>"%PSFILE%" echo param([string]$Root = '', [string]$Mode = 'stop', [int]$Grace = 60, [string]$Quiet = '')
>>"%PSFILE%" echo $ErrorActionPreference = 'SilentlyContinue'
>>"%PSFILE%" echo $ct = [System.StringComparison]::OrdinalIgnoreCase
>>"%PSFILE%" echo $venvdir = $Root + '\.venv\Scripts'
>>"%PSFILE%" echo $engdir = $Root + '\engine'
>>"%PSFILE%" echo function UnderDir($path, $dir) {
>>"%PSFILE%" echo     if ($path -eq '') { return $false }
>>"%PSFILE%" echo     return $path.StartsWith($dir, $ct)
>>"%PSFILE%" echo }
>>"%PSFILE%" echo function Mentions($text, $needle) {
>>"%PSFILE%" echo     if ($text -eq '') { return $false }
>>"%PSFILE%" echo     return $text.IndexOf($needle, $ct) -ge 0
>>"%PSFILE%" echo }
rem A model config and the Chat settings saved next to it are both "strata-*.json": Windows
rem matches a dot with *, so the sidecar server.py writes (strata-TAG.shared-settings.json)
rem would win the "newest" test below.  A real config carries the engine path and arguments.
>>"%PSFILE%" echo function IsConfig($path) {
>>"%PSFILE%" echo     if ($path -eq '') { return $false }
>>"%PSFILE%" echo     if ($path -like '*.shared-settings.json') { return $false }
>>"%PSFILE%" echo     $j = $null
>>"%PSFILE%" echo     try { $j = ConvertFrom-Json (Get-Content -Raw -LiteralPath $path) } catch { return $false }
>>"%PSFILE%" echo     if ($j -eq $null) { return $false }
>>"%PSFILE%" echo     if (-not $j.exe) { return $false }
>>"%PSFILE%" echo     if (-not $j.args) { return $false }
>>"%PSFILE%" echo     return $true
>>"%PSFILE%" echo }
>>"%PSFILE%" echo function RoundGB($bytes) {
>>"%PSFILE%" echo     return [string][math]::Round($bytes / 1GB, 1) + ' GB'
>>"%PSFILE%" echo }
>>"%PSFILE%" echo function KillPid($n) {
>>"%PSFILE%" echo     try {
>>"%PSFILE%" echo         $pr = Get-Process -Id $n -ErrorAction SilentlyContinue
>>"%PSFILE%" echo         if ($pr -ne $null) { $pr.Kill(); return $true }
>>"%PSFILE%" echo     } catch { }
>>"%PSFILE%" echo     return $false
>>"%PSFILE%" echo }
>>"%PSFILE%" echo function AliveMb($ids) {
>>"%PSFILE%" echo     $mb = 0
>>"%PSFILE%" echo     foreach ($i in $ids) {
>>"%PSFILE%" echo         $pr = Get-Process -Id $i -ErrorAction SilentlyContinue
>>"%PSFILE%" echo         if ($pr -ne $null) { $mb = $mb + $pr.WorkingSet64 }
>>"%PSFILE%" echo     }
>>"%PSFILE%" echo     return $mb
>>"%PSFILE%" echo }
>>"%PSFILE%" echo function AliveCount($ids) {
>>"%PSFILE%" echo     $n = 0
>>"%PSFILE%" echo     foreach ($i in $ids) {
>>"%PSFILE%" echo         $pr = Get-Process -Id $i -ErrorAction SilentlyContinue
>>"%PSFILE%" echo         if ($pr -ne $null) { $n = $n + 1 }
>>"%PSFILE%" echo     }
>>"%PSFILE%" echo     return $n
>>"%PSFILE%" echo }
>>"%PSFILE%" echo $all = @(Get-CimInstance Win32_Process)
>>"%PSFILE%" echo $servers = New-Object System.Collections.ArrayList
>>"%PSFILE%" echo $engines = New-Object System.Collections.ArrayList
>>"%PSFILE%" echo $seen = @{}
>>"%PSFILE%" echo foreach ($p in $all) {
>>"%PSFILE%" echo     $path = ''
>>"%PSFILE%" echo     if ($p.ExecutablePath) { $path = $p.ExecutablePath }
>>"%PSFILE%" echo     $cmd = ''
>>"%PSFILE%" echo     if ($p.CommandLine) { $cmd = $p.CommandLine }
>>"%PSFILE%" echo     $key = [string]$p.ProcessId
>>"%PSFILE%" echo     $isServer = $false
>>"%PSFILE%" echo     if ((UnderDir $path ($venvdir + '\')) -and (Mentions $cmd 'serve\server.py')) { $isServer = $true }
>>"%PSFILE%" echo     if (Mentions $cmd ($Root + '\serve\server.py')) { $isServer = $true }
>>"%PSFILE%" echo     if ($isServer -and -not $seen.ContainsKey($key)) {
>>"%PSFILE%" echo         $seen[$key] = 1
>>"%PSFILE%" echo         [void]$servers.Add($p)
>>"%PSFILE%" echo     }
>>"%PSFILE%" echo     if ((UnderDir $path ($engdir + '\')) -and -not $seen.ContainsKey($key)) {
>>"%PSFILE%" echo         $seen[$key] = 1
>>"%PSFILE%" echo         [void]$engines.Add($p)
>>"%PSFILE%" echo     }
>>"%PSFILE%" echo }
>>"%PSFILE%" echo $children = New-Object System.Collections.ArrayList
>>"%PSFILE%" echo $kidseen = @{}
>>"%PSFILE%" echo $byparent = @{}
>>"%PSFILE%" echo foreach ($p in $all) {
>>"%PSFILE%" echo     $k = [string]$p.ParentProcessId
>>"%PSFILE%" echo     if (-not $byparent.ContainsKey($k)) { $byparent[$k] = New-Object System.Collections.ArrayList }
>>"%PSFILE%" echo     [void]$byparent[$k].Add($p)
>>"%PSFILE%" echo }
>>"%PSFILE%" echo foreach ($s in $servers) {
>>"%PSFILE%" echo     $stack = New-Object System.Collections.Stack
>>"%PSFILE%" echo     $stack.Push([string]$s.ProcessId)
>>"%PSFILE%" echo     $steps = 0
>>"%PSFILE%" echo     while (($stack.Count -gt 0) -and ($steps -lt 500)) {
>>"%PSFILE%" echo         $steps = $steps + 1
>>"%PSFILE%" echo         $cur = [string]$stack.Pop()
>>"%PSFILE%" echo         if ($byparent.ContainsKey($cur)) {
>>"%PSFILE%" echo             foreach ($c in $byparent[$cur]) {
>>"%PSFILE%" echo                 $ck = [string]$c.ProcessId
>>"%PSFILE%" echo                 if (-not $kidseen.ContainsKey($ck)) {
>>"%PSFILE%" echo                     $kidseen[$ck] = 1
>>"%PSFILE%" echo                     [void]$children.Add($c)
>>"%PSFILE%" echo                     $stack.Push($ck)
>>"%PSFILE%" echo                 }
>>"%PSFILE%" echo             }
>>"%PSFILE%" echo         }
>>"%PSFILE%" echo     }
>>"%PSFILE%" echo }
>>"%PSFILE%" echo function JsonOf($line) {
>>"%PSFILE%" echo     if ($line -eq '') { return '' }
>>"%PSFILE%" echo     $m = [regex]::Matches($line, '\-\-config\D+?(\S+\.json)')
>>"%PSFILE%" echo     if ($m.Count -eq 0) { $m = [regex]::Matches($line, '\-\-config\D+?([A-Za-z]:.+?\.json)') }
>>"%PSFILE%" echo     if ($m.Count -eq 0) { return '' }
>>"%PSFILE%" echo     return $m[$m.Count - 1].Groups[1].Value.Trim('"')
>>"%PSFILE%" echo }
>>"%PSFILE%" echo function PortOf($line) {
>>"%PSFILE%" echo     if ($line -eq '') { return 0 }
>>"%PSFILE%" echo     $m = [regex]::Matches($line, '\-\-port\D+?(\d+)')
>>"%PSFILE%" echo     if ($m.Count -eq 0) { return 0 }
>>"%PSFILE%" echo     return [int]$m[$m.Count - 1].Groups[1].Value
>>"%PSFILE%" echo }
>>"%PSFILE%" echo $port = 0
>>"%PSFILE%" echo $cfg = ''
>>"%PSFILE%" echo if ($servers.Count -gt 0) {
>>"%PSFILE%" echo     $cmd0 = ''
>>"%PSFILE%" echo     if ($servers[0].CommandLine) { $cmd0 = $servers[0].CommandLine }
>>"%PSFILE%" echo     $port = PortOf $cmd0
>>"%PSFILE%" echo     $cfg = JsonOf $cmd0
>>"%PSFILE%" echo }
>>"%PSFILE%" echo if ($cfg -eq '') {
>>"%PSFILE%" echo     $newest = $null
>>"%PSFILE%" echo     foreach ($f in @(Get-ChildItem -LiteralPath $Root -Filter 'strata-*.json' -ErrorAction SilentlyContinue)) {
>>"%PSFILE%" echo         if (-not (IsConfig $f.FullName)) { continue }
>>"%PSFILE%" echo         if (($newest -eq $null) -or ($f.LastWriteTime -gt $newest.LastWriteTime)) { $newest = $f }
>>"%PSFILE%" echo     }
>>"%PSFILE%" echo     if ($newest -ne $null) { $cfg = $newest.FullName }
>>"%PSFILE%" echo }
>>"%PSFILE%" echo if (($port -eq 0) -and ($cfg -ne '')) {
>>"%PSFILE%" echo     $j = $null
>>"%PSFILE%" echo     try { $j = ConvertFrom-Json (Get-Content -Raw -LiteralPath $cfg) } catch { $j = $null }
>>"%PSFILE%" echo     if (($j -ne $null) -and $j.port) { $port = [int]$j.port }
>>"%PSFILE%" echo }
>>"%PSFILE%" echo if ($port -eq 0) { $port = 8080 }
>>"%PSFILE%" echo $running = $servers.Count -gt 0
>>"%PSFILE%" echo $alive = ($servers.Count + $engines.Count) -gt 0
>>"%PSFILE%" echo if ($Mode -eq 'status') {
>>"%PSFILE%" echo     if (-not $alive) {
>>"%PSFILE%" echo         Write-Host 'Strata is not running.'
>>"%PSFILE%" echo         exit 1
>>"%PSFILE%" echo     }
>>"%PSFILE%" echo     $health = 'not checked'
>>"%PSFILE%" echo     if ($running) {
>>"%PSFILE%" echo         try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 4 -Uri ('http://127.0.0.1:' + $port + '/health'); $health = 'HTTP ' + [int]$r.StatusCode } catch { $health = 'no answer' }
>>"%PSFILE%" echo     }
>>"%PSFILE%" echo     Write-Host ''
>>"%PSFILE%" echo     Write-Host ('Strata in ' + $Root)
>>"%PSFILE%" echo     foreach ($s in $servers) { Write-Host ('  server   pid ' + $s.ProcessId + '   started ' + $s.CreationDate + '   ' + (RoundGB $s.WorkingSetSize)) }
>>"%PSFILE%" echo     foreach ($e in $engines) { Write-Host ('  model    pid ' + $e.ProcessId + '   ' + [System.IO.Path]::GetFileName($e.ExecutablePath) + '   ' + (RoundGB $e.WorkingSetSize)) }
>>"%PSFILE%" echo     $others = 0
>>"%PSFILE%" echo     $othermb = 0
>>"%PSFILE%" echo     foreach ($c in $children) {
>>"%PSFILE%" echo         $known = $false
>>"%PSFILE%" echo         foreach ($e in $engines) { if ($e.ProcessId -eq $c.ProcessId) { $known = $true } }
>>"%PSFILE%" echo         if (-not $known) { $others = $others + 1; $othermb = $othermb + $c.WorkingSetSize }
>>"%PSFILE%" echo     }
>>"%PSFILE%" echo     if ($others -gt 0) { Write-Host ('  helpers  ' + $others + ' process(es)   ' + (RoundGB $othermb) + '   (image encoder, MCP tool servers)') }
>>"%PSFILE%" echo     if ($running) {
>>"%PSFILE%" echo         Write-Host ('  address  http://127.0.0.1:' + $port + '/   health: ' + $health)
>>"%PSFILE%" echo         Write-Host '  stopping it is one command: Strata_Stop.bat'
>>"%PSFILE%" echo     } else {
>>"%PSFILE%" echo         Write-Host '  no server is running: this model was left behind by a server that died, Strata_Stop.bat removes it'
>>"%PSFILE%" echo     }
>>"%PSFILE%" echo     Write-Host ''
>>"%PSFILE%" echo     exit 0
>>"%PSFILE%" echo }
>>"%PSFILE%" echo if (-not $alive) {
>>"%PSFILE%" echo     Write-Host 'Strata is not running - nothing to stop.'
>>"%PSFILE%" echo     exit 0
>>"%PSFILE%" echo }
>>"%PSFILE%" echo $freed = 0
>>"%PSFILE%" echo $waitids = New-Object System.Collections.ArrayList
>>"%PSFILE%" echo foreach ($e in $engines) { $freed = $freed + $e.WorkingSetSize; [void]$waitids.Add([int]$e.ProcessId) }
>>"%PSFILE%" echo foreach ($c in $children) {
>>"%PSFILE%" echo     $known = $false
>>"%PSFILE%" echo     foreach ($e in $engines) { if ($e.ProcessId -eq $c.ProcessId) { $known = $true } }
>>"%PSFILE%" echo     if (-not $known) { [void]$waitids.Add([int]$c.ProcessId) }
>>"%PSFILE%" echo }
>>"%PSFILE%" echo if ($Quiet -eq '') {
>>"%PSFILE%" echo     Write-Host ''
>>"%PSFILE%" echo     Write-Host ('Stopping Strata in ' + $Root)
>>"%PSFILE%" echo     foreach ($s in $servers) { Write-Host ('  server   pid ' + $s.ProcessId + '   ' + [System.IO.Path]::GetFileName($s.ExecutablePath) + '   ' + (RoundGB $s.WorkingSetSize)) }
>>"%PSFILE%" echo     foreach ($e in $engines) { Write-Host ('  model    pid ' + $e.ProcessId + '   ' + [System.IO.Path]::GetFileName($e.ExecutablePath) + '   ' + (RoundGB $e.WorkingSetSize)) }
>>"%PSFILE%" echo }
>>"%PSFILE%" echo foreach ($s in $servers) { $ok = KillPid $s.ProcessId }
>>"%PSFILE%" echo if ($servers.Count -gt 0) {
>>"%PSFILE%" echo     Write-Host '  the server is gone: the model now sees the end of its input and exits on its own'
>>"%PSFILE%" echo }
>>"%PSFILE%" echo $left = AliveCount $waitids
>>"%PSFILE%" echo if ($left -gt 0) {
>>"%PSFILE%" echo     if ($Mode -eq 'force') {
>>"%PSFILE%" echo         Write-Host '  /force: not waiting for the model'
>>"%PSFILE%" echo     } else {
>>"%PSFILE%" echo         Write-Host ('  waiting up to ' + $Grace + 's for the model to finish what it was writing')
>>"%PSFILE%" echo     }
>>"%PSFILE%" echo }
>>"%PSFILE%" echo $t0 = Get-Date
>>"%PSFILE%" echo $tick = 0
>>"%PSFILE%" echo while ($left -gt 0) {
>>"%PSFILE%" echo     if ($Mode -eq 'force') { break }
>>"%PSFILE%" echo     $elapsed = [int]((Get-Date) - $t0).TotalSeconds
>>"%PSFILE%" echo     if ($elapsed -ge $Grace) { break }
>>"%PSFILE%" echo     if (($tick -eq 5) -and ($Quiet -eq '')) {
>>"%PSFILE%" echo         $tick = 0
>>"%PSFILE%" echo         Write-Host ('    still shutting down: ' + $left + ' process(es), ' + (RoundGB (AliveMb $waitids)) + ' held, ' + $elapsed + 's of ' + $Grace + 's')
>>"%PSFILE%" echo     }
>>"%PSFILE%" echo     $tick = $tick + 1
>>"%PSFILE%" echo     Start-Sleep -Seconds 1
>>"%PSFILE%" echo     $left = AliveCount $waitids
>>"%PSFILE%" echo }
>>"%PSFILE%" echo $killed = 0
>>"%PSFILE%" echo if ($left -gt 0) {
>>"%PSFILE%" echo     Write-Host '  what is left did not exit by itself, killing it:'
>>"%PSFILE%" echo     foreach ($i in $waitids) {
>>"%PSFILE%" echo         $pr = Get-Process -Id $i -ErrorAction SilentlyContinue
>>"%PSFILE%" echo         if ($pr -ne $null) {
>>"%PSFILE%" echo             Write-Host ('    pid ' + $i + '  ' + $pr.ProcessName + '  ' + (RoundGB $pr.WorkingSet64))
>>"%PSFILE%" echo             $ok = KillPid $i
>>"%PSFILE%" echo             $killed = $killed + 1
>>"%PSFILE%" echo         }
>>"%PSFILE%" echo     }
>>"%PSFILE%" echo     Start-Sleep -Milliseconds 800
>>"%PSFILE%" echo }
>>"%PSFILE%" echo $still = 0
>>"%PSFILE%" echo foreach ($e in $waitids) { $still = $still + (AliveCount $e) }
>>"%PSFILE%" echo foreach ($s in $servers) { $still = $still + (AliveCount $s.ProcessId) }
>>"%PSFILE%" echo $secs = [int]((Get-Date) - $t0).TotalSeconds
>>"%PSFILE%" echo if ($still -eq 0) {
>>"%PSFILE%" echo     if ($killed -gt 0) {
>>"%PSFILE%" echo         Write-Host ('Strata stopped: ' + $killed + ' process(es) had to be killed, about ' + (RoundGB $freed) + ' of memory released.')
>>"%PSFILE%" echo     } elseif ($engines.Count -gt 0) {
>>"%PSFILE%" echo         Write-Host ('Strata stopped: the model exited on its own after ' + $secs + 's, about ' + (RoundGB $freed) + ' of memory released.')
>>"%PSFILE%" echo     } elseif ($servers.Count -gt 0) {
>>"%PSFILE%" echo         Write-Host ('Strata stopped: the server stopped after ' + $secs + 's.')
>>"%PSFILE%" echo     } else {
>>"%PSFILE%" echo         Write-Host 'Strata stopped.'
>>"%PSFILE%" echo     }
>>"%PSFILE%" echo     exit 0
>>"%PSFILE%" echo }
>>"%PSFILE%" echo Write-Host ''
>>"%PSFILE%" echo Write-Host ('Something is still running after the stop: ' + $still + ' process(es).')
>>"%PSFILE%" echo foreach ($e in $waitids) {
>>"%PSFILE%" echo     $pr = Get-Process -Id $e -ErrorAction SilentlyContinue
>>"%PSFILE%" echo     if ($pr -ne $null) { Write-Host ('  pid ' + $e + '  ' + $pr.ProcessName) }
>>"%PSFILE%" echo }
>>"%PSFILE%" echo Write-Host 'Close its window, or end it in Task Manager, then run this script again.'
>>"%PSFILE%" echo exit 1

rem --- run it -----------------------------------------------------------------------------------
powershell -NoProfile -ExecutionPolicy Bypass -File "%PSFILE%" -Root "%ROOT%" -Mode "%MODE%" -Grace %GRACE% -Quiet "%QUIET%"
set "CODE=%ERRORLEVEL%"
del "%PSFILE%" >nul 2>nul
exit /b %CODE%

rem --- the script has to say what it does when it cannot do it -----------------------------------
:help
echo.
echo  Strata_Stop.bat - stop Strata and leave nothing behind
echo.
echo    Strata_Stop.bat              stop the server and the model it loaded
echo    Strata_Stop.bat /status      say what is running (server, port, model, RAM) and change nothing
echo    Strata_Stop.bat /force       do not wait for the model: kill what is left at once
echo    Strata_Stop.bat /timeout N   wait up to N seconds for the model (default 60)
echo    Strata_Stop.bat /quiet       skip the header lines
echo    Strata_Stop.bat /?           this text
echo.
echo  The server is killed first, which closes the stdin pipe of the engine: the model finishes the
echo  reply it was writing, exits by itself and releases its pinned memory.  Killing the model first,
echo  or pressing Ctrl+C in the server window, would cut it off mid-request instead.
echo.
exit /b 0

:noroot
echo.
echo  Strata was not found above this file.
echo  Put Strata_Stop.bat in the Strata folder, in its Scripts folder or in its .venv\Scripts folder,
echo  then run it again.  It looks for serve\server.py to know which folder is the Strata folder.
echo.
exit /b 3

:nops
echo.
echo  PowerShell was not found.  Windows 10 and 11 always have it; on an older Windows, install it
echo  from https://aka.ms/powershell - this script needs it to look at the running processes.
echo.
exit /b 3
