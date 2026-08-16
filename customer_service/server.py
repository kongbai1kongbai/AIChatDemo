"""
WeChat Customer Service Server
Receives message notifications via WeCom callback, fetches messages via sync_msg API,
dispatches to handler for AI reply generation.

Architecture:
  WeCom Cloud  →  POST /callback (kf_msg_or_event notification)
               →  server.py returns "success" immediately (< 5s)
               →  background thread: sync_msg → parse → handler.process()

Usage:
  python server.py
  python server.py --config /path/to/customer_service_config.json
  python server.py --port 8081
"""
import html
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

from shared.config import log, load_model_config, CS_CONFIG_PATH
from handler import CSHandler

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # 2 GB manual upload cap.

# Global state
_cs_config = {}
_handler = None


def load_cs_config(config_path):
    """Load customer service specific config"""
    if not os.path.exists(config_path):
        print(f"[error] Config not found: {config_path}")
        print(f"        Copy customer_service/config.json.example to workspace/customer_service_config.json")
        sys.exit(1)
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


@app.route("/callback", methods=["GET"])
def verify_callback():
    """WeCom URL verification (echostr handshake)"""
    from wechatpy.enterprise.crypto import WeChatCrypto

    msg_signature = request.args.get("msg_signature", "")
    timestamp = request.args.get("timestamp", "")
    nonce = request.args.get("nonce", "")
    echostr = request.args.get("echostr", "")

    crypto = WeChatCrypto(
        _cs_config["token"],
        _cs_config["encoding_aes_key"],
        _cs_config["corp_id"]
    )

    try:
        echo_str = crypto.check_signature(msg_signature, timestamp, nonce, echostr)
        print(f"[cs] URL verification OK")
        return echo_str
    except Exception as e:
        print(f"[cs] URL verification FAILED: {e}")
        return "verification failed", 403


@app.route("/callback", methods=["POST"])
def receive_notification():
    """Receive KF notification from WeCom, fetch actual messages via sync_msg.

    WeCom KF callbacks only contain an event notification (kf_msg_or_event),
    not the actual message content. We must call sync_msg to retrieve messages.
    """
    from wechatpy.enterprise.crypto import WeChatCrypto

    msg_signature = request.args.get("msg_signature", "")
    timestamp = request.args.get("timestamp", "")
    nonce = request.args.get("nonce", "")

    print(f"[cs] >>> POST /callback received (sig={msg_signature[:12]}..., ts={timestamp}, nonce={nonce})")
    print(f"[cs]     raw body length: {len(request.data)} bytes")

    crypto = WeChatCrypto(
        _cs_config["token"],
        _cs_config["encoding_aes_key"],
        _cs_config["corp_id"]
    )

    try:
        raw_body = request.data
        decrypted = crypto.decrypt_message(raw_body, msg_signature, timestamp, nonce)
        print(f"[cs]     decrypted OK: {decrypted[:200] if isinstance(decrypted, str) else decrypted}")

        # The decrypted XML is a notification, not the message itself.
        # We need to call sync_msg to get actual messages.
        # Process in background thread to meet < 5s response requirement.
        print(f"[cs]     spawning sync_and_process thread...")
        t = threading.Thread(target=_handler.sync_and_process, daemon=True)
        t.start()
        print(f"[cs]     thread started")

    except Exception as e:
        print(f"[cs] Notification processing failed: {e}", flush=True)

    return "success"


@app.route("/health", methods=["GET"])
def health():
    stats = _handler.get_stats() if _handler else {}
    return jsonify({"status": "ok", "service": "customer_service", **stats})


