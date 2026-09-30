@echo off
rem ==============================================================================================
rem  Strata_Start.bat - start Strata (the server and the model) from any shortcut.
rem
rem    Strata_Start.bat             start with the newest model config in the Strata folder
rem    Strata_Start.bat /window     start it in its own window and return at once
rem    Strata_Start.bat /noopen     do not open the browser
rem    Strata_Start.bat /restart    stop what is running first, then start it
rem    Strata_Start.bat /status     say what is running right now, and change nothing
rem    Strata_Start.bat /?          this text
rem    Strata_Start.bat --gpu 1     anything else is passed to serve\server.py
rem
rem  For a shortcut on the Desktop: right-click this file, Send to, Desktop (create shortcut).
rem  A shortcut works from anywhere; a copy of this file does not, because it finds the Strata
rem  folder by walking up from its own location until it sees serve\server.py.  That walk also
rem  makes the file work from the Strata folder, from its Scripts folder and from its
rem  .venv\Scripts folder, so all of them can hold a copy.
rem
rem  Starting twice is caught: if this folder already has a running server, this script says so
rem  (with the address to open) instead of failing later on a busy port.  /restart stops it first.
rem  The same goes for a model whose server died: it still holds its memory, so it is reported
rem  rather than loaded over.  With nothing installed yet it hands over to START-HERE.bat.
rem
rem  With no model installed this refuses to start rather than starting serve\server.py without
rem  --engine, whose default there is the mock engine - that would answer like a loaded model.
rem  --engine mock still starts it, deliberately.
rem
rem  Exit codes: 0 started (or already running), 2 bad option, 3 Strata could not be started
rem               (not found above this file, no PowerShell, the process probe did not answer,
rem               or no model is installed), 4 could not stop the old server for /restart, or
rem               the exit code of the server itself when it ends.
rem ==============================================================================================

setlocal EnableExtensions EnableDelayedExpansion
title Strata

rem shift moves %0 as well as %1, which would break %~dp0 and %~f0, so take them before the loop
set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"
set "SELF=%~f0"

rem --- arguments: our own switches first, the rest goes to serve\server.py -----------------------
set "WINDOW="
set "FOREGROUND="
set "NOOPEN="
set "OPEN=--open"
set "RESTART="
set "STATUS="
set "HELP="
set "EXTRA="
:parse
if "%~1"=="" goto parsed
if /i "%~1"=="/?"       (set "HELP=1" & shift & goto parse)
if /i "%~1"=="/h"       (set "HELP=1" & shift & goto parse)
if /i "%~1"=="/help"    (set "HELP=1" & shift & goto parse)
if /i "%~1"=="/window"  (set "WINDOW=1" & shift & goto parse)
if /i "%~1"=="/fg"      (set "FOREGROUND=1" & shift & goto parse)
if /i "%~1"=="/restart" (set "RESTART=1" & shift & goto parse)
if /i "%~1"=="/status"  (set "STATUS=1" & shift & goto parse)
if /i "%~1"=="/noopen"  (set "NOOPEN=1" & set "OPEN=" & shift & goto parse)
set "EXTRA=!EXTRA! "%~1""
shift
goto parse
:parsed
if defined HELP goto help

rem --- where is Strata? -------------------------------------------------------------------------
set "ROOT="
if exist "%HERE%\serve\server.py" set "ROOT=%HERE%"
if not defined ROOT for %%D in ("%HERE%\..") do if exist "%%~fD\serve\server.py" set "ROOT=%%~fD"
if not defined ROOT for %%D in ("%HERE%\..\..") do if exist "%%~fD\serve\server.py" set "ROOT=%%~fD"
if not defined ROOT goto noroot

rem --- /status is the stop script's job, so there is only one place that answers that ------------
if defined STATUS goto status

rem --- without an environment there is nothing to start: hand over to the first-run script ------
set "PY=%ROOT%\.venv\Scripts\python.exe"
if not exist "%PY%" goto setup

rem --- what is running, and which model would be started? ---------------------------------------
rem The probe is written to a temporary .ps1 file and run with -File: cmd cannot continue a caret
rem inside a quoted argument, so a multi-line -Command string is not possible, and cmd cannot list
rem processes by path at all.  It answers with KEY=VALUE lines, which are read back with for /f.
rem The extra arguments are handed over in a second temporary file, because their quotes would
rem not survive the command line.
rem PowerShell is looked for before the two files are made, so a machine without it leaves
rem nothing behind in %TEMP%.
where powershell >nul 2>nul
if errorlevel 1 goto nopowershell
set "PSFILE=%TEMP%\strata-start-%RANDOM%.ps1"
set "EXFILE=%TEMP%\strata-args-%RANDOM%.txt"
rem an empty %EXTRA% would make this "echo" alone, which writes the words "ECHO is on."
if defined EXTRA (>"%EXFILE%" echo %EXTRA%) else (type nul >"%EXFILE%")

