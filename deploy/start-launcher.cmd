@echo off
REM Starts the App Launcher. Put a shortcut to this file in your Startup
REM folder (Win+R, then: shell:startup) so it comes back after a restart.

REM ==================== settings - edit these ====================

REM The server's own address on the network. This appears in every link
REM handed to your team, so "localhost" would give them links that only work
REM on this machine. Find it with:  ipconfig
set LAUNCHER_PUBLIC_HOST=192.168.1.50

REM Where uploads, app files, logs and saved app data are kept. Back this up.
set LAUNCHER_DATA_DIR=C:\AppLauncherData

REM Full path to npm.cmd. Only needed if Node is not on PATH.
set LAUNCHER_NPM=C:\tools\node\npm.cmd

REM How many apps may build at once. Each build can use ~2GB of memory.
set LAUNCHER_MAX_BUILDS=2

REM The port the dashboard itself listens on.
set LAUNCHER_PORT=8080

REM ==============================================================

set LAUNCHER_RUNTIME=native
cd /d "%~dp0.."
echo Starting the App Launcher on http://%LAUNCHER_PUBLIC_HOST%:%LAUNCHER_PORT%/
echo Close this window to stop it.
.venv\Scripts\python.exe -m uvicorn launcher.app:app --host 0.0.0.0 --port %LAUNCHER_PORT%
pause
