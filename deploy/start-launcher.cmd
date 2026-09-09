@echo off
REM Starts the App Launcher. Put a shortcut to this file in your Startup
REM folder (Win+R, then: shell:startup) so it comes back after a restart.
REM Settings live in settings.cmd next to this file.

call "%~dp0settings.cmd"
cd /d "%~dp0.."

echo Starting the App Launcher on http://%LAUNCHER_PUBLIC_HOST%:%LAUNCHER_PORT%/
echo Close this window to stop it.
.venv\Scripts\python.exe -m uvicorn launcher.app:app --host 0.0.0.0 --port %LAUNCHER_PORT%
pause
