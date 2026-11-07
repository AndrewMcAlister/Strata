@echo off
rem Start Strata's server directly, in its own console window.
rem Used when Strata_Start.bat refuses because the probe mistakes the agent's
rem MCP tool servers (also under .venv\Scripts) for leftovers of an old run.
rem Closing this window stops the server.
title Strata
cd /d D:\Strata
set "PYTHONUTF8=1"
"D:\Strata\.venv\Scripts\python.exe" "D:\Strata\serve\server.py" --engine strata --config "D:\Strata\strata-unsloth-ud-q4_k_xl-dflash2-tuned-70k.json" --port 8080
echo.
echo Strata server ended with code %ERRORLEVEL%.
pause
