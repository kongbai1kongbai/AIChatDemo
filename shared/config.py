"""
shared/config.py - Shared configuration loader
"""
import json
import os

# Workspace directory: D:\AI\workspace (or equivalent)
# shared/ is at D:\AI\code\shared\, so go up 3 levels to D:\AI, then into workspace
_SHARED_DIR = os.path.dirname(os.path.abspath(__file__))
_CODE_DIR = os.path.dirname(_SHARED_DIR)
_AI_ROOT = os.path.dirname(_CODE_DIR)
WORKSPACE_DIR = os.path.join(_AI_ROOT, "workspace")

# Config file paths
CONFIG_PATH = os.path.join(WORKSPACE_DIR, "auto_reply_config.json")
MODEL_CONFIG_PATH = os.path.join(WORKSPACE_DIR, "model_config.json")
WECOM_CONFIG_PATH = os.path.join(WORKSPACE_DIR, "wecom_config.json")
WECOM_BOT_CONFIG_PATH = os.path.join(WORKSPACE_DIR, "wecom_bot_config.json")
CS_CONFIG_PATH = os.path.join(WORKSPACE_DIR, "customer_service_config.json")

# Max conversation history turns
MAX_HISTORY = 10

DEFAULT_CONFIG = {
    "enabled": True,
    "dm_enabled": True,
    "dm_system_prompt": "",
    "dm_blacklist": [],
    "dm_whitelist_mode": False,
    "dm_whitelist": [],
    "group_enabled": True,
    "group_system_prompt": "",
    "group_blacklist": [],
    "group_whitelist_mode": False,
    "group_whitelist": [],
    "group_trigger_words": [],
    "group_mention_names": ["@bot"],
    "reply_delay": 2,
    "log_file": "auto_reply.log"
}


def load_config():
    """Load auto_reply_config.json, merge with defaults"""
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                config = json.load(f)
            # Merge: fill any missing keys from DEFAULT_CONFIG
            for key, value in DEFAULT_CONFIG.items():
                if key not in config:
                    config[key] = value
            return config
        else:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
            print(f"[config] Created default config: {CONFIG_PATH}")
            return DEFAULT_CONFIG.copy()
    except Exception as e:
        print(f"[config] Load error: {e}, using defaults")
        return DEFAULT_CONFIG.copy()


def load_model_config():
    """Load model_config.json"""
    try:
        if os.path.exists(MODEL_CONFIG_PATH):
            with open(MODEL_CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"[config] Model config load error: {e}")
    return {"mode": "ollama", "model": "qwen3:8b"}


def save_model_config(cfg):
    """Save model_config.json"""
    try:
        with open(MODEL_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[config] Model config save error: {e}")


def log(message, config=None):
    """Print and optionally write to log file"""
    from datetime import datetime
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
