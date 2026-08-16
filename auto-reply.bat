@echo off
chcp 936 >nul
title AI Auto Reply
color 0A

echo.
echo  ============================================
echo        WeChat Auto Reply Bot
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
:: Search common paths
for %%d in ("C:\Program Files\Python312" "C:\Python312" "%LOCALAPPDATA%\Programs\Python\Python312") do (
    if exist "%%~d\python.exe" (
        set PYTHON_CMD="%%~d\python.exe"
        set "PATH=%%~d;%%~d\Scripts;%PATH%"
        goto :found_python
    )
)

echo  [ERROR] Python not found!
echo  Please install Python 3.12+ and add to PATH
pause
exit /b 1

:found_python
echo  Python: %PYTHON_CMD%
echo.

set OLLAMA_MODELS=D:\AI\models

:: Change to script directory
cd /d "%~dp0"

echo  [INFO] Make sure WeChat is running and logged in
echo.
echo  --------------------------------------------
echo    Select model:
echo  --------------------------------------------
echo.
echo    1. Remote API (recommended)
echo    2. Local Ollama
echo.

set /p CHOICE=  Select [1-2]:

if "%CHOICE%"=="1" goto remote_menu
if "%CHOICE%"=="2" goto local_start
goto local_start

:remote_menu
echo.
echo  --------------------------------------------
echo    Remote models (apiyihe)
echo  --------------------------------------------
echo.
echo    1. gemini-3.5-flash  (fast)
echo    2. glm-5.1           (fast)
echo    3. qwen3.7-max       (strong)
echo.

set MODEL_NAME=gemini-3.5-flash

set /p MODEL_CHOICE=  Select model [1-3]:

if "%MODEL_CHOICE%"=="1" set MODEL_NAME=gemini-3.5-flash
if "%MODEL_CHOICE%"=="2" set MODEL_NAME=glm-5.1
if "%MODEL_CHOICE%"=="3" set MODEL_NAME=qwen3.7-max

echo.
echo  -^> Remote API: %MODEL_NAME%
echo.
%PYTHON_CMD% auto_reply.py --remote %MODEL_NAME%
goto :end

:local_start
echo.
echo  -^> Local Ollama
echo.
%PYTHON_CMD% auto_reply.py
goto :end

:end
echo.
echo  [INFO] Bot exited.
pause
