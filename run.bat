@echo off
echo Starting BB Scanner...
echo.

:: Activate virtual environment
call .venv\Scripts\activate.bat
if errorlevel 1 (
    echo ERROR: Virtual environment not found. Please run setup.bat first.
    pause
    exit /b 1
)

:: Run the scanner
python main.py

pause
