# AIChatDemo 使用说明

本文档说明如何安装、配置、启动和排查当前仓库中的 AI 自动回复系统。

## 1. 运行前准备

### 1.1 推荐目录

项目默认按以下目录运行：

```text
D:\AI
├── code       # 源码仓库
├── workspace  # 运行配置、日志、输出文件
├── models     # Ollama 模型
└── whisper-base-model
```

如果移动目录，需要检查 `shared/config.py` 和 `auto_reply.py` 中对 `workspace` 的推导是否仍然正确。

### 1.2 Python 环境

推荐 Python 3.12+。

基础依赖：

```bash
pip install requests
```

个人微信自动回复：

```bash
pip install wxautoz
```

企业微信自建应用：

```bash
pip install flask wechatpy
```

企业微信智能机器人：

```bash
pip install wecom-aibot-sdk
```

搜索增强：

```bash
pip install ddgs
```

企业微信客服语音识别：

```bash
pip install faster-whisper imageio-ffmpeg
```

默认可通过 `model_config.json` 使用 OpenAI 兼容语音转写接口：

```json
{
  "voice_asr_provider": "api",
  "voice_asr_model": "gpt-4o-transcribe",
  "voice_asr_api_base": "",
  "voice_asr_api_key": "",
  "voice_asr_language": "zh",
  "voice_asr_timeout": 90,
  "voice_asr_fallback_local": true
}
```

`voice_asr_api_base` 和 `voice_asr_api_key` 留空时复用现有的 `api_base` 和 `api_key`。AMR 等微信语音格式会先转换为 16k 单声道 WAV。API 调用失败时，默认回退本地 faster-whisper。

### 1.3 本地模型

如果使用 Ollama：

```bash
ollama pull qwen3:8b
```

默认 Ollama 地址为：

```text
http://127.0.0.1:11434
```

如果使用客服语音识别，需要本地 Whisper 模型目录：

```text
D:\AI\whisper-base-model
```

可使用工具脚本下载或打包：

```bash
python tools\download_whisper_model.py --pack
```

## 2. 初始化配置

运行时配置应放在 `D:\AI\workspace`。

### 2.1 创建 workspace

如果目录不存在，先创建：

```bash
mkdir D:\AI\workspace
```

### 2.2 模型配置

复制模板：

```bash
copy D:\AI\code\model_config.json.example D:\AI\workspace\model_config.json
```

远程 API 示例：

```json
{
  "mode": "remote",
  "model": "gemini-3.5-flash",
  "api_base": "https://example.com/v1",
  "api_key": "sk-your-api-key",
  "remote_provider": "OpenAI Compatible",
  "bocha_api_key": "sk-your-bocha-key",
  "remote_max_tokens": 8192,
  "ollama_max_tokens": 8192,
  "qodercli_model": "Qwen3.7-Max",
  "qodercli_max_tokens": 8192,
  "qodercli_permission_mode": "bypass"
}
```

本地 Ollama 示例：

```json
{
  "mode": "ollama",
  "model": "qwen3:8b",
  "ollama_max_tokens": 8192
}
```

QoderCLI 示例：

```json
{
  "mode": "qodercli",
  "qodercli_model": "Qwen3.7-Max",
  "qodercli_max_tokens": 8192,
  "qodercli_permission_mode": "ask",
  "workspace_dir": "D:\\AI\\workspace"
}
```

`qodercli_permission_mode` 建议：

- `ask`：先向用户说明计划，用户确认后执行，适合客服场景。
- `bypass`：自动允许执行，适合完全可信的本地自动化场景。

### 2.3 自动回复配置

复制模板：

```bash
copy D:\AI\code\auto_reply_config.json D:\AI\workspace\auto_reply_config.json
```

常用字段：

```json
{
  "enabled": true,
  "dm_enabled": true,
  "dm_system_prompt": "你是一个友好、专业的聊天助手。回复简洁自然。",
  "dm_blacklist": [],
  "dm_whitelist_mode": false,
  "dm_whitelist": [],
  "group_enabled": true,
  "group_system_prompt": "你是一个群聊助手。只在被提及时回复，回复简短。",
  "group_blacklist": [],
  "group_whitelist_mode": false,
  "group_whitelist": [],
  "group_trigger_words": [],
  "group_mention_names": ["@机器人", "@bot", "@助手"],
  "reply_delay": 2,
  "log_file": "auto_reply.log"
}
```

配置说明：

- `enabled=false` 会暂停整体回复。
- 私聊黑白名单按聊天名称匹配。
- 群聊默认需要 @ 名称或触发词才回复。
- 修改后多数服务会在下一次处理消息时重新读取配置。

## 3. 启动个人微信自动回复

### 3.1 前置条件

