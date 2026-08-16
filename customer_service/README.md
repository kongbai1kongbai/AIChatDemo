# WeChat Customer Service AI Module

WeChat Customer Service (微信客服) integration — lets personal WeChat users chat with AI via a dedicated customer service window.

## How it works

```
User scans QR / clicks link → WeChat customer service window
    → sends message → WeCom cloud pushes notification to /callback
    → server.py returns "success" immediately (< 5s)
    → background thread calls sync_msg to fetch actual messages
    → handler.py generates AI reply via shared/ai_engine
    → sends reply via kf/send_msg API
    → (optional) forwards Q&A to user or group via message/send or appchat/send
```

## Prerequisites

- WeCom self-built app with "微信客服" API enabled
- Server IP added to the app's **企业可信IP** whitelist (errcode 60020 if missing)
- KF account authorized for the app (WeCom admin → 应用管理 → 自建应用 → 可调用接口的应用 → 前往配置)
- `qoderclicn login` completed if using `qodercli` mode
- `codex login` completed if using `codexcli` mode

## Setup

1. In WeCom admin console: enable "微信客服", create a customer service account
2. Configure the self-built app's callback URL: `https://your-server:8081/callback`
3. Note the Token and EncodingAESKey from callback settings
4. Get the app's Secret
5. Copy `config.json.example` to `workspace/customer_service_config.json`:

```json
{
  "corp_id": "your enterprise corp_id",
  "corp_secret": "app secret",
  "token": "callback verification token",
  "encoding_aes_key": "43-char AES key from callback settings",
  "host": "0.0.0.0",
  "port": 8081,
  "public_base_url": "https://your-server:8081",
  "open_kfid": "KF account ID (auto-discovered if empty)",
  "forward_chatid": "optional: internal group chatid for forwarding",
  "forward_userid": "optional: individual user ID for forwarding (takes priority over forward_chatid)",
  "agent_id": "required if forward_userid is set"
}
```

6. Run: `python customer_service\server.py` or `customer_service\customer_service.bat`

## Forwarding

Two forwarding modes (forward_userid takes priority if both are set):

- **User forwarding**: set `forward_userid` + `agent_id` → forwards Q&A via `message/send` API
- **Group forwarding**: set `forward_chatid` → forwards Q&A via `appchat/send` API

Use `create_group.py` to create an appchat group:
```bash
python create_group.py --config workspace/customer_service_config.json --list-users
python create_group.py --config workspace/customer_service_config.json --chatid mygroup --name "CS转发群" --users UserA,UserB
```

## Startup behavior

On startup, the server drains any accumulated messages (advances cursor without processing), so only new messages arriving after startup get handled.

## File Handling

The customer service handler downloads WeCom file-like messages into the configured `workspace_dir` before invoking the AI backend.

Supported inbound file extensions include:

- Documents and sheets: `.pdf`, `.docx`, `.doc`, `.xlsx`, `.xls`, `.pptx`, `.ppt`, `.txt`, `.csv`
- Archives: `.zip`, `.rar`, `.7z`, `.tar`, `.gz`
- Images: `.jpg`, `.jpeg`, `.png`, `.gif`, `.webp`, `.bmp`

Generated files can be returned with workspace-relative paths such as `result.xlsx` or `outputs/result.zip`; the service resolves them under `workspace_dir`, uploads them to WeCom, and sends them back as file or image messages.

If WeCom only exposes a large client-side file card as a text filename and does not provide `media_id`, set `public_base_url` in `workspace/customer_service_config.json`. The service will send a one-time `/upload/<token>` link so the user can upload the file through the browser; after upload, the file is saved under `workspace_dir/cs_files` and handled by the normal file workflow.

### Two-stage file rule routing

Every file instruction first goes through a lightweight classifier using the configured Codex CLI model. The classifier receives only the normalized file extension, the current instruction, and the registered rule descriptions. It does not receive the file name, path, file contents, attachment, conversation history, or workspace data.

Only a strict raw-JSON `match` with `high` confidence, a registered rule ID, and a compatible extension runs a local processor. Malformed output, uncertainty, a changed rule, classifier timeout or failure, missing local dependencies, and local processor errors all fall back to the existing full Codex attachment workflow.

