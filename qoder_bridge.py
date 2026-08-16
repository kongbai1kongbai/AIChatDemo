#!/usr/bin/env python3
"""
Qoder 中继桥接脚本
- 轮询 auto_reply.py 的 Qoder 中继服务器
- 检测到待回复请求后，调用 Ollama 生成高质量回复
- 将回复提交回中继服务器

用法:
  python qoder_bridge.py                         # 默认使用 Ollama
  python qoder_bridge.py --model qwen3:8b        # 指定模型
  python qoder_bridge.py --port 11435            # 指定端口
"""

import requests
import time
import json
import os
import sys
import re
import argparse
from datetime import datetime

# ===================== 配置 =====================

DEFAULT_RELAY_PORT = 11435
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3:8b"
POLL_INTERVAL = 2  # 秒

# No default persona prompt. Configure one explicitly when needed.
ENHANCED_SYSTEM_PROMPT = ""


def log(msg):
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] {msg}", flush=True)


def generate_reply_ollama(ollama_url, model, messages, system_prompt):
    """通过 Ollama 生成回复"""
    api_messages = [{"role": "system", "content": system_prompt}]
    api_messages.extend(messages[-8:])  # 最多传 8 条上下文

    try:
        resp = requests.post(
            f"{ollama_url}/api/chat",
            json={
                "model": model,
                "messages": api_messages,
                "stream": False,
                "options": {
                    "temperature": 0.85,
                    "num_predict": 512,
                }
            },
            timeout=60
        )
        resp.raise_for_status()
        data = resp.json()
        msg = data.get("message", {})
        reply = msg.get("content", "").strip()

        # qwen3 thinking 模式处理
        if not reply:
            thinking = msg.get("thinking", "")
            if thinking:
                parts = thinking.rsplit("\n\n", 1)
                if len(parts) > 1 and len(parts[-1]) < 200:
                    reply = parts[-1].strip()

        # 清理
        for prefix in ["助手：", "AI：", "Assistant: ", "Bot: "]:
            if reply.startswith(prefix):
                reply = reply[len(prefix):].strip()
        reply = re.sub(r'[\ud800-\udfff]', '', reply)

        return reply if reply else None

    except Exception as e:
        log(f"Ollama 调用失败: {e}")
        return None


def check_pending_relay(port):
    """检查中继服务器是否有待处理请求"""
    try:
        resp = requests.get(
            f"http://127.0.0.1:{port}/qoder/pending",
            timeout=3
        )
        if resp.status_code == 200:
            data = resp.json()
            if data and data.get("id"):
                return data
        return None
    except Exception:
        return None


def submit_reply(port, request_id, reply):
    """向中继服务器提交回复"""
    try:
        resp = requests.post(
            f"http://127.0.0.1:{port}/qoder/respond",
            json={"id": request_id, "reply": reply},
            timeout=5
        )
        return resp.status_code == 200
    except Exception as e:
        log(f"提交回复失败: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Qoder 中继桥接脚本")
    parser.add_argument("--port", type=int, default=DEFAULT_RELAY_PORT,
                        help=f"中继服务器端口 (默认: {DEFAULT_RELAY_PORT})")
    parser.add_argument("--model", type=str, default=DEFAULT_MODEL,
                        help=f"Ollama 模型 (默认: {DEFAULT_MODEL})")
    parser.add_argument("--ollama-url", type=str, default=DEFAULT_OLLAMA_URL,
                        help=f"Ollama URL (默认: {DEFAULT_OLLAMA_URL})")
    args = parser.parse_args()

    log(f"Qoder 桥接启动")
    log(f"  中继端口: {args.port}")
    log(f"  模型: {args.model}")
    log(f"  Ollama: {args.ollama_url}")

    # 检查 Ollama 可用性
    try:
        models = requests.get(f"{args.ollama_url}/api/tags", timeout=5).json()
        model_names = [m["name"] for m in models.get("models", [])]
        if args.model not in model_names:
            log(f"警告: 模型 {args.model} 不在已安装列表中")
            log(f"  可用模型: {', '.join(model_names)}")
            if model_names:
                log(f"  自动切换到: {model_names[0]}")
                args.model = model_names[0]
        else:
            log(f"Ollama 就绪: {args.model}")
    except Exception:
        log("错误: 无法连接 Ollama，桥接无法工作")
        sys.exit(1)

    # 验证中继连通性
    try:
        health = requests.get(f"http://127.0.0.1:{args.port}/health", timeout=3)
        log(f"中继服务器状态: {health.json()}")
    except Exception:
        log("警告: 中继服务器暂未就绪，将持续重试")

    processed_ids = set()
    reply_count = 0

    while True:
        try:
            pending = check_pending_relay(args.port)

            if pending and pending.get("id") not in processed_ids:
                request_id = pending["id"]
                user_msg = pending.get("user_message", "")
                messages = pending.get("messages", [])

                processed_ids.add(request_id)

                log(f"收到请求 [{request_id[:8]}]: {user_msg[:60]}")

                # 生成回复
                reply = generate_reply_ollama(
                    args.ollama_url, args.model,
                    messages, ENHANCED_SYSTEM_PROMPT
                )

                if reply:
                    if submit_reply(args.port, request_id, reply):
                        reply_count += 1
                        log(f"已回复 [{request_id[:8]}]: {reply[:60]}")
                    else:
                        log(f"回复提交失败 [{request_id[:8]}]")
                else:
                    submit_reply(args.port, request_id, "我现在有点忙，稍后再聊~")
                    log(f"回复生成失败 [{request_id[:8]}]，已发送兜底消息")

                # 清理过期的已处理 ID
                if len(processed_ids) > 1000:
                    processed_ids.clear()

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            log("桥接已停止")
            break
        except Exception as e:
            log(f"异常: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
