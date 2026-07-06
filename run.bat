@echo off
rem Muesli launcher. First run creates a Python venv and installs dependencies.
cd /d "%~dp0"

if not exist ".venv\install.ok" (
    echo First run: setting up the Python environment. This takes a few minutes...
    if not exist ".venv\Scripts\python.exe" (
        python -m venv .venv
        if errorlevel 1 (
            echo.
            echo Could not create a virtual environment. Install Python 3.10 - 3.12 from
            echo python.org, check "Add python.exe to PATH" in its installer, then rerun.
            pause
            exit /b 1
        )
    )
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Dependency install failed. Check your internet connection and rerun run.bat.
        pause
        exit /b 1
    )
    echo ok> ".venv\install.ok"
)

start "" ".venv\Scripts\pythonw.exe" launch.py
exit /b 0
