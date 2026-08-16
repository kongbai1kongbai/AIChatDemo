# WeCom AI Bot - Enterprise WeChat Self-Built App

AI auto-reply bot for Enterprise WeChat (WeCom) groups and private chats.

## Setup Guide

### 1. Register WeCom Organization

1. Visit https://work.weixin.qq.com and register (individual registration is free)
2. After login, go to **"My Enterprise" (我的企业)** > **"Enterprise Info" (企业信息)**
3. Copy your **CorpID** (e.g., `ww1234567890abcdef`)

### 2. Create Self-Built App

1. Go to **"App Management" (应用管理)** > **"Self-built" (自建)** > **"Create App" (创建应用)**
2. Fill in: name, description, visible scope (select departments/users who can use the bot)
3. After creation, note:
   - **AgentId** (numeric, e.g., `1000002`)
   - **Secret** (click to view/copy)

### 3. Configure Callback URL

1. In your app settings, find **"Receive Messages" (接收消息)** > **"Set API Receive" (设置API接收)**
2. Fill in:
   - **URL**: `http://YOUR_PUBLIC_IP:8080/callback`
   - **Token**: click "Random Get" (随机获取), copy the value
   - **EncodingAESKey**: click "Random Get" (随机获取), copy the 43-char string
3. **Don't save yet** - start the server first (step 5)

### 4. Set Trusted IP

1. In app settings, find **"Enterprise Trusted IP" (企业可信IP)**
2. Add your server's public IP address
3. Without this, the bot cannot send messages via API

### 5. Configure and Start

```bash
# Copy config template
cd D:\AI\code\wecom
copy config.json.example config.json

# Edit config.json with your values:
#   corp_id: from step 1
#   corp_secret: from step 2
#   agent_id: from step 2
#   token: from step 3
#   encoding_aes_key: from step 3

# Install dependencies (first time only)
pip install "wechatpy[cryptography]" flask

# Start the server
python server.py
# or double-click wecom.bat
```

### 6. Complete Callback Verification

1. Now go back to WeCom admin and **save** the callback URL (step 3)
2. You should see `[callback] URL verification OK` in the server console
3. If verification fails, check that:
   - Your server is running and accessible from the internet
   - Token and EncodingAESKey match exactly

### 7. Test

1. Open WeCom app (mobile or desktop)
2. Find your self-built app in the work panel
3. Send a message - you should get an AI reply within a few seconds

## Local Development (ngrok)

If testing locally without a public IP:

```bash
# Terminal 1: start the bot
python server.py

# Terminal 2: create tunnel with ngrok
ngrok http 8080

# Copy the ngrok URL (e.g., https://abc123.ngrok.io)
# Set callback URL to: https://abc123.ngrok.io/callback
```

Alternative tunnels: cpolar, natapp, frp.

## Architecture

```
WeCom Cloud
    |
    | HTTPS POST (encrypted XML)
    v
Flask Server (server.py)
    |
    | Decrypt + parse message
    v
Handler (handler.py)
    |
    | Build conversation history
    v
AI Engine (shared/ai_engine.py)
    |
    | Call remote API / Ollama
    | + search fallback if needed
    v
WeCom API (send message)
    |
    | HTTPS POST
    v
WeCom Cloud -> User
```

## Config Fields

| Field | Description |
|-------|-------------|
| `corp_id` | Enterprise ID from WeCom admin |
| `corp_secret` | App secret from app details |
| `agent_id` | Numeric agent ID from app details |
| `token` | Callback verification token |
| `encoding_aes_key` | 43-char key for message encryption |
| `host` | Server bind address (default: 0.0.0.0) |
| `port` | Server port (default: 8080) |

## Troubleshooting

- **40001 error**: Invalid access_token - check corp_id and corp_secret
- **60020 error**: IP not in whitelist - add your server IP to trusted IPs
- **Callback verification fails**: Check token/encoding_aes_key match admin console
- **No reply**: Check server logs, verify model_config.json has valid API key
