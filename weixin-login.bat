@echo off
chcp 936 >nul
title 微信 MCP 登录
echo ============================================
echo   微信 MCP 登录认证
echo   请在弹出的二维码中用微信扫码
echo ============================================
echo.

set WEIXIN_MCP_DIR=D:\AI\workspace\weixin-mcp-data
set PATH=D:\software\nodejs;%APPDATA%\npm;%PATH%

npx weixin-mcp login

echo.
echo ============================================
echo   登录完成！凭据已保存到 D:\AI\weixin-mcp-data
echo ============================================
pause
