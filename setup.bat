@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title ClipFactory - установка

echo ==========================================================
echo                 ClipFactory - установка
echo ==========================================================
echo.

rem ---------- 1. Python 3.11+ ----------
echo [1/5] Проверка Python...
set "PY="
for %%V in (3.12 3.11 3.13) do (
    if not defined PY (
        py -%%V -c "import sys" >nul 2>nul && set "PY=py -%%V"
    )
)
if not defined PY (
    python -c "import sys; assert sys.version_info >= (3, 11)" >nul 2>nul && set "PY=python"
)
if not defined PY goto :no_python
for /f "delims=" %%i in ('!PY! -c "import sys; print(sys.version.split()[0])"') do set "PYVER=%%i"
echo       Найден Python !PYVER!
echo.

rem ---------- 2. Virtual environment ----------
echo [2/5] Создание виртуального окружения .venv ...
if not exist ".venv\Scripts\python.exe" (
    !PY! -m venv .venv
    if errorlevel 1 goto :venv_failed
) else (
    echo       Окружение уже существует.
)
echo.

rem ---------- 3. Dependencies ----------
echo [3/5] Установка библиотек Python. Это может занять несколько минут...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :pip_failed
echo       Библиотеки установлены.
echo.

rem ---------- 4. FFmpeg ----------
echo [4/5] Проверка FFmpeg...
set "FF_OK="
if exist "tools\ffmpeg\bin\ffmpeg.exe" if exist "tools\ffmpeg\bin\ffprobe.exe" set "FF_OK=1"
if not defined FF_OK (
    where ffmpeg >nul 2>nul && where ffprobe >nul 2>nul && set "FF_OK=1"
)
if defined FF_OK (
    echo       FFmpeg найден.
    goto :ffmpeg_done
)
echo.
echo       FFmpeg не найден. Он нужен для обработки видео.
echo       ClipFactory может скачать бесплатную сборку FFmpeg - gyan.dev, около 90 МБ -
echo       в папку tools\ffmpeg. Системные настройки при этом не меняются.
echo.
choice /c YN /n /m "      Скачать FFmpeg автоматически? [Y - да, N - нет]: "
if errorlevel 2 goto :ffmpeg_manual
echo       Скачивание FFmpeg...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;" ^
  "$zip=Join-Path $env:TEMP 'clipfactory-ffmpeg.zip'; $dst=Join-Path $env:TEMP 'clipfactory-ffmpeg';" ^
  "Invoke-WebRequest -Uri 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile $zip -UseBasicParsing;" ^
  "if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }; Expand-Archive $zip -DestinationPath $dst;" ^
  "$bin=Get-ChildItem $dst -Recurse -Filter ffmpeg.exe | Select-Object -First 1;" ^
  "New-Item -ItemType Directory -Force -Path 'tools\ffmpeg\bin' | Out-Null;" ^
  "Copy-Item (Join-Path $bin.DirectoryName '*.exe') 'tools\ffmpeg\bin' -Force; Remove-Item $zip,$dst -Recurse -Force"
if not exist "tools\ffmpeg\bin\ffmpeg.exe" goto :ffmpeg_manual
echo       FFmpeg установлен в tools\ffmpeg\bin
goto :ffmpeg_done

:ffmpeg_manual
echo.
echo   ВНИМАНИЕ: FFmpeg не установлен. Без него ClipFactory не сможет обрабатывать видео.
echo   Установите вручную одним из способов:
echo     1. В командной строке: winget install Gyan.FFmpeg  - затем перезапустите setup.bat
echo     2. Скачайте архив с https://www.gyan.dev/ffmpeg/builds/ - release essentials -
echo        и скопируйте ffmpeg.exe и ffprobe.exe в папку tools\ffmpeg\bin
echo.

:ffmpeg_done
echo.

rem ---------- 5. Folders and database ----------
echo [5/5] Подготовка папок и базы данных...
".venv\Scripts\python.exe" -c "from backend.main import init_app_state; init_app_state(); print('      Готово.')"
if errorlevel 1 goto :init_failed
if not exist ".env" if exist ".env.example" copy ".env.example" ".env" >nul

echo.
echo ==========================================================
echo   Установка завершена.
echo   Запускайте программу двойным щелчком по файлу start.bat
echo.
echo   При первом распознавании речи будет один раз скачана
echo   модель Whisper - нужен интернет. Дальше всё работает
echo   локально и бесплатно.
echo ==========================================================
pause
exit /b 0

:no_python
echo.
echo   ОШИБКА: не найден Python 3.11 или новее.
echo   Скачайте Python с https://www.python.org/downloads/windows/
echo   При установке обязательно отметьте галочку "Add python.exe to PATH".
echo   После установки запустите setup.bat ещё раз.
pause
exit /b 1

:venv_failed
echo.
echo   ОШИБКА: не удалось создать виртуальное окружение.
echo   Переустановите Python с официального сайта и повторите попытку.
pause
exit /b 1

:pip_failed
echo.
echo   ОШИБКА: не удалось установить библиотеки Python.
echo   Проверьте подключение к интернету и запустите setup.bat ещё раз.
pause
exit /b 1

:init_failed
echo.
echo   ОШИБКА: не удалось подготовить базу данных. Подробности выше.
pause
exit /b 1
