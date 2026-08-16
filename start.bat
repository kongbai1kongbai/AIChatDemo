@echo off
chcp 936 >nul
setlocal enabledelayedexpansion
title AI Station

:: ============================================
:: ģ��ѡ��
:: ============================================
echo.
echo  ================================================
echo    AI Station
echo  ================================================
echo.
echo    1. Զ�̴�ģ�� API (�Ƽ�, ��������)
echo    2. ���ش�ģ�� (Ollama qwen3:8b)
echo.

set USE_MODE=local
set /p MODE_CHOICE=  ��ѡ�� [1-2]:

if "%MODE_CHOICE%"=="1" goto remote_select
goto local_select

:remote_select
set USE_MODE=remote
echo.
echo  -----------------------------------------------
echo    Զ��ģ�� (apiyihe)
echo  -----------------------------------------------
echo.
echo    1. gemini-3.5-flash  (����)
echo    2. glm-5.1           (����)
echo    3. qwen3.7-max       (��ǿ)
echo.

set MODEL_NAME=gemini-3.5-flash
set /p MODEL_CHOICE=  ѡ��ģ�� [1-3]:

if "%MODEL_CHOICE%"=="1" set MODEL_NAME=gemini-3.5-flash
if "%MODEL_CHOICE%"=="2" set MODEL_NAME=glm-5.1
if "%MODEL_CHOICE%"=="3" set MODEL_NAME=qwen3.7-max

echo.
echo  -^> Զ��ģ��: %MODEL_NAME%
goto env_setup

:local_select
echo.
echo  -^> ����ģ��: Ollama qwen3:8b
goto env_setup

:env_setup
echo.
echo  ================================================
if "%USE_MODE%"=="remote" (
    echo    AI Station - Remote API [%MODEL_NAME%]
) else (
    echo    AI Station - Ollama + MCPo + Open WebUI
    echo    Model : qwen3:8b
    echo    GPU   : RTX 4070 Laptop 8GB VRAM
)
echo  ================================================
echo.

:: === Environment ===
for %%I in ("%~dp0..") do set "AI_ROOT=%%~fI"
set "MODEL_CONFIG=%AI_ROOT%\workspace\model_config.json"
set OLLAMA_MODELS=%AI_ROOT%\models
set OLLAMA_BASE_URL=http://127.0.0.1:11434
set DATA_DIR=%AI_ROOT%\workspace\open-webui-data
set HF_ENDPOINT=https://hf-mirror.com
set WEIXIN_MCP_DIR=%AI_ROOT%\workspace\weixin-mcp-data
set PATH=D:\software\nodejs;%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%LOCALAPPDATA%\Programs\Ollama;%APPDATA%\npm;%PATH%

:: Remote API env vars for Open WebUI
if "%USE_MODE%"=="remote" (
    set "OPENAI_API_BASE_URL="
    set "OPENAI_API_KEY="
    if not exist "%MODEL_CONFIG%" (
        echo    [FAIL] Missing model config: %MODEL_CONFIG%
        echo    Copy model_config.json.example to the workspace and configure api_base/api_key.
        pause
        exit /b 1
    )
    for /f "usebackq tokens=1,* delims==" %%E in (`powershell -NoProfile -Command "$modelConfig = Get-Content -Raw -LiteralPath '%MODEL_CONFIG%' | ConvertFrom-Json; if ($modelConfig.api_base) { 'OPENAI_API_BASE_URL=' + $modelConfig.api_base }; if ($modelConfig.api_key) { 'OPENAI_API_KEY=' + $modelConfig.api_key }"`) do set "%%E=%%F"
    if not defined OPENAI_API_BASE_URL (
        echo    [FAIL] api_base is missing in %MODEL_CONFIG%
        pause
        exit /b 1
    )
    if not defined OPENAI_API_KEY (
        echo    [FAIL] api_key is missing in %MODEL_CONFIG%
        pause
        exit /b 1
    )
)

:: ============================================
:: [1/3] Ollama (������ģʽ)
:: ============================================
if "%USE_MODE%"=="remote" goto skip_ollama

echo  [1/3] Ollama (11434)
echo  -----------------------------------------------

:: Always restart Ollama to ensure OLLAMA_MODELS is correct
taskkill /F /IM "ollama app.exe" >nul 2>&1
taskkill /F /IM "ollama.exe" >nul 2>&1
timeout /t 2 /nobreak >nul

echo    Starting ollama serve...
echo.
start /B ollama serve
echo.

