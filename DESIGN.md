# AIChatDemo 设计文档

本文档基于当前代码仓库整理，说明系统边界、目录结构、核心模块、消息流、配置模型和主要设计取舍。

## 1. 项目定位

AIChatDemo 是一套面向个人微信、企业微信自建应用、企业微信智能机器人、企业微信客服场景的 AI 自动回复系统。

系统目标是把不同消息入口统一接到同一套 AI 回复能力上，并支持以下能力：

- 个人微信 PC 客户端自动监听与回复。
- 企业微信自建应用 HTTP 回调接入。
- 企业微信智能机器人 WebSocket 长连接接入。
- 企业微信客服消息同步、回复、转发与多媒体处理。
- 本地 Ollama、远程 OpenAI 兼容 API、Qoder/QoderCLI 等模型后端。
- 博查、DDG/Bing、Bing China、百度等搜索瀑布回退。
- 图片、文件、语音等多模态或准多模态处理路径。

## 2. 仓库与运行目录

当前代码仓库位于 `D:\AI\code`。仓库上一级 `D:\AI` 还包含运行数据、模型文件和工作区。

```text
D:\AI
├── code                    # Git 管理的源码仓库
├── workspace               # 运行时配置、密钥、日志、数据库、临时输出
├── models                  # Ollama 模型数据
├── whisper-base-model      # faster-whisper 本地语音识别模型
└── whisper-base-model.zip  # whisper 模型压缩包
```

代码和运行时数据分离是本项目的重要约定：

- `code` 保存源码、脚本、示例配置和文档。
- `workspace` 保存真实配置、API Key、日志、上传文件、Open WebUI 数据等，不应提交到 Git。
- `models` 和 `whisper-base-model` 体积较大，作为本地运行依赖存在。

## 3. 源码目录结构

```text
D:\AI\code
├── auto_reply.py                 # 个人微信自动回复主程序
├── auto-reply.bat                # 个人微信启动菜单
├── start.bat                     # AI Station / Ollama / Open WebUI 启动脚本
├── stop.bat                      # 停止相关服务
├── setup.ps1                     # 环境部署脚本
├── weixin-login.bat              # weixin-mcp 登录辅助
├── model_config.json.example     # 模型配置示例
├── auto_reply_config.json        # 自动回复配置模板
├── qoder_bridge.py               # Qoder 中继桥接进程
├── qoder_long_poll.py            # Qoder 长轮询辅助
├── qoder_responder.py            # Qoder 回复提交辅助
├── shared
│   ├── config.py                 # 共享配置路径、加载、日志
│   ├── ai_engine.py              # 统一 AI 回复引擎
│   └── search_engine.py          # 搜索、天气、回避检测
├── wecom
│   ├── server.py                 # 企业微信自建应用 HTTP 回调服务
│   ├── handler.py                # 自建应用消息处理器
│   ├── config.json.example       # 自建应用配置示例
│   └── wecom.bat                 # 启动脚本
├── wecom_bot
│   ├── bot.py                    # 企业微信智能机器人 WebSocket 客户端
│   ├── config.json.example       # 智能机器人配置示例
│   └── bot.bat                   # 启动脚本
├── customer_service
│   ├── server.py                 # 企业微信客服回调服务
│   ├── handler.py                # 客服消息同步、处理、回复
│   ├── create_group.py           # 内部群创建辅助
│   ├── diagnose.py               # 诊断辅助
│   ├── config.json.example       # 客服配置示例
│   └── customer_service.bat      # 启动脚本
├── tools
│   └── download_whisper_model.py # Whisper 模型下载/打包工具
├── mcp
│   └── config.json               # MCP 配置
└── wxauto
    └── README.md                 # wxauto 相关说明
```

## 4. 总体架构

系统采用“多入口 + 共享能力 + 多后端”的结构：

```text
个人微信 PC
  -> auto_reply.py
     -> 本地回复实现 / Qoder 中继

企业微信自建应用
  -> wecom/server.py
     -> wecom/handler.py
        -> shared/ai_engine.py

企业微信智能机器人
  -> wecom_bot/bot.py
     -> shared/ai_engine.py

企业微信客服
  -> customer_service/server.py
     -> customer_service/handler.py
        -> shared/ai_engine.py

shared/ai_engine.py
  -> Ollama
  -> OpenAI 兼容远程 API
  -> QoderCLI
  -> shared/search_engine.py
```

个人微信 `auto_reply.py` 是较早的独立实现，内部包含自己的配置、模型选择、搜索和 Qoder 中继逻辑。企业微信三条链路则更多复用 `shared` 目录中的统一配置、模型与搜索能力。

## 5. 核心模块设计

### 5.1 shared/config.py

