"""
微信自动回复脚本 (wxauto 版)
- 私聊消息：自动回复所有私聊
- 群聊消息：仅在被 @提及时回复
- 通过 wxautoz (UI自动化) 直接读取微信消息
- 支持多种模型后端：
  - 本地 Ollama (qwen3, llama3 等)
  - 远程 API (OpenAI, DeepSeek, Groq, 硅基流动, 智谱等 OpenAI 兼容格式)
  - Qoder 代理回复 (通过本地中继)

依赖：wxautoz (pip install wxautoz), requests
要求：微信 PC 版 3.9.x 已启动并登录
"""

import json
import re
import time
import requests
import os
import signal
import sys
from datetime import datetime
from collections import defaultdict
import threading
import getpass
import subprocess
import argparse

# ===================== 配置区 =====================

# Ollama 配置
OLLAMA_URL = "http://127.0.0.1:11434"
OLLAMA_MODEL = "qwen3:8b"

# 轮询间隔（秒）
POLL_INTERVAL = 3

# 每轮最多连续读取新消息的次数（防止一次循环耗时过长）
MAX_POLLS_PER_ROUND = 10

# 每个会话保留的最大对话轮数
MAX_HISTORY = 10

# 工作空间目录（配置文件、日志、运行时数据）
WORKSPACE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workspace")

# 配置文件路径
CONFIG_PATH = os.path.join(WORKSPACE_DIR, "auto_reply_config.json")

# 模型选择配置文件路径
MODEL_CONFIG_PATH = os.path.join(WORKSPACE_DIR, "model_config.json")

# Qoder 中继服务器端口
QODER_RELAY_PORT = 11435

