"""
WeCom Intelligent Robot - WebSocket Long Connection Mode
Uses wecom_aibot_sdk for persistent connection to WeCom servers.

Usage:
  python bot.py
  python bot.py --config /path/to/config.json
"""
import os
import re
import sys
import json
import time
import base64
import hashlib
import asyncio
import argparse
import tempfile
from collections import defaultdict

# Add project root to path for shared imports
_CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CODE_DIR not in sys.path:
    sys.path.insert(0, _CODE_DIR)

from shared.config import (
    log, load_config, load_model_config, WECOM_BOT_CONFIG_PATH,
    MAX_HISTORY
)
from shared.ai_engine import generate_reply

try:
    from wecom_aibot_sdk import WSClient, generate_req_id
except ImportError:
    print("[error] wecom_aibot_sdk not installed!")
    print("        Run: pip install wecom-aibot-sdk")
    sys.exit(1)


def load_bot_config(config_path):
    """Load bot-specific config"""
    if not os.path.exists(config_path):
        print(f"[error] Config not found: {config_path}")
        print(f"        Copy wecom_bot/config.json.example to workspace/wecom_bot_config.json")
        sys.exit(1)
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


class IntelligentBotHandler:
    """Handler for WeCom Intelligent Robot via WebSocket"""

    def __init__(self, model_config):
        self.model_config = model_config

        # Conversation history per chat (user or group)
        self.history = defaultdict(list)

        # Pending images: user sent image, waiting for text description
        # Key: history_key, Value: {"file_path": str, "b64_data": str, "mime": str, "timestamp": float}
        self.pending_images = {}

        # Rate limiting
        self.last_reply = {}
        self.min_interval = 2  # seconds

        # WebSocket client reference
        self.client = None

    def set_client(self, client):
        """Set the WSClient instance"""
        self.client = client

    def _get_history_key(self, chat_id, user_id):
        """Get unique key for conversation history"""
        if chat_id:
            return f"group:{chat_id}"
        return f"user:{user_id}"

    async def on_text(self, frame):
        """Handle incoming text message"""
        body = frame["body"]
        text_content = body.get("text", {}).get("content", "").strip()
        chat_id = body.get("chatid", "")
        user_id = body.get("from", {}).get("userid", "")

        if not text_content:
            return

        is_group = bool(chat_id)
        history_key = self._get_history_key(chat_id, user_id)

        log_source = f"group:{chat_id}" if is_group else f"user:{user_id}"
        print(f"[bot] Msg from {log_source}: {text_content[:60]}")

        # --- Check for pending image: user sent text after image ---
        if history_key in self.pending_images:
            await self._process_pending_image(history_key, text_content, frame)
            return

        # Rate limiting
        now = time.time()
        if history_key in self.last_reply:
            elapsed = now - self.last_reply[history_key]
            if elapsed < self.min_interval:
                print(f"[bot] Rate limited: {history_key} ({elapsed:.1f}s)")
                return

        # Load config (hot reload)
        config = load_config()

        # Check if enabled
        if is_group and not config.get("group_enabled", True):
            print(f"[bot] Group chat disabled, skipping")
            return
        if not is_group and not config.get("dm_enabled", True):
            print(f"[bot] DM disabled, skipping")
            return

        # Add to history
        self.history[history_key].append({"role": "user", "content": text_content})

        # Trim history
        max_msgs = MAX_HISTORY * 2
        if len(self.history[history_key]) > max_msgs:
            self.history[history_key] = self.history[history_key][-max_msgs:]

        # Get system prompt
        if is_group:
            system_prompt = config.get(
                "group_system_prompt",
                ""
            )
        else:
            system_prompt = config.get(
                "dm_system_prompt",
                ""
            )

        # Generate reply in thread to not block async event loop
        loop = asyncio.get_event_loop()
        reply = await loop.run_in_executor(
            None,
            lambda: generate_reply(
                self.history[history_key],
                system_prompt,
                self.model_config
            )
        )

        if not reply:
            reply = "Sorry, something went wrong. Please try again."

        # Send reply using reply_stream (correct format for intelligent robot)
        try:
            stream_id = generate_req_id("stream")
            await self.client.reply_stream(frame, stream_id, reply, finish=True)
            print(f"[bot] Replied to {log_source}: {reply[:60]}")

            # Add to history
            self.history[history_key].append({"role": "assistant", "content": reply})
            self.last_reply[history_key] = time.time()

            # Check for generated images in reply and send them
            await self._send_generated_images(reply, frame)

        except Exception as e:
            print(f"[bot] Reply failed: {e}")

    async def on_image(self, frame):
        """Handle incoming image message: download, store, wait for description"""
        body = frame["body"]
        chat_id = body.get("chatid", "")
        user_id = body.get("from", {}).get("userid", "")

        image_data = body.get("image", {})
        image_url = image_data.get("url", "")
        media_id = image_data.get("media_id", "")
        aes_key = image_data.get("aeskey", "")
        caption = body.get("text", {}).get("content", "").strip() if "text" in body else ""

        is_group = bool(chat_id)
        history_key = self._get_history_key(chat_id, user_id)
        log_source = f"group:{chat_id}" if is_group else f"user:{user_id}"
        print(f"[bot] Image from {log_source}: url={image_url[:40]}... aeskey={'yes' if aes_key else 'no'}")

        # Clean up any existing pending image for this conversation
        if history_key in self.pending_images:
            old = self.pending_images.pop(history_key)
            try:
                os.remove(old["file_path"])
                os.rmdir(os.path.dirname(old["file_path"]))
            except Exception:
                pass

        # Download and decrypt image via SDK (AES-256-CBC)
        if not image_url:
            print(f"[bot] No image URL, media_id={media_id[:20]}...")
            try:
                stream_id = generate_req_id("stream")
                await self.client.reply_stream(frame, stream_id,
                    "抱歉，图片下载失败，请重新发送。", finish=True)
            except Exception as e:
                print(f"[bot] Error reply failed: {e}")
            return

        try:
            result = await self.client.download_file(image_url, aes_key or None)
            image_bytes = result["buffer"]
            sdk_filename = result.get("filename")
        except Exception as e:
            print(f"[bot] SDK download_file error: {e}")
            try:
                stream_id = generate_req_id("stream")
                await self.client.reply_stream(frame, stream_id,
                    "抱歉，图片解密失败，请重新发送。", finish=True)
            except Exception as e2:
                print(f"[bot] Error reply failed: {e2}")
            return

        # Save decrypted bytes to temp file
        file_path, b64_data, mime = self._save_decrypted_image(image_bytes, sdk_filename)
        if not file_path:
            try:
                stream_id = generate_req_id("stream")
                await self.client.reply_stream(frame, stream_id,
                    "抱歉，图片下载失败，请重新发送。", finish=True)
            except Exception as e:
                print(f"[bot] Error reply failed: {e}")
            return

        # Store pending image
        self.pending_images[history_key] = {
            "file_path": file_path,
            "b64_data": b64_data,
            "mime": mime,
            "frame": frame,
            "timestamp": time.time(),
        }

        # If caption provided, process immediately
        if caption:
            await self._process_pending_image(history_key, caption, frame)
            return

        # Otherwise acknowledge and wait
        try:
            stream_id = generate_req_id("stream")
            await self.client.reply_stream(frame, stream_id,
                "📷 图片已收到，请告诉我你想对这张图片做什么？", finish=True)
            print(f"[bot] Acknowledged image from {log_source}, waiting for description")
        except Exception as e:
            print(f"[bot] Ack reply failed: {e}")

    async def _process_pending_image(self, history_key, user_text, frame):
        """Process a pending image with the user's text description."""
        pending = self.pending_images.pop(history_key)
        file_path = pending["file_path"]
        b64_data = pending["b64_data"]
        mime = pending["mime"]

        # Add to history
        self.history[history_key].append({"role": "user", "content": user_text})

        # Trim history
        max_msgs = MAX_HISTORY * 2
        if len(self.history[history_key]) > max_msgs:
            self.history[history_key] = self.history[history_key][-max_msgs:]

        # System prompt with vision instruction
        config = load_config()
        is_group = history_key.startswith("group:")
        if is_group:
            system_prompt = config.get(
                "group_system_prompt",
                ""
            )
        else:
            system_prompt = config.get(
                "dm_system_prompt",
                ""
            )

        # Generate reply with image
        loop = asyncio.get_event_loop()
        reply = await loop.run_in_executor(
            None,
            lambda: generate_reply(
                self.history[history_key],
                system_prompt,
                self.model_config,
                image_path=file_path,
                image_base64=b64_data,
                image_mime=mime,
            )
        )

        # Clean up temp file
        try:
            os.remove(file_path)
            os.rmdir(os.path.dirname(file_path))
        except Exception:
            pass

        if not reply:
            reply = "抱歉，我暂时无法理解这张图片，请稍后再试。"

        # Send reply
        try:
            stream_id = generate_req_id("stream")
            await self.client.reply_stream(frame, stream_id, reply, finish=True)
            log_source = history_key
            print(f"[bot] Replied to {log_source}: {reply[:60]}")

            self.history[history_key].append({"role": "assistant", "content": reply})
            self.last_reply[history_key] = time.time()

            # Check for generated images in reply and send them
            await self._send_generated_images(reply, frame)

        except Exception as e:
            print(f"[bot] Reply failed: {e}")

    def _save_decrypted_image(self, image_bytes, sdk_filename=None):
        """Save decrypted image bytes to temp file.

        Returns (file_path, base64_data, mime_type) or (None, None, None).
        """
        try:
            # Detect MIME from magic bytes
            mime = "image/jpeg"
            ext = ".jpg"
            if image_bytes[:8] == b'\x89PNG\r\n\x1a\n':
                mime = "image/png"
                ext = ".png"
            elif image_bytes[:4] == b'GIF8':
                mime = "image/gif"
                ext = ".gif"
            elif image_bytes[:4] == b'RIFF' and image_bytes[8:12] == b'WEBP':
                mime = "image/webp"
                ext = ".webp"

            # Use SDK filename extension if available
            if sdk_filename and "." in sdk_filename:
                sdk_ext = "." + sdk_filename.rsplit(".", 1)[-1].lower()
                if sdk_ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
                    ext = sdk_ext

            # Save to temp file
            tmp_dir = tempfile.mkdtemp(prefix="bot_media_")
            file_path = os.path.join(tmp_dir, f"image_{int(time.time())}{ext}")
            with open(file_path, "wb") as f:
                f.write(image_bytes)

            # Base64 encode
            b64_data = base64.b64encode(image_bytes).decode("utf-8")

            size_kb = len(image_bytes) / 1024
            print(f"[bot] Image saved: {file_path} ({size_kb:.1f} KB, {mime})")
            return file_path, b64_data, mime

        except Exception as e:
            print(f"[bot] Image save error: {e}")
            return None, None, None

    def _extract_image_paths(self, text):
        """Extract local image file paths from AI reply text.

        Looks for Windows/Unix paths ending in image extensions.
        Returns list of valid, existing file paths.
        """
        pattern = r'`?([A-Za-z]:\\[^\s`]+?\.(?:png|jpg|jpeg|gif|webp|bmp))`?'
        matches = re.findall(pattern, text)

        valid_paths = []
        for path in matches:
            path = path.rstrip('.,;:')
            if os.path.isfile(path):
                valid_paths.append(path)
                print(f"[bot] Found image in reply: {path}")
            else:
                print(f"[bot] Image path not found: {path}")

        return valid_paths

    async def _send_generated_images(self, reply, frame):
        """Detect image paths in reply text, upload and send as image messages."""
        image_paths = self._extract_image_paths(reply)

        for img_path in image_paths:
            try:
                with open(img_path, "rb") as f:
                    image_bytes = f.read()

                md5_hash = hashlib.md5(image_bytes).hexdigest()
                filename = os.path.basename(img_path)

                print(f"[bot] Uploading generated image: {filename} ({len(image_bytes)/1024:.1f} KB)")
                result = await self.client.upload_media(
                    image_bytes, type="image", filename=filename
                )
                media_id = result["media_id"]

                await self.client.reply_media(frame, "image", media_id)
                print(f"[bot] Generated image sent: {filename}")

            except Exception as e:
                print(f"[bot] Failed to send generated image {img_path}: {e}")

    async def on_enter_chat(self, frame):
        """Handle user entering chat"""
        body = frame["body"]
        user_id = body.get("from", {}).get("userid", "")
        print(f"[bot] User entered chat: {user_id}")

        # Send welcome message (must respond within 5 seconds)
        config = load_config()
        welcome = config.get(
            "welcome_message",
            "Hi! I'm an AI assistant. Ask me anything!"
        )
        try:
            await self.client.reply_welcome(frame, {
                "msgtype": "text",
                "text": {"content": welcome}
            })
        except Exception as e:
            print(f"[bot] Welcome message failed: {e}")


