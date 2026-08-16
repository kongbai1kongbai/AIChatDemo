@echo off
chcp 936 >nul
title 停止 AI 服务
echo ============================================
echo   停止所有 AI 服务
echo ============================================
echo.

:: 停止 Open WebUI (端口 3000)
echo [1/3] 停止 Open WebUI (端口 3000)...
set FOUND=0
for /f "skip=4 tokens=5" %%a in ('netstat -ano ^| findstr ":3000" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1 && set FOUND=1
)
if %FOUND%==1 (echo       已停止) else (echo       未在运行)

:: 停止 MCPo (端口 8000)
echo [2/3] 停止 MCPo 代理 (端口 8000)...
set FOUND=0
for /f "skip=4 tokens=5" %%a in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1 && set FOUND=1
)
if %FOUND%==1 (echo       已停止) else (echo       未在运行)

:: 停止 Ollama
echo [3/3] 停止 Ollama...
taskkill /F /IM "ollama app.exe" >nul 2>&1
taskkill /F /IM "ollama.exe" >nul 2>&1
echo       已停止
echo.
echo ============================================
echo   所有服务已停止
echo ============================================
pause
