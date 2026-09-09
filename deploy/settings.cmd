@echo off
REM Settings shared by install-windows.cmd and start-launcher.cmd, so the
REM self-test checks the same configuration the launcher actually runs with.
REM Edit the values here - nowhere else.

REM The server's own address on the network. This appears in every link handed
REM to your team, so an address they cannot reach gives them broken links.
REM Find it with:  ipconfig
set LAUNCHER_PUBLIC_HOST=192.168.1.50

REM Where uploads, app files, logs and saved app data are kept. Back this up.
REM Do NOT put this under %LOCALAPPDATA%: Python installed from the Microsoft
REM Store redirects writes there into a private folder, and every deploy fails.
set LAUNCHER_DATA_DIR=%USERPROFILE%\AppLauncherData

REM Full path to npm.cmd. Leave empty if Node is already on PATH.
set LAUNCHER_NPM=

REM How many apps may build at once. Each build can use ~2GB of memory.
set LAUNCHER_MAX_BUILDS=2

REM The port the dashboard itself listens on.
set LAUNCHER_PORT=8080

set LAUNCHER_RUNTIME=native
