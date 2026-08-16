# AIChatDemo - AI 自动回复系统

基于 wxautoz UI 自动化直连微信 PC 客户端，支持本地 Ollama、远程 API、企业微信自建应用和企业微信智能机器人四种接入方式的 AI 自动回复系统。内置博查搜索 + 智能搜索触发 + 天气 API 集成。

## 系统架构

```
微信 PC 客户端 (3.9.x)
    ↕ UI 自动化 (wxautoz)
auto_reply.py (Python)
    ├── 本地模式 → Ollama (端口 11434, qwen3:8b)
    ├── 远程模式 → OpenAI 兼容 API → gemini / glm / qwen
    └── Qoder 模式 → Qoder 中继 (端口 11435)
                                    ↕
                          QoderWork cron 长轮询

企业微信自建应用 (HTTP 回调)
    ↕ Flask 服务器 (端口 8080)
wecom/server.py → wecom/handler.py → shared/ai_engine.py
    └── 需要公网 IP 或内网穿透

企业微信智能机器人 (WebSocket 长连接)
    ↕ wecom_aibot_sdk.WSClient
wecom_bot/bot.py → shared/ai_engine.py
    └── 无需公网 IP，直连 wss://openws.work.weixin.qq.com
```

## 功能特性

- **多模型后端**：本地 Ollama（离线推理）、远程 OpenAI 兼容 API、Qoder 中继三种模式自由切换
- **企业微信集成**：自建应用（HTTP 回调）和智能机器人（WebSocket 长连接）两套独立实现
- **智能搜索系统**：
  - 博查搜索 API（国内最稳定） → DuckDuckGo/Bing → cn.bing.com 抓取 → 百度抓取，四级瀑布回退
  - 模型可主动请求搜索：输出 `[SEARCH: 关键词]` 标记，系统自动搜索
  - 回避回复自动检测：模型说"查不到"时自动搜索重试（最多 2 轮）
  - 强化重试提示词：搜索结果以结构化格式注入，强制模型使用
- **天气 API 集成**：博查网络搜索优先（国内稳定），wttr.in 作为补充
- **Tool call 防护**：三层检测自动清理 JSON 工具调用输出
- **微信自动化**：私聊自动回复、群聊 @回复
- **Qoder 中继**：ThreadingHTTPServer 桥接 QoderWork 定时任务

## 项目结构

```
D:\AI\
├── code/                          # 代码仓库 (git 管理)
│   ├── auto_reply.py              # 微信自动回复主程序（独立搜索实现）
│   ├── auto_reply_config.json     # 配置模板（黑白名单、人设提示）
│   ├── model_config.json.example  # 模型配置模板
│   ├── auto-reply.bat             # 启动自动回复（模型选择菜单）
│   ├── start.bat                  # AI Station 一键启动（Ollama + Open WebUI）
│   ├── stop.bat                   # 一键停止所有服务
│   ├── weixin-login.bat           # 微信扫码登录（weixin-mcp）
│   ├── setup.ps1                  # 一键部署脚本
│   ├── qoder_bridge.py            # Qoder 中继桥接脚本
│   ├── qoder_long_poll.py         # Qoder 长轮询脚本（供 cron 任务）
│   ├── qoder_responder.py         # Qoder 回复处理脚本
│   ├── shared/                    # 共享模块（wecom/wecom_bot 复用）
│   │   ├── config.py              # 配置加载器、路径常量
│   │   ├── ai_engine.py           # AI 回复引擎（远程 + Ollama + 智能搜索）
│   │   └── search_engine.py       # 搜索引擎（博查/Bing/百度 + 天气 + 回避检测）
│   ├── wecom/                     # 企业微信自建应用（HTTP 回调模式）
│   │   ├── server.py              # Flask 回调服务器
│   │   ├── handler.py             # 消息处理（wechatpy.enterprise）
│   │   └── wecom.bat              # 启动脚本
│   ├── wecom_bot/                 # 企业微信智能机器人（WebSocket 长连接）
│   │   ├── bot.py                 # 异步 WebSocket 客户端
│   │   └── bot.bat                # 启动脚本
│   ├── mcp/
│   │   └── config.json            # MCPo 配置
│   ├── 操作手册.md                # 使用指南
│   └── 技术实现文档.md            # 技术实现细节
│
└── workspace/                     # 运行时数据 (不纳入 git)
    ├── model_config.json          # 模型配置（含 API key、bocha_api_key）
    ├── auto_reply_config.json     # 自动回复运行配置
    ├── wecom_config.json          # 企业微信自建应用凭证
    ├── wecom_bot_config.json      # 企业微信智能机器人凭证
    ├── models/                    # Ollama 模型权重 (D:\AI\models)
    ├── open-webui-data/           # Open WebUI 数据库
    ├── auto_reply.log             # 运行日志
    └── contacts.json              # 联系人缓存
```

