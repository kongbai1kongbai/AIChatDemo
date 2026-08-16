"""
WeCom Message Handler
Processes incoming text messages, generates AI replies, sends via WeCom API.
Supports both private chat and group chat.
"""
import time
from collections import defaultdict
from wechatpy.enterprise import WeChatClient

from shared.config import load_config, log, MAX_HISTORY
from shared.ai_engine import generate_reply


class WeComHandler:
    def __init__(self, wecom_config, model_config):
        self.wecom_config = wecom_config
        self.model_config = model_config
        self.corp_id = wecom_config["corp_id"]
        self.corp_secret = wecom_config["corp_secret"]
        self.agent_id = wecom_config["agent_id"]

        # WeChatClient handles access_token caching automatically
        self.client = WeChatClient(self.corp_id, self.corp_secret)

        # Conversation history: per user for private chat, per group for group chat
        self.history = defaultdict(list)

        # Rate limiting: track last reply time per conversation
        self.last_reply = {}
        self.min_interval = 2  # seconds between replies

    def _get_history_key(self, user_id, chat_id):
        """Get unique key for conversation history"""
        if chat_id:
            return f"group:{chat_id}"
        return f"user:{user_id}"

    def handle_text_message(self, user_id, content, agent_id, chat_id=""):
        """Process a text message and send AI reply.
        
        Args:
            user_id: Sender's WeCom user ID
            content: Message text
            agent_id: App agent ID
            chat_id: Group chat ID (empty for private chat)
        """
        if not content or not content.strip():
            return

        is_group = bool(chat_id)
        history_key = self._get_history_key(user_id, chat_id)

        # Rate limiting
        now = time.time()
        if history_key in self.last_reply:
            elapsed = now - self.last_reply[history_key]
            if elapsed < self.min_interval:
                print(f"[handler] Rate limited: {history_key} ({elapsed:.1f}s)")
                return

        # Load config (hot reload)
        config = load_config()

        # Check if group chat is enabled
        if is_group and not config.get("group_enabled", True):
            print(f"[handler] Group chat disabled, ignoring")
            return

        # Add user message to history
        self.history[history_key].append({"role": "user", "content": content})

        # Trim history
        max_msgs = MAX_HISTORY * 2
        if len(self.history[history_key]) > max_msgs:
            self.history[history_key] = self.history[history_key][-max_msgs:]

        # Get system prompt (different for private vs group)
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

        # Generate reply
        reply = generate_reply(
            self.history[history_key],
            system_prompt,
            self.model_config
        )

        if not reply:
            reply = "Sorry, something went wrong. Please try again."

        # Send reply
        try:
            if is_group:
                # Reply to group chat
                # Note: wechatpy enterprise send_text sends to user, not directly to group
                # For group reply, we send to the sender (they see it in the group context)
                self.client.message.send_text(
                    self.agent_id, user_id, reply
                )
                print(f"[handler] Replied to group {chat_id} (user {user_id}): {reply[:60]}")
            else:
                # Reply to private chat
                # wechatpy API: send_text(agent_id, user_ids, content)
                self.client.message.send_text(
                    self.agent_id, user_id, reply
                )
                print(f"[handler] Replied to {user_id}: {reply[:60]}")

            # Add to history
            self.history[history_key].append({"role": "assistant", "content": reply})
            self.last_reply[history_key] = time.time()

        except Exception as e:
            print(f"[handler] Send failed: {e}")
