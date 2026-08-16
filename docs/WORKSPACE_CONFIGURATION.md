# Workspace 配置总指南

本文是 `D:\AI\workspace` 的统一配置参考，适用于当前仓库中的个人微信自动回复、企业微信自建应用、企业微信智能机器人和微信客服服务。

> 安全原则：`workspace` 中包含 API Key、企业微信密钥、CLI 登录状态相关文件和业务数据，不应整体提交到 Git。仓库只保存脱敏模板；真实值只写入部署机器的 `workspace`。

## 1. 路径和加载规则

仓库默认采用代码与运行数据分离的目录结构：

```text
D:\AI\
├── code\                 # Git 仓库
└── workspace\            # 配置、密钥、日志、附件和生成结果
```

`shared/config.py` 会从代码仓库的同级目录查找 `workspace`。如果远端目录不是 `D:\AI`，应保持 `code` 与 `workspace` 同级；同时把 `model_config.json` 中的 `workspace_dir` 和 `codexcli_path` 改成远端实际路径。

| 文件 | 使用方 | 生效方式 | 是否提交 Git |
|---|---|---|---|
| `model_config.json` | 所有 AI 入口 | 通常重启服务 | 否，包含 API Key |
| `auto_reply_config.json` | 个人微信、共享消息策略 | 下一条消息通常即可生效 | 否，仓库保留脱敏模板 |
| `customer_service_config.json` | 微信客服 | 重启客服服务 | 否，包含企业密钥 |
| `wecom_config.json` | 企业微信自建应用 | 重启服务 | 否，包含企业密钥 |
| `wecom_bot_config.json` | 企业微信智能机器人 | 重启服务 | 否，包含机器人密钥 |
| `.npmrc` | 本地 Node/npm 工具链 | 新 npm 进程 | 否 |
| `contacts.json` | 联系人或会话缓存 | 由运行程序维护 | 否 |
| `.qoder_relay_request.json` | 旧版 Qoder 中继 | 由运行程序维护 | 否 |
| `.webui_secret_key` | Open WebUI | 重启 Open WebUI | 否 |

## 2. 首次配置

在 `D:\AI\code` 中执行以下 PowerShell 命令。已有真实配置时不要覆盖。

```powershell
Copy-Item .\model_config.json.example ..\workspace\model_config.json
Copy-Item .\auto_reply_config.json ..\workspace\auto_reply_config.json
Copy-Item .\customer_service\config.json.example ..\workspace\customer_service_config.json
Copy-Item .\wecom\config.json.example ..\workspace\wecom_config.json
Copy-Item .\wecom_bot\config.json.example ..\workspace\wecom_bot_config.json
```

配置文件必须是合法 JSON，不能写注释，也不能保留末尾逗号。可用以下命令检查：

```powershell
Get-Content ..\workspace\model_config.json -Raw | ConvertFrom-Json | Out-Null
Get-Content ..\workspace\auto_reply_config.json -Raw | ConvertFrom-Json | Out-Null
Get-Content ..\workspace\customer_service_config.json -Raw | ConvertFrom-Json | Out-Null
```

## 3. `model_config.json`

### 3.1 模式与入口差异

`mode` 决定 AI 后端，但不同入口支持范围不同：

| `mode` | 个人微信 `auto_reply.py` | 企业微信/机器人/微信客服 `shared/ai_engine.py` | 说明 |
|---|---:|---:|---|
| `remote` | 支持 | 支持 | OpenAI 兼容 HTTP API |
| `ollama` | 支持 | 支持 | 本机 Ollama，默认端口 `11434` |
| `qoder` | 支持 | 不支持 | 个人微信旧版 Qoder HTTP 中继 |
| `qodercli` | 不直接支持 | 支持 | Qoder CN CLI |
| `codexcli` | 不直接支持 | 支持 | OpenAI Codex CLI |
| `codecli` | 不直接支持 | 支持 | `codexcli` 的兼容别名，建议新配置使用 `codexcli` |