代码和运行时数据分离：`code/` 存放可版本控制的源码，`workspace/` 存放 API 密钥、模型权重、日志等运行时数据。`model_config.json` 不在 git 中，远程部署时需手动同步。

## 快速开始

### 环境要求

- Python 3.12+
- 微信 PC 版 3.9.x（用于 auto-reply 个人微信）
- Node.js（可选，用于 weixin-mcp）

### 安装依赖

```bash
# 核心依赖
pip install wxautoz requests

# 搜索增强（可选，博查 API 为主力后端）
pip install duckduckgo-search

# 企业微信（可选）
pip install wechatpy flask
pip install wecom-aibot-sdk  # 智能机器人
```

本地模式还需要 [Ollama](https://ollama.com)：

```bash
ollama pull qwen3:8b
```

### 配置模型

编辑 `workspace/model_config.json`：

```json
{
  "mode": "remote",
  "model": "qwen3.7-max",
  "api_base": "https://z.apiyihe.org/v1",
  "api_key": "sk-your-api-key",
  "remote_provider": "Gemini Flash (apiyihe)",
  "bocha_api_key": "sk-your-bocha-key"
}
```

`bocha_api_key` 为博查搜索 API 密钥（[申请地址](https://open.bochaai.com)），未配置时搜索自动回退到 DuckDuckGo/Bing。

### 启动自动回复

1. 确保微信 PC 版已启动并登录
2. 双击 `auto-reply.bat`，按菜单选择模型
3. 机器人开始监听并自动回复消息

命令行参数：

```bash
python auto_reply.py --remote qwen3.7-max   # 指定远程模型
python auto_reply.py --remote               # 使用上次远程模型
python auto_reply.py --last                 # 使用上次保存的配置
```

## 搜索系统

当远程模型无法回答实时性问题时，系统通过四条路径触发搜索：

1. **模型主动请求** — 模型输出 `[SEARCH: 关键词]` 标记，系统自动搜索后重新生成
2. **回避自动检测** — 模型回复包含"查不到""连不上网"等回避用语，自动触发搜索重试（最多 2 轮）
3. **关键词匹配** — 用户消息包含天气、新闻、今天等关键词，且回复过短时触发
4. **天气专属路径** — 天气查询优先走博查网络搜索（国内稳定），wttr.in API 作为补充

搜索瀑布：博查 API → DuckDuckGo/Bing → cn.bing.com 抓取 → 百度抓取

## 企业微信接入

### 自建应用（HTTP 回调模式）

适合有公网 IP 的场景。通过 Flask 服务器接收企业微信回调消息，使用 wechatpy.enterprise 库处理。

配置 `workspace/wecom_config.json`，双击 `wecom/wecom.bat` 启动。详见 [wecom/README.md](wecom/README.md)。

### 智能机器人（WebSocket 长连接）

无需公网 IP。通过 wecom_aibot_sdk 与企业微信服务器建立 WebSocket 长连接，支持流式回复。

配置 `workspace/wecom_bot_config.json`，双击 `wecom_bot/bot.bat` 启动。

## 配置说明

### auto_reply_config.json

| 字段 | 说明 |
|------|------|
| `dm_system_prompt` | 私聊人设提示词 |
| `group_system_prompt` | 群聊人设提示词 |
| `dm_blacklist` / `dm_whitelist` | 私聊黑白名单 |
| `group_blacklist` / `group_whitelist` | 群聊黑白名单 |
| `group_mention_names` | 群聊 @触发名字列表 |
| `group_trigger_words` | 群聊触发词列表 |

修改配置后无需重启，脚本自动热加载。

### model_config.json

| 字段 | 说明 |
|------|------|
| `mode` | 运行模式：`ollama` / `remote` / `qoder` |
| `model` | 模型名称 |
| `api_base` | 远程 API 地址 |
| `api_key` | API 密钥 |
| `remote_provider` | 提供商名称（日志显示用） |
| `bocha_api_key` | 博查搜索 API 密钥 |

注意：`save_model_config()` 采用合并写入模式，保留已有字段（如 bocha_api_key），不会被新配置覆盖。

## 技术要点

- **ThreadingHTTPServer**：Qoder 中继使用多线程 HTTP 服务器，避免单线程死锁
- **配置合并保存**：`save_model_config()` 读取现有文件后合并新值，防止额外字段丢失
- **GBK 编码**：所有 .bat 文件必须使用 GBK 编码（cmd.exe 代码页 936）
- **max_tokens >= 4096**：reasoning 模型的思考 token 计入上限，设太小会截断
- **搜索回避检测**：长回复（>100 字）需要 2+ 回避短语命中才判定，减少误判
- **企业微信 API**：wechatpy 使用 `wechatpy.enterprise`（非 `wechatpy.work`），`send_text()` 使用位置参数

## 许可

MIT License
