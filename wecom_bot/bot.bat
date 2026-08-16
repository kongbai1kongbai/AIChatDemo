@echo off
chcp 936 >nul
title WeCom Intelligent Bot
color 0E

echo.
echo  ============================================
echo    WeCom Intelligent Bot (WebSocket Mode)
echo  ============================================
echo.

:: Auto-detect Python
set PYTHON_CMD=
for %%p in (python py python3) do (
    where %%p >nul 2>&1
    if not errorlevel 1 (
        set PYTHON_CMD=%%p
        goto :found_python
    )
)
for %%d in ("C:\Program Files\Python312" "C:\Python312" "%LOCALAPPDATA%\Programs\Python\Python312") do (
    if exist "%%~d\python.exe" (
        set PYTHON_CMD="%%~d\python.exe"
        set "PATH=%%~d;%%~d\Scripts;%PATH%"
        goto :found_python
    )
)

echo  [ERROR] Python not found!
pause
exit /b 1

:found_python
echo  Python: %PYTHON_CMD%

:: Start bot (config loaded from workspace/wecom_bot_config.json)
cd /d "%~dp0"
echo  Starting WeCom Intelligent Bot (WebSocket)...
echo  Press Ctrl+C to stop.
echo.
%PYTHON_CMD% bot.py
pause
