"""
WeChat Customer Service Handler
- Fetches messages via kf/sync_msg API
- Generates AI replies via shared/ai_engine
- Sends replies via kf/send_msg API
- Optionally forwards conversation to internal group via appchat/send
- Supports image messages (download via media API, pass to AI engine)

WeCom KF API reference:
  sync_msg:  POST /cgi-bin/kf/sync_msg   (fetch pending messages)
  send_msg:  POST /cgi-bin/kf/send_msg   (reply to customer)
  media/get: GET  /cgi-bin/media/get     (download media files)
  appchat:   POST /cgi-bin/appchat/send  (forward to internal group)

Rate limit: 5 server→user messages per 48h window per customer conversation.
"""
import os
import re
import time
import base64
import tempfile
import json
import secrets
import shutil
import subprocess
import threading
import requests
from collections import defaultdict
from pathlib import Path
from wechatpy.enterprise import WeChatClient

from shared.config import load_config, log, MAX_HISTORY
from shared.ai_engine import (
    generate_reply,
    generate_reply_qodercli_plan,
    generate_reply_qodercli_execute,
    generate_reply_codexcli_plan,
    generate_reply_codexcli_execute,
)
from customer_service.rule_router import classify_file_rule, load_rule_handler
# WeCom API base
_QYAPI = "https://qyapi.weixin.qq.com/cgi-bin"


