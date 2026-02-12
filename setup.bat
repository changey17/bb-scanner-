@echo off
echo Setting up BB Scanner...
echo.

:: Create virtual environment
python -m venv .venv
if errorlevel 1 (
    echo ERROR: Python not found. Please install Python 3.10+ from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

:: Activate and install dependencies
call .venv\Scripts\activate.bat
pip install -r requirements.txt

:: Create .env from example if it doesn't exist
if not exist .env (
    copy .env.example .env
    echo.
    echo Created .env file - please edit it with your BookieBashing credentials:
    echo   BB_USERNAME=your_email@example.com
    echo   BB_PASSWORD=your_password
)

echo.
echo Setup complete! Double-click run.bat to start the scanner.
echo.
pause