需要特别注意：

- `model` 控制 `remote` 和 `ollama` 模式的模型。
- `codexcli_model` 才是 Codex CLI 实际使用的模型。
- `qodercli_model` 才是 Qoder CN CLI 实际使用的模型。
- 微信客服启动日志若显示通用 `model`，不能据此判断 Codex CLI 最终模型；应看 Codex 调用日志中的 `model:`。
- 个人微信 UI 自动化入口目前不会直接调用 Codex CLI；Codex CLI 主要由企业微信和微信客服共享引擎调用。

### 3.2 通用字段

| 字段 | 类型/默认值 | 含义 |
|---|---|---|
| `mode` | 字符串；配置缺失时回退 `ollama` | AI 后端模式，取值见上表。 |
| `workspace_dir` | 路径；通常为 `D:\AI\workspace` | CLI 工作目录、附件保存目录、输出文件搜索根目录。远端必须改为真实路径。 |
| `model` | 字符串；缺失时可能回退 `qwen3:8b` | `remote` 或 `ollama` 模式的模型名。 |
| `bocha_api_key` | 字符串；空 | 博查搜索 API Key。为空时按 DuckDuckGo/Bing/百度等路径回退。 |
| `system_prompt_override` | 字符串；空 | 覆盖渠道提示词。当前主要用于远程 API 路径；为空时使用 `auto_reply_config.json` 的提示词。 |

### 3.3 远程 API 字段

| 字段 | 类型/默认值 | 含义 |
|---|---|---|
| `api_base` | URL | OpenAI 兼容 API 根地址，通常以 `/v1` 结尾。 |
| `api_key` | 字符串 | 远程模型 API Key；也可作为语音识别 API 的后备 Key。 |
| `remote_provider` | 字符串；空 | 提供商显示名称，仅用于日志和界面标识，不参与路由。 |
| `remote_max_tokens` | 整数；`8192` | 远程 API 单次回复的最大输出 token。推理模型设置过低可能截断。 |

脱敏示例：

```json
{
  "mode": "remote",
  "workspace_dir": "D:\\AI\\workspace",
  "model": "your-model-name",
  "api_base": "https://your-openai-compatible-host/v1",
  "api_key": "YOUR_API_KEY",
  "remote_provider": "Your Provider",
  "remote_max_tokens": 8192,
  "bocha_api_key": ""
}
```

### 3.4 Ollama 与旧版 Qoder 中继

| 字段 | 类型/默认值 | 含义 |
|---|---|---|
| `ollama_max_tokens` | 整数；`8192` | Ollama 的 `num_predict` 上限。 |
| `relay_port` | 整数；`11435` | 仅个人微信 `mode: qoder` 使用的旧版 Qoder 中继端口。 |

Ollama 服务地址目前由程序按本机默认地址访问，不通过 `model_config.json` 单独配置。远端若使用 Ollama，应先确认服务可用：

```powershell
Invoke-RestMethod http://127.0.0.1:11434/api/tags
```

### 3.5 Qoder CN CLI 字段

| 字段 | 类型/默认值 | 含义 |
|---|---|---|
| `qodercli_path` | 路径；空则自动发现 | Qoder CN CLI 可执行文件路径。多 Windows 用户环境建议显式填写。 |
| `qodercli_model` | 字符串；`Qwen3.7-Max` | Qoder CN CLI 模型名。 |
| `qodercli_max_tokens` | 整数；代码默认 `4096` | Qoder CLI 最大输出 token；模板建议 `8192`。 |
| `qodercli_permission_mode` | `bypass` / `ask`；`bypass` | `ask` 时微信客服先生成计划并等待用户确认，`bypass` 直接执行。 |

CLI 登录信息属于 Windows 用户。服务由哪个账号启动，就必须在同一账号下执行安装和登录；只复制 `workspace` 无法复制登录状态。