class CSHandler:
    def __init__(self, cs_config, model_config):
        self.cs_config = cs_config
        self.model_config = model_config
        self.corp_id = cs_config["corp_id"]
        self.corp_secret = cs_config["corp_secret"]

        # WeChatClient handles access_token caching
        self.client = WeChatClient(self.corp_id, self.corp_secret)

        # Conversation history: keyed by f"{open_kfid}:{external_userid}"
        self.history = defaultdict(list)

        # Message quota tracking: 5 messages per 48h window
        # Key: f"{open_kfid}:{external_userid}"
        # Value: {"count": int, "window_start": float}
        self.quota = {}
        self.QUOTA_LIMIT = 5
        self.QUOTA_WINDOW = 48 * 3600  # 48 hours in seconds

        # Sync cursor: tracks last fetched message position
        self.sync_cursor = ""

        # Message dedup: skip already-processed messages (multiple callbacks can fire for the same msg)
        self.seen_msgids = set()
        self.MAX_SEEN = 1000  # cap to avoid unbounded memory growth

        # CLI ask mode: pending permission sessions
        # Key: conv_key, Value: {"session_id": str, "system_prompt": str, "plan": str, "timestamp": float}
        self.pending_sessions = {}
        self.qodercli_permission_mode = model_config.get("qodercli_permission_mode", "bypass")
        self.codexcli_permission_mode = model_config.get("codexcli_permission_mode", "bypass")

        # Pending images: user sent image, waiting for text description
        # Key: conv_key, Value: {"file_path": str, "b64_data": str, "mime": str, "timestamp": float}
        self.pending_images = {}

        # Pending files: user sent file (PDF/DOCX/etc), waiting for text instruction
        # Key: conv_key, Value: {"file_path": str, "file_bytes": bytes, "filename": str, "timestamp": float}
        self.pending_files = {}

        # Pending manual uploads for clients that only expose a filename via sync_msg.
        # Key: token, Value: {"open_kfid": str, "external_userid": str, "filename": str, "timestamp": float}
        self.pending_uploads = {}

        # Accepted send_msg calls can still fail asynchronously. Keep recent
        # outbound text by msgid so msg_send_fail events can retry the exact
        # missing chunk instead of silently losing part of a long reply.
        self.outbound_messages = {}
        self.outbound_lock = threading.Lock()

        # Internal forwarding (optional)
        self.forward_chatid = cs_config.get("forward_chatid", "")
        self.forward_userid = cs_config.get("forward_userid", "")
        self.agent_id = cs_config.get("agent_id", "")

        # open_kfid: the customer service account ID (required for sync_msg)
        # Auto-discover from API if not set in config
        self.open_kfid = cs_config.get("open_kfid", "")
        if self.open_kfid:
            print(f"[cs] Using configured open_kfid: {self.open_kfid}")
        else:
            self._discover_open_kfid()

        # Stats
        self.total_messages = 0
        self.total_replies = 0
        self.total_forwards = 0

    def _discover_open_kfid(self):
        """Auto-discover open_kfid from kf/account/list API.
        If multiple accounts exist, list them all and use the first one
        (user should set open_kfid in config to pick a specific one).
        """
        try:
            token = self.client.access_token
            resp = requests.get(
                f"{_QYAPI}/kf/account/list?access_token={token}",
                timeout=10,
            )
            data = resp.json()
            accounts = data.get("account_list", [])
            if not accounts:
                print(f"[cs] WARNING: No customer service accounts found. Set open_kfid in config.")
                return

            print(f"[cs] Found {len(accounts)} customer service account(s):")
            for acc in accounts:
                marker = ""
                if acc["open_kfid"] == self.open_kfid:
                    marker = " ← configured"
                print(f"[cs]   {acc['open_kfid']}  {acc.get('name', '')}{marker}")

            if not self.open_kfid:
                self.open_kfid = accounts[0]["open_kfid"]
                name = accounts[0].get("name", "")
                print(f"[cs] Using first account: {self.open_kfid} ({name})")
                if len(accounts) > 1:
                    print(f"[cs] NOTE: Multiple accounts found. Set 'open_kfid' in config to pick a specific one.")

        except Exception as e:
            print(f"[cs] Failed to discover open_kfid: {e}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def sync_and_process(self):
        """Fetch new messages via sync_msg and process each one."""
        try:
            messages = self._sync_messages()
            if not messages:
                print("[cs] sync_msg returned 0 messages")
                return

            print(f"[cs] sync_msg returned {len(messages)} message(s)")

            for msg in messages:
                self._dispatch(msg)

        except Exception as e:
            print(f"[cs] sync_and_process error: {e}", flush=True)

    def get_stats(self):
        return {
            "total_messages": self.total_messages,
            "total_replies": self.total_replies,
            "total_forwards": self.total_forwards,
            "active_conversations": len(self.history),
        }

    # ------------------------------------------------------------------
    # Message fetching
    # ------------------------------------------------------------------

    def _sync_messages(self):
        """Call kf/sync_msg to fetch pending messages.

        Returns list of message dicts from the API response.
        Updates self.sync_cursor for next call.
        """
        token = self.client.access_token

        payload = {
            "cursor": self.sync_cursor,
            "open_kfid": self.open_kfid,
            "limit": 100,
        }

        resp = requests.post(
            f"{_QYAPI}/kf/sync_msg?access_token={token}",
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("errcode", 0) != 0:
            print(f"[cs] sync_msg error: {data}", flush=True)
            return []

        # Update cursor for next sync
        self.sync_cursor = data.get("next_cursor", "")

        msg_list = data.get("msg_list", [])

        # If has_more, fetch again (recursive but bounded by API rate limits)
        if data.get("has_more") and self.sync_cursor:
            more = self._sync_messages()
            msg_list.extend(more)

        return msg_list

    # ------------------------------------------------------------------
    # Message dispatch
    # ------------------------------------------------------------------

    _FILE_EXTS = {
        ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt",
        ".txt", ".csv", ".zip", ".rar", ".7z", ".tar", ".gz",
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
    }

    def _looks_like_filename(self, value):
        if not isinstance(value, str):
            return False
        name = value.strip().strip('"\'`')
        if not name or len(name) > 260:
            return False
        return os.path.splitext(name)[1].lower() in self._FILE_EXTS

    def _find_workspace_file_by_name(self, filename):
        if not self._looks_like_filename(filename):
            return None

        workspace_dir = self.model_config.get("workspace_dir") or os.getcwd()
        if not os.path.isdir(workspace_dir):
            workspace_dir = os.getcwd()

        target = os.path.basename(filename.strip().strip('"\'`'))
        direct = os.path.join(workspace_dir, target)
        if os.path.isfile(direct):
            return direct

        for subdir in ("cs_files", "cs_media"):
            candidate = os.path.join(workspace_dir, subdir, target)
            if os.path.isfile(candidate):
                return candidate

        return None

    def _make_upload_url(self, token):
        base_url = (self.cs_config.get("public_base_url") or "").rstrip("/")
        if base_url:
            return f"{base_url}/upload/{token}"
        return f"/upload/{token}"

    def _create_upload_request(self, open_kfid, external_userid, filename):
        token = secrets.token_urlsafe(24)
        self.pending_uploads[token] = {
            "open_kfid": open_kfid,
            "external_userid": external_userid,
            "filename": os.path.basename(filename.strip().strip('"\'`')),
            "timestamp": time.time(),
        }
        return token, self._make_upload_url(token)

    def _walk_dicts(self, value):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from self._walk_dicts(child)
        elif isinstance(value, list):
            for item in value:
                yield from self._walk_dicts(item)

    def _extract_file_payload(self, msg):
        """Best-effort extraction for WeCom file/attachment payload variants."""
        text_name = msg.get("text", {}).get("content", "")

        filename_keys = (
            "filename", "file_name", "name", "title", "display_name",
            "fileName", "file_name_utf8",
        )
        media_keys = ("media_id", "mediaid", "mediaId")

        for item in self._walk_dicts(msg):
            media_id = ""
            for key in media_keys:
                if item.get(key):
                    media_id = str(item.get(key))
                    break
            if not media_id:
                continue

            filename = ""
            for key in filename_keys:
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    filename = value.strip()
                    break

            if not filename and self._looks_like_filename(text_name):
                filename = text_name.strip()

            if filename or msg.get("msgtype") in {"file", "attachment"}:
                return media_id, filename

        return "", ""

    def _dispatch(self, msg):
        """Route a message to the appropriate handler based on type."""
        # Dedup: skip messages we've already processed
        msgid = msg.get("msgid", "")
        if msgid:
            if msgid in self.seen_msgids:
                print(f"[cs] Duplicate msgid {msgid}, skipping")
                return
            self.seen_msgids.add(msgid)
            # Cap the set size to avoid memory growth
            if len(self.seen_msgids) > self.MAX_SEEN:
                self.seen_msgids = set(list(self.seen_msgids)[-500:])

        msg_type = msg.get("msgtype", "")

        # Events do not necessarily carry origin=3. Handle them before the
        # customer-message filter so asynchronous send failures stay visible.
        if msg_type == "event":
            self._handle_event(msg)
            return

        origin = msg.get("origin", 0)  # 3=customer, 4=service
        # Only process customer-sent messages (origin=3)
        if origin != 3:
            # Service-side messages (our own replies) or system events — skip
            return

        open_kfid = msg.get("open_kfid", "")
        external_userid = msg.get("external_userid", "")

        if not open_kfid or not external_userid:
            print(f"[cs] Missing open_kfid or external_userid, skipping: {msg}")
            return

        self.total_messages += 1

        file_media_id, file_name = self._extract_file_payload(msg)
        if file_media_id and (msg_type not in {"image", "voice"} or file_name):
            print(f"[cs] File-like msg type={msg_type}, filename={file_name}, "
                  f"media_id={file_media_id[:20]}...", flush=True)
            self._handle_file(open_kfid, external_userid, file_media_id, file_name)
            return

        text_content = msg.get("text", {}).get("content", "")
        if msg_type == "text" and self._looks_like_filename(text_content):
            try:
                raw = json.dumps(msg, ensure_ascii=False)
                print(f"[cs] Filename-like text without media_id, raw msg: {raw}", flush=True)
            except Exception:
                print(f"[cs] Filename-like text without media_id, msg keys: {list(msg.keys())}", flush=True)

        if msg_type == "text":
            content = msg.get("text", {}).get("content", "")
            self._handle_text(open_kfid, external_userid, content)

        elif msg_type == "image":
            media_id = msg.get("image", {}).get("media_id", "")
            # Some messages may have a text caption alongside the image
            caption = msg.get("text", {}).get("content", "") if "text" in msg else ""
            self._handle_image(open_kfid, external_userid, media_id, caption)

        elif msg_type == "voice":
            media_id = msg.get("voice", {}).get("media_id", "")
            self._handle_voice(open_kfid, external_userid, media_id)

        elif msg_type == "file":
            print(f"[cs] File message has no downloadable media_id: {msg}", flush=True)
            self._send_reply(open_kfid, external_userid,
                             "我收到了文件消息，但没有拿到可下载的文件内容。请重新发送一次文件。")

        else:
            # Unknown message type — acknowledge but don't process
            print(f"[cs] Unsupported msg type: {msg_type} from {external_userid}")
            self._send_reply(open_kfid, external_userid,
                             "暂时支持文字、图片、语音和常见文件。请直接输入问题，或重新发送文件。")

    def _handle_event(self, msg):
        """Log WeCom customer-service events, especially async send failures."""
        event = msg.get("event", {})
        event_type = event.get("event_type", "")
        external_userid = event.get("external_userid") or msg.get("external_userid", "")

        if event_type == "msg_send_fail":
            fail_type = event.get("fail_type", 0)
            reasons = {
                0: "unknown reason",
                1: "customer-service account deleted",
                2: "application disabled",
                4: "conversation expired (over 48 hours)",
                5: "conversation closed",
                6: "five-message quota exceeded",
                7: "Video Account not linked",
                8: "organization not verified",
                9: "Video Account not linked and organization not verified",
                10: "customer rejected messages",
                13: "not documented by public WeCom API references",
            }
            try:
                fail_type_number = int(fail_type)
            except (TypeError, ValueError):
                fail_type_number = 0
            reason = reasons.get(fail_type_number, "unrecognized failure type")
            print(
                f"[cs] MESSAGE DELIVERY FAILED: user={external_userid}, "
                f"msgid={event.get('fail_msgid', '')}, fail_type={fail_type} ({reason})",
                flush=True,
            )

            failed = self._pop_outbound_message(event.get("fail_msgid", ""))
            if fail_type_number == 13 and failed:
                self._retry_failed_reply(failed, fail_type_number)
            elif failed and failed.get("retry_count", 0) > 0:
                print(
                    f"[cs] Retry delivery failed; no further retry: "
                    f"msgid={event.get('fail_msgid', '')}",
                    flush=True,
                )
            return

        print(f"[cs] Event: {event_type or 'unknown'} from {external_userid}", flush=True)

    # ------------------------------------------------------------------
    # Text message handling (AI reply)
    # ------------------------------------------------------------------

    def _handle_text(self, open_kfid, external_userid, content):
        """Process a text message: generate AI reply and send."""
        if not content or not content.strip():
            return

        conv_key = f"{open_kfid}:{external_userid}"

        # WeCom server-side resets the 48h/5-msg window on every user message.
        # Mirror that behavior: reset our local quota tracker now.
        self._reset_quota(conv_key)

        # Check quota (should always pass after reset, kept as safety net)
        if not self._check_quota(conv_key):
            print(f"[cs] Quota exhausted for {conv_key}, skipping reply")
            self._send_reply(open_kfid, external_userid,
                             "本轮对话额度已用完，请过一段时间再来提问~")
            return

        # Load runtime config (hot reload)
        config = load_config()
        if not config.get("enabled", True):
            print("[cs] Service disabled in config, skipping")
            return

        # System prompt (use DM prompt — customer service is 1:1)
        system_prompt = config.get(
            "dm_system_prompt",
            ""
        )

        # --- CLI "ask" mode: check for pending permission session ---
        if conv_key in self.pending_sessions:
            self._handle_permission_response(
                open_kfid, external_userid, conv_key, content, system_prompt)
            return

        # --- Check for pending image: user sent text after image ---
        if conv_key in self.pending_images:
            self._process_pending_image(
                open_kfid, external_userid, conv_key, content)
            return

        # --- Check for pending file: user sent text after file ---
        if conv_key in self.pending_files:
            self._process_pending_file(
                open_kfid, external_userid, conv_key, content)
            return

        if self._looks_like_filename(content) and not self._find_workspace_file_by_name(content):
            token, upload_url = self._create_upload_request(open_kfid, external_userid, content)
            if upload_url.startswith("http"):
                tip = f"请打开这个上传链接，把文件「{content.strip()}」传上来：\n{upload_url}"
            else:
                tip = (
                    f"我这里只收到了文件名「{content.strip()}」，没有收到文件内容。"
                    f"请让管理员配置 customer_service_config.json 里的 public_base_url，"
                    f"或把文件放到服务器 workspace\\cs_files 后再告诉我。"
                )
            self._send_reply(
                open_kfid,
                external_userid,
                tip
            )
            return

        # Add user message to history
        self.history[conv_key].append({"role": "user", "content": content})

        # Trim history
        max_msgs = MAX_HISTORY * 2
        if len(self.history[conv_key]) > max_msgs:
            self.history[conv_key] = self.history[conv_key][-max_msgs:]

        # --- CLI "ask" mode: phase 1 (plan) ---
        mode = self.model_config.get("mode", "ollama")
        if mode == "qodercli" and self.qodercli_permission_mode == "ask":
            self._handle_cli_ask(
                "qodercli",
                open_kfid, external_userid, conv_key, system_prompt)
            return
        if mode in ("codexcli", "codecli") and self.codexcli_permission_mode == "ask":
            self._handle_cli_ask(
                "codexcli",
                open_kfid, external_userid, conv_key, system_prompt)
            return

        # --- Normal reply generation ---
        print(f"[cs] Generating reply for {external_userid}: {content[:60]}...")
        generation_started_at = time.time()
        reply = generate_reply(
            self.history[conv_key],
            system_prompt,
            self.model_config,
        )

        if not reply:
            reply = "抱歉，系统暂时出了点问题，请稍后再试。"

        self._finish_reply(open_kfid, external_userid, conv_key, content, reply,
                           generation_started_at=generation_started_at)

    # ------------------------------------------------------------------
    # Image message handling (multimodal)
    # ------------------------------------------------------------------

    def _workspace_subdir(self, name):
        workspace_dir = self.model_config.get("workspace_dir") or os.getcwd()
        if not os.path.isdir(workspace_dir):
            workspace_dir = os.getcwd()
        subdir = os.path.join(workspace_dir, name)
        os.makedirs(subdir, exist_ok=True)
        return subdir

    def _create_request_output_dir(self):
        output_root = Path(self._workspace_subdir("outputs"))
        request_dir = output_root / f"request_{secrets.token_hex(12)}"
        request_dir.mkdir(parents=False, exist_ok=False)
        return request_dir

    def _safe_workspace_filename(self, filename, fallback):
        filename = os.path.basename(filename or fallback)
        filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename).strip()
        return filename or fallback

    def accept_uploaded_file(self, token, uploaded_file):
        """Accept a browser-uploaded file for a pending customer session."""
        pending = self.pending_uploads.pop(token, None)
        if not pending:
            return False, "上传链接无效或已过期。"
        if time.time() - pending.get("timestamp", 0) > 3600:
            return False, "上传链接已过期，请重新发送文件名获取新的上传链接。"
        if not uploaded_file or not uploaded_file.filename:
            return False, "没有选择文件。"

        original_name = uploaded_file.filename
        expected_name = pending.get("filename") or original_name
        filename = self._safe_workspace_filename(original_name, expected_name)
        file_dir = self._workspace_subdir("cs_files")
        file_path = os.path.join(file_dir, filename)
        uploaded_file.save(file_path)

        size_bytes = os.path.getsize(file_path)
        file_bytes = b""
        if size_bytes <= 10 * 1024 * 1024:
            with open(file_path, "rb") as f:
                file_bytes = f.read()

        open_kfid = pending["open_kfid"]
        external_userid = pending["external_userid"]
        conv_key = f"{open_kfid}:{external_userid}"
        self.pending_files[conv_key] = {
            "file_path": file_path,
            "file_bytes": file_bytes,
            "filename": filename,
            "timestamp": time.time(),
        }
        self._send_reply(
            open_kfid,
            external_userid,
            f"文件「{filename}」已上传成功，请告诉我你想对这个文件做什么？"
        )
        print(f"[cs] Manual upload accepted: {filename} ({size_bytes / 1024:.1f} KB)")
        return True, "上传成功，可以回到微信继续发送处理要求。"

    def _download_media(self, media_id):
        """Download a media file from WeCom API.

        Returns (file_path, base64_data, mime_type) or (None, None, None) on failure.
        The file is saved under workspace so CLI tools can use relative paths.
        """
        if not media_id:
            print("[cs] Empty media_id, skipping download")
            return None, None, None

        token = self.client.access_token

        try:
            resp = requests.get(
                f"{_QYAPI}/media/get",
                params={"access_token": token, "media_id": media_id},
                timeout=30,
            )

            content_type = resp.headers.get("Content-Type", "")

            # WeCom returns JSON error if media_id is invalid
            if "application/json" in content_type or "text/plain" in content_type:
                data = resp.json()
                print(f"[cs] media/get error: {data}", flush=True)
                return None, None, None

            # Determine MIME type and extension
            mime = content_type
            ext = ".jpg"
            if "png" in content_type:
                ext = ".png"
                mime = "image/png"
            elif "gif" in content_type:
                ext = ".gif"
                mime = "image/gif"
            elif "webp" in content_type:
                ext = ".webp"
                mime = "image/webp"
            elif "jpeg" in content_type or "jpg" in content_type:
                mime = "image/jpeg"

            media_dir = self._workspace_subdir("cs_media")
            file_path = os.path.join(media_dir, f"image_{int(time.time())}{ext}")
            with open(file_path, "wb") as f:
                f.write(resp.content)

            # Base64 encode for remote API
            b64_data = base64.b64encode(resp.content).decode("utf-8")

            size_kb = len(resp.content) / 1024
            print(f"[cs] Media downloaded: {file_path} ({size_kb:.1f} KB, {mime})")
            return file_path, b64_data, mime

        except Exception as e:
            print(f"[cs] media/get exception: {e}", flush=True)
            return None, None, None

    # ------------------------------------------------------------------
    # Voice message handling (ASR → text → AI reply)
    # ------------------------------------------------------------------

    def _download_voice(self, media_id):
        """Download a voice file from WeCom API.

        Returns (file_path, content_type) or (None, None) on failure.
        WeCom voice files are typically in AMR format.
        """
        if not media_id:
            print("[cs] Empty voice media_id, skipping")
            return None, None

        token = self.client.access_token

        try:
            resp = requests.get(
                f"{_QYAPI}/media/get",
                params={"access_token": token, "media_id": media_id},
                timeout=30,
            )

            content_type = resp.headers.get("Content-Type", "")

            if "application/json" in content_type or "text/plain" in content_type:
                data = resp.json()
                print(f"[cs] voice media/get error: {data}", flush=True)
                return None, None

            # Determine extension from content type
            ext = ".amr"
            if "mp3" in content_type or "mpeg" in content_type:
                ext = ".mp3"
            elif "wav" in content_type:
                ext = ".wav"
            elif "ogg" in content_type:
                ext = ".ogg"
            elif "silk" in content_type or "x-silk" in content_type:
                ext = ".silk"

            # Save to temp file
            tmp_dir = tempfile.mkdtemp(prefix="cs_voice_")
            file_path = os.path.join(tmp_dir, f"voice_{int(time.time())}{ext}")
            with open(file_path, "wb") as f:
                f.write(resp.content)

            size_kb = len(resp.content) / 1024
            print(f"[cs] Voice downloaded: {file_path} ({size_kb:.1f} KB, {content_type})")
            return file_path, content_type

        except Exception as e:
            print(f"[cs] voice download error: {e}", flush=True)
            return None, None

    # -- Whisper model cache (loaded once, reused across calls) --
    _whisper_model = None

    @classmethod
    def _get_whisper_model(cls):
        """Load faster-whisper model once and cache it."""
        if cls._whisper_model is not None:
            return cls._whisper_model

        # Make ffmpeg available (bundled via imageio-ffmpeg)
        try:
            import imageio_ffmpeg
            ffmpeg_dir = os.path.dirname(imageio_ffmpeg.get_ffmpeg_exe())
            if ffmpeg_dir not in os.environ.get("PATH", ""):
                os.environ["PATH"] = ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")
        except ImportError:
            pass

        # Local model path: D:\AI\whisper-base-model (code 上级目录)
        model_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "whisper-base-model",
        )

        try:
            from faster_whisper import WhisperModel
            if not os.path.isdir(model_dir):
                print(f"[cs] Whisper model not found at {model_dir}", flush=True)
                print("[cs] Run: python tools/download_whisper_model.py --pack", flush=True)
                return None
            cls._whisper_model = WhisperModel(model_dir, device="cpu",
                                              compute_type="int8")
            print(f"[cs] Whisper model loaded from {model_dir}", flush=True)
            return cls._whisper_model
        except Exception as e:
            print(f"[cs] Failed to load Whisper model: {e}", flush=True)
            return None

    def _convert_audio_for_api(self, file_path):
        """Convert WeCom voice formats to a 16 kHz mono WAV for ASR APIs."""
        ffmpeg_exe = shutil.which("ffmpeg")
        try:
            import imageio_ffmpeg
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError:
            pass

        if not ffmpeg_exe:
            print("[cs] FFmpeg is unavailable; cannot convert voice for API ASR", flush=True)
            return None, False

        output_path = os.path.splitext(file_path)[0] + "_asr.wav"
        try:
            result = subprocess.run(
                [
                    ffmpeg_exe, "-y", "-i", file_path,
                    "-vn", "-ac", "1", "-ar", "16000",
                    "-c:a", "pcm_s16le", output_path,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=int(self.model_config.get("voice_asr_convert_timeout", 60)),
                check=False,
            )
            if result.returncode != 0 or not os.path.isfile(output_path):
                error_tail = (result.stderr or "")[-300:].replace("\n", " ")
                print(f"[cs] Voice conversion failed: {error_tail}", flush=True)
                return None, False
            return output_path, True
        except Exception as e:
            print(f"[cs] Voice conversion error: {e}", flush=True)
            return None, False

    def _transcribe_audio_api(self, file_path):
        """Transcribe audio through an OpenAI-compatible transcription API."""
        api_base = str(
            self.model_config.get("voice_asr_api_base")
            or self.model_config.get("api_base")
            or os.environ.get("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        ).rstrip("/")
        api_key = str(
            self.model_config.get("voice_asr_api_key")
            or self.model_config.get("api_key")
            or os.environ.get("OPENAI_API_KEY")
            or ""
        )
        model = str(self.model_config.get("voice_asr_model") or "gpt-4o-transcribe")
        language = str(self.model_config.get("voice_asr_language") or "zh")
        timeout = int(self.model_config.get("voice_asr_timeout", 90))

        if not api_key:
            print("[cs] Voice ASR API key is not configured", flush=True)
            return None

        upload_path, should_remove = self._convert_audio_for_api(file_path)
        if not upload_path:
            return None

        try:
            started_at = time.time()
            with open(upload_path, "rb") as audio_file:
                resp = requests.post(
                    f"{api_base}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    files={
                        "file": (
                            os.path.basename(upload_path),
                            audio_file,
                            "audio/wav",
                        )
                    },
                    data={"model": model, "language": language},
                    timeout=(10, timeout),
                )

            try:
                data = resp.json()
            except ValueError:
                data = {}

            if resp.status_code >= 400:
                error = data.get("error", {}) if isinstance(data, dict) else {}
                message = error.get("message", "") if isinstance(error, dict) else ""
                print(
                    f"[cs] Voice ASR API error: HTTP {resp.status_code} {message[:200]}",
                    flush=True,
                )
                return None

            text = str(data.get("text", "")).strip() if isinstance(data, dict) else ""
            elapsed = time.time() - started_at
            print(
                f"[cs] Voice ASR API: {text[:80]} "
                f"(model={model}, {elapsed:.2f}s)",
                flush=True,
            )
            return text if text else None
        except Exception as e:
            print(f"[cs] Voice ASR API request failed: {e}", flush=True)
            return None
        finally:
            if should_remove and upload_path and os.path.isfile(upload_path):
                try:
                    os.remove(upload_path)
                except OSError:
                    pass

    def _transcribe_audio_local(self, file_path):
        """Transcribe audio using the local faster-whisper model."""
        model = self._get_whisper_model()
        if model is None:
            print("[cs] Whisper model not available", flush=True)
            return None

        try:
            segments, info = model.transcribe(
                file_path,
                language="zh",
                beam_size=5,
                vad_filter=True,
            )
            text = "".join(seg.text for seg in segments).strip()
            print(f"[cs] Whisper: {text[:80]} (lang={info.language}, "
                  f"prob={info.language_probability:.2f})", flush=True)
            return text if text else None

        except Exception as e:
            print(f"[cs] Whisper error: {e}", flush=True)
            return None

    def _transcribe_audio(self, file_path):
        """Transcribe through the configured provider with an optional local fallback."""
        provider = str(self.model_config.get("voice_asr_provider") or "local").lower()

        if provider in {"api", "openai", "remote"}:
            text = self._transcribe_audio_api(file_path)
            if text:
                return text
            if self.model_config.get("voice_asr_fallback_local", True):
                print("[cs] Voice ASR API failed; falling back to local Whisper", flush=True)
                return self._transcribe_audio_local(file_path)
            return None

        if provider == "auto":
            has_api_key = bool(
                self.model_config.get("voice_asr_api_key")
                or self.model_config.get("api_key")
                or os.environ.get("OPENAI_API_KEY")
            )
            if has_api_key:
                text = self._transcribe_audio_api(file_path)
                if text:
                    return text
            return self._transcribe_audio_local(file_path)

        return self._transcribe_audio_local(file_path)

    def _handle_voice(self, open_kfid, external_userid, media_id):
        """Process a voice message: download → transcribe → treat as text."""
        conv_key = f"{open_kfid}:{external_userid}"
        self._reset_quota(conv_key)

        print(f"[cs] Voice from {external_userid}, media_id={media_id[:20]}...")

        # Download voice file
        file_path, content_type = self._download_voice(media_id)
        if not file_path:
            self._send_reply(open_kfid, external_userid,
                             "抱歉，语音下载失败，请用文字发送~")
            return

        # Transcribe
        try:
            text = self._transcribe_audio(file_path)
        finally:
            try:
                os.remove(file_path)
                os.rmdir(os.path.dirname(file_path))
            except Exception:
                pass

        if not text:
            self._send_reply(open_kfid, external_userid,
                             "抱歉，语音识别失败，请用文字发送~")
            return

        print(f"[cs] Voice transcribed: {text[:60]}")

        # Process as a normal text message
        self._handle_text(open_kfid, external_userid, text)

    # ------------------------------------------------------------------
    # File message handling (download → extract text → AI reply)
    # ------------------------------------------------------------------

    # Supported text-based extensions (read directly)
    _TEXT_EXTENSIONS = {
        ".txt", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml",
        ".md", ".rst", ".log", ".ini", ".cfg", ".conf", ".env",
        ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".cpp",
        ".h", ".cs", ".go", ".rs", ".rb", ".php", ".sh", ".bat",
        ".sql", ".html", ".htm", ".css", ".scss", ".less",
        ".toml", ".properties", ".gitignore", ".dockerfile",
    }

    def _download_file_content(self, media_id, filename=""):
        """Download a file from WeCom media/get API.

        Returns (file_path, file_bytes, content_type) or (None, None, None).
        """
        if not media_id:
            return None, None, None

        token = self.client.access_token

        try:
            url = f"{_QYAPI}/media/get"
            params = {"access_token": token, "media_id": media_id}
            chunk_size = 19 * 1024 * 1024  # WeCom Range chunks must stay under 20 MB.
            resp = requests.get(
                url,
                params=params,
                headers={"Range": f"bytes=0-{chunk_size - 1}"},
                timeout=120,
            )
            content_type = resp.headers.get("Content-Type", "")

            if "application/json" in content_type:
                data = resp.json()
                print(f"[cs] file media/get error: {data}", flush=True)
                return None, None, None
            if "text/plain" in content_type:
                try:
                    data = resp.json()
                    if isinstance(data, dict) and "errcode" in data:
                        print(f"[cs] file media/get error: {data}", flush=True)
                        return None, None, None
                except Exception:
                    pass
            if resp.status_code not in (200, 206):
                print(f"[cs] file media/get HTTP {resp.status_code}: {resp.text[:200]}", flush=True)
                return None, None, None

            first_chunk = resp.content

            # Determine extension from filename, or detect via magic bytes
            ext = os.path.splitext(filename)[1].lower() if filename else ""
            if not ext:
                # Content-Disposition may have filename
                cd = resp.headers.get("Content-Disposition", "")
                if cd:
                    import urllib.parse
                    # Try filename*=UTF-8''... (RFC 5987) first
                    if "filename*=" in cd:
                        m = re.search(r"filename\*=(?:UTF-8''|utf-8'')(.+?)(?:;|$)", cd)
                        if m:
                            filename = urllib.parse.unquote(m.group(1))
                            ext = os.path.splitext(filename)[1].lower()
                    # Fallback to filename=... and fix mojibake
                    if not ext and "filename=" in cd:
                        raw = cd.split("filename=")[-1].strip('"\' ')
                        try:
                            # Re-encode as Latin-1 then decode as UTF-8
                            filename = raw.encode("latin-1").decode("utf-8")
                        except (UnicodeDecodeError, UnicodeEncodeError):
                            filename = raw
                        ext = os.path.splitext(filename)[1].lower()

            if not ext:
                # Magic bytes detection
                head = first_chunk[:8]
                if head[:4] == b"%PDF":
                    ext = ".pdf"
                    filename = f"file_{int(time.time())}.pdf"
                elif head[:7] == b"Rar!\x1a\x07\x00" or head[:8] == b"Rar!\x1a\x07\x01\x00":
                    ext = ".rar"
                    filename = f"file_{int(time.time())}.rar"
                elif head[:6] == b"7z\xbc\xaf\x27\x1c":
                    ext = ".7z"
                    filename = f"file_{int(time.time())}.7z"
                elif head[:2] == b"PK":
                    # Could be docx/xlsx/pptx — check [Content_Types].xml
                    try:
                        import zipfile, io
                        zf = zipfile.ZipFile(io.BytesIO(first_chunk))
                        names = zf.namelist()
                        if "word/document.xml" in names:
                            ext = ".docx"
                            filename = f"file_{int(time.time())}.docx"
                        elif "xl/sharedStrings.xml" in names or "xl/worksheets/" in str(names):
                            ext = ".xlsx"
                            filename = f"file_{int(time.time())}.xlsx"
                        elif "ppt/presentation.xml" in names:
                            ext = ".pptx"
                            filename = f"file_{int(time.time())}.pptx"
                        else:
                            ext = ".zip"
                            filename = f"file_{int(time.time())}.zip"
                    except Exception:
                        ext = ".zip"
                        filename = f"file_{int(time.time())}.zip"
                elif head[:4] == b"\xd0\xcf\x11\xe0":
                    ext = ".doc"
                    filename = f"file_{int(time.time())}.doc"
                else:
                    ext = ".bin"

            if not filename:
                filename = f"file_{int(time.time())}{ext}"

            filename = self._safe_workspace_filename(filename, f"file_{int(time.time())}{ext}")
            file_dir = self._workspace_subdir("cs_files")
            file_path = os.path.join(file_dir, filename)

            content_range = resp.headers.get("Content-Range", "")
            total_size = None
            range_match = re.search(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", content_range)
            if range_match and range_match.group(3) != "*":
                total_size = int(range_match.group(3))

            keep_bytes_limit = 10 * 1024 * 1024
            collected = bytearray()
            with open(file_path, "wb") as f:
                f.write(first_chunk)
                if len(first_chunk) <= keep_bytes_limit:
                    collected.extend(first_chunk)

                downloaded = len(first_chunk)
                if resp.status_code == 206:
                    while total_size is None or downloaded < total_size:
                        end = downloaded + chunk_size - 1
                        chunk_resp = requests.get(
                            url,
                            params=params,
                            headers={"Range": f"bytes={downloaded}-{end}"},
                            timeout=120,
                        )
                        chunk_type = chunk_resp.headers.get("Content-Type", "")
                        if "application/json" in chunk_type:
                            print(f"[cs] file chunk download error: {chunk_resp.text[:300]}", flush=True)
                            return None, None, None
                        if chunk_resp.status_code not in (200, 206):
                            print(f"[cs] file chunk HTTP {chunk_resp.status_code}: {chunk_resp.text[:200]}", flush=True)
                            return None, None, None
                        chunk = chunk_resp.content
                        if not chunk:
                            break
                        f.write(chunk)
                        if len(collected) + len(chunk) <= keep_bytes_limit:
                            collected.extend(chunk)
                        downloaded += len(chunk)
                        print(f"[cs] File download chunk: {downloaded / 1024 / 1024:.1f}"
                              f"{('/' + str(round(total_size / 1024 / 1024, 1)) + ' MB') if total_size else ' MB'}",
                              flush=True)
                        if chunk_resp.status_code == 200 or len(chunk) < chunk_size:
                            break

            size_bytes = os.path.getsize(file_path)
            file_bytes = bytes(collected) if size_bytes <= keep_bytes_limit else b""
            print(f"[cs] File downloaded: {filename} ({size_bytes / 1024:.1f} KB, {content_type})")
            return file_path, file_bytes, content_type

        except Exception as e:
            print(f"[cs] file download error: {e}", flush=True)
            return None, None, None

    def _extract_text_from_file(self, file_path, file_bytes, filename=""):
        """Extract text content from a downloaded file.

        Supports: text files, PDF, DOCX.
        Returns (extracted_text, is_truncated) or (None, False) if unsupported.
        """
        ext = os.path.splitext(filename)[1].lower() if filename else os.path.splitext(file_path)[1].lower()
        MAX_CHARS = 8000  # limit to avoid overwhelming the AI context

        # Text-based files: read directly
        if ext in self._TEXT_EXTENSIONS or ext == "":
            try:
                text = file_bytes.decode("utf-8", errors="replace")
                truncated = len(text) > MAX_CHARS
                if truncated:
                    text = text[:MAX_CHARS]
                label = f"[文件: {filename or os.path.basename(file_path)}]\n\n"
                return label + text, truncated
            except Exception as e:
                print(f"[cs] Text read error: {e}", flush=True)
                return None, False

        # PDF: try PyPDF2
        if ext == ".pdf":
            try:
                from PyPDF2 import PdfReader
                import io
                reader = PdfReader(io.BytesIO(file_bytes))
                pages = []
                for page in reader.pages:
                    page_text = page.extract_text() or ""
                    pages.append(page_text)
                text = "\n".join(pages)
                truncated = len(text) > MAX_CHARS
                if truncated:
                    text = text[:MAX_CHARS]
                label = f"[PDF文件: {filename}]\n\n"
                print(f"[cs] PDF extracted: {len(text)} chars from {len(reader.pages)} pages")
                return label + text, truncated
            except ImportError:
                print("[cs] PyPDF2 not installed, cannot read PDF", flush=True)
                return None, False
            except Exception as e:
                print(f"[cs] PDF extract error: {e}", flush=True)
                return None, False

        # DOCX: try zipfile XML parsing (no python-docx dependency)
        if ext == ".docx":
            try:
                import zipfile
                import io
                import xml.etree.ElementTree as ET
                zf = zipfile.ZipFile(io.BytesIO(file_bytes))
                xml_content = zf.read("word/document.xml")
                root = ET.fromstring(xml_content)
                # Extract all text nodes
                ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
                paragraphs = []
                for p in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
                    texts = []
                    for t in p.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"):
                        if t.text:
                            texts.append(t.text)
                    if texts:
                        paragraphs.append("".join(texts))
                text = "\n".join(paragraphs)
                truncated = len(text) > MAX_CHARS
                if truncated:
                    text = text[:MAX_CHARS]
                label = f"[Word文档: {filename}]\n\n"
                print(f"[cs] DOCX extracted: {len(text)} chars")
                return label + text, truncated
            except Exception as e:
                print(f"[cs] DOCX extract error: {e}", flush=True)
                return None, False

        # XLSX: zipfile XML parsing
        if ext == ".xlsx":
            try:
                import zipfile
                import io
                import xml.etree.ElementTree as ET
                zf = zipfile.ZipFile(io.BytesIO(file_bytes))
                rows_text = []
                # Try shared strings first
                strings = []
                if "xl/sharedStrings.xml" in zf.namelist():
                    ss_xml = zf.read("xl/sharedStrings.xml")
                    ss_root = ET.fromstring(ss_xml)
                    for si in ss_root.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}si"):
                        parts = []
                        for t in si.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t"):
                            if t.text:
                                parts.append(t.text)
                        strings.append("".join(parts))
                # Read sheet1
                sheet_xml = zf.read("xl/worksheets/sheet1.xml")
                sheet_root = ET.fromstring(sheet_xml)
                for row in sheet_root.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row"):
                    cells = []
                    for c in row.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c"):
                        v = c.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}v")
                        if v is not None and v.text:
                            # Check if it's a shared string reference
                            if c.get("t") == "s":
                                idx = int(v.text)
                                cells.append(strings[idx] if idx < len(strings) else v.text)
                            else:
                                cells.append(v.text)
                    if cells:
                        rows_text.append("\t".join(cells))
                text = "\n".join(rows_text)
                truncated = len(text) > MAX_CHARS
                if truncated:
                    text = text[:MAX_CHARS]
                label = f"[Excel文件: {filename}]\n\n"
                print(f"[cs] XLSX extracted: {len(text)} chars, {len(rows_text)} rows")
                return label + text, truncated
            except Exception as e:
                print(f"[cs] XLSX extract error: {e}", flush=True)
                return None, False

        # XLS (old binary BIFF format): use xlrd
        if ext == ".xls":
            try:
                import xlrd
                import io
                book = xlrd.open_workbook(file_contents=file_bytes)
                rows_text = []
                for sheet in book.sheets():
                    for row_idx in range(sheet.nrows):
                        cells = []
                        for col_idx in range(sheet.ncols):
                            cell = sheet.cell(row_idx, col_idx)
                            val = cell.value
                            if cell.ctype == xlrd.XL_CELL_NUMBER:
                                # Avoid .0 for integers
                                val = int(val) if val == int(val) else val
                            cells.append(str(val))
                        if any(c for c in cells):
                            rows_text.append("\t".join(cells))
                text = "\n".join(rows_text)
                truncated = len(text) > MAX_CHARS
                if truncated:
                    text = text[:MAX_CHARS]
                label = f"[Excel文件: {filename}]\n\n"
                print(f"[cs] XLS extracted: {len(text)} chars, {len(rows_text)} rows, "
                      f"{book.nsheets} sheet(s)")
                return label + text, truncated
            except ImportError:
                print("[cs] xlrd not installed, cannot read .xls", flush=True)
                return None, False
            except Exception as e:
                print(f"[cs] XLS extract error: {e}", flush=True)
                return None, False

        # Unsupported format
        return None, False

    def _handle_file(self, open_kfid, external_userid, media_id, filename=""):
        """Process a file message: download, store pending, ask user what to do."""
        conv_key = f"{open_kfid}:{external_userid}"
        self._reset_quota(conv_key)

        # Clean up old pending file if any
        if conv_key in self.pending_files:
            old = self.pending_files.pop(conv_key)
            try:
                os.remove(old["file_path"])
                os.rmdir(os.path.dirname(old["file_path"]))
            except Exception:
                pass

        print(f"[cs] File from {external_userid}: {filename}, media_id={media_id[:20]}...")

        # Download file
        file_path, file_bytes, content_type = self._download_file_content(media_id, filename)
        if not file_path:
            self._send_reply(open_kfid, external_userid,
                             "抱歉，文件下载失败，请重新发送。")
            return

        # Use file_path basename as display name if original filename is empty
        if not filename:
            filename = os.path.basename(file_path)

        # Store pending file
        self.pending_files[conv_key] = {
            "file_path": file_path,
            "file_bytes": file_bytes,
            "filename": filename,
            "timestamp": time.time(),
        }

        ext = os.path.splitext(filename)[1].lower()
        type_label = {
            ".pdf": "PDF", ".docx": "Word", ".doc": "Word",
            ".xlsx": "Excel", ".xls": "Excel",
            ".csv": "CSV", ".txt": "文本",
            ".zip": "压缩包", ".rar": "压缩包", ".7z": "压缩包",
            ".tar": "压缩包", ".gz": "压缩包",
        }.get(ext, "文件")

        self._send_reply(open_kfid, external_userid,
                         f"📎 {type_label}文件「{filename}」已收到，请告诉我你想对这个文件做什么？")

    def _process_pending_file(self, open_kfid, external_userid, conv_key, user_text):
        """Process a pending file with the user's text instruction.

        Always passes the file as --attachment to qodercli, which can read
        and process any file type directly.
        """
        pending = self.pending_files.pop(conv_key)
        file_path = pending["file_path"]
        filename = pending["filename"]
        ext = os.path.splitext(filename)[1].lower()
        request_output_dir = None

        try:
            try:
                request_output_dir = self._create_request_output_dir()
            except OSError as exc:
                request_output_dir = Path(tempfile.mkdtemp(prefix="cs-request-output-"))
                print(
                    f"[cs] Request output directory unavailable: {exc}; "
                    f"using temporary directory {request_output_dir}",
                    flush=True,
                )

            try:
                rule_decision = classify_file_rule(
                    filename,
                    user_text,
                    self.model_config,
                )
            except Exception as exc:
                rule_decision = None
                print(
                    f"[rule-router] Classification failed for {filename}: {exc}; "
                    "falling back to full Codex workflow",
                    flush=True,
                )

            if rule_decision and rule_decision.rule_id:
                generation_started_at = time.time()
                try:
                    process_file = load_rule_handler(rule_decision.rule_id)
                    result = process_file(
                        file_path,
                        request_output_dir,
                    )
                except Exception as exc:
                    print(
                        f"[rule-router] Local rule {rule_decision.rule_id} failed "
                        f"for {filename}: {exc}; falling back to full Codex workflow",
                        flush=True,
                    )
                else:
                    workspace_dir = self.model_config.get("workspace_dir") or os.getcwd()
                    try:
                        relative_output = os.path.relpath(result.output_path, workspace_dir)
                    except ValueError:
                        relative_output = Path(result.output_path).name
                    relative_output = relative_output.replace(os.sep, "/")
                    reply = (
                        f"已完成收款匹配：候选组 {result.candidate_groups}，"
                        f"保留组 {result.kept_groups}，剔除组 {result.removed_groups}，"
                        f"输出行 {result.output_rows}。耗时 {result.timings['total_seconds']:.2f} 秒。\n"
                        f"结果文件：`{relative_output}`"
                    )
                    self.history[conv_key].append(
                        {"role": "user", "content": f"我发了一个文件「{filename}」，请帮我{user_text}。"}
                    )
                    self._finish_reply(
                        open_kfid,
                        external_userid,
                        conv_key,
                        user_text,
                        reply,
                        generation_started_at=generation_started_at,
                        exclude_paths=[file_path],
                        request_output_dirs=[request_output_dir],
                    )
                    return

            config = load_config()
            system_prompt = config.get(
                "dm_system_prompt",
                ""
            )

            # Data processing keywords → instruct file output
            _DATA_EXTS = {".xlsx", ".xls", ".csv"}
            _DATA_KEYWORDS = ("筛选", "过滤", "排序", "汇总", "合并", "导出",
                              "去重", "删除", "提取", "分类", "统计", "计算",
                              "转换", "生成", "处理数据", "整理", "匹配")
            need_file_output = (ext in _DATA_EXTS
                                and any(kw in user_text for kw in _DATA_KEYWORDS))

            workspace_dir = self.model_config.get("workspace_dir") or os.getcwd()
            try:
                request_output_relative = os.path.relpath(request_output_dir, workspace_dir)
                output_destination = f"相对目录 `{request_output_relative.replace(os.sep, '/')}`"
                output_reply_rule = (
                    "回复中只包含生成文件的相对路径，不要输出盘符或绝对路径"
                )
            except ValueError:
                request_output_relative = str(request_output_dir)
                output_destination = f"目录 `{request_output_relative.replace(os.sep, '/')}`"
                output_reply_rule = (
                    "回复中只包含生成文件的文件名，不要输出保存目录、盘符或绝对路径"
                )
            output_constraint = (
                f"[如需生成或修改文件，必须将所有输出保存到{output_destination}。"
                f"完成后{output_reply_rule}。]"
            )

            if need_file_output:
                instruction = (
                    f"我发了一个文件「{filename}」，请帮我{user_text}。\n\n"
                    f"[请使用Python代码读取附件文件并处理数据，将结果保存为新的文件"
                    f"（格式尽量与原文件一致，如{ext}），"
                    f"文件名要有描述性，必须保存到{output_destination}。"
                    f"完成后{output_reply_rule}。]"
                )
            else:
                instruction = (
                    f"我发了一个文件「{filename}」，请帮我{user_text}。\n\n"
                    f"{output_constraint}"
                )

            self.history[conv_key].append({"role": "user", "content": instruction})
            max_msgs = MAX_HISTORY * 2
            if len(self.history[conv_key]) > max_msgs:
                self.history[conv_key] = self.history[conv_key][-max_msgs:]

            print(f"[cs] Processing file as attachment for {external_userid}: "
                  f"{filename}, instruction: {user_text[:40]}...")

            generation_started_at = time.time()
            reply = generate_reply(
                self.history[conv_key],
                system_prompt,
                self.model_config,
                image_path=file_path,
            )

            if not reply:
                reply = "抱歉，文件处理失败，请稍后再试。"

            self._finish_reply(open_kfid, external_userid, conv_key, instruction, reply,
                               generation_started_at=generation_started_at,
                               exclude_paths=[file_path],
                               request_output_dirs=[request_output_dir])

        finally:
            try:
                os.remove(file_path)
                os.rmdir(os.path.dirname(file_path))
            except Exception:
                pass

    def _handle_image(self, open_kfid, external_userid, media_id, caption=""):
        """Process an image message: download and store, wait for user description.

        Instead of immediately generating an AI reply, we store the image and
        ask the user to describe what they want to do with it. The actual AI
        processing happens when the user sends the next text message.
        """
        conv_key = f"{open_kfid}:{external_userid}"

        # If there's already a pending image, clean it up (user sent a new one)
        if conv_key in self.pending_images:
            old = self.pending_images.pop(conv_key)
            try:
                os.remove(old["file_path"])
                os.rmdir(os.path.dirname(old["file_path"]))
            except Exception:
                pass

        print(f"[cs] Image from {external_userid}, media_id={media_id[:20]}...")

        # Download image
        file_path, b64_data, mime = self._download_media(media_id)
        if not file_path:
            self._send_reply(open_kfid, external_userid,
                             "抱歉，图片下载失败，请重新发送。")
            return

        # Store pending image
        self.pending_images[conv_key] = {
            "file_path": file_path,
            "b64_data": b64_data,
            "mime": mime,
            "caption": caption,
            "timestamp": time.time(),
        }

        # If user sent a caption with the image, process immediately
        if caption and caption.strip():
            self._process_pending_image(open_kfid, external_userid, conv_key,
                                       caption.strip())
            return

        # Otherwise, acknowledge and wait for description
        self._send_reply(open_kfid, external_userid,
                         "📷 图片已收到，请告诉我你想对这张图片做什么？")

    def _process_pending_image(self, open_kfid, external_userid, conv_key, user_text):
        """Process a pending image with the user's text description."""
        pending = self.pending_images.pop(conv_key)
        file_path = pending["file_path"]
        b64_data = pending["b64_data"]
        mime = pending["mime"]

        # Load runtime config
        config = load_config()
        system_prompt = config.get(
            "dm_system_prompt",
            ""
        )

        # Add to history
        self.history[conv_key].append({"role": "user", "content": user_text})

        # Trim history
        max_msgs = MAX_HISTORY * 2
        if len(self.history[conv_key]) > max_msgs:
            self.history[conv_key] = self.history[conv_key][-max_msgs:]

        mode = self.model_config.get("mode", "ollama")
        print(f"[cs] Processing image with text via {mode} for {external_userid}: {user_text[:40]}...")

        try:
            generation_started_at = time.time()
            reply = generate_reply(
                self.history[conv_key],
                system_prompt,
                self.model_config,
                image_path=file_path,
                image_base64=b64_data,
                image_mime=mime,
            )
        finally:
            # Clean up temp file
            try:
                os.remove(file_path)
                os.rmdir(os.path.dirname(file_path))
            except Exception:
                pass

        if not reply:
            reply = "抱歉，我暂时无法理解这张图片，请稍后再试。"

        self._finish_reply(open_kfid, external_userid, conv_key, user_text, reply,
                           generation_started_at=generation_started_at)

    def _finish_reply(self, open_kfid, external_userid, conv_key, user_content, reply,
                      generation_started_at=None, exclude_paths=None,
                      request_output_dirs=None):
        """Send reply, update history, send any generated images/files, and optionally forward."""
        ok = self._send_reply(open_kfid, external_userid, reply)
        if ok:
            self.history[conv_key].append({"role": "assistant", "content": reply})
            self.total_replies += 1

        excluded = {
            os.path.normcase(os.path.realpath(path))
            for path in (exclude_paths or [])
        }
        allowed_roots = [
            os.path.normcase(os.path.realpath(path))
            for path in (request_output_dirs or [])
        ]

        def is_allowed_file(path):
            normalized = os.path.normcase(os.path.realpath(path))
            if normalized in excluded:
                return False
            if not allowed_roots:
                return True
            for root in allowed_roots:
                try:
                    if os.path.commonpath((normalized, root)) == root:
                        return True
                except ValueError:
                    continue
            return False

        # Check if reply contains image file paths — upload and send as image messages
        image_paths = [
            path for path in self._extract_image_paths(reply) if is_allowed_file(path)
        ]
        if not image_paths and generation_started_at:
            image_paths = self._find_recent_workspace_images(
                generation_started_at,
                search_dirs=request_output_dirs,
            )
        for img_path in image_paths:
            media_id = self._upload_media_file(img_path, "image")
            if media_id:
                self._send_media_reply(open_kfid, external_userid, media_id, "image")

        # Check if reply contains document file paths — upload and send as file messages
        file_paths = [
            path
            for path in self._extract_file_paths(reply)
            if is_allowed_file(path)
        ]
        if not file_paths and generation_started_at is not None:
            file_paths = self._find_recent_workspace_files(
                generation_started_at,
                exclude_paths=exclude_paths,
                request_output_dirs=request_output_dirs,
            )
        for fpath in file_paths:
            media_id = self._upload_media_file(fpath, "file")
            if media_id:
                self._send_media_reply(open_kfid, external_userid, media_id, "file")

        # Forward conversation (forward_userid takes priority over forward_chatid)
        if self.forward_userid and self.agent_id:
            self._forward_to_user(external_userid, user_content, reply)
        elif self.forward_chatid:
            self._forward_to_group(external_userid, user_content, reply)

    def _handle_cli_ask(self, backend, open_kfid, external_userid,
                        conv_key, system_prompt):
        """CLI ask mode phase 1: identify what permissions are needed."""
        print(f"[cs-ask] Phase 1: checking {backend} permissions for {external_userid}...")

        if backend == "codexcli":
            result = generate_reply_codexcli_plan(
                self.history[conv_key],
                system_prompt,
                self.model_config,
            )
        else:
            result = generate_reply_qodercli_plan(
                self.history[conv_key],
                system_prompt,
                self.model_config,
            )

        if result["type"] == "reply":
            # Completed without needing permission — send directly
            print(f"[cs-ask] No permission needed, replying directly")
            self._finish_reply(open_kfid, external_userid, conv_key,
                              self.history[conv_key][-1]["content"],
                              result["content"])

        elif result["type"] == "permission_needed":
            # Store pending session
            self.pending_sessions[conv_key] = {
                "session_id": result["session_id"],
                "system_prompt": system_prompt,
                "plan": result["plan"],
                "backend": backend,
                "timestamp": time.time(),
            }
            # Ask the customer for approval
            ask_text = (
                f"🔐 执行需要你的授权，计划如下：\n\n"
                f"{result['plan']}\n\n"
                f"回复「确认」允许执行，回复「取消」拒绝。"
            )
            self._send_reply(open_kfid, external_userid, ask_text)
            print(f"[cs-ask] Permission request sent to {external_userid}, "
                  f"session={result['session_id']}")

        else:
            # Error
            error_msg = result.get("content", f"{backend} 执行出错")
            self._finish_reply(open_kfid, external_userid, conv_key,
                              self.history[conv_key][-1]["content"],
                              f"抱歉，系统出了点问题：{error_msg}")

    def _handle_permission_response(self, open_kfid, external_userid, conv_key,
                                    content, system_prompt):
        """Handle user's approval/denial of a pending CLI permission request."""
        session = self.pending_sessions.pop(conv_key)
        user_text = content.strip().lower()
        approved = user_text in ("确认", "同意", "允许", "approve", "yes", "y",
                                 "ok", "好", "执行", "可以")

        action = "approved" if approved else "denied"
        backend = session.get("backend", "qodercli")
        print(f"[cs-ask] User {action} {backend} permission (session={session['session_id']})")

        if not approved:
            self._send_reply(open_kfid, external_userid, "好的，操作已取消。")
            return

        # Phase 2: execute with bypass_permissions
        self._send_reply(open_kfid, external_userid, "⏳ 正在执行，请稍候...")

        if backend == "codexcli":
            reply = generate_reply_codexcli_execute(
                self.history[conv_key],
                system_prompt,
                self.model_config,
                session.get("plan", ""),
                user_approved=True,
            )
        else:
            reply = generate_reply_qodercli_execute(
                self.history[conv_key],
                system_prompt,
                self.model_config,
                session["session_id"],
                user_approved=True,
            )

        if not reply:
            reply = "操作已执行，但没有返回结果。"

        self._finish_reply(open_kfid, external_userid, conv_key,
                          self.history[conv_key][-1]["content"], reply)

    # ------------------------------------------------------------------
    # Sending replies
    # ------------------------------------------------------------------

    @staticmethod
    def _truncate_utf8(text, max_bytes):
        """Return the longest UTF-8-safe prefix within max_bytes."""
        encoded = text.encode("utf-8")
        if len(encoded) <= max_bytes:
            return text
        return encoded[:max_bytes].decode("utf-8", errors="ignore")

    @classmethod
    def _split_reply_text(cls, text, max_bytes=1800, max_parts=4):
        """Split text without breaking UTF-8 characters or WeCom's byte limit."""
        remaining = str(text or "").strip()
        if not remaining:
            return []

        chunks = []
        separators = ("\n\n", "\n", "。", "！", "？", "；", "，", " ")
        while remaining and len(chunks) < max_parts:
            if len(remaining.encode("utf-8")) <= max_bytes:
                chunks.append(remaining)
                remaining = ""
                break

            prefix = cls._truncate_utf8(remaining, max_bytes)
            cutoff = len(prefix)
            earliest_break = max(1, cutoff // 2)
            preferred_cutoff = -1
            for separator in separators:
                index = prefix.rfind(separator, earliest_break)
                if index >= 0:
                    preferred_cutoff = max(preferred_cutoff, index + len(separator))
            if preferred_cutoff > 0:
                cutoff = preferred_cutoff

            chunk = remaining[:cutoff].strip()
            if not chunk:
                chunk = prefix
                cutoff = len(prefix)
            chunks.append(chunk)
            remaining = remaining[cutoff:].lstrip()

        if remaining:
            suffix = "\n\n（回复过长，后续内容已省略）"
            available = max_bytes - len(suffix.encode("utf-8"))
            chunks[-1] = cls._truncate_utf8(chunks[-1], available).rstrip() + suffix

        return chunks

    def _ensure_outbound_tracking(self):
        """Initialize delivery tracking for normal and lightweight test instances."""
        if not hasattr(self, "outbound_messages"):
            self.outbound_messages = {}
        if not hasattr(self, "outbound_lock"):
            self.outbound_lock = threading.Lock()

    def _remember_outbound_message(self, msgid, message):
        self._ensure_outbound_tracking()
        now = time.time()
        with self.outbound_lock:
            expired = [
                key for key, value in self.outbound_messages.items()
                if now - value.get("timestamp", now) > 600
            ]
            for key in expired:
                self.outbound_messages.pop(key, None)
            self.outbound_messages[msgid] = message

            # A ten-minute cleanup normally keeps this small; retain a hard
            # cap as protection during a callback outage.
            if len(self.outbound_messages) > 500:
                oldest = sorted(
                    self.outbound_messages,
                    key=lambda key: self.outbound_messages[key].get("timestamp", 0),
                )[:100]
                for key in oldest:
                    self.outbound_messages.pop(key, None)

    def _pop_outbound_message(self, msgid):
        if not msgid:
            return None
        self._ensure_outbound_tracking()
        with self.outbound_lock:
            return self.outbound_messages.pop(msgid, None)

    @staticmethod
    def _sanitize_retry_text(text):
        """Convert a rejected chunk to conservative plain text for one retry."""
        value = str(text or "")
        value = re.sub(r"\[([^\]]+)\]\(https?://[^)]+\)", r"\1", value)
        value = re.sub(r"https?://\S+", "", value)
        value = value.replace("**", "").replace("__", "").replace("`", "")
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()

    def _send_text_content(self, open_kfid, external_userid, content, *,
                           chunk_text, chunk_index, total_chunks, retry_count=0):
        """Submit one text message and retain it for asynchronous failure handling."""
        msgid = (
            f"cs_{int(time.time() * 1000)}_{chunk_index}_{secrets.token_hex(3)}"
        )
        payload = {
            "touser": external_userid,
            "open_kfid": open_kfid,
            "msgid": msgid,
            "msgtype": "text",
            "text": {"content": content},
        }
        self._remember_outbound_message(msgid, {
            "open_kfid": open_kfid,
            "external_userid": external_userid,
            "chunk_text": chunk_text,
            "chunk_index": chunk_index,
            "total_chunks": total_chunks,
            "retry_count": retry_count,
            "timestamp": time.time(),
        })

        try:
            token = self.client.access_token
            resp = requests.post(
                f"{_QYAPI}/kf/send_msg?access_token={token}",
                json=payload,
                timeout=10,
            )
            data = resp.json()
            if data.get("errcode", 0) != 0:
                self._pop_outbound_message(msgid)
                print(
                    f"[cs] send_msg error on chunk {chunk_index}/{total_chunks}: {data}",
                    flush=True,
                )
                return False

            byte_count = len(content.encode("utf-8"))
            action = "Retry accepted" if retry_count else "Reply accepted"
            print(
                f"[cs] {action} for {external_userid}: "
                f"chunk {chunk_index}/{total_chunks}, {byte_count} bytes, msgid={msgid}",
                flush=True,
            )
            self._consume_quota(f"{open_kfid}:{external_userid}")
            return True
        except Exception as e:
            self._pop_outbound_message(msgid)
            print(
                f"[cs] send_msg exception on chunk {chunk_index}/{total_chunks}: {e}",
                flush=True,
            )
            return False

    def _retry_failed_reply(self, failed, fail_type):
        """Retry an undocumented type-13 rejection once as shorter plain text."""
        if failed.get("retry_count", 0) >= 1:
            print(
                f"[cs] Retry delivery failed with fail_type={fail_type}; giving up",
                flush=True,
            )
            return False

        clean_text = self._sanitize_retry_text(failed.get("chunk_text", ""))
        retry_chunks = self._split_reply_text(clean_text, max_bytes=850, max_parts=2)
        if not retry_chunks:
            return False

        original_index = failed.get("chunk_index", 1)
        original_total = failed.get("total_chunks", 1)
        retry_total = len(retry_chunks)
        print(
            f"[cs] Retrying rejected chunk {original_index}/{original_total} "
            f"as {retry_total} plain-text message(s)",
            flush=True,
        )
        for retry_index, retry_chunk in enumerate(retry_chunks, start=1):
            prefix = f"（补发原第 {original_index}/{original_total} 段"
            if retry_total > 1:
                prefix += f"，{retry_index}/{retry_total}"
            content = prefix + f"）\n{retry_chunk}"
            if not self._send_text_content(
                failed["open_kfid"],
                failed["external_userid"],
                content,
                chunk_text=retry_chunk,
                chunk_index=retry_index,
                total_chunks=retry_total,
                retry_count=failed.get("retry_count", 0) + 1,
            ):
                return False
        return True

    def _send_reply(self, open_kfid, external_userid, text):
        """Send a text reply to the customer via kf/send_msg.

        Long replies are split by UTF-8 byte length. Returns True only when
        every chunk is accepted by the API; final delivery is asynchronous.
        """
        chunks = self._split_reply_text(text)
        if not chunks:
            return False

        total = len(chunks)
        for index, chunk in enumerate(chunks, start=1):
            content = f"（{index}/{total}）\n{chunk}" if total > 1 else chunk
            if not self._send_text_content(
                open_kfid,
                external_userid,
                content,
                chunk_text=chunk,
                chunk_index=index,
                total_chunks=total,
            ):
                return False

        return True

    def _resolve_reply_path(self, path):
        path = path.rstrip('.,;:，。；：')
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", path):
            return None
        if os.path.isabs(path) and os.path.isfile(path):
            return path

        workspace_dir = self.model_config.get("workspace_dir") or os.getcwd()
        if not os.path.isdir(workspace_dir):
            workspace_dir = os.getcwd()

        clean_path = path.strip().strip("`").lstrip(".\\/").replace("/", os.sep)
        candidate = os.path.join(workspace_dir, clean_path)
        if os.path.isfile(candidate):
            return candidate

        basename_candidate = os.path.join(workspace_dir, os.path.basename(clean_path))
        if os.path.isfile(basename_candidate):
            return basename_candidate

        return None

    def _extract_reply_path_candidates(self, text, exts):
        # Citations and bare web links may end in .pdf/.png but are not local
        # generated files. Remove URLs before looking for path-like tokens.
        local_text = re.sub(r"https?://[^\s\])}>]+", "", text, flags=re.IGNORECASE)
        backtick_pattern = rf'`([^`]+?\.(?:{exts}))`'
        path_pattern = (
            rf'(?<![\w\\/:.-])'
            rf'((?:[A-Za-z]:\\|\.?[\\/])?[^\s`<>"\'()\[\]{{}}]+?\.(?:{exts}))'
        )
        candidates = re.findall(backtick_pattern, local_text, re.IGNORECASE)
        candidates += re.findall(path_pattern, local_text, re.IGNORECASE)
        return list(dict.fromkeys(path.strip() for path in candidates if path.strip()))

    def _extract_image_paths(self, text):
        """Extract image paths from AI reply text."""
        matches = self._extract_reply_path_candidates(
            text, r"png|jpg|jpeg|gif|webp|bmp"
        )

        valid_paths = []
        for path in matches:
            resolved = self._resolve_reply_path(path)
            if resolved:
                valid_paths.append(resolved)
                print(f"[cs] Found image in reply: {resolved}")
            else:
                print(f"[cs] Image path not found: {path}")

        return valid_paths

    def _find_recent_workspace_images(self, since_ts, search_dirs=None):
        if search_dirs is None:
            workspace_dir = self.model_config.get("workspace_dir") or os.getcwd()
            if not os.path.isdir(workspace_dir):
                workspace_dir = os.getcwd()
            search_dirs = [
                workspace_dir,
                os.path.join(workspace_dir, "outputs"),
                os.path.join(workspace_dir, "generated"),
                os.path.join(workspace_dir, "images"),
            ]
        image_exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
        candidates = []
        for directory in search_dirs:
            if not os.path.isdir(directory):
                continue
            try:
                for name in os.listdir(directory):
                    path = os.path.join(directory, name)
                    if not os.path.isfile(path):
                        continue
                    if os.path.splitext(name)[1].lower() not in image_exts:
                        continue
                    mtime = os.path.getmtime(path)
                    if mtime >= since_ts - 2:
                        candidates.append((mtime, path))
            except Exception as e:
                print(f"[cs] Recent image scan failed in {directory}: {e}", flush=True)

        candidates.sort(reverse=True)
        paths = [path for _, path in candidates[:3]]
        if paths:
            print(f"[cs] Found recent generated image(s): {paths}", flush=True)
        return paths

    def _find_recent_workspace_files(
        self, since_ts, exclude_paths=None, request_output_dirs=None
    ):
        """Find recent document outputs only in directories owned by this request."""
        excluded = {
            os.path.normcase(os.path.realpath(path))
            for path in (exclude_paths or [])
        }
        document_exts = {
            ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt",
            ".txt", ".csv", ".zip", ".rar", ".7z", ".tar", ".gz",
        }
        candidates = []
        for directory in (request_output_dirs or []):
            if not os.path.isdir(directory):
                continue
            try:
                for name in os.listdir(directory):
                    path = os.path.join(directory, name)
                    if not os.path.isfile(path):
                        continue
                    if os.path.splitext(name)[1].lower() not in document_exts:
                        continue
                    if os.path.normcase(os.path.realpath(path)) in excluded:
                        continue
                    mtime = os.path.getmtime(path)
                    if mtime >= since_ts - 2:
                        candidates.append((mtime, path))
            except Exception as exc:
                print(f"[cs] Recent document scan failed in {directory}: {exc}", flush=True)

        candidates.sort(reverse=True)
        paths = [path for _, path in candidates[:3]]
        if paths:
            print(f"[cs] Found recent generated file(s): {paths}", flush=True)
        return paths

    def _extract_file_paths(self, text):
        """Extract local document file paths from AI reply text.

        Looks for paths ending in document extensions (pdf, docx, xlsx, etc.).
        Returns list of valid, existing file paths.
        """
        _DOC_EXTS = (
            r"pdf|docx|doc|xlsx|xls|pptx|ppt|txt|csv|"
            r"zip|rar|7z|tar|gz"
        )
        matches = self._extract_reply_path_candidates(text, _DOC_EXTS)

        valid_paths = []
        for path in matches:
            resolved = self._resolve_reply_path(path)
            if resolved:
                valid_paths.append(resolved)
                print(f"[cs] Found file in reply: {resolved}")
            else:
                print(f"[cs] File path not found: {path}")

        return valid_paths

    def _upload_media_file(self, file_path, media_type="image"):
        """Upload a file to WeCom media/upload API.

        Args:
            file_path: local file path
            media_type: "image" or "file"

        Returns media_id string on success, None on failure.
        WeCom temporary media is valid for 3 days.
        """
        token = self.client.access_token

        # Guess MIME from extension
        ext = os.path.splitext(file_path)[1].lower()
        mime_map = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
            ".pdf": "application/pdf", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ".doc": "application/msword", ".xls": "application/vnd.ms-excel",
            ".txt": "text/plain", ".csv": "text/csv",
            ".zip": "application/zip",
            ".rar": "application/vnd.rar",
            ".7z": "application/x-7z-compressed",
            ".tar": "application/x-tar",
            ".gz": "application/gzip",
        }
        mime = mime_map.get(ext, "application/octet-stream")

        try:
            with open(file_path, "rb") as f:
                resp = requests.post(
                    f"{_QYAPI}/media/upload",
                    params={"access_token": token, "type": media_type},
                    files={"media": (os.path.basename(file_path), f, mime)},
                    timeout=30,
                )
            data = resp.json()

            if data.get("errcode", 0) == 0 and data.get("media_id"):
                media_id = data["media_id"]
                print(f"[cs] Media uploaded: {os.path.basename(file_path)} -> {media_id[:30]}...")
                return media_id
            else:
                print(f"[cs] media/upload error: {data}", flush=True)
                return None

        except Exception as e:
            print(f"[cs] media/upload exception: {e}", flush=True)
            return None

    def _send_media_reply(self, open_kfid, external_userid, media_id, msg_type="image"):
        """Send a media message (image or file) to the customer via kf/send_msg.

        Args:
            msg_type: "image" or "file"

        Returns True on success, False on failure.
        """
        token = self.client.access_token

        prefix = "cs_img" if msg_type == "image" else "cs_file"
        payload = {
            "touser": external_userid,
            "open_kfid": open_kfid,
            "msgid": f"{prefix}_{int(time.time() * 1000)}",
            "msgtype": msg_type,
            msg_type: {"media_id": media_id},
        }

        try:
            resp = requests.post(
                f"{_QYAPI}/kf/send_msg?access_token={token}",
                json=payload,
                timeout=10,
            )
            data = resp.json()

            if data.get("errcode", 0) == 0:
                print(f"[cs] {msg_type} sent to {external_userid}: media_id={media_id[:30]}...")
                conv_key = f"{open_kfid}:{external_userid}"
                self._consume_quota(conv_key)
                return True
            else:
                print(f"[cs] send_msg({msg_type}) error: {data}", flush=True)
                return False

        except Exception as e:
            print(f"[cs] send_msg({msg_type}) exception: {e}", flush=True)
            return False

    # ------------------------------------------------------------------
    # Internal group forwarding
    # ------------------------------------------------------------------

    def _forward_to_group(self, external_userid, question, answer):
        """Forward Q&A pair to internal group via appchat/send."""
        token = self.client.access_token

        forward_text = (
            f"📩 客户咨询\n"
            f"用户: {external_userid}\n"
            f"问题: {question}\n"
            f"AI回复: {answer}"
        )

        payload = {
            "chatid": self.forward_chatid,
            "msgtype": "text",
            "text": {"content": forward_text},
        }

        try:
            resp = requests.post(
                f"{_QYAPI}/appchat/send?access_token={token}",
                json=payload,
                timeout=10,
            )
            data = resp.json()

            if data.get("errcode", 0) == 0:
                print(f"[cs] Forwarded to group {self.forward_chatid}")
                self.total_forwards += 1
            else:
                print(f"[cs] appchat/send error: {data}", flush=True)

        except Exception as e:
            print(f"[cs] forward exception: {e}", flush=True)

    def _forward_to_user(self, external_userid, question, answer):
        """Forward Q&A pair to an individual user via message/send."""
        token = self.client.access_token

        forward_text = (
            f"📩 客户咨询\n"
            f"用户: {external_userid}\n"
            f"问题: {question}\n"
            f"AI回复: {answer}"
        )

        payload = {
            "touser": self.forward_userid,
            "msgtype": "text",
            "agentid": int(self.agent_id),
            "text": {"content": forward_text},
        }

        try:
            resp = requests.post(
                f"{_QYAPI}/message/send?access_token={token}",
                json=payload,
                timeout=10,
            )
            data = resp.json()

            if data.get("errcode", 0) == 0:
                print(f"[cs] Forwarded to user {self.forward_userid}")
                self.total_forwards += 1
            else:
                print(f"[cs] message/send error: {data}", flush=True)

        except Exception as e:
            print(f"[cs] forward to user exception: {e}", flush=True)

    # ------------------------------------------------------------------
    # Quota management (5 messages per 48h window)
    # ------------------------------------------------------------------

    def _check_quota(self, conv_key):
        """Check if conversation has remaining reply quota."""
        if conv_key not in self.quota:
            return True

        q = self.quota[conv_key]
        now = time.time()

        # Window expired — reset
        if now - q["window_start"] > self.QUOTA_WINDOW:
            del self.quota[conv_key]
            return True

        return q["count"] < self.QUOTA_LIMIT

    def _reset_quota(self, conv_key):
        """Reset quota window on user message (mirrors WeCom server behavior)."""
        self.quota[conv_key] = {"count": 0, "window_start": time.time()}

    def _consume_quota(self, conv_key):
        """Consume one reply from the quota."""
        now = time.time()

        if conv_key not in self.quota:
            self.quota[conv_key] = {"count": 0, "window_start": now}

        q = self.quota[conv_key]

        # Reset if window expired
        if now - q["window_start"] > self.QUOTA_WINDOW:
            q["count"] = 0
            q["window_start"] = now

        q["count"] += 1
        remaining = self.QUOTA_LIMIT - q["count"]
        if remaining <= 1:
            print(f"[cs] Quota warning: {conv_key} has {remaining} reply left")
