@echo off
rem Double-click (or run from a terminal) to restart the BuildRig API, Pi worker and web app.
rem Options: -Stop  stop the services    -NoBrowser  do not open the web app
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-buildrig.ps1" %*
if errorlevel 1 pause
