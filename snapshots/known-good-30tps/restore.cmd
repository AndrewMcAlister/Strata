@echo off
rem Restore the known-good 30 tok/s config and restart Strata on it.
rem See README.md in this folder for what the snapshot contains and why.
setlocal
set "SNAP=%~dp0"
set "ROOT=D:\Strata"
set "CFG=strata-unsloth-ud-q4_k_xl-dflash2-tuned-70k.json"

if not exist "%SNAP%%CFG%" (
  echo Snapshot config missing: "%SNAP%%CFG%"
  exit /b 1
)

echo Restoring %CFG% ...
copy /Y "%SNAP%%CFG%" "%ROOT%%CFG%" >nul
if errorlevel 1 (
  echo Could not write "%ROOT%%CFG%"
  exit /b 1
)

echo Restarting Strata on the snapshot ...
call "%ROOT%\Scripts\Strata_Start.bat" /restart /window /noopen /newest
if errorlevel 1 (
  echo.
  echo Restart failed. Run it by hand:
  echo   "%ROOT%\Scripts\Strata_Start.bat" /restart
  pause
  exit /b 1
)
echo.
echo Restored. The dashboard at http://127.0.0.1:8080/ needs one refresh.