职责：

- 计算运行时目录 `D:\AI\workspace`。
- 提供统一配置文件路径：
  - `auto_reply_config.json`
  - `model_config.json`
  - `wecom_config.json`
  - `wecom_bot_config.json`
  - `customer_service_config.json`
- 提供 `load_config()`、`load_model_config()`、`save_model_config()`。
- 提供 `log()`，把日志输出到控制台和 workspace 日志文件。

设计特点：

- 所有运行配置默认从 `workspace` 读取，避免密钥进入代码仓库。
- `load_config()` 会把缺失字段与默认配置合并，便于兼容旧配置。
- 企业微信和客服模块热加载通用自动回复配置，修改提示词或开关后无需重启部分服务。

### 5.2 shared/ai_engine.py

职责：

- 作为企业微信相关入口的统一回复生成层。
- 根据 `model_config.mode` 分发到不同模型后端。
- 远程 API 模式下支持智能搜索、天气查询、工具调用清理。
- 支持图片作为远程 API multimodal 内容，或作为 QoderCLI 附件传入。
- 支持 QoderCLI “直接执行”和“授权后执行”两种路径。

主要入口：

- `generate_reply(...)`
- `generate_reply_remote(...)`
- `generate_reply_ollama(...)`
- `generate_reply_qodercli(...)`
- `generate_reply_qodercli_plan(...)`
- `generate_reply_qodercli_execute(...)`

支持的模型模式：

| mode | 说明 |
| --- | --- |
| `remote` | OpenAI 兼容接口，使用 `/chat/completions` |
| `ollama` | 本地 Ollama `/api/chat` |
| `qodercli` | 调用 `qoderclicn` 或 `qodercli` 命令行 |

远程模式的搜索增强流程：

1. 给系统提示词追加规则，要求模型需要搜索时输出 `[SEARCH: 关键词]`。
2. 首次调用远程模型生成回复。
3. 如果模型输出 `[SEARCH: ...]`，系统执行搜索并带结果重试。
4. 如果回复像“无法联网”“查不到”等回避回答，系统自动搜索并重试。
5. 如果用户问题包含实时查询意图且回复过短，也尝试搜索补强。
6. 最终清理 JSON 工具调用、异常代理输出和无效 surrogate 字符。

### 5.3 shared/search_engine.py

职责：

- 识别是否需要联网搜索。
- 检测模型回避型回答。
- 提取搜索关键词和天气地点。
- 执行天气查询和网络搜索。
- 把“今天、明天、昨天、后天”等相对日期替换为具体日期。

搜索瀑布：

```text
Bocha Search API
  -> DDG/Bing backend
     -> cn.bing.com 页面抓取
        -> baidu.com 页面抓取
```

设计特点：

- 优先使用博查 API，适配中国大陆网络环境。
- 无博查密钥时自动回退到其他搜索路径。
- 长回复中只出现单个轻微回避短语时不直接判定失败，降低误判。
- 天气问题优先使用网络搜索获取中文实时结果，`wttr.in` 作为补充。

### 5.4 auto_reply.py

职责：

- 连接微信 PC 客户端。
- 轮询未读消息。
- 根据私聊/群聊、黑白名单、@ 提及等规则决定是否回复。
- 维护会话历史和消息去重。
- 根据模型配置选择 Ollama、远程 API 或 Qoder 中继。
- 将生成结果发送回微信。

关键设计：

- 使用 `wxautoz.WeChat()` 连接微信客户端。
- `GetNextNewMessage()` 每次拉取一个会话的新消息。
- 私聊默认自动回复；群聊默认只在被 @ 或命中触发词时回复。
- `conversation_history` 按会话保存最近 `MAX_HISTORY` 轮上下文。
- `processed_msg_ids` 做消息去重，超过 5000 条会清理。
- 主循环每轮最多连续读取 `MAX_POLLS_PER_ROUND` 次，防止单轮耗时过长。
- 每次循环重新读取自动回复配置，实现基础热加载。

Qoder 中继模式：

```text
auto_reply.py
  -> 启动本地 ThreadingHTTPServer，端口 11435
  -> /qoder/chat 接收微信消息并等待回复
  -> qoder_bridge.py 轮询 /qoder/pending
  -> qoder_bridge.py 调用 Ollama 生成回复
  -> /qoder/respond 回填结果
```

### 5.5 wecom/server.py 与 wecom/handler.py

职责：

- 提供企业微信自建应用回调服务。
- 处理 URL 验证和加密消息解密。
- 收到文本消息后在后台线程生成回复，避免超过企业微信 5 秒响应要求。
- 使用企业微信应用消息 API 发送回复。

消息流：

