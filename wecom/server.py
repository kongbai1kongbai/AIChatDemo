"""
WeCom Callback Server - Enterprise WeChat self-built app
Receives messages via callback, generates AI replies, sends via API.

Usage:
  python server.py
  python server.py --config /path/to/config.json
"""
import json
import os
import sys
import argparse
import threading
from flask import Flask, request, jsonify

# Add project root to path for shared imports
_CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CODE_DIR not in sys.path:
    sys.path.insert(0, _CODE_DIR)

from shared.config import log, load_model_config, WECOM_CONFIG_PATH
from handler import WeComHandler

app = Flask(__name__)

# Global state
_wecom_config = {}
_handler = None


def load_wecom_config(config_path):
    """Load WeCom-specific config"""
    if not os.path.exists(config_path):
        print(f"[error] Config not found: {config_path}")
        print(f"        Copy wecom/config.json.example to workspace/wecom_config.json and fill in your values")
        sys.exit(1)
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


@app.route("/callback", methods=["GET"])
def verify_callback():
    """WeCom URL verification (GET request)"""
    from wechatpy.enterprise.crypto import WeChatCrypto

    msg_signature = request.args.get("msg_signature", "")
    timestamp = request.args.get("timestamp", "")
    nonce = request.args.get("nonce", "")
    echostr = request.args.get("echostr", "")

    crypto = WeChatCrypto(
        _wecom_config["token"],
        _wecom_config["encoding_aes_key"],
        _wecom_config["corp_id"]
    )

    try:
        echo_str = crypto.check_signature(msg_signature, timestamp, nonce, echostr)
        print(f"[callback] URL verification OK")
        return echo_str
    except Exception as e:
        print(f"[callback] URL verification FAILED: {e}")
        return "verification failed", 403


@app.route("/callback", methods=["POST"])
def receive_message():
    """Receive and process messages from WeCom"""
    from wechatpy.enterprise.crypto import WeChatCrypto
    from wechatpy.enterprise import parse_message

    msg_signature = request.args.get("msg_signature", "")
    timestamp = request.args.get("timestamp", "")
    nonce = request.args.get("nonce", "")

    crypto = WeChatCrypto(
        _wecom_config["token"],
        _wecom_config["encoding_aes_key"],
        _wecom_config["corp_id"]
    )

    try:
        raw_body = request.data
        decrypted = crypto.decrypt_message(raw_body, msg_signature, timestamp, nonce)
        msg = parse_message(decrypted)

        # Detect group chat: group messages have chat_id attribute
        chat_id = getattr(msg, 'chat_id', None) or ''
        is_group = bool(chat_id)

        if is_group:
            print(f"[msg] type={msg.type}, group={chat_id}, from={msg.source}, content={getattr(msg, 'content', '')[:60]}")
        else:
            print(f"[msg] type={msg.type}, from={msg.source}, content={getattr(msg, 'content', '')[:60]}")

        if msg.type == "text":
            # Process in background thread (WeCom requires <5s response)
            t = threading.Thread(
                target=_handler.handle_text_message,
                args=(msg.source, msg.content, msg.agent, chat_id)
            )
            t.daemon = True
            t.start()

    except Exception as e:
        print(f"[error] Message processing failed: {e}")

    return "success"


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "agent_id": _wecom_config.get("agent_id")})


def main():
    global _wecom_config, _handler

    parser = argparse.ArgumentParser(description="WeCom AI Bot Server")
    parser.add_argument("--config", default=WECOM_CONFIG_PATH)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--host", default="")
    args = parser.parse_args()

    _wecom_config = load_wecom_config(args.config)
    model_config = load_model_config()

    host = args.host or _wecom_config.get("host", "0.0.0.0")
    port = args.port or _wecom_config.get("port", 8080)

    _handler = WeComHandler(_wecom_config, model_config)

    print(f"[server] WeCom AI Bot starting...")
    print(f"[server] Callback URL: http://{host}:{port}/callback")
    print(f"[server] Agent ID: {_wecom_config.get('agent_id')}")
    print(f"[server] Model: {model_config.get('model', 'unknown')} ({model_config.get('mode', 'unknown')})")

    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
