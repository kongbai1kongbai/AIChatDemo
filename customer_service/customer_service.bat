@echo off
REM WeChat Customer Service AI Server Launcher
REM Usage: customer_service.bat [port]

set PATH=D:\software\nodejs;%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%LOCALAPPDATA%\Programs\Ollama;%APPDATA%\npm;%PATH%

set SCRIPT_DIR=%~dp0
set CS_SERVER=%SCRIPT_DIR%server.py

echo ============================================
echo   WeChat Customer Service AI Server
echo ============================================
echo.

REM Check Python
where python >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Python not found. Please install Python 3.12+
    pause
    exit /b 1
)

REM Check config file
if not exist "%SCRIPT_DIR%..\..\workspace\customer_service_config.json" (
    echo [ERROR] Config not found: workspace\customer_service_config.json
    echo         Copy customer_service\config.json.example to workspace\customer_service_config.json
    echo         Then fill in your corp_id, corp_secret, token, and encoding_aes_key.
    pause
    exit /b 1
)

REM Optional port override
if not "%~1"=="" (
    echo Starting on port %1...
    python "%CS_SERVER%" --port %1
) else (
    echo Starting with default port...
    python "%CS_SERVER%"
)

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Server exited with error code %ERRORLEVEL%
    pause
)