The initial registered rule is `payment_match_v1`, which performs the optimized medical-insurance positive-payment and non-insurance negative-payment workbook match. Its local processor keeps the one-load, in-memory validation, one-export workflow. Add or change rules in `customer_service/rule_router.py`; keep processors lazily imported so optional spreadsheet dependencies do not become service startup requirements.

Set the classifier timeout independently in `workspace/model_config.json`:

```json
{
  "codexcli_rule_timeout": 120
}
```

Runtime decisions are logged with the `[rule-router]` prefix. Classification prompts and results are internal and are not appended to customer conversation history.

## Voice transcription

Voice messages can use an OpenAI-compatible transcription API while keeping local faster-whisper as a fallback. Incoming AMR and other WeCom voice formats are converted to a 16 kHz mono WAV before upload.

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

When `voice_asr_api_base` and `voice_asr_api_key` are empty, the service reuses `api_base` and `api_key`. Set `voice_asr_provider` to `local` to use only faster-whisper, or `auto` to use the API when a key is available and otherwise use the local model.

## CLI backend selection

The customer service module reads `workspace/model_config.json`. Set `mode` to choose the AI backend:

```json
{
  "mode": "codexcli",
  "workspace_dir": "D:\\AI\\workspace",
  "codexcli_path": "D:\\AI\\workspace\\codex-cli.cmd",
  "codexcli_model": "gpt-5.6-sol",
  "codexcli_reasoning_effort": "none",
  "codexcli_sandbox": "workspace-write",
  "codexcli_approval_policy": "never",
  "codexcli_permission_mode": "bypass",
  "codexcli_dangerously_bypass": true,
  "codexcli_rule_timeout": 120,
  "codexcli_proxy": ""
}
```

Use `mode: "qodercli"` to keep the existing QoderCLI backend. Use `mode: "codexcli"` to invoke `codex exec` in non-interactive mode.

## CLI permission mode

When using `qodercli` as the AI backend, the `qodercli_permission_mode` field in `model_config.json` controls how permission requests are handled:

- **`bypass`** (default): all permission requests are auto-approved, qoderclicn runs with full autonomy.
- **`ask`**: qoderclicn first runs in "plan only" mode (no execution). If it produces an execution plan, the plan is sent to the user for confirmation. The user replies with `确认`/`同意`/`ok` to proceed, or `取消` to abort. Only after approval does the session resume with full permissions.

When using `codexcli`, the equivalent field is `codexcli_permission_mode`:

- **`bypass`** (default): runs `codex exec` unattended with `codexcli_approval_policy` (default: `never`) and `codexcli_sandbox` (default: `workspace-write`).
- **`ask`**: first runs Codex CLI in read-only planning mode. If it returns an execution plan, the plan is sent to the user for confirmation. After approval, the module runs Codex CLI again to execute the approved plan.

For parity with the previous QoderCLI default, `codexcli_dangerously_bypass` defaults to `true` in bypass mode. This maps to Codex CLI's no-sandbox bypass mode and should only be used in a trusted environment. Set it to `false` to use `codexcli_approval_policy` and `codexcli_sandbox` instead.

## CodexCLI proxy configuration

CodexCLI inherits proxy variables from the service process. You can also configure proxy explicitly in `workspace/model_config.json`, which is recommended for remote servers:

```json
{
  "mode": "codexcli",
  "codexcli_proxy": "http://127.0.0.1:7990",
  "codexcli_no_proxy": "localhost,127.0.0.1,::1"
}
```

On Windows, `codexcli_use_windows_proxy` defaults to `true`, so the backend will also try to read the current user's system proxy from the registry when explicit proxy fields are not set. On Linux servers, set `codexcli_proxy` or standard `HTTP_PROXY`/`HTTPS_PROXY` environment variables.

## Utilities

- `diagnose.py` — tests API connectivity step by step (token, account list, sync_msg, send_msg)
- `create_group.py` — creates appchat groups and lists enterprise users

```bash
python customer_service/diagnose.py --config workspace/customer_service_config.json
```

## Rate limits

WeChat Customer Service allows **5 server→user messages per 48-hour window** per conversation. Quota resets on each user message (mirrors WeCom server behavior).

## Dependencies

Same as wecom/: `wechatpy`, `cryptography`, `flask`, `requests`.
