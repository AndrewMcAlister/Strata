@echo off
rem Strata's engine for AMD gfx906 on Windows (Instinct MI50 / MI60, Radeon VII): a hand build, not a feed package.
rem
rem   tools\hip\build_windows_gfx906.bat           build build-906-win\strata.exe and strata-device.exe
rem   tools\hip\build_windows_gfx906.bat check     say what would be installed and built, then stop (nothing is downloaded)
rem   tools\hip\build_windows_gfx906.bat tests     build the tests as well (ctest in build-906-win; they need the card)
rem
rem gfx906 is wave64 and has no WMMA and no packed byte arithmetic, so the wave32 backend (tools\hip\build_windows.bat,
rem STRATA_ENABLE_HIP) refuses it.  This script takes the other route: -DSTRATA_HIP_GFX906=ON compiles the CUDA-shaped
rem .cu sources as HIP through include\strata\platform\hip_compat (one CUDA warp = one half of a 64-lane wavefront) and
rem keeps every "CUDA" branch of the tree.  It is a separate switch and a separate build folder; the wave32 route, the
rem NVIDIA (CUDA) engine and every feed package are untouched by it.
rem
rem The project's own note on this route (docs\AMD_HIP.md, "gfx906"): built by hand, community-tested on Linux only,
rem and not yet run on Windows by anyone.  A hand build is not a released component, so a run config points at the built
rem exe by path ("exe", plus "lib_dirs" at the ROCm bin folder) and not at "component:strata-engine".
rem
rem Needs: Visual Studio 2022 (or 2019) Build Tools with the C++ workload, Python 3.10+ and git.  No admin rights and
rem no AMD GPU: ROCm comes from AMD's TheRock Python wheels into a private venv (ROCM_VENV, about 7 GB).  Those wheels
rem carry the compiler, the HIP runtime and hipBLAS/rocBLAS for gfx906; hipBLASLt is not used (it has no gfx906
rem kernels).  CMake refuses to mix cl.exe with Clang for HIP, so ROCm's clang compiles the host code too, targeting
rem the MSVC ABI (MSVC's headers, CRT and linker from vcvars64).
rem
rem Settings (environment variables, all optional):
rem   STRATA_HIP_ARCHS     gfx906             the arch this build holds machine code for; a wave32 card is refused at start
rem   STRATA_ROCM_VERSION  10.3.0a20261118    checked 18 Nov 2026: rocm-sdk-device-gfx906 has a win_amd64 wheel for it
rem   STRATA_ROCM_INDEX    https://nightly.repo.amd.com/rocm/whl-next/
rem   ROCM_VENV            <repo>\.rocm-win    (a second arch family needs its own folder: the stamp records the archs)
rem   BUILD_DIR            <repo>\build-906-win
rem   STRATA_ROCM_KEEP     1 = use ROCM_VENV as it is (no pip install, even when its archs differ from STRATA_HIP_ARCHS)
rem   STRATA_GGML_DIR      optional override (kept for two releases): a llama.cpp checkout to take ggml from; default: the
rem                        vendored third_party/strata-ggml
rem   STRATA_CMAKE_EXTRA   extra cmake configure arguments (for example -DSTRATA_WINDOW_MAX_T=8)
rem
rem Not built here, on purpose: the picture readers (tools\vision) and the release zip.  The release zip needs a
rem rocBLAS .kpack for gfx906 (tools\hip\package_windows.py exits naming the wheel when one is missing), and this
rem script's job is the text engine: build-906-win\strata.exe.
setlocal EnableDelayedExpansion
for %%I in ("%~dp0..\..") do set "SRC=%%~fI"
if not defined STRATA_HIP_ARCHS set "STRATA_HIP_ARCHS=gfx906"
if not defined STRATA_ROCM_VERSION set "STRATA_ROCM_VERSION=10.3.0a20261118"
if not defined STRATA_ROCM_INDEX set "STRATA_ROCM_INDEX=https://nightly.repo.amd.com/rocm/whl-next/"
if not defined ROCM_VENV set "ROCM_VENV=%SRC%\.rocm-win"
if not defined BUILD_DIR set "BUILD_DIR=%SRC%\build-906-win"
set "TESTS=OFF"
if /i "%~1"=="tests" set "TESTS=ON"
set "CHECK="
if /i "%~1"=="check" set "CHECK=1"

rem ---- 0. the tools: Python, Visual Studio, CMake and Ninja (the last two ship inside Visual Studio and are often
rem         not on PATH, so they are taken from there when PATH has none)
set "PY="
py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys" >nul 2>nul && set "PY=python"
if not defined PY (echo Python 3.10+ is needed & exit /b 1)
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
set "VS="
if exist "%VSWHERE%" for /f "delims=" %%V in ('"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath') do set "VS=%%V"
if not defined VS (echo Visual Studio Build Tools with the C++ workload are needed & exit /b 1)
set "CMAKE="
for /f "delims=" %%C in ('where cmake 2^>nul') do if not defined CMAKE set "CMAKE=%%C"
if not defined CMAKE if exist "%VS%\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe" set "CMAKE=%VS%\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
if not defined CMAKE (echo CMake was not found: install it, or the Visual Studio component that ships it & exit /b 1)
set "NINJA_DIR="
for /f "delims=" %%N in ('where ninja 2^>nul') do if not defined NINJA_DIR for %%D in ("%%N") do set "NINJA_DIR=%%~dpD"
if not defined NINJA_DIR if exist "%VS%\Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja\ninja.exe" set "NINJA_DIR=%VS%\Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja\"
if not defined NINJA_DIR (echo Ninja was not found: install it, or the Visual Studio component that ships it & exit /b 1)

rem ---- 1. what this run is about to do (printed by "check" too)
set "EXTRAS=libraries,devel"
for %%A in (%STRATA_HIP_ARCHS:;= %) do set "EXTRAS=!EXTRAS!,device-%%A"
echo Strata gfx906 engine - a hand build, not a release package
echo   source         %SRC%
echo   archs          %STRATA_HIP_ARCHS% (wave64)
echo   ROCm           rocm[%EXTRAS%]==%STRATA_ROCM_VERSION%
echo                  from %STRATA_ROCM_INDEX%
echo   ROCm venv      %ROCM_VENV%
echo   build folder   %BUILD_DIR%
echo   cmake          %CMAKE%
echo   ninja          %NINJA_DIR%ninja.exe
echo   Visual Studio  %VS%
if defined CHECK (
  echo.
  echo check: nothing was downloaded, installed or built.  Run it without "check" to do it.
  endlocal
  exit /b 0
)

rem ---- 2. ROCm (TheRock wheels: the compiler, the HIP runtime, hipBLAS/rocBLAS, a device package per arch)
if not exist "%ROCM_VENV%\Scripts\python.exe" %PY% -m venv "%ROCM_VENV%" || exit /b 1
set "STAMP=%ROCM_VENV%\strata-rocm.txt"
set "WANT=%STRATA_ROCM_VERSION% %EXTRAS%"
set "HAVE="
if exist "%STAMP%" set /p HAVE=<"%STAMP%"
if not "!HAVE!"=="!WANT!" if not "%STRATA_ROCM_KEEP%"=="1" (
  echo Installing ROCm %STRATA_ROCM_VERSION% [%EXTRAS%] into %ROCM_VENV% ...
  "%ROCM_VENV%\Scripts\python.exe" -m pip install --disable-pip-version-check --index-url "%STRATA_ROCM_INDEX%" "rocm[%EXTRAS%]==%STRATA_ROCM_VERSION%" || exit /b 1
  "%ROCM_VENV%\Scripts\rocm-sdk.exe" init || exit /b 1
  >"%STAMP%" echo !WANT!
)
for /f "delims=" %%R in ('"%ROCM_VENV%\Scripts\rocm-sdk.exe" path --root') do set "ROCM=%%R"
if not exist "%ROCM%\lib\llvm\bin\clang++.exe" (echo ROCm has no compiler in "%ROCM%" & exit /b 1)
set "ROCM_F=%ROCM:\=/%"
set "BITCODE=%ROCM_F%/lib/llvm/amdgcn/bitcode"

rem ---- 3. MSVC: the linker, the C runtime and the Windows SDK (ROCm's clang uses them)
call "%VS%\VC\Auxiliary\Build\vcvars64.bat" >nul || exit /b 1
rem      Visual Studio's C++ workload does not include the Windows SDK, and ROCm's clang needs it twice: rc.exe at
rem      configure time (CMake's Windows-Clang module) and kernel32.lib and the other import libraries at link time.
where rc >nul 2>nul
if errorlevel 1 (
  echo The Windows SDK is missing.  It holds rc.exe and kernel32.lib, and ROCm's clang needs both.
  echo Install it, then run this again ^(no reboot is needed^):
  echo   winget install --id Microsoft.WindowsSDK.10.0.26100 -e --accept-package-agreements --accept-source-agreements
  exit /b 1
)
set "HIP_PLATFORM=amd"
set "HIP_PATH=%ROCM%"
set "ROCM_PATH=%ROCM%"
set "PATH=%NINJA_DIR%;%ROCM%\bin;%ROCM%\lib\llvm\bin;%PATH%"

rem ---- 4. configure + build.  STRATA_PORTABLE: the CPU kernels for an AVX2 baseline, as in the NVIDIA zip.
rem      Deliberately absent: -DSTRATA_ENABLE_HIP (CMake refuses it together with the gfx906 switch) and
rem      -DSTRATA_PREFILL_MMQ (it requires STRATA_ENABLE_HIP).  This route builds its own kernels instead.
set "GGML="
if defined STRATA_GGML_DIR set "GGML=-DSTRATA_GGML_DIR=%STRATA_GGML_DIR:\=/%"
if not exist "%BUILD_DIR%\build.ninja" (
  "%CMAKE%" -G Ninja -S "%SRC%" -B "%BUILD_DIR%" -DCMAKE_BUILD_TYPE=Release ^
    -DSTRATA_HIP_GFX906=ON -DSTRATA_BUILD_TESTS=%TESTS% ^
    -DSTRATA_NATIVE_EXPERTS=ON -DSTRATA_PORTABLE=ON -DSTRATA_MMQ=OFF ^
    "-DCMAKE_HIP_ARCHITECTURES=%STRATA_HIP_ARCHS%" ^
    "-DCMAKE_C_COMPILER=%ROCM_F%/lib/llvm/bin/clang.exe" "-DCMAKE_CXX_COMPILER=%ROCM_F%/lib/llvm/bin/clang++.exe" ^
    "-DCMAKE_HIP_COMPILER=%ROCM_F%/lib/llvm/bin/clang++.exe" "-DCMAKE_HIP_COMPILER_ROCM_ROOT=%ROCM_F%" ^
    "-DCMAKE_PREFIX_PATH=%ROCM_F%" "-DCMAKE_HIP_FLAGS=--rocm-path=%ROCM_F% --rocm-device-lib-path=%BITCODE%" ^
    %GGML% %STRATA_CMAKE_EXTRA% || exit /b 1
)
if "%TESTS%"=="ON" (
  "%CMAKE%" --build "%BUILD_DIR%" || exit /b 1
) else (
  "%CMAKE%" --build "%BUILD_DIR%" --target strata strata-device || exit /b 1
)

rem ---- 5. where the engine is, and what to do with it
echo.
echo Built: %BUILD_DIR%\strata.exe
echo        %BUILD_DIR%\strata-device.exe
echo.
echo Next, by hand, before any service is touched:
echo   "%BUILD_DIR%\strata-device.exe" --list-devices
echo   "%BUILD_DIR%\strata-device.exe" --selftest
echo Then a run config with, instead of "component:strata-engine":
echo   "exe": "%BUILD_DIR:\=\\%\\strata.exe"
echo   "lib_dirs": ["%ROCM:\=\\%\\bin"]
echo and:  python serve\\server.py --engine strata --config ^<that config^> --port 8080
endlocal