async def main_async(bot_config, model_config):
    """Main async entry point"""
    handler = IntelligentBotHandler(model_config)

    # Create WebSocket client (positional args: bot_id, secret)
    client = WSClient(bot_config["bot_id"], bot_config["secret"])
    handler.set_client(client)

    # Register event handlers
    client.on("message.text", handler.on_text)
    client.on("message.image", handler.on_image)
    client.on("event.enter_chat", handler.on_enter_chat)

    # Lifecycle handlers
    client.on("authenticated", lambda: print("[bot] Authenticated, listening for messages..."))
    client.on("disconnected", lambda reason: print(f"[bot] Disconnected: {reason}"))
    client.on("reconnecting", lambda attempt: print(f"[bot] Reconnecting (attempt {attempt})..."))
    client.on("error", lambda err: print(f"[bot] Error: {err}"))

    print(f"[bot] WeCom Intelligent Bot starting...")
    print(f"[bot] Bot ID: {bot_config['bot_id']}")
    print(f"[bot] Model: {model_config.get('model', 'unknown')} ({model_config.get('mode', 'unknown')})")
    print(f"[bot] Connecting via WebSocket (wss://openws.work.weixin.qq.com)...")

    # Connect (non-blocking)
    await client.connect()

    # Keep alive
    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("[bot] Shutting down...")
        await client.disconnect()


def main():
    parser = argparse.ArgumentParser(description="WeCom Intelligent Bot")
    parser.add_argument("--config", default=WECOM_BOT_CONFIG_PATH)
    args = parser.parse_args()

    bot_config = load_bot_config(args.config)
    model_config = load_model_config()

    try:
        asyncio.run(main_async(bot_config, model_config))
    except KeyboardInterrupt:
        print("\n[bot] Shutting down...")


if __name__ == "__main__":
    main()
