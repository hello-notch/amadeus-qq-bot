@echo off
setlocal

set "PROJECT_ROOT=%~dp0"

if not defined NAPCAT_DIR (
    echo [ERROR] NAPCAT_DIR is not set. Point it to an external NapCat.Shell directory.
    exit /b 1
)

if not defined QQ_UIN (
    echo [ERROR] QQ_UIN is not set. Set it only in your local environment.
    exit /b 1
)

if not exist "%NAPCAT_DIR%\launcher-user.bat" (
    echo [ERROR] NapCat launcher was not found in "%NAPCAT_DIR%".
    exit /b 1
)

start "NapCat" /d "%NAPCAT_DIR%" cmd /k call launcher-user.bat "%QQ_UIN%"
timeout /t 1 /nobreak >nul
start "Amadeus NoneBot" /d "%PROJECT_ROOT%" cmd /k uv run amadeus-bot