>"%PSFILE%" echo param([string]$Root = '', [string]$Extras = '')
>>"%PSFILE%" echo $ErrorActionPreference = 'SilentlyContinue'
>>"%PSFILE%" echo $ct = [System.StringComparison]::OrdinalIgnoreCase
>>"%PSFILE%" echo $venvdir = $Root + '\.venv\Scripts'
>>"%PSFILE%" echo $engdir = $Root + '\engine'
>>"%PSFILE%" echo $extra = ''
>>"%PSFILE%" echo if (Test-Path -LiteralPath $Extras) { $extra = [string](Get-Content -LiteralPath $Extras -TotalCount 1) }
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
rem would win the "newest" test below and be started as if it were a model.  A real config is
rem the one that carries the engine path and its arguments.
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
>>"%PSFILE%" echo $all = @(Get-CimInstance Win32_Process)
>>"%PSFILE%" echo $server = $null
>>"%PSFILE%" echo $engines = New-Object System.Collections.ArrayList
>>"%PSFILE%" echo foreach ($p in $all) {
>>"%PSFILE%" echo     $path = ''
>>"%PSFILE%" echo     if ($p.ExecutablePath) { $path = $p.ExecutablePath }
>>"%PSFILE%" echo     $cmd = ''
>>"%PSFILE%" echo     if ($p.CommandLine) { $cmd = $p.CommandLine }
>>"%PSFILE%" echo     $isServer = $false
>>"%PSFILE%" echo     if ((UnderDir $path ($venvdir + '\')) -and (Mentions $cmd 'serve\server.py')) { $isServer = $true }
>>"%PSFILE%" echo     if (Mentions $cmd ($Root + '\serve\server.py')) { $isServer = $true }
>>"%PSFILE%" echo     if ($isServer -and ($server -eq $null)) { $server = $p }
>>"%PSFILE%" echo     if (UnderDir $path ($engdir + '\')) { [void]$engines.Add($p) }
>>"%PSFILE%" echo }
rem The engine of a running server is normal, so an engine only counts as left behind when no
rem running server is above it.  That one is different from a second server: it holds the pack
rem and the pinned memory, and a new engine started beside it cannot load.
>>"%PSFILE%" echo $orphan = $null
>>"%PSFILE%" echo if ($engines.Count -gt 0) {
>>"%PSFILE%" echo     $kin = @{}
>>"%PSFILE%" echo     if ($server -ne $null) {
>>"%PSFILE%" echo         $byparent = @{}
>>"%PSFILE%" echo         foreach ($p in $all) {
>>"%PSFILE%" echo             $k = [string]$p.ParentProcessId
>>"%PSFILE%" echo             if (-not $byparent.ContainsKey($k)) { $byparent[$k] = New-Object System.Collections.ArrayList }
>>"%PSFILE%" echo             [void]$byparent[$k].Add($p)
>>"%PSFILE%" echo         }
>>"%PSFILE%" echo         $stack = New-Object System.Collections.Stack
>>"%PSFILE%" echo         $stack.Push([string]$server.ProcessId)
>>"%PSFILE%" echo         while ($stack.Count -gt 0) {
>>"%PSFILE%" echo             $cur = $stack.Pop()
>>"%PSFILE%" echo             if ($byparent.ContainsKey($cur)) {
>>"%PSFILE%" echo                 foreach ($c in $byparent[$cur]) {
>>"%PSFILE%" echo                     $ck = [string]$c.ProcessId
>>"%PSFILE%" echo                     if (-not $kin.ContainsKey($ck)) { $kin[$ck] = 1; $stack.Push($ck) }
>>"%PSFILE%" echo                 }
>>"%PSFILE%" echo             }
>>"%PSFILE%" echo         }
>>"%PSFILE%" echo     }
>>"%PSFILE%" echo     foreach ($e in $engines) {
>>"%PSFILE%" echo         if (-not $kin.ContainsKey([string]$e.ProcessId)) { $orphan = $e; break }
>>"%PSFILE%" echo     }
>>"%PSFILE%" echo }
>>"%PSFILE%" echo $cmdline = ''
>>"%PSFILE%" echo if ($server -ne $null) { if ($server.CommandLine) { $cmdline = $server.CommandLine } }
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
>>"%PSFILE%" echo $cfg = JsonOf $extra
>>"%PSFILE%" echo if ($cfg -eq '') { $cfg = JsonOf $cmdline }
>>"%PSFILE%" echo if ($cfg -eq '') {
>>"%PSFILE%" echo     $newest = $null
>>"%PSFILE%" echo     foreach ($f in @(Get-ChildItem -LiteralPath $Root -Filter 'strata-*.json' -ErrorAction SilentlyContinue)) {
>>"%PSFILE%" echo         if (-not (IsConfig $f.FullName)) { continue }
>>"%PSFILE%" echo         if (($newest -eq $null) -or ($f.LastWriteTime -gt $newest.LastWriteTime)) { $newest = $f }
>>"%PSFILE%" echo     }
>>"%PSFILE%" echo     if ($newest -ne $null) { $cfg = $newest.FullName }
>>"%PSFILE%" echo }
>>"%PSFILE%" echo $model = ''
>>"%PSFILE%" echo $exe = ''
>>"%PSFILE%" echo $jsonport = 0
>>"%PSFILE%" echo if ($cfg -ne '') {
>>"%PSFILE%" echo     $j = $null
>>"%PSFILE%" echo     try { $j = ConvertFrom-Json (Get-Content -Raw -LiteralPath $cfg) } catch { $j = $null }
>>"%PSFILE%" echo     if ($j -ne $null) {
>>"%PSFILE%" echo         if ($j.model_name) { $model = [string]$j.model_name }
>>"%PSFILE%" echo         if ($j.exe) { $exe = [string]$j.exe }
>>"%PSFILE%" echo         if ($j.port) { $jsonport = [int]$j.port }
>>"%PSFILE%" echo     }
>>"%PSFILE%" echo }
rem SRVPORT is the port of the server already running, for the "already running" message: it is
rem that server's port, not the one this command line asked for.  PORT is the port a start here
rem would use - what was asked for first, then the running server's, then the config's, then
rem 8080.  8095 is the mock engine's default in serve\server.py and is not this product's.
>>"%PSFILE%" echo $srvport = PortOf $cmdline
>>"%PSFILE%" echo $wanted = PortOf $extra
>>"%PSFILE%" echo if ($wanted -gt 0) { $port = $wanted }
>>"%PSFILE%" echo elseif ($srvport -gt 0) { $port = $srvport }
>>"%PSFILE%" echo elseif ($jsonport -gt 0) { $port = $jsonport }
>>"%PSFILE%" echo else { $port = 8080 }
>>"%PSFILE%" echo if ($srvport -eq 0) { $srvport = $port }
>>"%PSFILE%" echo if ($model -eq '') { $model = 'strata' }
>>"%PSFILE%" echo if ($server -ne $null) { Write-Output ('RUNNING=' + $server.ProcessId) } else { Write-Output 'RUNNING=0' }
>>"%PSFILE%" echo if ($orphan -ne $null) { Write-Output ('ENGINE=' + $orphan.ProcessId) } else { Write-Output 'ENGINE=0' }
>>"%PSFILE%" echo Write-Output ('PORT=' + $port)
>>"%PSFILE%" echo Write-Output ('SRVPORT=' + $srvport)
>>"%PSFILE%" echo Write-Output ('MODEL=' + $model)
>>"%PSFILE%" echo Write-Output ('CFG=' + $cfg)
>>"%PSFILE%" echo Write-Output ('EXE=' + $exe)
>>"%PSFILE%" echo exit 0

set "RUNNING="
set "ENGINE="
set "PORT="
set "SRVPORT="
set "MODEL="
set "CFG="
set "EXE="
set "PSOUT=%TEMP%\strata-start-%RANDOM%.out"
powershell -NoProfile -ExecutionPolicy Bypass -File "%PSFILE%" -Root "%ROOT%" -Extras "%EXFILE%" >"%PSOUT%" 2>nul
set "PROBECODE=%ERRORLEVEL%"
if "%PROBECODE%"=="0" for /f "usebackq delims=" %%A in ("%PSOUT%") do set "%%A"
del "%PSFILE%" >nul 2>nul
del "%PSOUT%" >nul 2>nul
del "%EXFILE%" >nul 2>nul
if not defined RUNNING goto noprobe
if not defined ENGINE set "ENGINE=0"
if not defined PORT set "PORT=8080"
if not defined SRVPORT set "SRVPORT=%PORT%"
if not defined MODEL set "MODEL=strata"

rem --- starting twice would only fail later on a busy port, so deal with it here -----------------
rem An engine that outlived its server is the one case the server check cannot see, and it is the
rem one where a second start is worst: the pack and the pinned memory are still held.
if not "%ENGINE%"=="0" if not defined RESTART goto orphan
if not "%RUNNING%"=="0" if not defined RESTART goto already
if not "%RUNNING%"=="0" goto restart
if not "%ENGINE%"=="0" goto restart

rem --- the arguments for serve\server.py ---------------------------------------------------------
rem Normally this script names the engine and the newest config itself, which is what makes a
rem double-click on this file enough.  If --engine was given, nothing is added and the command
rem line is exactly what was asked for.
:launch
set "ENGINEARG="
if not "%CFG%"=="" set "ENGINEARG=--engine strata --config "%CFG%" --port %PORT%"
rem Whether the command line brought its own --engine.  The substitution has to be guarded:
rem with %EXTRA% empty, cmd does not treat "%EXTRA:--engine=%" as a substitution at all and
rem leaves the literal "--engine=" behind, so the comparison below would always say "asked for
rem one" - which is how an empty command line came to start serve\server.py's mock engine.
set "USERTEST=%EXTRA%"
if defined EXTRA set "USERTEST=%EXTRA:--engine=%"
if not "%USERTEST%"=="%EXTRA%" set "ENGINEARG="
rem With no config and no --engine of its own, serve\server.py falls back to its mock engine
rem (that is that flag's default), which answers on 8095 as if a model were loaded: a
rem double-click would look like a success.  Refused here, so the mock stays a deliberate
rem choice - passing --engine mock still starts it.
if "%CFG%"=="" if "%USERTEST%"=="%EXTRA%" goto noconfig
if defined WINDOW goto window

echo.
echo  Strata - %MODEL%
echo    folder   %ROOT%
if not "%CFG%"=="" echo    config   %CFG%
if not "%EXE%"=="" echo    engine   %EXE%
echo    address  http://127.0.0.1:%PORT%/
echo.
echo  The model is loading: the first start takes a minute or two, watch this window for the
echo  ready line.  Stop it with Ctrl+C here, or with Strata_Stop.bat from another window.
echo.
cd /d "%ROOT%"
"%PY%" "%ROOT%\serve\server.py" %ENGINEARG% %OPEN% %EXTRA%
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" echo.
if not "%CODE%"=="0" echo  Strata ended with code %CODE%.
if not "%CODE%"=="0" echo  The last lines above say why; ending with 1 usually means the model
if not "%CODE%"=="0" echo  could not be loaded, so check the config and its log file.
if defined FOREGROUND goto eof
pause
goto eof

rem --- already running --------------------------------------------------------------------------
:already
echo.
echo  Strata is already running in this folder, so nothing was started:
echo    server   pid %RUNNING%
echo    address  http://127.0.0.1:%SRVPORT%/
if not "%CFG%"=="" echo    config   %CFG%
echo.
echo  Two servers cannot share one port and one model.  Strata_Start.bat /restart stops it and
echo  starts it again; Strata_Stop.bat stops it and leaves it stopped.
echo.
if not "%OPEN%"=="" start "" "http://127.0.0.1:%SRVPORT%/"
exit /b 0

rem --- a model left behind by a server that died -------------------------------------------------
rem It still holds the pack and the pinned memory, so a new one cannot load beside it, and
rem nothing can talk to it any more.  Strata_Stop.bat clears exactly this.
:orphan
echo.
echo  A model from an earlier run is still loaded (pid %ENGINE%), but the server that started it
echo  is gone, so nothing can talk to it and it still holds its memory.
echo.
echo  Strata_Start.bat /restart clears it and starts again; Strata_Stop.bat clears it and leaves
echo  it stopped.
echo.
exit /b 0

rem --- /restart: stop first, because the port and the pinned memory are still in use -------------
:restart
set "STOPBAT=%HERE%\Strata_Stop.bat"
if not exist "%STOPBAT%" set "STOPBAT=%ROOT%\Scripts\Strata_Stop.bat"
if not exist "%STOPBAT%" goto nostop
echo.
echo  /restart: stopping the running Strata first ...
call "%STOPBAT%" /quiet
if errorlevel 1 goto stopfailed
goto launch

rem --- /window: hand the work to a window of its own, the shortcut gets its prompt back ---------
:window
set "CHILDARGS=/fg"
if defined NOOPEN set "CHILDARGS=/fg /noopen"
echo  Starting Strata in a window of its own.  Closing that window stops it.
start "Strata" /D "%ROOT%" cmd /c call "%SELF%" !CHILDARGS! %EXTRA%
exit /b 0

:eof
exit /b %CODE%

rem --- /status is the stop script's job, so there is only one place that answers that -----------
:status
set "STOPBAT=%HERE%\Strata_Stop.bat"
if not exist "%STOPBAT%" set "STOPBAT=%ROOT%\Scripts\Strata_Stop.bat"
if not exist "%STOPBAT%" goto nostop
call "%STOPBAT%" /status
exit /b %ERRORLEVEL%

rem --- first run: with no environment, START-HERE.bat is the path that installs everything ------
:setup
echo.
echo  Strata is not installed in this folder yet.
echo  START-HERE.bat is the first-run path: it checks Python, builds .venv, asks which model to
echo  download and starts it.  Handing over to it now.
echo.
if not exist "%ROOT%\START-HERE.bat" goto noinstall
call "%ROOT%\START-HERE.bat"
exit /b %ERRORLEVEL%

rem --- what to say when something that must be there is not -------------------------------------
:help
echo.
echo  Strata_Start.bat - start Strata (the server and the model) from any shortcut
echo.
echo    Strata_Start.bat             start with the newest model config in the Strata folder
echo    Strata_Start.bat /window     start it in its own window and return at once
echo    Strata_Start.bat /noopen     do not open the browser
echo    Strata_Start.bat /restart    stop what is running first, then start it
echo    Strata_Start.bat /status     say what is running right now, and change nothing
echo    Strata_Start.bat /?          this text
echo    Strata_Start.bat --gpu 1     anything else is passed to serve\server.py
echo.
echo  This file may sit anywhere in the Strata folder: next to START-HERE.bat, in Scripts\ or in
echo  .venv\Scripts\.  It finds the Strata folder by walking up from its own location until it
echo  sees serve\server.py, so a shortcut to it works from any working directory.
echo.
echo  The newest strata-*.json in the Strata folder is the model that is started.  The Chat
echo  settings saved beside a config (strata-MODEL.shared-settings.json) are not a config and
echo  are ignored, as is any file without an engine path and arguments in it.
echo.
echo  --engine mock starts serve\server.py's scripted stand-in engine, which needs no model.
echo  Without a config and without --engine this script refuses to start instead of falling back
echo  to that mock.
echo.
exit /b 0

:noroot
echo.
echo  Strata was not found above this file.
echo  Put Strata_Start.bat in the Strata folder, in its Scripts folder or in its .venv\Scripts
echo  folder, then run it again.  It looks for serve\server.py to know which folder is Strata.
echo.
exit /b 3

:noconfig
echo.
echo  No model is installed in this folder, so there is nothing to start.
echo.
echo  Run START-HERE.bat once: it installs a model and starts it, and this script starts it
echo  without asking anything from then on.  (SETUP.bat changes the settings or adds a model.)
echo.
echo  If a model is still downloading - setup.py in Task Manager, or a .part file under
echo  Strata-data\models - wait for that to finish instead: a second install would write the
echo  same files.
echo.
echo  --engine mock still starts the scripted stand-in engine, which needs no model.
echo.
exit /b 3

:nopowershell
echo.
echo  Strata could not be started: PowerShell was not found.  Windows 10 and 11 always have it,
echo  so this is unusual; on an older Windows, install it from https://aka.ms/powershell and
echo  try again.
echo.
exit /b 3

:noprobe
echo.
echo  Strata could not be started: PowerShell is there and was run, but it did not answer, so
echo  what is already running could not be looked at.  That is usually a locked-down account, or
echo  security software blocking the WMI query.
echo.
echo  The probe left nothing behind; run the server by hand to start it without one:
echo    "%PY%" "%ROOT%\serve\server.py" --engine strata --config "strata-YOUR.json" --port 8080
echo.
exit /b 3

:nostop
echo.
echo  Strata_Stop.bat was not found next to this file or in the Scripts folder of the Strata
echo  folder, so what is running cannot be stopped and its port cannot be freed.
echo.
exit /b 4

:stopfailed
echo.
echo  The server that was running did not stop, so its port is still in use and a second server
echo  would only fail to bind it.  Run Strata_Stop.bat once on its own to see what is left, then
echo  try this again.
echo.
exit /b 4

:noinstall
echo.
echo  START-HERE.bat was not found in %ROOT%, so Strata cannot be set up automatically.
echo  Unpack or clone the whole Strata folder again.
echo.
exit /b 3
