@echo off
rem Restore the learned-profile config and restart Strata on it.
rem Uses start-server-70k.cmd, not Strata_Start.bat: the latter refuses to start
rem while the agent's MCP tool servers are running (see README.md).
setlocal
set "SNAP=%~dp0"
set "ROOT=D:\Strata"

if not exist "%SNAP%strata-unsloth-ud-q4_k_xl-dflash2-tuned-70k.json" goto missing
if not exist "%SNAP%expert-profile-learned-70k-2026-11-07.bin" goto missing

echo Restoring config and learned profile ...
copy /Y "%SNAP%strata-unsloth-ud-q4_k_xl-dflash2-tuned-70k.json" "%ROOT%\" >nul || goto failed
copy /Y "%SNAP%expert-profile-learned-70k-2026-11-07.bin" "%ROOT%\data\" >nul || goto failed

echo Stopping any running server ...
call "%ROOT%\Scripts\Strata_Stop.bat" /force /quiet >nul 2>&1
timeout /t 4 /nobreak >nul

echo Starting Strata ...
start "Strata" /D "%ROOT%" "%ROOT%\Scripts\start-server-70k.cmd"
echo.
echo Restored. Refresh http://127.0.0.1:8080/ once the model reports loaded.
exit /b 0

:missing
echo Snapshot files are missing next to this script.
exit /b 1
:failed
echo Could not copy the snapshot files into "%ROOT%".
exit /b 1