### 3.6 Codex CLI 字段

| 字段 | 类型/默认值 | 含义 |
|---|---|---|
| `codexcli_path` | 路径；空则自动发现 | Codex CLI 或包装脚本路径。推荐指向 `workspace\codex-cli.cmd`，便于固定 npm 安装位置。 |
| `codexcli_model` | 字符串；代码当前默认 `gpt-5.6-sol` | Codex CLI 实际模型。模型是否可用取决于远端 CLI 版本和账号权限。 |
| `codexcli_reasoning_effort` | 字符串；空 | 传给 Codex 的推理强度。有效值取决于模型和 CLI 版本；`none` 表示关闭额外推理强度。 |
| `codexcli_permission_mode` | `bypass` / `ask`；`bypass` | 微信客服中的业务交互模式。`ask` 先展示计划，确认后执行。 |
| `codexcli_dangerously_bypass` | 布尔；代码默认 `true` | 为 `true` 时添加完整绕过参数，CLI 可直接访问系统和网络。仅可信专机使用。 |
| `codexcli_sandbox` | 字符串；`workspace-write` | 未启用完整绕过时的沙箱，如 `read-only`、`workspace-write`、`danger-full-access`。 |
| `codexcli_approval_policy` | 字符串；`never` | 未启用完整绕过时的 CLI 审批策略。无人值守服务通常使用 `never`。 |
| `codexcli_timeout` | 秒；普通请求 `600`，附件请求 `900` | 常规执行和确认后执行的超时；显式配置后统一使用该值。 |
| `codexcli_rule_timeout` | 秒；`120` | 两阶段规则路由中，让 Codex 判断是否匹配已有规则的超时。 |
| `codexcli_plan_timeout` | 秒；`300` | `permission_mode: ask` 时生成执行计划的超时。 |
| `codexcli_print_prompt` | 布尔；`true` | 是否在终端打印提交给 Codex 的完整提示词。生产环境含敏感内容时建议设为 `false`。 |
| `codexcli_extra_args` | 字符串数组；`[]` | 追加到 Codex CLI 的高级参数。配置错误会直接导致命令失败。 |

权限关系：

- `codexcli_dangerously_bypass: true` 时，`codexcli_sandbox` 和 `codexcli_approval_policy` 基本不再限制本次执行。
- `codexcli_dangerously_bypass: false` 时，才由 `codexcli_sandbox` 和 `codexcli_approval_policy` 共同控制权限。
- `codexcli_permission_mode: ask` 是客服业务层的“两阶段确认”，与 Codex CLI 自身审批策略不是同一个概念。

较稳妥的无人值守示例：

```json
{
  "mode": "codexcli",
  "workspace_dir": "D:\\AI\\workspace",
  "codexcli_path": "D:\\AI\\workspace\\codex-cli.cmd",
  "codexcli_model": "your-codex-model",
  "codexcli_reasoning_effort": "none",
  "codexcli_permission_mode": "bypass",
  "codexcli_dangerously_bypass": false,
  "codexcli_sandbox": "workspace-write",
  "codexcli_approval_policy": "never",
  "codexcli_timeout": 900,
  "codexcli_rule_timeout": 120,
  "codexcli_print_prompt": false
}
```

### 3.7 Codex CLI 代理字段

| 字段 | 类型/默认值 | 含义 |
|---|---|---|
| `codexcli_proxy` | URL；空 | 同时作为 HTTP/HTTPS 代理的简写，例如 `http://127.0.0.1:7890`。 |
| `codexcli_http_proxy` | URL；空 | 仅 HTTP 请求代理，高级用法。 |
| `codexcli_https_proxy` | URL；空 | 仅 HTTPS 请求代理，高级用法。 |
| `codexcli_all_proxy` | URL；空 | 全协议代理，高级用法。 |
| `codexcli_no_proxy` | 字符串；`localhost,127.0.0.1,::1` | 不经过代理的主机列表，逗号分隔。 |
| `codexcli_use_windows_proxy` | 布尔；`true` | 未显式填写代理时，是否读取当前 Windows 用户的系统代理。Linux 服务器上该项不起作用。 |

