@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title ClipFactory

if not exist ".venv\Scripts\python.exe" (
    echo ClipFactory ещё не установлен. Сначала запустите setup.bat
    pause
    exit /b 1
)

echo ==========================================================
echo   ClipFactory запускается...
echo   Браузер откроется автоматически через пару секунд.
echo   Не закрывайте это окно, пока работаете с программой.
echo   Для выхода закройте окно или нажмите Ctrl+C.
echo ==========================================================
echo.

".venv\Scripts\python.exe" -m backend.main
if errorlevel 1 (
    echo.
    echo ClipFactory завершился с ошибкой. Подробности - в storage\logs\clipfactory.log
    pause
)