- 安装并登录微信 PC 版。
- 微信窗口可被 UI 自动化访问。
- 已安装 `wxautoz`。
- 已配置 `D:\AI\workspace\model_config.json` 和 `auto_reply_config.json`。

### 3.2 双击启动

在资源管理器中双击：

```text
D:\AI\code\auto-reply.bat
```

启动后按菜单选择：

- 远程大模型 API。
- 本地 Ollama。
- Qoder 代理回复。
- 直接使用上次配置。

### 3.3 命令行启动

进入源码目录：

```bash
cd /d D:\AI\code
```

使用上次配置：

```bash
python auto_reply.py --last
```

使用远程 API 的上次模型：

```bash
python auto_reply.py --remote
```

指定远程模型：

```bash
python auto_reply.py --remote qwen3.7-max
```

### 3.4 回复规则

个人微信：

- 私聊：默认回复所有非系统、非自己发送的消息。
- 群聊：默认仅在被 @ 或命中 `group_trigger_words` 时回复。
- 系统会话如微信支付、腾讯新闻、服务通知等会被跳过。
- 每个会话保留最近 10 轮上下文。

## 4. 启动企业微信自建应用

### 4.1 创建配置

复制模板：

```bash
copy D:\AI\code\wecom\config.json.example D:\AI\workspace\wecom_config.json
```

填写：

```json
{
  "corp_id": "YOUR_CORP_ID",
  "corp_secret": "YOUR_APP_SECRET",
  "agent_id": 1000002,
  "token": "YOUR_CALLBACK_TOKEN",
  "encoding_aes_key": "YOUR_43_CHAR_ENCODING_AES_KEY",
  "host": "0.0.0.0",
  "port": 8080
}
```

### 4.2 启动服务

```bash
cd /d D:\AI\code\wecom
python server.py
```

或指定端口：

```bash
python server.py --port 8080
```

### 4.3 配置企业微信后台

在企业微信自建应用中配置回调 URL：

```text
http://你的公网地址:8080/callback
```

企业微信需要能访问该地址。本地调试时可使用内网穿透。

## 5. 启动企业微信智能机器人

### 5.1 创建配置

复制模板：

```bash
copy D:\AI\code\wecom_bot\config.json.example D:\AI\workspace\wecom_bot_config.json
```

填写：

```json
{
  "bot_id": "YOUR_BOT_ID",
  "secret": "YOUR_BOT_SECRET"
}
```

### 5.2 启动服务

```bash
cd /d D:\AI\code\wecom_bot
python bot.py
```

智能机器人通过 WebSocket 连接企业微信服务器，通常不需要公网回调地址。

### 5.3 图片处理

用户发送图片后，机器人会先回复“图片已收到”，然后等待用户继续说明处理需求。用户下一条文字会和图片一起传给 AI 引擎。

## 6. 启动企业微信客服

### 6.1 创建配置

复制模板：

```bash
copy D:\AI\code\customer_service\config.json.example D:\AI\workspace\customer_service_config.json
```

填写：

```json
{
  "corp_id": "YOUR_CORP_ID",
  "corp_secret": "YOUR_CORP_SECRET",
  "token": "YOUR_CALLBACK_TOKEN",
  "encoding_aes_key": "YOUR_43_CHAR_AES_KEY",
  "host": "0.0.0.0",
  "port": 8081,
  "open_kfid": "",
  "forward_chatid": "",
  "forward_userid": "",
  "agent_id": ""
}
```

字段说明：

- `open_kfid` 可留空，系统会尝试自动发现第一个客服账号。
- `forward_chatid` 填写后，会把客户问答转发到内部群。
- `forward_userid` 和 `agent_id` 填写后，会优先把客户问答转发给指定成员。

### 6.2 启动服务

```bash
cd /d D:\AI\code\customer_service
python server.py
```

或指定端口：

```bash
python server.py --port 8081
```

### 6.3 配置客服回调

在企业微信客服后台配置回调 URL：

```text
http://你的公网地址:8081/callback
```

服务收到通知后会调用 `kf/sync_msg` 获取真实消息内容。

### 6.4 支持的消息类型

| 类型 | 处理方式 |
| --- | --- |
| 文本 | 直接进入 AI 回复 |
| 图片 | 下载后暂存，等待用户文字说明，再交给 AI |
| 语音 | 转换为 WAV 后优先调用语音转写 API，失败时回退 faster-whisper，再进入 AI 回复 |
| 文件 | 下载后暂存，等待用户处理指令，适合配合 QoderCLI |
| 事件 | 打印日志，默认不回复 |

## 7. 搜索与天气

远程 API 模式下，系统会自动增强实时问题：