# 远程 API 预设
API_PRESETS = {
    "1": ("DeepSeek", "https://api.deepseek.com/v1", "deepseek-chat"),
    "2": ("OpenAI", "https://api.openai.com/v1", "gpt-4o-mini"),
    "3": ("OpenRouter", "https://openrouter.ai/api/v1", "google/gemini-2.0-flash-exp:free"),
    "4": ("Groq", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    "5": ("硅基流动 SiliconFlow", "https://api.siliconflow.cn/v1", "Qwen/Qwen2.5-72B-Instruct"),
    "6": ("智谱 GLM", "https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
}

# 远程可用模型（共享同一 API 连接，直接选择即可）
REMOTE_MODELS = [
    "gemini-3.5-flash",
    "glm-5.1",
    "qwen3.7-max",
]

# ===================== 默认配置 =====================

DEFAULT_CONFIG = {
    "enabled": True,

    # --- 私聊设置 ---
    "dm_enabled": True,
    "dm_system_prompt": "",
    "dm_blacklist": [],
    "dm_whitelist_mode": False,
    "dm_whitelist": [],

    # --- 群聊设置 ---
    "group_enabled": True,
    "group_system_prompt": "",
    "group_blacklist": [],
    "group_whitelist_mode": False,
    "group_whitelist": [],
    "group_trigger_words": [],
    "group_mention_names": ["@机器人", "@bot", "@助手"],

    # --- 通用设置 ---
    "reply_delay": 2,
    "log_file": "auto_reply.log"
}

# ===================== 全局状态 =====================

# 会话历史（按聊天名称分组）
conversation_history = defaultdict(list)

# 已处理消息去重 (msg_id 集合)
processed_msg_ids = set()

# wxauto WeChat 实例
wx = None

# 当前微信登录用户名
self_name = ""

# 当前选中的模型配置
selected_model = {"mode": "ollama", "model": OLLAMA_MODEL}


def load_config():
    """加载配置文件，不存在则创建默认配置"""
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
        for key, value in DEFAULT_CONFIG.items():
            if key not in config:
                config[key] = value
        return config
    else:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        print(f"[配置] 已创建默认配置文件: {CONFIG_PATH}")
        return DEFAULT_CONFIG.copy()


def log(message, config=None):
    """打印日志并写入日志文件"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{timestamp}] {message}"
    print(log_line, flush=True)
    if config:
        log_file = os.path.join(WORKSPACE_DIR,
                                config.get("log_file", "auto_reply.log"))
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(log_line + "\n")
        except Exception:
            pass


# ===================== wxauto 封装 =====================

def init_wechat(retries=3):
    """初始化微信实例"""
    global wx
    from wxautoz import WeChat

    for i in range(retries):
        try:
            wx = WeChat()
            name = getattr(wx, 'nickname', '') or '未知'
            print(f"[微信] 已连接，当前账号: {name}", flush=True)
            # 验证实例是否可用
            sessions = wx.GetSession()
            print(f"[微信] 当前会话数: {len(sessions)}", flush=True)
            return wx
        except Exception as e:
            print(f"[微信] 连接失败 (尝试 {i+1}/{retries}): {e}", flush=True)
            if i < retries - 1:
                time.sleep(3)

    print("[微信] 无法连接微信，请确认微信已启动并登录", flush=True)
    return None


def poll_new_messages():
    """
    获取新消息（一次只获取一个会话的未读消息）
    返回: [(chat_name, chat_type, sender_attr, content, msg_id), ...]
    """
    global wx
    results = []
    if not wx:
        return results

    try:
        # GetNextNewMessage 返回:
        # {}  (无新消息)
        # {'chat_name': str, 'chat_type': str, 'msg': [Message, ...]}
        result = wx.GetNextNewMessage()

        if not result:
            return results

        chat_name = result.get('chat_name', '未知')
        chat_type = result.get('chat_type', 'unknown')
        messages = result.get('msg', [])

        if not messages:
            return results

        print(f"[消息] 来自 [{chat_type}]{chat_name}，{len(messages)} 条:", flush=True)

        for msg in messages:
            # 获取消息属性
            sender_attr = getattr(msg, 'attr', 'unknown')  # 'friend', 'self', 'system' 等
            content = getattr(msg, 'content', '') or ''
            msg_id = getattr(msg, 'id', None) or id(msg)
            msg_type = getattr(msg, 'type', 'unknown')

            print(f"  [{sender_attr}/{msg_type}] {content[:80]}", flush=True)

            results.append((chat_name, chat_type, sender_attr, content, msg_id))

    except Exception as e:
        err_str = str(e)
        # 忽略常见的无害异常（如列表为空、控件不存在等）
        if not any(kw in err_str.lower() for kw in ['list index', 'index out of range', 'not found']):
            print(f"[调试] 获取消息异常: {err_str[:200]}", flush=True)

    return results


def send_reply(chat_name, text):
    """发送回复到指定聊天"""
    global wx
    if not wx:
        return False
    try:
        print(f"[发送] 切换到 {chat_name}...", flush=True)
        wx.ChatWith(chat_name)
        time.sleep(0.5)
        print(f"[发送] 正在发送消息 ({len(text)}字)...", flush=True)
        result = wx.SendMsg(text)
        success = bool(result)
        print(f"[发送] 结果: {'成功' if success else '失败'} - {result.get('message', '') if isinstance(result, dict) else result}", flush=True)
        return success
    except Exception as e:
        print(f"[错误] 发送失败: {e}", flush=True)
        return False


# ===================== 模型选择与管理 =====================

def load_model_config():
    """加载模型选择配置"""
    if os.path.exists(MODEL_CONFIG_PATH):
        try:
            with open(MODEL_CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"mode": "ollama", "model": OLLAMA_MODEL}


def save_model_config(cfg):
    """保存模型选择配置，合并已有字段避免丢失额外配置（如 bocha_api_key）"""
    existing = {}
    if os.path.exists(MODEL_CONFIG_PATH):
        try:
            with open(MODEL_CONFIG_PATH, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            pass
    existing.update(cfg)
    with open(MODEL_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)


def get_ollama_models():
    """获取 Ollama 可用模型列表"""
    try:
        resp = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        resp.raise_for_status()
        models = resp.json().get("models", [])
        return [(m["name"], m.get("size", 0)) for m in models]
    except Exception:
        return []


def format_size(size_bytes):
    """格式化文件大小"""
    if not size_bytes:
        return ""
    gb = size_bytes / (1024 ** 3)
    if gb >= 1:
        return f" ({gb:.1f}GB)"
    mb = size_bytes / (1024 ** 2)
    return f" ({mb:.0f}MB)"


def model_selection_menu():
    """显示模型选择菜单并获取用户选择（两步流程：先选类别，再选模型）"""
    global selected_model

    # 尝试加载上次配置
    saved = load_model_config()

    while True:
        has_remote = bool(saved.get("api_base") and saved.get("api_key"))
        last_mode = saved.get("mode", "")

        print("", flush=True)
        print("=" * 55, flush=True)
        print("  选择回复模型", flush=True)
        print("=" * 55, flush=True)

        # --- 第一步：选择类别 ---
        last_label = ""
        if last_mode == "remote":
            last_label = f" (上次: {saved.get('model', '')} / {saved.get('remote_provider', '')})"
        elif last_mode == "ollama":
            last_label = f" (上次: {saved.get('model', '')})"
        elif last_mode == "qoder":
            last_label = " (上次: Qoder 代理)"

        print(f"\n  1. 远程大模型 API{' (已配置)' if has_remote else ''}", flush=True)
        print(f"  2. 本地大模型 (Ollama)", flush=True)
        print(f"  3. Qoder 代理回复", flush=True)

        if last_mode:
            print(f"\n  [Enter] 使用上次选择{last_label}", flush=True)

        print("", flush=True)
        cat = input("  请选择 [1-3]: ").strip()

        # Enter 键使用上次配置
        if not cat and last_mode:
            selected_model = saved
            print(f"\n  -> 使用上次选择: {last_label.strip()}", flush=True)
            return

        # ===== 远程 API =====
        if cat == "1":
            if not has_remote:
                # 无连接信息，先配置
                remote_cfg = config_remote_api(saved)
                if remote_cfg:
                    selected_model = remote_cfg
                    save_model_config(remote_cfg)
                    print(f"\n  -> 已选择远程模型: {remote_cfg['model']}", flush=True)
                    return
                continue  # 配置取消，回到菜单

            # 有连接信息，列出模型供选择
            provider = saved.get("remote_provider", "远程API")
            print(f"\n  连接: {saved['api_base']} ({provider})", flush=True)
            print(f"  可用模型:", flush=True)
            for i, name in enumerate(REMOTE_MODELS, 1):
                tag = " <--" if last_mode == "remote" and saved.get("model") == name else ""
                print(f"    {i}. {name}{tag}", flush=True)
            print(f"    {len(REMOTE_MODELS) + 1}. 重新配置连接信息", flush=True)
            print("", flush=True)

            m_choice = input(f"  选择模型 [1-{len(REMOTE_MODELS) + 1}]: ").strip()
            if not m_choice and last_mode == "remote":
                selected_model = saved
                print(f"\n  -> 使用上次选择: {saved.get('model', '')}", flush=True)
                return

            try:
                m_idx = int(m_choice)
            except ValueError:
                print("  无效选择", flush=True)
                continue

            if 1 <= m_idx <= len(REMOTE_MODELS):
                entry = {
                    "mode": "remote",
                    "model": REMOTE_MODELS[m_idx - 1],
                    "api_base": saved["api_base"],
                    "api_key": saved["api_key"],
                    "remote_provider": provider,
                }
                selected_model = entry
                save_model_config(entry)
                print(f"\n  -> 已选择远程模型: {entry['model']}", flush=True)
                return
            elif m_idx == len(REMOTE_MODELS) + 1:
                remote_cfg = config_remote_api(saved)
                if remote_cfg:
                    selected_model = remote_cfg
                    save_model_config(remote_cfg)
                    return
                continue
            else:
                print("  无效选择", flush=True)
                continue

        # ===== 本地 Ollama =====
        elif cat == "2":
            print("\n[模型] 正在扫描本地模型...", flush=True)
            ollama_models = get_ollama_models()
            if not ollama_models:
                print("[模型] 未检测到 Ollama 或未安装任何模型", flush=True)
                input("  按 Enter 返回...")
                continue

            print(f"\n  可用本地模型:", flush=True)
            for i, (name, size) in enumerate(ollama_models, 1):
                tag = " <--" if last_mode == "ollama" and saved.get("model") == name else ""
                print(f"    {i}. {name}{format_size(size)}{tag}", flush=True)
            print("", flush=True)

            m_choice = input(f"  选择模型 [1-{len(ollama_models)}]: ").strip()
            if not m_choice and last_mode == "ollama":
                selected_model = saved
                print(f"\n  -> 使用上次选择: {saved.get('model', '')}", flush=True)
                return

            try:
                m_idx = int(m_choice)
            except ValueError:
                print("  无效选择", flush=True)
                continue

            if 1 <= m_idx <= len(ollama_models):
                entry = {"mode": "ollama", "model": ollama_models[m_idx - 1][0]}
                selected_model = entry
                save_model_config(entry)
                print(f"\n  -> 已选择本地模型: {entry['model']}", flush=True)
                return
            else:
                print("  无效选择", flush=True)
                continue

        # ===== Qoder =====
        elif cat == "3":
            qoder_cfg = config_qoder_relay()
            if qoder_cfg:
                selected_model = qoder_cfg
                save_model_config(qoder_cfg)
                return
            continue

        else:
            print("  无效选择，请重试", flush=True)
            continue


def config_remote_api(saved):
    """交互式配置远程 API 连接信息"""
    print("\n" + "-" * 55, flush=True)
    print("  远程 API 连接配置", flush=True)
    print("-" * 55, flush=True)

    # 如果已有旧配置，显示并询问是否复用
    old_base = saved.get("api_base", "")
    old_key = saved.get("api_key", "")
    old_provider = saved.get("remote_provider", "")

    if old_base:
        print(f"\n  当前连接: {old_base} ({old_provider})", flush=True)
        reuse = input("  复用当前连接信息? [Y/n]: ").strip().lower()
        if reuse != 'n':
            base_url = old_base
            api_key = old_key
            provider_name = old_provider
            # 跳到测试环节
            return _test_and_save_remote(base_url, api_key, provider_name, saved)

    # 新配置
    print(f"\n  可用模型: {', '.join(REMOTE_MODELS)}", flush=True)
    base_url = input("\n  API Base URL (如 https://api.example.com/v1): ").strip()
    if not base_url:
        print("  已取消", flush=True)
        return None

    api_key = getpass.getpass("  API Key (输入时不可见): ").strip()
    if not api_key and old_key:
        reuse = input("  未输入 API Key，使用上次的? [Y/n]: ").strip().lower()
        if reuse != 'n':
            api_key = old_key
    if not api_key:
        print("  未提供 API Key，已取消", flush=True)
        return None

    provider_name = input("  连接名称 (如 Gemini Flash): ").strip() or "远程API"

    return _test_and_save_remote(base_url, api_key, provider_name, saved)


def _test_and_save_remote(base_url, api_key, provider_name, saved):
    """测试远程连接并返回配置"""
    test_model = REMOTE_MODELS[0] if REMOTE_MODELS else "gemini-3.5-flash"
    print(f"\n  正在测试连接 (使用 {test_model})...", flush=True)
    try:
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": test_model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5},
            timeout=15
        )
        if resp.status_code in (200, 201):
            print(f"  连接成功!", flush=True)
        else:
            print(f"  连接返回 {resp.status_code}: {resp.text[:100]}", flush=True)
            cont = input("  是否仍要使用? [y/N]: ").strip().lower()
            if cont != 'y':
                return None
    except Exception as e:
        print(f"  连接失败: {e}", flush=True)
        cont = input("  是否仍要保存? [y/N]: ").strip().lower()
        if cont != 'y':
            return None

    cfg = {
        "mode": "remote",
        "remote_provider": provider_name,
        "api_base": base_url,
        "model": test_model,
        "api_key": api_key,
    }
    return cfg


def config_qoder_relay():
    """配置 Qoder 中继服务"""
    print("\n" + "-" * 55, flush=True)
    print("  Qoder 代理回复配置", flush=True)
    print("-" * 55, flush=True)
    print("\n  工作原理:", flush=True)
    print("  1. 脚本在本地启动 HTTP 中继服务 (端口 {})".format(QODER_RELAY_PORT), flush=True)
    print("  2. 收到微信消息后，将消息发送到中继", flush=True)
    print("  3. QoderWork 接收请求并生成回复", flush=True)
    print("  4. 回复通过中继返回并发送到微信", flush=True)
    print("\n  注意: 需要 QoderWork 在运行中才能生成回复", flush=True)
    print("        如果 QoderWork 未运行，会等待最多 30 秒后超时", flush=True)

    cont = input("\n  启动 Qoder 中继? [Y/n]: ").strip().lower()
    if cont == 'n':
        return None

    cfg = {
        "mode": "qoder",
        "model": "qoder-relay",
        "relay_port": QODER_RELAY_PORT,
    }
    print(f"\n  -> Qoder 中继已配置 (端口 {QODER_RELAY_PORT})", flush=True)
    return cfg


# ===================== 回复生成 (多后端) =====================

def generate_reply_ollama(messages_history, system_prompt, model_name):
    """通过本地 Ollama 生成回复"""
    api_messages = [{"role": "system", "content": system_prompt}]
    api_messages.extend(messages_history[-MAX_HISTORY * 2:])

    try:
        print(f"[{model_name}] 正在生成回复 (上下文 {len(api_messages)} 条)...", flush=True)
        resp = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": model_name,
                "messages": api_messages,
                "stream": False,
                "options": {
                    "temperature": 0.8,
                    "num_predict": selected_model.get("ollama_max_tokens", 8192)
                    # 注意：不要设置 stop 参数，qwen3 的 thinking 模式中会产出 \n\n，
                    # 若设 stop=["\n\n"] 会导致 thinking 阶段提前终止，content 为空
                }
            },
            timeout=60
        )
        resp.raise_for_status()
        data = resp.json()
        msg = data.get("message", {})
        reply = msg.get("content", "").strip()

        # qwen3 思考模式：若 content 为空，尝试从 thinking 字段末尾提取回复
        if not reply:
            thinking = msg.get("thinking", "")
            if thinking:
                parts = thinking.rsplit("\n\n", 1)
                if len(parts) > 1 and len(parts[-1]) < 200:
                    reply = parts[-1].strip()
                    print(f"[{model_name}] 从 thinking 字段提取回复", flush=True)

        # 清理常见前缀
        for prefix in ["助手：", "AI：", "Assistant: ", "Bot: "]:
            if reply.startswith(prefix):
                reply = reply[len(prefix):].strip()

        # 清理无效 Unicode 代理字符
        reply = re.sub(r'[\ud800-\udfff]', '', reply)

        result = reply if reply else "抱歉，我没想好怎么回复。"
        print(f"[{model_name}] 回复: {result[:80]}", flush=True)
        return result

    except requests.exceptions.ConnectionError:
        print("[错误] 无法连接 Ollama，请确认已启动", flush=True)
        return None
    except Exception as e:
        print(f"[错误] Ollama 调用失败: {e}", flush=True)
        return None


# ===================== 网络搜索辅助 =====================

# 搜索意图关键词
SEARCH_KEYWORDS = [
    "天气", "新闻", "最新", "搜索", "查一下", "帮我查", "搜一下",
    "实时", "今天", "现在", "最近", "刚刚", "昨天", "明天",
    "多少钱", "股价", "汇率", "比分", "热搜", "排行",
    "联网", "网上", "百度", "谷歌", "搜素", "查找",
    "发生了什么", "怎么了", "出事", "突发",
]

# 模型回避/无法回答的特征短语（正式 + 口语化）
EVASIVE_PHRASES = [
    # 正式表达
    "无法获取", "没有联网", "看不到", "无法搜索", "没有实时",
    "无法提供实时", "没有网络", "无法查询", "不支持联网",
    "我无法", "抱歉，我无法", "抱歉，我不能", "作为AI", "作为人工智能",
    "没有访问", "无法访问", "信息截止",
    "knowledge cutoff", "训练数据",
    "无法确定", "不太清楚具体", "不确定具体",
    # 口语化表达（模型常用来绕过检测）
    "查不到", "查不了", "没查到", "不知道具体", "不太清楚",
    "不太了解", "没发查", "没法查", "看不到具体",
    "不联网", "上不了网", "网卡了", "断网了",
    "我这边没", "我这没法", "我这边看不了",
    "建议你自己", "你还是自己", "自己查一下",
    "没有实时", "没有最新", "获取不到",
    # 更多口语变体
    "网不太好", "网络不太好", "连不上网", "网速不行",
    "没法帮你", "帮不了你", "帮不到你",
    "你自己看", "你自己查", "自己瞅一眼", "自己看看",
    "看看窗外", "瞅一眼手机", "看看手机",
    "不太确定", "我说不好", "说不好",
]

# 回避回复的正则模式（更智能的检测）
EVASIVE_PATTERNS = [
    r'(?:查|搜|看|找|获取).{0,3}(?:不到|不了|不出来|不出来)',  # 查不到/搜不了
    r'(?:没有|没).{0,3}(?:联网|网络|实时|最新)',  # 没有联网/没实时
    r'(?:我|这边).{0,10}(?:无法|没法|不能|帮不了|看不了|查不了)',  # 我...没法（放宽间距）
    r'(?:网|网络).{0,3}(?:不太好|不好|不行|有问题|断了|断了)',  # 网不太好/网络不好
    r'(?:你自己|自己|你还是).{0,5}(?:看看|查查|搜搜|瞅|打开)',  # 你自己看看
]


def needs_web_search(user_message):
    """检测用户消息是否包含搜索意图"""
    msg = user_message.lower()
    matched = [kw for kw in SEARCH_KEYWORDS if kw in msg]
    if matched:
        print(f"[搜索判定] 用户消息命中搜索关键词: {matched}", flush=True)
    return bool(matched)


def response_is_evasive(reply):
    """检测模型回复是否回避/无法回答问题。
    对长回复(>100字)需要2个以上回避短语命中才判定，减少误判。
    """
    if not reply or len(reply) < 15:
        print(f"[搜索判定] 回复过短({len(reply or '')}字)，判定为回避", flush=True)
        return True
    # 短语匹配
    matched = [p for p in EVASIVE_PHRASES if p in reply]
    if matched:
        # 长回复中有1个边界短语可能是正常引用，需要2+命中才判定回避
        if len(reply) > 100 and len(matched) < 2:
            print(f"[搜索判定] 长回复({len(reply)}字)中仅命中{matched}，不判定为回避", flush=True)
            return False
        print(f"[搜索判定] 回复命中回避短语: {matched}", flush=True)
        return True
    # 正则模式匹配
    for pattern in EVASIVE_PATTERNS:
        if re.search(pattern, reply):
            if len(reply) > 100:
                print(f"[搜索判定] 长回复({len(reply)}字)中命中模式{pattern}，不判定为回避", flush=True)
                return False
            print(f"[搜索判定] 回复命中回避模式: {pattern}", flush=True)
            return True
    return False


# 搜索指令性词语（从用户消息中去掉这些再搜索）
SEARCH_COMMAND_WORDS = [
    "联网查询", "联网搜索", "帮我查一下", "帮我搜一下",
    "帮我查", "帮我搜", "帮我分析", "帮我看看",
    "查一下", "搜一下", "分析一下", "分析下",
    "搜索一下", "查询一下", "一下",
    "联网", "搜索", "查询", "查找", "搜素", "分析",
    "请问", "请帮我", "帮我看", "帮我看看",
    "告诉我", "介绍", "了解", "说说",
]


def extract_search_query(user_message):
    """从用户消息中提取干净的搜索查询词"""
    query = user_message.strip()
    # 去掉指令性词语
    for cmd in SEARCH_COMMAND_WORDS:
        query = query.replace(cmd, "")
    query = query.strip(" ,，.。!！?？、")
    # 如果清理后太短，就用原始消息
    if len(query) < 3:
        query = user_message.strip()
    return query


# 天气相关关键词
WEATHER_KEYWORDS = ["天气", "气温", "温度", "下雨", "下雪", "多少度", "穿什么", "带伞", "防晒"]


def is_weather_query(user_message):
    """判断是否为天气查询"""
    return any(kw in user_message for kw in WEATHER_KEYWORDS)


def extract_location(query):
    """从查询中提取地名"""
    import re as _re
    # 去掉日期和天气词，剩下的可能就是地名
    cleaned = query
    for word in ["天气", "气温", "温度", "今天", "明天", "昨天", "后天",
                 "多少度", "下雨", "下雪", "穿什么", "带伞", "防晒",
                 "查询", "搜索", "查一下", "联网", "分析", "分析下",
                 "帮我", "下", "的"]:
        cleaned = cleaned.replace(word, "")
    # 去掉日期格式
    cleaned = _re.sub(r'\d{4}年\d{2}月\d{2}日', '', cleaned)
    cleaned = cleaned.strip(" ,，.。!！?？、")
    return cleaned if len(cleaned) >= 2 else query


def weather_search(query, max_results=5):
    """使用 wttr.in 免费天气 API 获取实时天气数据，失败时走 Bocha/Bing 网络搜索"""
    location = extract_location(query)
    print(f"[天气API] 提取地名: {location}", flush=True)
    weather_data = ""
    try:
        resp = requests.get(
            f"https://wttr.in/{location}?format=j1&lang=zh",
            timeout=20
        )
        resp.raise_for_status()
        data = resp.json()

        current = data.get("current_condition", [{}])[0]
        today = data.get("weather", [{}])[0]
        tomorrow = data.get("weather", [{}])[1] if len(data.get("weather", [])) > 1 else {}

        # 天气描述中文映射
        weather_desc = current.get("lang_zh", [{}])[0].get(
            "value", current.get("weatherDesc", [{}])[0].get("value", "未知")
        )

        lines = [
            f"【{location}实时天气】",
            f"当前温度: {current.get('temp_C', '?')}°C，体感温度: {current.get('FeelsLikeC', '?')}°C",
            f"天气状况: {weather_desc}",
            f"湿度: {current.get('humidity', '?')}%，风速: {current.get('windspeedKmph', '?')} km/h",
            f"能见度: {current.get('visibility', '?')} km",
        ]

        if today:
            lines.append(f"\n【今日预报 ({today.get('date', '今天')})】")
            lines.append(f"最高温: {today.get('maxtempC', '?')}°C，最低温: {today.get('mintempC', '?')}°C")
            # 逐时段预报
            for h in today.get("hourly", []):
                time_val = h.get("time", "0").zfill(4)
                hour = time_val[:2] + ":" + time_val[2:] if len(time_val) >= 4 else time_val
                desc = h.get("lang_zh", [{}])[0].get("value", h.get("weatherDesc", [{}])[0].get("value", ""))
                lines.append(f"  {hour} - {h.get('tempC', '?')}°C, {desc}, 降水概率{h.get('chanceofrain', '?')}%")

        if tomorrow:
            lines.append(f"\n【明日预报 ({tomorrow.get('date', '明天')})】")
            lines.append(f"最高温: {tomorrow.get('maxtempC', '?')}°C，最低温: {tomorrow.get('mintempC', '?')}°C")

        result = "\n".join(lines)
        print(f"[天气API] 获取成功，数据长度: {len(result)} 字符", flush=True)
        return result

    except Exception as e:
        print(f"[天气API] 获取失败: {e}，回退博查/Bing搜索天气", flush=True)

    # wttr.in 失败，通过博查/Bing搜索天气信息
    print(f"[天气API] 使用网络搜索获取 {location} 天气", flush=True)
    bocha_results = _search_bocha(f"{location} 今天天气预报 温度 降水")
    if bocha_results:
        return bocha_results
    return web_search(f"{location} 今天天气")


def _search_bocha(query, max_results=5):
    """博查搜索 API - 国内最稳定的搜索后端"""
    bocha_api_key = selected_model.get("bocha_api_key", "")
    if not bocha_api_key:
        print("[搜索] Bocha 跳过: 未配置 bocha_api_key", flush=True)
        return ""
    try:
        resp = requests.post(
            "https://api.bochaai.com/v1/web-search",
            headers={
                "Authorization": f"Bearer {bocha_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "query": query,
                "freshness": "noLimit",
                "summary": True,
                "count": max_results,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        pages = data.get("data", {}).get("webPages", {}).get("value", [])
        if not pages:
            return ""

        lines = []
        for i, page in enumerate(pages[:max_results], 1):
            title = page.get("name", "")
            snippet = page.get("summary", page.get("snippet", ""))
            href = page.get("url", "")
            lines.append(f"[{i}] {title}\n{snippet}\n来源: {href}")

        result = "\n\n".join(lines)
        print(f"[搜索] Bocha 返回 {len(pages)} 条结果", flush=True)
        return result
    except Exception as e:
        print(f"[搜索] Bocha 搜索失败: {e}", flush=True)
        return ""


def web_search(query, max_results=5):
    """搜索瀑布：Bocha -> ddgs/Bing -> Bing 抓取"""
    # 1. Bocha（国内最稳定）
    results = _search_bocha(query, max_results)
    if results:
        return results
    # 2. DuckDuckGo + Bing 后端
    results = _search_ddgs(query, max_results)
    if results:
        return results
    # 3. Bing 网页抓取
    print(f"[搜索] DDG 无结果，尝试 Bing...", flush=True)
    return _search_bing(query, max_results)


def _search_ddgs(query, max_results=5):
    """DuckDuckGo 搜索（ddgs 包）"""
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        if not results:
            return ""
        print(f"[搜索] DDG 返回 {len(results)} 条结果", flush=True)
        lines = []
        for i, r in enumerate(results, 1):
            title = r.get("title", "")
            body = r.get("body", "")
            url = r.get("href", "")
            lines.append(f"[{i}] {title}\n{body}\n来源: {url}")
        return "\n\n".join(lines)
    except Exception as e:
        print(f"[搜索] DDG 搜索失败: {e}", flush=True)
        return ""


def _search_bing(query, max_results=5):
    """Bing 网页抓取搜索（无需 API key）"""
    try:
        from urllib.parse import quote_plus
        url = f"https://cn.bing.com/search?q={quote_plus(query)}&ensearch=0"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/125.0.0.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        html = resp.text

        import re as _re
        results = []

        # 提取 h2>a 标题和链接
        titles = _re.findall(
            r'<h2[^>]*><a[^>]*href="([^"]+)"[^>]*>(.*?)</a></h2>',
            html, _re.DOTALL
        )
        # 提取 b_caption 中的摘要
        snippets = _re.findall(
            r'<div class="b_caption"><p[^>]*>(.*?)</p>',
            html, _re.DOTALL
        )

        for i, (href, title_raw) in enumerate(titles[:max_results]):
            title = _re.sub(r'<[^>]+>', '', title_raw).strip()
            snippet = _re.sub(r'<[^>]+>', '', snippets[i]).strip() if i < len(snippets) else ""
            if title and len(title) > 3:
                results.append(f"[{len(results)+1}] {title}\n{snippet}\n来源: {href}")

        if results:
            print(f"[搜索] Bing 返回 {len(results)} 条结果", flush=True)
            return "\n\n".join(results)
        return ""
    except Exception as e:
        print(f"[搜索] Bing 搜索失败: {e}", flush=True)
        return ""


def generate_reply_remote(messages_history, system_prompt):
    """通过远程 OpenAI 兼容 API 生成回复"""
    global selected_model
    api_base = selected_model.get("api_base", "").rstrip("/")
    model_name = selected_model.get("model", "")
    api_key = selected_model.get("api_key", "")
    override_prompt = selected_model.get("system_prompt_override", "")
    if override_prompt:
        system_prompt = override_prompt

    # 追加禁止 tool call 输出的指令
    system_prompt += (
        "\n\n重要规则：你必须直接用自然语言回复，不要输出任何JSON格式、"
        "工具调用（如call/get_weather/web_search等）、函数调用或代码块。"
        "如果你无法回答某个问题，直接用口语化的方式说明即可。"
    )

    api_messages = [{"role": "system", "content": system_prompt}]
    api_messages.extend(messages_history[-MAX_HISTORY * 2:])

    try:
        label = selected_model.get("remote_provider", "远程API")
        print(f"[{label}/{model_name}] 正在生成回复 (上下文 {len(api_messages)} 条)...", flush=True)
        resp = requests.post(
            f"{api_base}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            json={
                "model": model_name,
                "messages": api_messages,
                "temperature": 0.8,
                "max_tokens": selected_model.get("remote_max_tokens", 8192),
            },
            timeout=60
        )
        resp.raise_for_status()
        data = resp.json()
        choice = data.get("choices", [{}])[0]
        reply = choice.get("message", {}).get("content", "").strip()
        finish = choice.get("finish_reason", "")
        if finish == "length":
            print(f"[警告] 回复被截断 (finish_reason=length)，可能需要增大 max_tokens", flush=True)

        for prefix in ["助手：", "AI：", "Assistant: ", "Bot: "]:
            if reply.startswith(prefix):
                reply = reply[len(prefix):].strip()

        reply = re.sub(r'[\ud800-\udfff]', '', reply)

        # --- 清理 tool call 格式输出 ---
        tool_call_detected = False
        # 检测 "call\n{...}" 或直接 JSON 工具调用
        if re.match(r'^(call|function|tool)\s*\n?\s*\{', reply, re.IGNORECASE):
            tool_call_detected = True
            print(f"[搜索链路] 检测到 tool call 输出: {reply[:80]}", flush=True)
            reply = ""
        # 检测纯 JSON 输出（{"name": ..., "arguments": ...}）
        elif re.match(r'^\s*\{.*"name"\s*:.*"arguments"', reply, re.DOTALL):
            tool_call_detected = True
            print(f"[搜索链路] 检测到 JSON 工具调用输出", flush=True)
            reply = ""

        # --- 网络搜索重试逻辑 ---
        # 提取最后一条用户消息
        user_msg = ""
        for m in reversed(messages_history):
            if m.get("role") == "user":
                user_msg = m.get("content", "")
                break

        print(f"[搜索链路] 用户消息: {user_msg[:60]}", flush=True)
        print(f"[搜索链路] 模型首次回复: {reply[:80]}", flush=True)

        search_needed = needs_web_search(user_msg)
        # tool call 输出或回避性回复都需要搜索重试
        evasive = (tool_call_detected or response_is_evasive(reply)) if search_needed else False

        print(f"[搜索链路] 搜索意图={search_needed}, 回复回避={evasive}", flush=True)

        if search_needed and evasive:
            print(f"[搜索链路] >>> 触发搜索重试", flush=True)
            search_query = extract_search_query(user_msg)
            # 将"今天/明天/昨天"替换为具体日期，让搜索结果更精准
            from datetime import timedelta as _td
            today = datetime.now()
            date_map = {
                "今天": today.strftime("%Y年%m月%d日"),
                "明天": (today + _td(days=1)).strftime("%Y年%m月%d日"),
                "昨天": (today - _td(days=1)).strftime("%Y年%m月%d日"),
                "后天": (today + _td(days=2)).strftime("%Y年%m月%d日"),
            }
            for rel, abs_date in date_map.items():
                if rel in search_query:
                    search_query = search_query.replace(rel, abs_date)
                    break
            print(f"[搜索链路] 提取搜索词: {search_query}", flush=True)
            # 天气查询：Bocha网络搜索优先（国内稳定），wttr.in API 作为补充
            if is_weather_query(user_msg):
                location = extract_location(search_query)
                print(f"[搜索链路] 天气查询，优先 Bocha 网络搜索，地名: {location}", flush=True)
                search_results = _search_bocha(f"{location} 今天天气预报 温度 降水")
                if not search_results:
                    search_results = web_search(f"{location} 今天天气")
                if not search_results:
                    print(f"[搜索链路] Bocha/Bing 无结果，回退 wttr.in 天气API", flush=True)
                    search_results = weather_search(search_query)
            else:
                search_results = web_search(search_query)
            if search_results:
                print(f"[搜索链路] 搜索结果长度: {len(search_results)} 字符", flush=True)
                now_str = datetime.now().strftime("%Y年%m月%d日 %A")
                search_context = (
                    f"你是一个智能助手。以下是系统通过实时网络搜索获取到的最新信息。\n\n"
                    f"当前时间: {now_str}\n\n"
                    f"===== 搜索结果 =====\n{search_results}\n===== 搜索结果结束 =====\n\n"
                    f"重要要求：\n"
                    f"1. 你必须基于以上搜索结果来回答用户的问题。\n"
                    f"2. 绝对不要说你无法搜索、没有联网、查不到或获取不到——数据已经在上面了。\n"
                    f"3. 如果搜索结果中包含天气、新闻等实时数据，直接告诉用户具体内容。\n"
                    f"4. 用自然口语化的方式回复，简洁明了。\n"
                )
                retry_messages = [{"role": "system", "content": search_context}]
                retry_messages.extend(messages_history[-MAX_HISTORY * 2:])

                best_reply = None
                try:
                    print(f"[搜索链路] 正在用搜索上下文重新调用API...", flush=True)
                    resp2 = requests.post(
                        f"{api_base}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json"
                        },
                        json={
                            "model": model_name,
                            "messages": retry_messages,
                            "temperature": 0.7,
                            "max_tokens": selected_model.get("remote_max_tokens", 8192),
                        },
                        timeout=60
                    )
                    resp2.raise_for_status()
                    data2 = resp2.json()
                    choice2 = data2.get("choices", [{}])[0]
                    reply2 = choice2.get("message", {}).get("content", "").strip()

                    for prefix in ["助手：", "AI：", "Assistant: ", "Bot: "]:
                        if reply2.startswith(prefix):
                            reply2 = reply2[len(prefix):].strip()
                    reply2 = re.sub(r'[\ud800-\udfff]', '', reply2)

                    # 记录最佳回复，即使仍有轻微回避用语
                    if reply2:
                        best_reply = reply2

                    if reply2 and not response_is_evasive(reply2):
                        reply = reply2
                        print(f"[搜索链路] 搜索重试成功，使用新回复", flush=True)
                    else:
                        # 优先使用搜索后的回复，而非原始回避回复
                        if best_reply and len(best_reply) > len(reply or ""):
                            reply = best_reply
                            print(f"[搜索链路] 搜索重试回复略含回避用语，但优于原始回复，已采用", flush=True)
                        else:
                            print(f"[搜索链路] 搜索重试后回复仍不理想，使用原始回复", flush=True)
                except Exception as e:
                    print(f"[搜索链路] 重试调用失败: {e}，使用原始回复", flush=True)
            else:
                print(f"[搜索链路] DuckDuckGo 未返回结果，使用原始回复", flush=True)
        else:
            if not search_needed:
                print(f"[搜索链路] 无搜索意图，跳过搜索", flush=True)

        result = reply if reply else "抱歉，我没想好怎么回复。"
        # 最终安全兜底：防止 tool call 或 JSON 格式输出发送给用户
        if re.match(r'^(call|function|tool)\s*\n?\s*\{', result, re.IGNORECASE) or \
           re.match(r'^\s*\{.*"name"\s*:', result, re.DOTALL):
            print(f"[搜索链路] 兜底拦截：最终回复仍含工具调用格式，已替换", flush=True)
            result = "这个问题我帮你查了一下，不过暂时没找到准确的信息，你可以再问我一次试试。"
        print(f"[{label}] 最终回复: {result[:80]}", flush=True)
        return result

    except requests.exceptions.ConnectionError:
        print(f"[错误] 无法连接远程 API ({api_base})", flush=True)
        return None
    except requests.exceptions.HTTPError as e:
        print(f"[错误] API 返回错误: {e.response.status_code} - {e.response.text[:150]}", flush=True)
        return None
    except Exception as e:
        print(f"[错误] 远程 API 调用失败: {e}", flush=True)
        return None


# ===================== Qoder 中继服务器 =====================

_relay_server = None
_relay_pending = {}  # request_id -> threading.Event + result
_bridge_process = None  # Qoder 桥接子进程


def start_qoder_relay(port):
    """在后台线程启动 Qoder 中继 HTTP 服务器"""
    from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
    import json as _json

    class RelayHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path == '/qoder/chat':
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length)
                try:
                    data = _json.loads(body.decode('utf-8'))
                    messages = data.get('messages', [])
                    # 提取最后一条用户消息
                    user_msg = ""
                    for m in reversed(messages):
                        if m.get('role') == 'user':
                            user_msg = m.get('content', '')
                            break

                    request_id = str(time.time())
                    event = threading.Event()
                    _relay_pending[request_id] = {"event": event, "result": None}

                    # 将请求信息写入文件，供 QoderWork 读取
                    request_file = os.path.join(WORKSPACE_DIR,
                                                ".qoder_relay_request.json")
                    with open(request_file, "w", encoding="utf-8") as f:
                        _json.dump({
                            "id": request_id,
                            "user_message": user_msg,
                            "messages": messages[-6:],  # 只传最近几条
                            "timestamp": time.time()
                        }, f, ensure_ascii=False, indent=2)

                    # 等待回复（最多 90 秒，桥接进程通常秒级响应，cron 任务每分钟轮询）
                    event.wait(timeout=90)

                    # 检查是否有回复
                    entry = _relay_pending.pop(request_id, None)
                    if entry and entry.get("result"):
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.end_headers()
                        self.wfile.write(_json.dumps({"reply": entry["result"]}).encode('utf-8'))
                    else:
                        # 超时，返回默认回复
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.end_headers()
                        self.wfile.write(_json.dumps({"reply": "我现在有点忙，稍后再聊~"}).encode('utf-8'))

                except Exception as e:
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(str(e).encode('utf-8'))

            elif self.path == '/qoder/respond':
                # QoderWork 调用此接口提交回复
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length)
                try:
                    data = _json.loads(body.decode('utf-8'))
                    request_id = data.get("id", "")
                    reply_text = data.get("reply", "")

                    if request_id in _relay_pending:
                        _relay_pending[request_id]["result"] = reply_text
                        _relay_pending[request_id]["event"].set()

                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"ok": true}')
                except Exception as e:
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(str(e).encode('utf-8'))
            else:
                self.send_response(404)
                self.end_headers()

        def do_GET(self):
            if self.path == '/health':
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"status": "ok", "pending": ' + str(len(_relay_pending)).encode() + b'}')
            elif self.path == '/qoder/pending':
                # QoderWork 轮询此接口获取待回复的请求
                request_file = os.path.join(WORKSPACE_DIR,
                                            ".qoder_relay_request.json")
                if os.path.exists(request_file):
                    with open(request_file, "r", encoding="utf-8") as f:
                        content = f.read()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(content.encode('utf-8'))
                else:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(b'{}')
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            # 静默 HTTP 日志，改为自己的格式
            pass

    global _relay_server
    try:
        _relay_server = ThreadingHTTPServer(('127.0.0.1', port), RelayHandler)
        thread = threading.Thread(target=_relay_server.serve_forever, daemon=True)
        thread.start()
        print(f"[Qoder] 中继服务器已启动: http://127.0.0.1:{port}", flush=True)
        return True
    except Exception as e:
        print(f"[Qoder] 中继服务器启动失败: {e}", flush=True)
        return False


def generate_reply_qoder(messages_history, system_prompt):
    """通过 Qoder 中继生成回复"""
    port = selected_model.get("relay_port", QODER_RELAY_PORT)

    api_messages = [{"role": "system", "content": system_prompt}]
    api_messages.extend(messages_history[-MAX_HISTORY * 2:])

    try:
        print(f"[Qoder] 正在生成回复 (上下文 {len(api_messages)} 条)...", flush=True)
        resp = requests.post(
            f"http://127.0.0.1:{port}/qoder/chat",
            json={"messages": api_messages},
            timeout=95  # 略大于中继的 90s 等待时间
        )
        resp.raise_for_status()
        data = resp.json()
        reply = data.get("reply", "").strip()

        reply = re.sub(r'[\ud800-\udfff]', '', reply)
        result = reply if reply else "抱歉，我没想好怎么回复。"
        print(f"[Qoder] 回复: {result[:80]}", flush=True)
        return result

    except requests.exceptions.ConnectionError:
        print("[错误] Qoder 中继未响应，请确认 QoderWork 正在运行", flush=True)
        return None
    except Exception as e:
        print(f"[错误] Qoder 调用失败: {e}", flush=True)
        return None


# ===================== 统一调度 =====================

def generate_reply(messages_history, system_prompt):
    """根据选中的模型模式分发调用"""
    mode = selected_model.get("mode", "ollama")

    if mode == "remote":
        return generate_reply_remote(messages_history, system_prompt)
    elif mode == "qoder":
        return generate_reply_qoder(messages_history, system_prompt)
    else:
        model_name = selected_model.get("model", OLLAMA_MODEL)
        return generate_reply_ollama(messages_history, system_prompt, model_name)


# ===================== 消息过滤 =====================

SYSTEM_NAMES = {'折叠的群聊', '微信支付', '腾讯新闻', '微信运动', '朋友圈',
                '文件传输助手', '微信团队', '服务通知', '订阅号', '订阅号消息'}


def is_system_chat(name):
    """是否是系统会话"""
    return name in SYSTEM_NAMES


def is_mentioned(content, config):
    """检查消息中是否 @了机器人"""
    if not content:
        return False

    mention_names = list(config.get("group_mention_names", []))
    if self_name:
        mention_names.append(f"@{self_name}")
    mention_names = list(set(mention_names))

    for name in mention_names:
        if name.lower() in content.lower():
            return True

    at_pattern = re.findall(r"@(\S+)", content)
    for mentioned in at_pattern:
        for name in mention_names:
            clean_name = name.lstrip("@")
            if mentioned.lower() == clean_name.lower():
                return True

    trigger_words = config.get("group_trigger_words", [])
    for word in trigger_words:
        if word in content:
            return True

    return False


def strip_mention(content, config):
    """从消息内容中去掉 @提及部分"""
    cleaned = content
    mention_names = list(set(config.get("group_mention_names", [])))
    if self_name:
        mention_names.append(f"@{self_name}")

    for name in mention_names:
        cleaned = cleaned.replace(name, "")

    cleaned = re.sub(r"@\S+\s*", "", cleaned)
    cleaned = cleaned.strip()
    return cleaned if cleaned else content.strip()


# ===================== 去重 =====================

def is_processed(msg_id):
    """检查消息是否已处理"""
    if msg_id in processed_msg_ids:
        return True
    processed_msg_ids.add(msg_id)
    return False


def cleanup_dedup():
    """清理过期的去重缓存"""
    global processed_msg_ids
    if len(processed_msg_ids) > 5000:
        processed_msg_ids.clear()


# ===================== 私聊处理 =====================

def should_reply_dm(chat_name, config):
    """判断是否应该回复私聊"""
    if not config.get("dm_enabled", True):
        return False

    blacklist = config.get("dm_blacklist", [])
    if chat_name in blacklist:
        return False

    if config.get("dm_whitelist_mode", False):
        whitelist = config.get("dm_whitelist", [])
        if chat_name not in whitelist:
            return False

    return True


def handle_dm(chat_name, content, config):
    """处理私聊消息"""
    if not should_reply_dm(chat_name, config):
        print(f"[跳过] 私聊 {chat_name} 在黑名单或未启用", flush=True)
        return

    log(f"[私聊] {chat_name}: {content[:80]}", config)

    conversation_history[chat_name].append({"role": "user", "content": content})

    system_prompt = config.get("dm_system_prompt", DEFAULT_CONFIG["dm_system_prompt"])
    reply = generate_reply(conversation_history[chat_name], system_prompt)

    if reply is None:
        log("[错误] 无法生成回复，请检查模型服务是否运行", config)
        return

    delay = config.get("reply_delay", 2)
    if delay > 0:
        time.sleep(delay)

    if send_reply(chat_name, reply):
        conversation_history[chat_name].append({"role": "assistant", "content": reply})
        log(f"[私聊回复] -> {chat_name}: {reply[:80]}", config)
        # 裁剪历史
        max_msgs = MAX_HISTORY * 2
        if len(conversation_history[chat_name]) > max_msgs:
            conversation_history[chat_name] = conversation_history[chat_name][-max_msgs:]
    else:
        log(f"[错误] 私聊回复发送失败 -> {chat_name}", config)


# ===================== 群聊处理 =====================

def should_reply_group(chat_name, config):
    """判断是否应该回复群消息"""
    if not config.get("group_enabled", True):
        return False

    blacklist = config.get("group_blacklist", [])
    if chat_name in blacklist:
        return False

    if config.get("group_whitelist_mode", False):
        whitelist = config.get("group_whitelist", [])
        if chat_name not in whitelist:
            return False

    return True


def handle_group(chat_name, content, config):
    """处理群聊消息（仅在被 @时触发）"""
    if not should_reply_group(chat_name, config):
        print(f"[跳过] 群聊 {chat_name} 在黑名单或未启用", flush=True)
        return

    if not is_mentioned(content, config):
        print(f"[跳过] 群聊 {chat_name} 未@机器人", flush=True)
        return

    clean_content = strip_mention(content, config)
    log(f"[群聊] {chat_name}: {clean_content[:80]}", config)

    history_key = f"group:{chat_name}"
    conversation_history[history_key].append({"role": "user", "content": clean_content})

    system_prompt = config.get("group_system_prompt", DEFAULT_CONFIG["group_system_prompt"])
    reply = generate_reply(conversation_history[history_key], system_prompt)

    if reply is None:
        log("[错误] 无法生成回复，请检查模型服务是否运行", config)
        return

    delay = config.get("reply_delay", 2)
    if delay > 0:
        time.sleep(delay)

    if send_reply(chat_name, reply):
        conversation_history[history_key].append({"role": "assistant", "content": reply})
        log(f"[群聊回复] -> {chat_name}: {reply[:80]}", config)
        max_msgs = MAX_HISTORY * 2
        if len(conversation_history[history_key]) > max_msgs:
            conversation_history[history_key] = conversation_history[history_key][-max_msgs:]
    else:
        log(f"[错误] 群聊回复发送失败 -> {chat_name}", config)


# ===================== 主流程 =====================

def cleanup(signum=None, frame=None):
    """清理资源"""
    global wx, _relay_server, _bridge_process
    print("\n[系统] 正在清理资源...", flush=True)
    if _bridge_process:
        try:
            _bridge_process.terminate()
            _bridge_process.wait(timeout=3)
        except Exception:
            try:
                _bridge_process.kill()
            except Exception:
                pass
    if _relay_server:
        try:
            _relay_server.shutdown()
        except Exception:
            pass
    if wx:
        try:
            wx.StopListening(remove=True)
        except Exception:
            pass
    print("[系统] 自动回复已停止", flush=True)
    sys.exit(0)


def main():
    global wx, self_name, selected_model

    # 解析命令行参数
    parser = argparse.ArgumentParser(description="微信自动回复机器人")
    parser.add_argument("--remote", nargs="?", const="default", default=None,
                        help="使用远程 API (可选指定模型名称，默认用上次选择的远程模型)")
    parser.add_argument("--last", action="store_true",
                        help="直接使用上次保存的模型配置，跳过选择菜单")
    cli_args = parser.parse_args()

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    print("=" * 55, flush=True)
    print("  微信自动回复机器人 (wxauto 版)", flush=True)
    print(f"  私聊: 自动回复所有消息", flush=True)
    print(f"  群聊: 仅 @提及时回复", flush=True)
    print(f"  轮询间隔: {POLL_INTERVAL}s", flush=True)
    print("  按 Ctrl+C 停止", flush=True)
    print("=" * 55, flush=True)

    config = load_config()
    log(f"[系统] 私聊人设: {config['dm_system_prompt'][:50]}...", config)
    log(f"[系统] 群聊人设: {config['group_system_prompt'][:50]}...", config)

    # 初始化微信
    log("[检查] 正在连接微信...", config)
    wx = init_wechat()
    if not wx:
        log("[错误] 无法连接微信，请确认微信已启动并登录", config)
        return

    self_name = getattr(wx, 'nickname', '') or ''
    log(f"[系统] 当前微信: {self_name}", config)

    # 检查 Ollama 状态（仅用于信息显示，不阻塞流程）
    ollama_models = []
    try:
        ollama_models = get_ollama_models()
        if ollama_models:
            names = [m[0] for m in ollama_models]
            log(f"[检查] Ollama 就绪，可用模型: {', '.join(names)}", config)
        else:
            log("[检查] Ollama 未运行或无已安装模型", config)
    except Exception:
        log("[检查] Ollama 未检测到 (本地模型不可用)", config)

    # ========= 模型选择 =========
    saved_cfg = load_model_config()

    if cli_args.remote is not None:
        # --remote 模式：使用远程 API
        if not saved_cfg.get("api_base") or not saved_cfg.get("api_key"):
            log("[错误] 未找到远程 API 连接信息，请先手动运行一次配置", config)
            return
        model_name = cli_args.remote if cli_args.remote != "default" else saved_cfg.get("model", REMOTE_MODELS[0])
        selected_model = {
            "mode": "remote",
            "model": model_name,
            "api_base": saved_cfg["api_base"],
            "api_key": saved_cfg["api_key"],
            "remote_provider": saved_cfg.get("remote_provider", "远程API"),
        }
        # 保留已有配置中的额外字段（如 bocha_api_key）
        for key in saved_cfg:
            if key not in selected_model:
                selected_model[key] = saved_cfg[key]
        save_model_config(selected_model)
    elif cli_args.last and saved_cfg.get("mode"):
        # --last 模式：使用上次配置
        selected_model = saved_cfg
    else:
        # 交互式菜单
        print("", flush=True)
        model_selection_menu()

    mode = selected_model.get("mode", "ollama")
    model_label = selected_model.get("model", "")
    if mode == "remote":
        model_label = f"{selected_model.get('remote_provider', '远程API')} / {model_label}"
    elif mode == "qoder":
        model_label = "Qoder 代理"
    log(f"[系统] 当前模型: {model_label} ({mode})", config)

    # 如果选择 Qoder，启动中继服务器和桥接进程
    if mode == "qoder":
        port = selected_model.get("relay_port", QODER_RELAY_PORT)
        if not start_qoder_relay(port):
            log("[错误] Qoder 中继启动失败，回退到 Ollama", config)
            selected_model["mode"] = "ollama"
            if ollama_models:
                selected_model["model"] = ollama_models[0][0]
            else:
                log("[错误] 无可用本地模型，退出", config)
                return
        else:
            # 启动桥接子进程，自动轮询中继并生成回复
            bridge_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qoder_bridge.py")
            if os.path.exists(bridge_script):
                global _bridge_process
                _bridge_process = subprocess.Popen(
                    [sys.executable, bridge_script,
                     "--port", str(port)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                log(f"[系统] Qoder 桥接进程已启动 (PID: {_bridge_process.pid})", config)
            else:
                log(f"[警告] 桥接脚本不存在: {bridge_script}", config)

    # 显示会话列表
    try:
        sessions = wx.GetSession()
        if sessions:
            log(f"[会话] 当前 {len(sessions)} 个会话:", config)
            for s in sessions[:20]:
                name = getattr(s, 'name', '') or str(s)
                log(f"  - {name}", config)
    except Exception as e:
        log(f"[警告] 获取会话列表失败: {e}", config)

    log("[系统] 开始监听微信消息...", config)
    print("", flush=True)

    # 主循环
    poll_count = 0
    msg_count = 0
    reply_count = 0

    while True:
        try:
            config = load_config()

            if not config.get("enabled", True):
                time.sleep(POLL_INTERVAL)
                continue

            # 检查微信连接
            if not wx:
                print("[系统] 尝试重新连接微信...", flush=True)
                wx = init_wechat()
                if not wx:
                    time.sleep(10)
                    continue

            # 轮询新消息（一轮可能获取多个会话的消息）
            round_msgs = 0
            for _ in range(MAX_POLLS_PER_ROUND):
                messages = poll_new_messages()
                if not messages:
                    break

                round_msgs += len(messages)
                msg_count += len(messages)

                for chat_name, chat_type, sender_attr, content, msg_id in messages:
                    # 跳过自己发的消息
                    if sender_attr == 'self':
                        print(f"[跳过] 自己发的消息", flush=True)
                        continue

                    # 跳过系统消息（时间、系统提示等）
                    if sender_attr not in ('friend', 'human'):
                        print(f"[跳过] 非聊天消息 (type={sender_attr})", flush=True)
                        continue

                    # 跳过系统会话
                    if is_system_chat(chat_name):
                        continue

                    # 去重
                    if is_processed(msg_id):
                        print(f"[跳过] 重复消息", flush=True)
                        continue

                    # 跳过空消息
                    if not content or not content.strip():
                        continue

                    # 路由到私聊或群聊处理
                    if chat_type == 'group':
                        handle_group(chat_name, content, config)
                    else:
                        handle_dm(chat_name, content, config)
                    reply_count += 1

            poll_count += 1

            # 心跳（每20次轮询）
            if poll_count % 20 == 0:
                now = datetime.now().strftime("%H:%M:%S")
                model_info = selected_model.get("model", "?")
                print(f"[心跳] {now} | 模型: {model_info} | 轮询 {poll_count} 次 | "
                      f"收到 {msg_count} 条 | 回复 {reply_count} 条 | "
                      f"对话 {len(conversation_history)} 个", flush=True)

            # 定期清理去重缓存
            if poll_count % 100 == 0:
                cleanup_dedup()

        except KeyboardInterrupt:
            cleanup()
        except Exception as e:
            print(f"[错误] 主循环异常: {e}", flush=True)
            import traceback
            traceback.print_exc()
            # 尝试重新连接
            try:
                wx = init_wechat()
            except Exception:
                wx = None

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