```text
企业微信服务器
  -> GET /callback 验证 URL
  -> POST /callback 推送加密消息
  -> WeChatCrypto 解密
  -> parse_message
  -> WeComHandler.handle_text_message(...)
  -> shared.ai_engine.generate_reply(...)
  -> WeChatClient.message.send_text(...)
```

设计特点：

- `server.py` 只负责 HTTP 协议与消息解密。
- `handler.py` 负责配置热加载、上下文、限速、AI 调用和发送。
- 每个用户或群聊维护独立上下文。
- 同一会话最小回复间隔为 2 秒。

### 5.6 wecom_bot/bot.py

职责：

- 使用 `wecom_aibot_sdk.WSClient` 建立企业微信智能机器人 WebSocket 长连接。
- 处理文本、图片和进入会话事件。
- 使用流式回复接口回复用户。
- 对 AI 回复中出现的本地图片路径进行上传并发送。

文本消息流：

```text
WebSocket message.text
  -> on_text
  -> 配置与限速检查
  -> 更新会话历史
  -> shared.ai_engine.generate_reply
  -> client.reply_stream
```

图片消息流：

```text
WebSocket message.image
  -> on_image
  -> SDK 下载和解密图片
  -> 临时保存图片 + base64
  -> 等待用户下一条文字说明
  -> generate_reply(image_path/image_base64)
  -> 删除临时文件
  -> reply_stream
```

设计特点：

- WebSocket 事件循环中把 AI 调用放到 executor，避免阻塞异步循环。
- 图片先暂存，等待用户说明要做什么，避免没有上下文时误处理。
- 支持把 AI 生成的本地图片路径识别出来并发送回企业微信。

### 5.7 customer_service/server.py 与 customer_service/handler.py

职责：

- 接入企业微信客服。
- 通过回调收到 `kf_msg_or_event` 通知。
- 调用 `kf/sync_msg` 拉取真实消息。
- 处理文本、图片、语音、文件。
- 使用 `kf/send_msg` 回复客户。
- 可选转发问答到内部群或指定成员。

消息流：

```text
企业微信客服回调
  -> customer_service/server.py POST /callback
  -> 解密通知
  -> 后台线程 sync_and_process
  -> kf/sync_msg 拉取消息
  -> CSHandler._dispatch
  -> 文本/图片/语音/文件分流
  -> shared.ai_engine.generate_reply
  -> kf/send_msg
  -> 可选 appchat/send 或 message/send 内部转发
```

多媒体处理：

- 图片：通过 `media/get` 下载，临时保存，同时转 base64 给远程多模态 API。
- 语音：下载后统一转换为 16k 单声道 WAV，优先调用 OpenAI 兼容转写 API；API 失败时可回退 `faster-whisper` 本地模型，再走文本回复。
- 文件：下载到临时目录，等待用户给出处理指令，然后作为 QoderCLI 附件处理。
- 回复中若包含本地图片或文档路径，会尝试上传并作为媒体消息发送。

设计特点：

- 回调立即返回 `success`，实际处理放后台线程。
- 启动后先拉取并丢弃旧消息，只处理服务启动后的新消息。
- 使用 `seen_msgids` 做去重，避免多次回调导致重复回复。
- 本地模拟企业微信客服 48 小时 5 条服务端消息限制，收到用户消息后重置窗口。
- 支持 QoderCLI `ask` 授权模式：先返回执行计划，用户回复“确认”后才执行。

## 6. 配置设计

### 6.1 model_config.json

位置：`D:\AI\workspace\model_config.json`

示例来源：`D:\AI\code\model_config.json.example`

关键字段：

| 字段 | 说明 |
| --- | --- |
| `mode` | 模型模式：`remote`、`ollama`、`qodercli` 等 |
| `model` | 模型名称 |
| `api_base` | OpenAI 兼容 API 地址 |
| `api_key` | 远程 API Key |
| `remote_provider` | 远程服务显示名 |
| `bocha_api_key` | 博查搜索 API Key，可选 |
| `remote_max_tokens` | 远程模型输出 token 上限 |
| `ollama_max_tokens` | Ollama 输出 token 上限 |
| `qodercli_model` | QoderCLI 使用的模型名 |
| `qodercli_max_tokens` | QoderCLI 输出 token 上限 |
| `qodercli_permission_mode` | `bypass` 或 `ask` |

### 6.2 auto_reply_config.json

位置：`D:\AI\workspace\auto_reply_config.json`

示例来源：`D:\AI\code\auto_reply_config.json`

关键字段：