优先级是：Codex 专用字段 → 进程环境变量 → Windows 当前用户代理（启用时）。远端 Linux 或不同 Windows 用户无法继承本机代理设置，应在服务账号环境变量或配置文件中单独设置。

代码仍兼容旧字段 `terminal_proxy`、`proxy`、`http_proxy`、`https_proxy`、`all_proxy`、`no_proxy` 及其大写形式。新配置统一使用 `codexcli_*` 字段，避免与其他进程的通用代理设置混淆。

若已经使用 `enable-vpn-proxy-env.cmd` 持久化了当前用户的 `HTTP_PROXY`、`HTTPS_PROXY` 等变量，`codexcli_proxy` 可以留空。脚本执行后需要关闭旧终端，再用同一 Windows 用户打开新终端或重启服务。

### 3.8 语音识别字段

这些字段目前由微信客服语音消息流程使用。

| 字段 | 类型/默认值 | 含义 |
|---|---|---|
| `voice_asr_provider` | `api` / `openai` / `remote` / `auto` / `local`；`local` | 语音识别后端。前三个走兼容 API；`auto` 有 Key 时走 API，否则本地；其他值走本地 faster-whisper。 |
| `voice_asr_model` | 字符串；`gpt-4o-transcribe` | 云端语音识别模型。 |
| `voice_asr_api_base` | URL；空 | 语音 API 根地址。空时依次回退到 `api_base`、`OPENAI_BASE_URL`、OpenAI 默认地址。 |
| `voice_asr_api_key` | 字符串；空 | 语音 API Key。空时依次回退到 `api_key`、`OPENAI_API_KEY`。 |
| `voice_asr_language` | 字符串；`zh` | 识别语言提示。 |
| `voice_asr_timeout` | 秒；`90` | 云端语音识别请求超时。 |
| `voice_asr_fallback_local` | 布尔；`true` | 云端失败后是否回退本地 faster-whisper。 |
| `voice_asr_convert_timeout` | 秒；`60` | AMR 等音频转换为 16 kHz 单声道 WAV 的超时。 |

本地回退默认查找 `D:\AI\whisper-base-model`，在无 GPU 的 2 核服务器上速度和准确率都有限。已配置可用 API 时，建议 `voice_asr_provider` 使用 `api`。

## 4. `auto_reply_config.json`

该文件控制是否回复、对谁回复、何时触发以及使用什么渠道提示词。缺失字段会与代码默认值合并。

| 字段 | 类型/默认值 | 含义 |
|---|---|---|
| `enabled` | 布尔；`true` | 总开关。为 `false` 时停止自动生成回复。 |
| `dm_enabled` | 布尔；`true` | 是否处理私聊消息。 |
| `dm_system_prompt` | 字符串；空 | 私聊系统提示词。空字符串表示不追加自定义人设。 |
| `dm_blacklist` | 字符串数组；`[]` | 私聊黑名单，按会话显示名精确匹配；优先级高于白名单。 |
| `dm_whitelist_mode` | 布尔；`false` | 为 `true` 时只处理 `dm_whitelist` 中的会话。 |
| `dm_whitelist` | 字符串数组；`[]` | 私聊白名单。只有白名单模式开启时才作为准入条件。 |
| `group_enabled` | 布尔；`true` | 是否处理群聊消息。 |
| `group_system_prompt` | 字符串；空 | 群聊系统提示词。 |
| `group_blacklist` | 字符串数组；`[]` | 群聊黑名单，按群名称精确匹配。 |
| `group_whitelist_mode` | 布尔；`false` | 为 `true` 时只处理 `group_whitelist` 中的群。 |
| `group_whitelist` | 字符串数组；`[]` | 群聊白名单。 |
| `group_trigger_words` | 字符串数组；`[]` | 群消息包含任一字符串时触发，属于包含匹配。 |
| `group_mention_names` | 字符串数组 | 可识别并移除的 `@名称`，运行时也会加入机器人自身名称。 |
| `reply_delay` | 数字；`2` | 回复前等待秒数；设为 `0` 可关闭延迟。 |
| `log_file` | 路径；`auto_reply.log` | 日志文件。相对路径按 `workspace` 解析，建议保持相对路径。 |
| `welcome_message` | 字符串；英文默认欢迎语 | 仅企业微信智能机器人使用：用户进入机器人会话时，在 5 秒内返回的欢迎语。 |

