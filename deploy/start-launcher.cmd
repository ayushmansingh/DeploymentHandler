@echo off
REM Starts the App Launcher. Put a shortcut to this file in your Startup
REM folder (Win+R, then: shell:startup) so it comes back after a restart.
REM Settings live in settings.cmd next to this file.

REM Create settings.cmd from the template the first time, so an update that
REM replaces the template cannot overwrite the values on this machine.
if not exist "%~dp0settings.example.cmd" (
  echo.
  echo   This folder is not a complete copy of the App Launcher: the file
  echo   deploy\settings.example.cmd is missing. Extract the whole ZIP again,
  echo   keeping its folder structure, and run this from there.
  echo.
  pause
  exit /b 1
)
if not exist "%~dp0settings.cmd" (
  copy /y "%~dp0settings.example.cmd" "%~dp0settings.cmd" >nul
  echo Created deploy\settings.cmd from the template. Edit it if you need to.
)

call "%~dp0settings.cmd"
cd /d "%~dp0.."

REM A folder that has never been installed has no .venv, and cmd.exe would
REM report that as a bare "The system cannot find the path specified." after
REM the banner had already promised the launcher was starting. Say which
REM folder is not set up and what to run, before promising anything.
if not exist ".venv\Scripts\python.exe" (
  echo.
  echo   This copy has not been set up yet - there is no Python environment in
  echo.
  echo       %CD%\.venv
  echo.
  echo   Run deploy\install-windows.cmd in THIS folder once. It takes a few
  echo   minutes and needs no administrator rights.
  echo.
  echo   If you already have a working App Launcher somewhere else, start that
  echo   one instead. Your apps and data are kept outside the program folder,
  echo   in %LAUNCHER_DATA_DIR%, so either copy finds them.
  echo.
  pause
  exit /b 1
)

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
