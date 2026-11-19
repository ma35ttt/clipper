@echo off
rem Double-click this file to update clipper to the newest version.
cd /d "%~dp0"
where py >nul 2>nul && (py -3 -m clipper update) || (python -m clipper update)
echo.
pause