| 字段 | 说明 |
| --- | --- |
| `enabled` | 总开关 |
| `dm_enabled` | 私聊开关 |
| `dm_system_prompt` | 私聊系统提示词 |
| `dm_blacklist` | 私聊黑名单 |
| `dm_whitelist_mode` | 是否启用私聊白名单模式 |
| `dm_whitelist` | 私聊白名单 |
| `group_enabled` | 群聊开关 |
| `group_system_prompt` | 群聊系统提示词 |
| `group_blacklist` | 群聊黑名单 |
| `group_whitelist_mode` | 是否启用群聊白名单模式 |
| `group_whitelist` | 群聊白名单 |
| `group_trigger_words` | 群聊触发词 |
| `group_mention_names` | 群聊 @ 名称列表 |
| `reply_delay` | 回复前延迟秒数 |
| `log_file` | 日志文件名 |

### 6.3 企业微信配置

位置均在 `D:\AI\workspace`：

- `wecom_config.json`：企业微信自建应用。
- `wecom_bot_config.json`：企业微信智能机器人。
- `customer_service_config.json`：企业微信客服。

对应示例文件位于：

- `D:\AI\code\wecom\config.json.example`
- `D:\AI\code\wecom_bot\config.json.example`
- `D:\AI\code\customer_service\config.json.example`

## 7. 运行时状态

主要运行时状态都保存在内存中：

- 会话历史：按用户、群或客服会话 key 分组。
- 消息去重：个人微信使用 `processed_msg_ids`，客服使用 `seen_msgids`。
- 限速状态：企业微信处理器记录 `last_reply`。
- 客服同步游标：`sync_cursor` 只在进程内维护，重启后由启动 drain 流程跳过旧消息。
- 临时媒体：图片、语音、文件下载到临时目录，用完后删除。

持久化文件主要在 `workspace`：

- 配置文件。
- 日志文件。
- Open WebUI 数据。
- 上传或生成的文件。
- Qoder 中继请求文件 `.qoder_relay_request.json`。

## 8. 安全与隐私设计

- 真实 API Key、企业微信密钥、客服配置都放在 `workspace`，不放在仓库示例文件中。
- `.gitignore` 应继续覆盖运行配置、日志和缓存目录。
- 企业微信回调使用 `WeChatCrypto` 校验签名并解密。
- QoderCLI `ask` 模式可把高风险文件或命令操作改为用户确认后执行。
- 图片、语音、文件以临时文件方式处理，正常路径会在处理完成后删除。

需要注意：

- `qodercli_permission_mode=bypass` 会让 QoderCLI 自动批准工具权限，只适合可信环境。
- 个人微信自动化依赖桌面客户端 UI 状态，不适合无人值守高可靠场景。
- 搜索抓取路径可能受网络、反爬、页面结构变化影响。

## 9. 设计取舍

### 9.1 多入口复用 shared

企业微信相关模块都复用 `shared/ai_engine.py` 和 `shared/search_engine.py`，减少重复实现。个人微信 `auto_reply.py` 仍保留独立逻辑，优点是单文件启动简单，缺点是与 shared 中的 AI 引擎存在重复。

### 9.2 配置与代码分离

仓库只放模板，真实配置在 `workspace`。这样便于部署和保护密钥，但要求运行时目录结构固定在 `D:\AI\workspace` 或等价上级目录。

### 9.3 搜索作为模型补偿层

系统没有直接暴露工具调用协议，而是用 `[SEARCH: ...]` 标记和回避检测触发搜索。这种方式兼容多数 OpenAI 风格模型，但搜索意图识别依赖提示词和启发式规则。

### 9.4 回调快速返回，后台处理

企业微信自建应用和客服回调都尽快返回成功，把 AI 调用放后台线程。这满足企业微信 5 秒响应要求，但进程崩溃时后台任务不会恢复。

### 9.5 本地文件路径作为媒体回传协议

QoderCLI 或模型生成文件后，只要回复中包含本地绝对路径，系统就能上传并发送给用户。这降低了接口复杂度，但要求回复文本中的路径准确且文件真实存在。

## 10. 已知边界与改进建议

- 个人微信和 shared AI 引擎存在重复，可逐步让 `auto_reply.py` 复用 `shared.ai_engine`。
- 客服 `sync_cursor` 当前只在内存保存，若需要严格不丢消息，可持久化到 `workspace`。
- 搜索抓取依赖页面结构，可为 Bing/Baidu 抓取增加 HTML parser 和单元测试。
- 多入口的依赖目前靠手工安装，可增加 `requirements.txt` 或 `pyproject.toml`。
- 日志主要是文本输出，可增加结构化日志和日志轮转。
- 配置校验较弱，可引入 schema 校验，启动时给出明确缺失字段。
- 企业微信自建应用群聊发送方式受 API 能力限制，需要按真实业务进一步验证群消息回送效果。
