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

if not exist "%PROJECT_ROOT%run-windows-hidden.vbs" (
    echo [ERROR] Tray launcher was not found in "%PROJECT_ROOT%".
    exit /b 1
)

wscript.exe "%PROJECT_ROOT%run-windows-hidden.vbs"