@app.route("/upload/<token>", methods=["GET", "POST"])
def upload_file(token):
    """Manual upload fallback when WeCom only sends a filename without media_id."""
    if not _handler:
        return "service not ready", 503

    if request.method == "GET":
        safe_token = html.escape(token)
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>上传文件</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; line-height: 1.5; color: #111827; }}
    main {{ max-width: 560px; margin: 0 auto; }}
    input, button {{ font-size: 16px; margin-top: 16px; }}
    button {{ padding: 10px 18px; border: 0; border-radius: 8px; background: #0a84ff; color: white; }}
    button:disabled {{ background: #9ca3af; }}
    .bar {{ width: 100%; height: 14px; margin-top: 20px; border-radius: 999px; background: #e5e7eb; overflow: hidden; }}
    .fill {{ width: 0%; height: 100%; background: #16a34a; transition: width .15s linear; }}
    .meta {{ margin-top: 10px; color: #4b5563; }}
    .result {{ margin-top: 18px; white-space: pre-wrap; }}
  </style>
</head>
<body>
  <main>
    <h2>上传文件</h2>
    <p class="meta">大文件上传可能需要几分钟，上传过程中请保持页面打开。</p>
    <form id="uploadForm">
      <input id="fileInput" type="file" name="file" required>
      <br>
      <button id="uploadButton" type="submit">上传</button>
    </form>
    <div class="bar" aria-label="上传进度"><div id="progressFill" class="fill"></div></div>
    <div id="progressMeta" class="meta">请选择文件后点击上传</div>
    <div id="result" class="result"></div>
  </main>
  <script>
    const form = document.getElementById('uploadForm');
    const fileInput = document.getElementById('fileInput');
    const button = document.getElementById('uploadButton');
    const fill = document.getElementById('progressFill');
    const meta = document.getElementById('progressMeta');
    const result = document.getElementById('result');

    function formatBytes(bytes) {{
      if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
      const units = ['B', 'KB', 'MB', 'GB'];
      let size = bytes;
      let unit = 0;
      while (size >= 1024 && unit < units.length - 1) {{
        size /= 1024;
        unit += 1;
      }}
      return `${{size.toFixed(unit === 0 ? 0 : 1)}} ${{units[unit]}}`;
    }}

    form.addEventListener('submit', (event) => {{
      event.preventDefault();
      const file = fileInput.files[0];
      if (!file) {{
        meta.textContent = '请先选择文件';
        return;
      }}

      const data = new FormData();
      data.append('file', file);

      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/upload/{safe_token}');
      button.disabled = true;
      fileInput.disabled = true;
      result.textContent = '';
      fill.style.width = '0%';
      meta.textContent = `准备上传：${{file.name}}（${{formatBytes(file.size)}}）`;

      xhr.upload.onprogress = (event) => {{
        if (!event.lengthComputable) {{
          meta.textContent = `上传中：${{file.name}}`;
          return;
        }}
        const percent = Math.min(100, Math.round((event.loaded / event.total) * 100));
        fill.style.width = `${{percent}}%`;
        meta.textContent = `上传中 ${{percent}}%：${{formatBytes(event.loaded)}} / ${{formatBytes(event.total)}}`;
      }};

      xhr.onload = () => {{
        fill.style.width = xhr.status >= 200 && xhr.status < 300 ? '100%' : fill.style.width;
        meta.textContent = xhr.status >= 200 && xhr.status < 300 ? '上传完成' : '上传失败';
        result.textContent = xhr.responseText || (xhr.status >= 200 && xhr.status < 300 ? '上传成功' : '上传失败');
      }};

      xhr.onerror = () => {{
        meta.textContent = '网络错误，上传失败';
        result.textContent = '请检查网络后重试。';
      }};

      xhr.onloadend = () => {{
        button.disabled = false;
        fileInput.disabled = false;
      }};

      xhr.send(data);
    }});
  </script>
</body>
</html>"""

    ok, message = _handler.accept_uploaded_file(token, request.files.get("file"))
    status = 200 if ok else 400
    return html.escape(message), status


def main():
    global _cs_config, _handler

    parser = argparse.ArgumentParser(description="WeChat Customer Service AI Server")
    parser.add_argument("--config", default=CS_CONFIG_PATH)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--host", default="")
    args = parser.parse_args()

    _cs_config = load_cs_config(args.config)
    model_config = load_model_config()

    host = args.host or _cs_config.get("host", "0.0.0.0")
    port = args.port or _cs_config.get("port", 8081)

    _handler = CSHandler(_cs_config, model_config)

    print(f"[cs] Customer Service AI Server starting...")
    print(f"[cs] Callback URL: http://{host}:{port}/callback")
    print(f"[cs] Corp ID: {_cs_config.get('corp_id', '')[:8]}...")
    print(f"[cs] Model: {model_config.get('model', 'unknown')} ({model_config.get('mode', 'unknown')})")

    forward_chatid = _cs_config.get("forward_chatid", "")
    forward_userid = _cs_config.get("forward_userid", "")
    if forward_chatid:
        print(f"[cs] Group forwarding: enabled → chatid={forward_chatid}")
    else:
        print(f"[cs] Group forwarding: disabled (set forward_chatid in config to enable)")
    if forward_userid:
        print(f"[cs] User forwarding: enabled → userid={forward_userid} (agentid={_cs_config.get('agent_id', '')})")
    else:
        print(f"[cs] User forwarding: disabled (set forward_userid + agent_id in config to enable)")

    public_base_url = _cs_config.get("public_base_url", "")
    if public_base_url:
        print(f"[cs] Manual upload URL base: {public_base_url.rstrip('/')}/upload/<token>")
    else:
        print("[cs] Manual upload fallback disabled (set public_base_url in config to enable upload links)")

    # Drain pending messages on startup: advance cursor without processing,
    # so only new messages arriving after startup get handled.
    def _drain_old_messages():
        import time
        time.sleep(2)
        print("[cs] Draining old messages (cursor advance only)...")
        try:
            old = _handler._sync_messages()
            if old:
                print(f"[cs] Discarded {len(old)} pending message(s), cursor is now up-to-date")
            else:
                print("[cs] No pending messages, cursor is up-to-date")
        except Exception as e:
            print(f"[cs] Drain error (non-fatal): {e}")

    threading.Thread(target=_drain_old_messages, daemon=True).start()

    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
