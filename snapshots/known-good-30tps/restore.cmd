@echo off
rem Restore the known-good 30 tok/s config and restart Strata on it.
rem Uses start-server-70k.cmd, not Strata_Start.bat: the latter refuses to start
rem while the agent's MCP tool servers are running (see README.md).
setlocal
set "SNAP=%~dp0"
set "ROOT=D:\Strata"
set "CFG=strata-unsloth-ud-q4_k_xl-dflash2-tuned-70k.json"

if not exist "%SNAP%%CFG%" goto missing

echo Restoring %CFG% ...
copy /Y "%SNAP%%CFG%" "%ROOT%%CFG%" >nul || goto failed

echo Stopping any running server ...
call "%ROOT%\Scripts\Strata_Stop.bat" /force /quiet >nul 2>&1
timeout /t 4 /nobreak >nul

echo Starting Strata ...
start "Strata" /D "%ROOT%" "%ROOT%\Scripts\start-server-70k.cmd"
echo.
echo Restored. Refresh http://127.0.0.1:8080/ once the model reports loaded.
exit /b 0

:missing
echo Snapshot config missing: "%SNAP%%CFG%"
exit /b 1
:failed
echo Could not write "%ROOT%%CFG%"
exit /b 1