- 模型主动输出 `[SEARCH: 关键词]`。
- 模型回答“无法联网”“查不到”等回避内容。
- 用户问题包含今天、最新、天气、新闻、汇率、股价等实时意图。

建议在 `model_config.json` 中配置：

```json
{
  "bocha_api_key": "sk-your-bocha-key"
}
```

未配置博查时会回退到 DDG/Bing、Bing China 和百度抓取。

## 8. Qoder 相关模式

### 8.1 个人微信 Qoder 中继

个人微信 `auto_reply.py` 的 Qoder 模式会启动本地中继：

```text
http://127.0.0.1:11435
```

`qoder_bridge.py` 会轮询待处理请求，调用 Ollama 生成回复后提交回中继。

通常通过 `auto-reply.bat` 菜单选择即可。

### 8.2 QoderCLI 模式

企业微信相关入口可使用 `shared.ai_engine` 的 QoderCLI 模式：

```json
{
  "mode": "qodercli",
  "qodercli_model": "Qwen3.7-Max",
  "qodercli_permission_mode": "ask",
  "workspace_dir": "D:\\AI\\workspace"
}
```

适合处理文件、生成图片或生成文档。客服模块会识别回复中的本地图片或文档路径，上传并发送给用户。

## 9. 常用运维操作

### 9.1 查看健康检查

企业微信自建应用：

```text
http://127.0.0.1:8080/health
```

企业微信客服：

```text
http://127.0.0.1:8081/health
```

个人微信 Qoder 中继：

```text
http://127.0.0.1:11435/health
```

### 9.2 查看日志

默认日志：

```text
D:\AI\workspace\auto_reply.log
```

其他服务主要输出到启动窗口。建议生产运行时用进程管理器或脚本重定向日志。

### 9.3 停止服务

个人微信自动回复和 Python 服务可在窗口中按 `Ctrl+C` 停止。

也可使用：

```text
D:\AI\code\stop.bat
```

## 10. 排错指南

### 10.1 微信连接失败

检查：

- 微信 PC 版是否已启动并登录。
- 微信版本是否兼容 `wxautoz`。
- 是否以合适权限运行终端或脚本。
- 微信窗口是否被远程桌面、锁屏、最小化等状态影响。

### 10.2 Ollama 无法调用

检查：

- Ollama 是否正在运行。
- `http://127.0.0.1:11434/api/tags` 是否可访问。
- `model_config.json` 中模型名是否已安装。

### 10.3 远程 API 报错

检查：

- `api_base` 是否包含 `/v1`，且能访问 `/chat/completions`。
- `api_key` 是否有效。
- `model` 是否是服务商支持的模型名。
- `remote_max_tokens` 是否过小。

### 10.4 搜索没有结果

检查：

- `bocha_api_key` 是否配置。
- 当前网络是否能访问搜索后端。
- 是否安装 `ddgs`。
- 搜索问题是否过短或缺少关键词。

### 10.5 企业微信回调验证失败

检查：

- 企业微信后台的 Token、EncodingAESKey、CorpID 是否与配置一致。
- 回调 URL 是否公网可达。
- 端口是否被防火墙拦截。
- 服务是否监听 `0.0.0.0`。

### 10.6 客服没有收到真实消息

检查：

- 回调是否只收到通知但没有调用成功 `kf/sync_msg`。
- `corp_secret` 是否拥有客服接口权限。
- `open_kfid` 是否正确。
- 启动时 drain 会丢弃旧消息，只处理启动后的新消息。

### 10.7 图片或文件处理失败

检查：

- 模型模式是否支持图片或附件。
- 远程模型是否支持 multimodal。
- QoderCLI 是否可执行。
- 临时文件是否被安全软件删除。
- 回复中的文件路径是否为真实存在的绝对路径。

## 11. 推荐启动组合

个人使用：

1. 启动 Ollama 或配置远程 API。
2. 登录微信 PC。
3. 双击 `auto-reply.bat`。

企业内部机器人：

1. 配置远程 API 或 Ollama。
2. 配置 `wecom_bot_config.json`。
3. 启动 `wecom_bot\bot.py`。

客服场景：

1. 配置 `customer_service_config.json`。
2. 模型建议使用 `remote` 或 `qodercli`。
3. 如果要处理文件，把 `qodercli_permission_mode` 设为 `ask`。
4. 启动 `customer_service\server.py` 并配置公网回调。

## 12. 文档与代码维护建议

- 新增入口时优先复用 `shared.ai_engine.generate_reply()`。
- 新增配置字段时同步更新示例配置和本文档。
- 涉及密钥的真实配置只放 `D:\AI\workspace`。
- 对 `.bat` 文件编辑时注意 Windows 控制台编码。
- 对搜索、媒体上传、客服同步等外部接口变更要做真实环境验证。
