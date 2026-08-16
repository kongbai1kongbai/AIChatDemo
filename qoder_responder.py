#!/usr/bin/env python3
"""
Qoder 中继响应脚本
由 QoderWork 定时任务调用，检查待处理的微信消息并生成回复。
也可以手动运行: python qoder_responder.py
"""

import requests
import json
import time
import sys
import os

RELAY_PORT = 11435
RELAY_BASE = f"http://127.0.0.1:{RELAY_PORT}"

# 已处理的请求 ID（避免重复处理）
WORKSPACE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workspace")
PROCESSED_FILE = os.path.join(WORKSPACE_DIR, ".qoder_processed_ids.json")


def load_processed_ids():
    """加载已处理的请求 ID"""
    if os.path.exists(PROCESSED_FILE):
        try:
            with open(PROCESSED_FILE, "r") as f:
                ids = json.load(f)
                # 只保留最近的 500 个 ID
                return set(ids[-500:]) if len(ids) > 500 else set(ids)
        except Exception:
            pass
    return set()


def save_processed_ids(ids):
    """保存已处理的请求 ID"""
    try:
        with open(PROCESSED_FILE, "w") as f:
            json.dump(list(ids), f)
    except Exception:
        pass


def check_pending():
    """检查是否有待处理的请求"""
    try:
        resp = requests.get(f"{RELAY_BASE}/qoder/pending", timeout=3)
        if resp.status_code == 200:
            data = resp.json()
            if data and data.get("id"):
                return data
    except Exception:
        pass
    return None


def submit_reply(request_id, reply):
    """提交回复到中继服务器"""
    try:
        resp = requests.post(
            f"{RELAY_BASE}/qoder/respond",
            json={"id": request_id, "reply": reply},
            timeout=5
        )
        return resp.status_code == 200
    except Exception:
        return False


def generate_reply_simple(messages):
    """
    使用简单的规则生成回复（作为 QoderWork 不可用时的兜底）
    实际使用中应该由 QoderWork 的 AI 能力替代
    """
    user_msg = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_msg = m.get("content", "")
            break

    if not user_msg:
        return "嗯，收到~"

    # 简单的关键词回复
    keywords = {
        "你好": "你好呀！最近怎么样？",
        "hello": "Hi! How's it going?",
        "在吗": "在的，什么事？",
        "谢谢": "不客气~",
        "好的": "嗯嗯，好的~",
        "哈哈": "哈哈，确实~",
        "吃了吗": "吃了，你呢？",
    }

    for keyword, reply in keywords.items():
        if keyword in user_msg.lower():
            return reply

    return f"收到你的消息了，稍后详细回你~"


def process_pending_request(processed_ids):
    """处理一个待回复的请求"""
    pending = check_pending()
    if not pending:
        return False

    request_id = pending.get("id", "")
    if request_id in processed_ids:
        return False

    user_msg = pending.get("user_message", "")
    messages = pending.get("messages", [])

    print(f"[Qoder Responder] 处理请求: {user_msg[:50]}...", flush=True)

    # 生成回复（这里使用简单规则，QoderWork 定时任务会用 AI 生成更好的回复）
    reply = generate_reply_simple(messages)

    if submit_reply(request_id, reply):
        processed_ids.add(request_id)
        save_processed_ids(processed_ids)
        print(f"[Qoder Responder] 已回复: {reply[:50]}...", flush=True)
        return True
    else:
        print(f"[Qoder Responder] 回复提交失败", flush=True)
        return False


def main():
    """主函数：检查并处理所有待回复请求"""
    print(f"[Qoder Responder] 开始检查中继 (端口 {RELAY_PORT})...", flush=True)

    # 先检查中继服务器是否运行
    try:
        resp = requests.get(f"{RELAY_BASE}/health", timeout=3)
        print(f"[Qoder Responder] 中继服务器状态: {resp.json()}", flush=True)
    except Exception:
        print("[Qoder Responder] 中继服务器未运行", flush=True)
        return

    processed_ids = load_processed_ids()

    # 处理所有待回复请求
    count = 0
    for _ in range(10):  # 最多处理 10 个请求
        if process_pending_request(processed_ids):
            count += 1
        else:
            break

    print(f"[Qoder Responder] 本轮处理了 {count} 条请求", flush=True)


if __name__ == "__main__":
    main()
