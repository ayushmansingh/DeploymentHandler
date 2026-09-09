@echo off
REM One-time setup on the Windows server. No administrator rights needed.
REM Double-click this file, or run it from a command prompt.

call "%~dp0settings.cmd"
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
if errorlevel 1 (
  echo.
  echo The self-test failed. Fix what it reported above, then run this again.
  pause
  exit /b 1
)

echo.
echo Setup complete. Edit deploy\settings.cmd with your settings, then run
echo deploy\start-launcher.cmd
pause
