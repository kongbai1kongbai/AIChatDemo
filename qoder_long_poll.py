#!/usr/bin/env python3
"""
Qoder 中继长轮询脚本
- 持续监听中继服务器是否有待处理的微信消息
- 发现消息后立即输出并退出，由调用方（QoderWork cron）生成回复
- 如果没有消息，轮询指定时间后安静退出

用法:
  python qoder_long_poll.py              # 默认监听 48 秒
  python qoder_long_poll.py --timeout 30 # 自定义超时
"""

import requests
import time
import json
import sys
import argparse
import os

DEFAULT_PORT = 11435
POLL_INTERVAL = 3  # 秒


def main():
    parser = argparse.ArgumentParser(description="Qoder 中继长轮询")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--timeout", type=int, default=290,
                        help="最长轮询时间（秒），默认 290")
    args = parser.parse_args()

    base = f"http://127.0.0.1:{args.port}"

    # 1. 检查中继是否运行
    try:
        r = requests.get(f"{base}/health", timeout=3)
        if r.status_code != 200:
            return  # 中继未运行，安静退出
    except Exception:
        return  # 连接失败，安静退出

    # 2. 持续轮询
    elapsed = 0
    WORKSPACE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workspace")
    processed_file = os.path.join(WORKSPACE_DIR,
                                  ".qoder_processed_ids.json")

    # 加载已处理的请求 ID（防止同一 cron 周期内重复处理）
    processed_ids = set()
    if os.path.exists(processed_file):
        try:
            with open(processed_file, "r") as f:
                processed_ids = set(json.load(f))
        except Exception:
            processed_ids = set()

    while elapsed < args.timeout:
        try:
            r = requests.get(f"{base}/qoder/pending", timeout=3)
            if r.status_code == 200:
                data = r.json()
                rid = data.get("id", "")

                if rid and rid not in processed_ids:
                    # 找到新请求！立即输出并退出
                    print(json.dumps(data, ensure_ascii=False), flush=True)

                    # 记录已处理
                    processed_ids.add(rid)
                    # 只保留最近的 ID，防止文件膨胀
                    if len(processed_ids) > 500:
                        processed_ids = set(list(processed_ids)[-200:])
                    with open(processed_file, "w") as f:
                        json.dump(list(processed_ids), f)
                    return

        except Exception:
            pass

        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL


if __name__ == "__main__":
    main()