无自定义提示词的最小示例：

```json
{
  "enabled": true,
  "dm_enabled": true,
  "dm_system_prompt": "",
  "dm_blacklist": [],
  "dm_whitelist_mode": false,
  "dm_whitelist": [],
  "group_enabled": true,
  "group_system_prompt": "",
  "group_blacklist": [],
  "group_whitelist_mode": false,
  "group_whitelist": [],
  "group_trigger_words": [],
  "group_mention_names": ["@机器人", "@bot", "@助手"],
  "reply_delay": 2,
  "log_file": "auto_reply.log",
  "welcome_message": "你好，我是 AI 助手。"
}
```

`auto_reply_config.json` 通常会在处理下一条消息时重新读取，因此开关、名单和提示词大多不需要重启。模型、CLI、API 凭证及服务监听配置应在修改后重启对应进程。

## 5. `customer_service_config.json`

该文件用于微信客服服务，默认回调端口为 `8081`。

| 字段 | 类型/默认值 | 含义 |
|---|---|---|
| `corp_id` | 字符串 | 企业 ID。 |
| `corp_secret` | 字符串 | 已授权使用微信客服接口的企业应用 Secret。它不一定与其他自建应用的 Secret 相同。 |
| `token` | 字符串 | 微信客服回调配置中的 Token。 |
| `encoding_aes_key` | 字符串 | 回调消息加解密用的 43 位 EncodingAESKey。 |
| `host` | 字符串；`0.0.0.0` | 本地监听地址。`0.0.0.0` 表示监听所有网卡。 |
| `port` | 整数；`8081` | 本地监听端口。同一机器上同一端口不能被两个进程直接占用。 |
| `public_base_url` | URL；空 | 外部可访问的服务根地址，用于生成大文件手工上传链接。只写协议、域名/IP 和端口，不要追加 `/callback` 或 `/upload`。为空时禁用该补偿入口。 |
| `open_kfid` | 字符串；空 | 指定微信客服账号 ID。为空时自动获取账号列表并使用第一个；存在多个客服账号时应明确配置。 |
| `forward_chatid` | 字符串；空 | 将用户消息和回复转发到企业微信群聊的 chatid。 |
| `forward_userid` | 字符串；空 | 将消息转发给企业微信成员的 userid。需与 `agent_id` 同时配置，并优先于群转发。 |
| `agent_id` | 字符串或整数；空 | 执行成员转发的企业微信应用 AgentId。 |

脱敏示例：

```json
{
  "corp_id": "YOUR_CORP_ID",
  "corp_secret": "YOUR_CUSTOMER_SERVICE_SECRET",
  "token": "YOUR_CALLBACK_TOKEN",
  "encoding_aes_key": "YOUR_43_CHAR_AES_KEY",
  "host": "0.0.0.0",
  "port": 8081,
  "public_base_url": "https://assistant.example.com",
  "open_kfid": "",
  "forward_chatid": "",
  "forward_userid": "",
  "agent_id": ""
}
```

企业微信后台回调 URL 使用：

```text
https://assistant.example.com/callback
```

