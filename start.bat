@echo off
REM Double-click this file to start the editor (Windows).
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel%==0 (
  python server.py
  goto end
)
where py >nul 2>nul
if %errorlevel%==0 (
  py server.py
  goto end
)
echo تعذّر إيجاد Python على هذا الجهاز.
echo ثبّته من https://www.python.org/downloads/ (فعِّل "Add python.exe to PATH" أثناء التثبيت) ثم أعد المحاولة.
:end
pause
