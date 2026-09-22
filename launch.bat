@echo off
setlocal enabledelayedexpansion

REM CMM-Gen launcher (Windows) -- sets up a virtual environment, installs
REM dependencies, runs a smoke test against the bundled sample part, and
REM launches the Streamlit visualizer.

cd /d "%~dp0"

echo ============================================
echo  CMM-Gen setup + launch
echo ============================================

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found on PATH. Install Python 3.11+ from
    echo         https://www.python.org/downloads/ and check "Add to PATH"
    echo         during install, then re-run this script.
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo Found Python %PYVER%

if not exist ".venv" (
    echo Creating virtual environment in .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create the virtual environment.
        pause
        exit /b 1
    )
)

call .venv\Scripts\activate.bat

echo Installing dependencies (first run only takes a few minutes) ...
python -m pip install --upgrade pip >nul
pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Dependency install failed -- see the error above.
    echo         cadquery/OCP is the most likely culprit; it needs a 64-bit
    echo         Python 3.11 or 3.12. Check your Python version and retry.
    pause
    exit /b 1
)

echo.
echo ---- Smoke test: parsing the bundled sample part ----
python -m cmm_gen.cli parse-cad --step tests\fixtures\sample_part.step
if errorlevel 1 (
    echo [ERROR] Smoke test failed -- the install is broken. See output above.
    pause
    exit /b 1
)

echo.
echo Smoke test passed. Launching the visualizer at http://localhost:8501 ...
echo (Close this window, or press Ctrl+C, to stop it.)
echo.
python -m cmm_gen.cli visualize

pause