若直接暴露端口，则外网防火墙、安全组和 Windows 防火墙都必须允许该 TCP 端口。生产环境建议由 HTTPS 反向代理转发到本机 `127.0.0.1:8081`，而不是直接暴露 Flask 开发服务器。

## 6. `wecom_config.json`

该文件用于企业微信自建应用 HTTP 回调服务，默认端口为 `8080`。

| 字段 | 类型/默认值 | 含义 |
|---|---|---|
| `corp_id` | 字符串 | 企业 ID。 |
| `corp_secret` | 字符串 | 自建应用 Secret。 |
| `agent_id` | 整数 | 自建应用 AgentId。 |
| `token` | 字符串 | 企业微信应用回调 Token。 |
| `encoding_aes_key` | 字符串 | 回调消息加解密用 EncodingAESKey。 |
| `host` | 字符串；`0.0.0.0` | 本地监听地址。 |
| `port` | 整数；`8080` | 本地监听端口，回调路径为 `/callback`。 |

`wecom_config.json` 与 `customer_service_config.json` 是两套接口的凭证，不要因为都属于同一企业就直接互换 `corp_secret`、Token 或 AES Key。

## 7. `wecom_bot_config.json`

该文件用于企业微信智能机器人 WebSocket 长连接模式。

| 字段 | 类型 | 含义 |
|---|---|---|
| `bot_id` | 字符串 | 企业微信智能机器人 ID。 |
| `secret` | 字符串 | 智能机器人长连接 Secret。 |

此模式主动连接企业微信的 WebSocket 服务，不需要配置本地回调端口或公网域名。

## 8. 其他文件和脚本

### 8.1 `.npmrc`

| 字段 | 含义 |
|---|---|
| `prefix` | npm 全局包安装目录，本项目常指向 `D:\AI\npm-global`。 |
| `cache` | npm 下载缓存目录，本项目常指向 `D:\AI\npm-cache`。 |

不要把 npm registry Token 写进可共享的 `.npmrc`。

### 8.2 代理脚本

| 脚本 | 用途 |
|---|---|
| `enable-vpn-proxy-env.cmd` | 将代理写入当前 Windows 用户环境；新终端和该用户启动的新服务生效。 |
| `disable-vpn-proxy-env.cmd` | 清除当前 Windows 用户的代理环境变量。 |
| `check-codex-proxy.cmd` | 检查环境变量、Windows 代理、DNS 和 Codex 访问情况。 |
| `codex-vpn.cmd` | 仅为一次 Codex 调用注入代理。 |
| `vpn-cmd.cmd` | 打开带代理环境的 CMD。 |
| `vpn-powershell.cmd` | 打开带代理环境的 PowerShell。 |
| `vpn-terminal.ps1` | 上述脚本使用的核心代理逻辑。 |

用户级代理只对同一 Windows 用户的新进程生效。计划任务、Windows 服务或其他账号启动的客服进程不会自动继承，应在对应服务账号下配置，或在服务启动脚本中显式注入。

### 8.3 CLI 和环境脚本

| 脚本 | 用途 |
|---|---|
| `codex-cli.cmd` | 从固定 npm 目录启动 Codex CLI，找不到时再回退到系统 `PATH`。 |
| `update-codex-cli.cmd` | 更新本地 `@openai/codex` 包。更新后应检查版本和登录状态。 |
| `repair-cli-env.cmd` | 修复 workspace、npm 和 CLI 的 `PATH`；管理员运行时可写机器级 PATH。 |
| `init-github-ssh.cmd` | 为当前 Windows 用户生成或检查 GitHub SSH Key。 |
| `install-env.ps1` / `setup.ps1` | 安装依赖和初始化目录，不是业务配置文件。 |

CLI 登录和 SSH 私钥都保存在 Windows 用户目录中。重启不会正常删除它们，但切换用户、临时用户配置文件、镜像还原或只复制 `workspace` 都会表现为“登录丢失”。

### 8.4 不应手工维护的运行文件

