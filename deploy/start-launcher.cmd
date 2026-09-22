@echo off
REM Starts the App Launcher. Put a shortcut to this file in your Startup
REM folder (Win+R, then: shell:startup) so it comes back after a restart.
REM Settings live in settings.cmd next to this file.

REM Create settings.cmd from the template the first time, so an update that
REM replaces the template cannot overwrite the values on this machine.
if not exist "%~dp0settings.cmd" (
  copy /y "%~dp0settings.example.cmd" "%~dp0settings.cmd" >nul
  echo Created deploy\settings.cmd from the template. Edit it if you need to.
)

call "%~dp0settings.cmd"
cd /d "%~dp0.."

REM %COMPUTERNAME% is this machine's own name, which Windows keeps across a
REM restart. The address the network hands out is not guaranteed to be the
REM same one twice, so a name is what a shared link should be built on.
echo.
echo   App Launcher starting.
echo.
echo   Share this:  http://%COMPUTERNAME%:%LAUNCHER_PORT%/
echo   On this PC:  http://localhost:%LAUNCHER_PORT%/
echo.
echo   The Server tab lists every address this machine currently answers on.
echo   Close this window to stop it.
echo.
.venv\Scripts\python.exe -m uvicorn launcher.app:app --host 0.0.0.0 --port %LAUNCHER_PORT%
pause