:: Wait for Ollama ready
set WAIT=0
:wait_ollama
timeout /t 1 /nobreak >nul
set /a WAIT+=1
curl -s -o nul http://127.0.0.1:11434 >nul 2>&1
if not errorlevel 1 goto :ollama_ready
if !WAIT! geq 30 goto :ollama_fail
goto :wait_ollama

:ollama_fail
echo    [FAIL] Ollama did not start within 30s
pause
exit /b 1

:ollama_ready
echo    Ollama ready in !WAIT!s

:: List models via API
echo    Models:
for /f "delims=" %%j in ('curl -s http://127.0.0.1:11434/api/tags 2^>nul') do (
    set "JSON=%%j"
)
echo      !JSON!
echo.
goto :mcpo_start

:skip_ollama
echo  [1/3] Ollama (������, ʹ��Զ��API)
echo  -----------------------------------------------
echo    Զ��API: %OPENAI_API_BASE_URL%
echo    ģ��: %MODEL_NAME%
echo.

:: ============================================
:: [2/3] MCPo
:: ============================================
:mcpo_start
echo  [2/3] MCPo proxy (8000)
echo  -----------------------------------------------

:: Check if already running
curl -s -o nul http://127.0.0.1:8000 >nul 2>&1
if not errorlevel 1 (
    echo    Already running
    goto :mcpo_done
)

start /B cmd /c "mcpo --config D:\AI\code\mcp\config.json --port 8000 >nul 2>&1"

set WAIT=0
:wait_mcpo
timeout /t 1 /nobreak >nul
set /a WAIT+=1
curl -s -o nul http://127.0.0.1:8000 >nul 2>&1
if not errorlevel 1 goto :mcpo_ready
if !WAIT! geq 30 goto :mcpo_warn
goto :wait_mcpo

:mcpo_warn
echo    [WARN] MCPo not ready, WeChat features unavailable
goto :mcpo_done

:mcpo_ready
echo    MCPo ready in !WAIT!s

:mcpo_done
echo.

:: ============================================
:: [3/3] Open WebUI
:: ============================================
echo  [3/3] Open WebUI (3000)
echo  -----------------------------------------------

:: Check if already running
curl -s -o nul http://127.0.0.1:3000 >nul 2>&1
if not errorlevel 1 (
    echo    Already running
    goto :webui_done
)

start /B cmd /c "open-webui serve --host 127.0.0.1 --port 3000 >nul 2>&1"
echo    Initializing (first launch may take a while)...

set WAIT=0
:wait_webui
timeout /t 1 /nobreak >nul
set /a WAIT+=1
curl -s -o nul http://127.0.0.1:3000 >nul 2>&1
if not errorlevel 1 goto :webui_ready
if !WAIT! geq 120 goto :webui_fail
goto :wait_webui

:webui_fail
echo    [FAIL] Open WebUI did not start within 120s
echo           First launch downloads embedding model (~80MB)
pause
exit /b 1

:webui_ready
echo    Open WebUI ready in !WAIT!s

:webui_done
echo.

:: ============================================
:: All ready
:: ============================================
start "" http://127.0.0.1:3000

echo  ================================================
echo    ALL SERVICES READY
echo  ================================================
echo.
echo    Open WebUI  :  http://127.0.0.1:3000
echo    MCPo API    :  http://127.0.0.1:8000/weixin/docs
if "%USE_MODE%"=="local" (
    echo    Ollama API  :  http://127.0.0.1:11434
) else (
    echo    Remote API  :  !OPENAI_API_BASE_URL! [%MODEL_NAME%]
)
echo.
echo    WeChat MCP config (Open WebUI admin):
echo      http://127.0.0.1:8000/weixin
echo.
if "%USE_MODE%"=="local" (
    echo    Ollama inference log below:
)
echo  -----------------------------------------------
echo.

:: ============================================
:: Shutdown
:: ============================================
:shutdown_wait
echo.
echo  Press any key to STOP all services...
pause >nul

echo.
echo  ================================================
echo    Shutting down...
echo  ================================================
if "%USE_MODE%"=="local" (
    taskkill /F /IM "ollama app.exe" >nul 2>&1
    taskkill /F /IM "ollama.exe" >nul 2>&1
    echo    [OK] Ollama stopped
)
for /f "skip=4 tokens=5" %%a in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do taskkill /F /PID %%a >nul 2>&1
echo    [OK] MCPo stopped
for /f "skip=4 tokens=5" %%a in ('netstat -ano ^| findstr ":3000" ^| findstr "LISTENING"') do taskkill /F /PID %%a >nul 2>&1
echo    [OK] Open WebUI stopped
echo.
echo    All services stopped. Press any key to exit.
pause >nul