| 文件/目录 | 含义 |
|---|---|
| `auto_reply.log` | 运行日志，可按运维策略轮转或归档。 |
| `contacts.json` | 联系人/会话状态缓存，结构中的顶层键通常是动态联系人 ID。 |
| `.qoder_relay_request.json` | 旧版 Qoder 中继请求状态，服务运行时不要修改。 |
| `.webui_secret_key` | Open WebUI 会话签名密钥；更换后已有登录会话可能失效。 |
| `cs_media/`、`cs_files/` | 微信客服收到的图片、语音和普通文件。 |
| `outputs/`、`generated/`、`images/` | Codex 或规则处理生成的结果文件。 |
| `open-webui-data/` | Open WebUI 数据库和运行数据。 |

## 9. 远端迁移清单

1. 在远端创建同级的 `code` 和 `workspace`，通过 Git 只同步 `code`。
2. 单独安全复制五个 JSON 配置；不要把包含真实密钥的文件提交到仓库。
3. 修改 `workspace_dir`、`codexcli_path`、`public_base_url` 和端口为远端实际值。
4. 使用服务实际运行的 Windows 用户安装并登录 Codex CLI/Qoder CLI。
5. 在同一用户的新终端中检查代理；远端没有本地 VPN 时必须使用远端可达代理。
6. 检查云安全组、Windows 防火墙、端口占用和公网 HTTPS 反向代理。
7. 先校验 JSON，再分别启动服务，观察启动日志中的模式、工作目录、端口和 CLI 模型。

常用检查命令：

```powershell
# JSON 是否有效
Get-Content D:\AI\workspace\model_config.json -Raw | ConvertFrom-Json | Out-Null

# CLI 路径、版本和登录用户
where.exe codex
codex --version
codex login status
whoami

# 端口是否监听
Get-NetTCPConnection -State Listen | Where-Object LocalPort -in 8080,8081,11434,11435

# Git SSH 是否可用
ssh -T git@github.com

# 当前进程可见的代理
Get-ChildItem Env:HTTP_PROXY,Env:HTTPS_PROXY,Env:NO_PROXY -ErrorAction SilentlyContinue
```

## 10. 故障定位顺序

1. **先看启动日志**：确认读取的是预期 `workspace`、`mode`、端口和 Windows 用户。
2. **再验 JSON**：解析失败时模型配置会回退，容易造成“明明写了 Codex 却仍在用 Qwen”。
3. **区分模型字段**：Codex 看 `codexcli_model`，Qoder CLI 看 `qodercli_model`，远程 API/Ollama 才看 `model`。
4. **区分入口能力**：个人微信的 `auto_reply.py` 不会直接执行 `codexcli`；微信客服和企业微信共享引擎才支持。
5. **检查账号环境**：CLI 登录、代理环境和 SSH Key 都与服务启动账号有关。
6. **检查路径和权限**：附件必须先保存到 `workspace_dir` 或其子目录，Codex 回复生成文件时应使用相对路径。
7. **最后检查网络**：分别验证 DNS、代理端口、API 地址、GitHub SSH 和企业微信公网回调。

## 11. 安全要求

- 禁止提交 `api_key`、`voice_asr_api_key`、`bocha_api_key`、`corp_secret`、`secret`、Token、AES Key、SSH 私钥和 CLI 登录凭证。
- 分享配置截图前，将密钥保留前后各 2 到 4 位，其余替换为 `***`。
- `codexcli_dangerously_bypass: true` 只用于隔离且可信的专用机器，并限制 `workspace_dir` 中可接触的数据。
- 生产环境建议关闭 `codexcli_print_prompt`，避免用户消息、文件名和提示词进入终端日志。
- 更换或泄露密钥后应立即在服务提供方吊销旧密钥，而不只是修改本地 JSON。

仓库中的 `*.example` 文件是可提交模板；`workspace` 中的真实配置是部署资产。两者职责分开，升级代码时不应覆盖真实配置。
