@echo off
REM One-time setup on the Windows server. No administrator rights needed.
REM Double-click this file, or run it from a command prompt.

cd /d "%~dp0.."
echo.
echo === Creating the Python environment ===
python -m venv .venv
if errorlevel 1 (
  echo.
  echo Could not create the environment. Is Python installed and on PATH?
  echo Check with:  python --version
  pause
  exit /b 1
)

echo.
echo === Installing dependencies ===
.venv\Scripts\python.exe -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
  echo.
  echo Dependency installation failed. If this is a proxy problem, ask IT for
  echo the pip index settings for this network.
  pause
  exit /b 1
)

echo.
echo === Checking this machine can build and run apps ===
.venv\Scripts\python.exe scripts\selftest.py
echo.
echo If the self-test passed, edit deploy\start-launcher.cmd with your settings
echo and run it.
pause
