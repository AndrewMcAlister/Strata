@echo off
rem ==============================================================================================
rem  Strata_Start.bat - start Strata (the server and the model) from any shortcut.
rem
rem    Strata_Start.bat             start a model in the Strata folder - it asks which one when
rem                                 more than one is installed, and starts the newest otherwise
rem    Strata_Start.bat /newest     do not ask: start the newest model config
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
rem  With more than one model installed this asks which one to start, because the newest config
rem  is not necessarily the one wanted, and the choice decides what an app's model list shows.
rem  Nothing else changes: /newest, a --config of your own, a running server, /restart or an
rem  input that is not a console (a scheduled task, a redirect) all start without asking, and
rem  with one model installed there is nothing to ask.
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
set "NEWEST="
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
if /i "%~1"=="/newest"  (set "NEWEST=1" & shift & goto parse)
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
rem AUTO says the config is the fallback below and not one asked for, so it is the only case where
rem asking which model to start is a real question.  The list is kept (newest first, the same order
rem and the same IsConfig test as before) so the start can offer it; the newest stays the default.
>>"%PSFILE%" echo $auto = 0
>>"%PSFILE%" echo $configs = New-Object System.Collections.ArrayList
>>"%PSFILE%" echo $newest = $null
>>"%PSFILE%" echo if ($cfg -eq '') {
>>"%PSFILE%" echo     foreach ($f in @(Get-ChildItem -LiteralPath $Root -Filter 'strata-*.json' -ErrorAction SilentlyContinue)) {
>>"%PSFILE%" echo         if (-not (IsConfig $f.FullName)) { continue }
>>"%PSFILE%" echo         [void]$configs.Add($f)
>>"%PSFILE%" echo         if (($newest -eq $null) -or ($f.LastWriteTime -gt $newest.LastWriteTime)) { $newest = $f }
>>"%PSFILE%" echo     }
>>"%PSFILE%" echo     if ($newest -ne $null) { $cfg = $newest.FullName; $auto = 1 }
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
rem Whether a person is at the console decides whether this start may ask anything at all: a
rem scheduled task, a redirect or a closed input has nobody to answer, and set /p on those
rem returns empty at once rather than waiting.  IsOutputRedirected is always true here (the
rem probe's own output is captured), so it is the input side that is asked about.
>>"%PSFILE%" echo $tty = 0
>>"%PSFILE%" echo try { if (-not [Console]::IsInputRedirected) { $tty = 1 } } catch { $tty = 0 }
>>"%PSFILE%" echo Write-Output ('TTY=' + $tty)
>>"%PSFILE%" echo Write-Output ('AUTO=' + $auto)
>>"%PSFILE%" echo Write-Output ('WANTPORT=' + $wanted)
rem The candidates, newest first, each with what the batch needs to start it: the list line the
rem user picks from, and the path, name, engine and port of that choice.  PORT is emitted rather
rem than recomputed because a config whose port differs from the running one would otherwise be
rem started on the wrong port.
>>"%PSFILE%" echo if ($auto -eq 1) {
>>"%PSFILE%" echo     $ordered = New-Object System.Collections.ArrayList
>>"%PSFILE%" echo     [void]$ordered.Add($newest)
>>"%PSFILE%" echo     foreach ($f in $configs) { if ($f.FullName -ne $newest.FullName) { [void]$ordered.Add($f) } }
>>"%PSFILE%" echo     Write-Output ('NCFG=' + $ordered.Count)
>>"%PSFILE%" echo     for ($i = 0; $i -lt $ordered.Count; $i++) {
>>"%PSFILE%" echo         $k = $i + 1
>>"%PSFILE%" echo         $p = $ordered[$i].FullName
>>"%PSFILE%" echo         $m = ''
>>"%PSFILE%" echo         $x = ''
>>"%PSFILE%" echo         $jp = 0
>>"%PSFILE%" echo         $jj = $null
>>"%PSFILE%" echo         try { $jj = ConvertFrom-Json (Get-Content -Raw -LiteralPath $p) } catch { $jj = $null }
>>"%PSFILE%" echo         if ($jj -ne $null) {
>>"%PSFILE%" echo             if ($jj.model_name) { $m = [string]$jj.model_name }
>>"%PSFILE%" echo             if ($jj.exe) { $x = [string]$jj.exe }
>>"%PSFILE%" echo             if ($jj.port) { $jp = [int]$jj.port }
>>"%PSFILE%" echo         }
>>"%PSFILE%" echo         if ($m -eq '') { $m = 'strata' }
>>"%PSFILE%" echo         Write-Output ('CFG' + $k + '=' + $p)
>>"%PSFILE%" echo         Write-Output ('MODEL' + $k + '=' + $m)
>>"%PSFILE%" echo         Write-Output ('EXE' + $k + '=' + $x)
>>"%PSFILE%" echo         Write-Output ('PORT' + $k + '=' + $jp)
>>"%PSFILE%" echo     }
>>"%PSFILE%" echo } else { Write-Output 'NCFG=0' }
>>"%PSFILE%" echo exit 0

set "RUNNING="
set "ENGINE="
set "PORT="
set "SRVPORT="
set "MODEL="
set "CFG="
set "EXE="
set "AUTO="
set "NCFG="
set "TTY="
set "WANTPORT="
set "SEL="
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
rem An older probe, or one that answered before these were emitted, leaves no choice to offer;
rem NCFG in particular has to be a number: for /l with an empty one is a syntax error.
if not defined AUTO set "AUTO=0"
if not defined NCFG set "NCFG=0"
if not defined TTY set "TTY=0"
if not defined WANTPORT set "WANTPORT=0"

rem --- starting twice would only fail later on a busy port, so deal with it here -----------------
rem An engine that outlived its server is the one case the server check cannot see, and it is the
rem one where a second start is worst: the pack and the pinned memory are still held.
if not "%ENGINE%"=="0" if not defined RESTART goto orphan
if not "%RUNNING%"=="0" if not defined RESTART goto already
if not "%RUNNING%"=="0" goto restart
if not "%ENGINE%"=="0" goto restart

rem --- the arguments for serve\server.py ---------------------------------------------------------
rem This script names the engine and the config itself, which is what makes a double-click on this
rem file enough.  The config is appended after the choice below, not here: a line this early would
rem carry the newest config whatever the picker then decided.  If --engine was given, nothing is
rem added and the command line is exactly what was asked for.
:launch
set "ENGINEARG="
rem Whether the command line brought its own --engine.  The substitution has to be guarded:
rem with %EXTRA% empty, cmd does not treat "%EXTRA:--engine=%" as a substitution at all and
rem leaves the literal "--engine=" behind, so the comparison below would always say "asked for
rem one" - which is how an empty command line came to start serve\server.py's mock engine.
set "USERTEST=%EXTRA%"
if defined EXTRA set "USERTEST=%EXTRA:--engine=%"
rem With no config and no --engine of its own, serve\server.py falls back to its mock engine
rem (that is that flag's default), which answers on 8095 as if a model were loaded: a
rem double-click would look like a success.  Refused here, so the mock stays a deliberate
rem choice - passing --engine mock still starts it.
if "%CFG%"=="" if "%USERTEST%"=="%EXTRA%" goto noconfig
if defined WINDOW goto window

rem --- more than one model installed: ask which one, unless something already answered that ----
rem Everything that is not this script's own newest-config fallback has already decided: a
rem --config of the user's own, the config of a server that is running, /restart (which restarts
rem that same server's model), /status and the two refusals above never reach here as AUTO, and
rem /newest asks for the old behaviour on purpose.  --engine of the user's own is checked too:
rem with it, the command line is taken as written and the choice below would be dropped.
if not "%AUTO%"=="1" goto banner
if not %NCFG% gtr 1 goto banner
if not "%TTY%"=="1" goto banner
if not "%USERTEST%"=="%EXTRA%" goto banner
if defined NEWEST goto banner

echo.
echo  More than one model is installed in this folder.  Choose the one to start:
echo.
rem The names go through delayed expansion: a percent-expanded name is expanded before cmd looks
rem for characters that mean something to it, so a name holding one would be taken as syntax.
rem With delayed expansion the name is already a value by then and is printed as it is.
for /l %%i in (1,1,%NCFG%) do echo    %%i. !MODEL%%i!
echo.
:pickprompt
rem A TIMED prompt. set /p waits for ever, which is wrong here twice over: a start at
rem logon has nobody watching it, and a start from a shortcut that is ignored would hold
rem the console open indefinitely. choice /t returns after ten seconds with the default,
rem which is the model without --vision (see the DEF block below): vision loads a second
rem engine and a 900 MB projector, VRAM an unattended start does not need, and the models
rem that keep vision stay selectable. ORDER IS STILL NEWEST-FIRST, so the numbering can
rem change; the default follows the property rather than the number.
rem WHAT THE DEFAULT IS comes from Strata_DefaultModel.ps1, not from a number written
rem here. That script prints the position of the first model without --vision, in the same
rem newest-first order this script lists. A hardcoded 2 was correct until the config meant
rem to be 2 was edited - which made it newest, moved it to 1, and pointed the default at a
rem vision model instead. A position is a fact about the clock; this is a fact about the model.
set "DEF="
if exist "%ROOT%\Scripts\Strata_DefaultModel.ps1" (
  for /f "usebackq delims=" %%A in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\Scripts\Strata_DefaultModel.ps1" -Root "%ROOT%"`) do set "DEF=%%A"
)
if not defined DEF set "DEF=1"
rem The key set is built from NCFG so choice can only accept a number that exists.
set "KEYS="
for /l %%i in (1,1,%NCFG%) do set "KEYS=!KEYS!%%i"
set "SEL="
choice /c !KEYS! /n /t 10 /d !DEF! /m "Start which model? [!DEF!, default in 10 seconds] "
set "SEL=!ERRORLEVEL!"
rem `choice` always answers, so SEL is always set; these lines stay as a guard for an
rem older probe that emitted no NCFG, and for the Ctrl+C case where choice returns 0.
if not defined SEL set "SEL=!DEF!"
if "!SEL!"=="0" set "SEL=!DEF!"
set "OK="
for /l %%i in (1,1,%NCFG%) do if "!SEL!"=="%%i" set "OK=1"
if defined OK goto picked
echo  Enter a number from 1 to %NCFG%.
goto pickprompt
:picked
rem The chosen number picks the config, and with it the name and engine the banner and the
rem command line use.  A port given on the command line still wins over the config's own.
set "CFG=!CFG%SEL%!"
set "MODEL=!MODEL%SEL%!"
set "EXE=!EXE%SEL%!"
if "%WANTPORT%"=="0" set "PORT=!PORT%SEL%!"

:banner
rem The command line for the model that is about to be started, built from the config chosen
rem above - the newest when nothing was asked.  It is refused when the command line carried its
rem own --engine: that flag decides which engine runs, and the mock takes no config.
if not "%CFG%"=="" if "%USERTEST%"=="%EXTRA%" set "ENGINEARG=--engine strata --config "%CFG%" --port %PORT%"
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
if not "%OPEN%"=="" start "" "http://127.0.0.1:%SRVPORT%/?open=%RANDOM%#monitor"
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
if defined NEWEST set "CHILDARGS=!CHILDARGS! /newest"
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
echo    Strata_Start.bat             start a model - asks which one when more than one is installed
echo    Strata_Start.bat /newest     do not ask: start the newest model config
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
echo  The strata-*.json files in the Strata folder are the installed models.  With more than one,
echo  a start lists them and asks which to load; pressing Enter takes the first, which is the
echo  newest.  /newest skips the question, and so does a --config of your own, a server that is
echo  already running, and any start whose input is not a console (a scheduled task, a redirect).
echo  Nothing is asked when only one model is installed.
echo.
echo  The Chat settings saved beside a config (strata-MODEL.shared-settings.json) are not a
echo  config and are ignored, as is any file without an engine path and arguments in it.
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
