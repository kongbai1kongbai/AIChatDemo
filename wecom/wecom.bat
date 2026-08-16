@echo off
chcp 936 >nul
title WeCom AI Bot
color 0B

echo.
echo  ============================================
echo    WeCom AI Bot (Enterprise WeChat)
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

:: Start server (config loaded from workspace/wecom_config.json)
cd /d "%~dp0"
echo  Starting WeCom callback server...
echo  Press Ctrl+C to stop.
echo.
%PYTHON_CMD% server.py
pause
