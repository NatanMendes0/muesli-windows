@echo off
rem Muesli with a console window and devtools, for troubleshooting.
cd /d "%~dp0"
if not exist ".venv\install.ok" (
    echo Run run.bat once first to set up the environment.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" launch.py --debug
pause
