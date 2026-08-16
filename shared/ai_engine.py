"""
shared/ai_engine.py - AI reply generation (remote API + Ollama + qodercli/codexcli + smart search)
"""
import os
import re
import time
import uuid
import base64
import platform
import subprocess
import threading
import requests
from datetime import datetime

from shared.config import MAX_HISTORY
from shared.search_engine import (
    needs_web_search, response_is_evasive, extract_search_query,
    extract_search_marker, is_weather_query, weather_search,
    weather_web_search, extract_location,
    web_search, inject_dates
)

# Ollama defaults
OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MAX_TOKENS = 8192  # default output token limit for all backends
DEFAULT_CODEXCLI_MODEL = "gpt-5.6-sol"
DEFAULT_CODEXCLI_REASONING_EFFORT = "none"


def _build_system_prompt(system_prompt):
    """Append search instructions and no-tool-call directive"""
    system_prompt += (
        "\n\n重要规则："
        "\n1. 你必须直接用自然语言回复，不要输出任何JSON格式、"
        "\n工具调用（如call/get_weather/web_search等）、函数调用或代码块。"
        "\n2. 如果你需要搜索互联网来回答问题，请在回复开头输出 [SEARCH: 搜索关键词]，"
        "\n系统会自动帮你搜索。例如：[SEARCH: 2024年奥运会奖牌榜]"
        "\n3. 如果你无法回答某个问题，直接用口语化的方式说明即可。"
    )
    return system_prompt


def _do_search_and_retry(user_msg, messages_history, system_prompt, model_config, max_rounds=2):
    """Perform web search and retry with results. Returns improved reply or None."""
    api_base = model_config.get("api_base", "").rstrip("/")
    model_name = model_config.get("model", "")
    api_key = model_config.get("api_key", "")
    bocha_api_key = model_config.get("bocha_api_key", "")
    provider = model_config.get("remote_provider", "Remote API")

    search_query = extract_search_query(user_msg)
    search_query = inject_dates(search_query)

    is_weather = is_weather_query(user_msg)
    best_reply = None  # Track best reply even if slightly evasive

    for round_num in range(1, max_rounds + 1):
        print(f"[search] Round {round_num}: searching '{search_query}'", flush=True)

        # Weather: web search first (Bocha/Bing reliable in China), wttr.in as supplement
        if round_num == 1 and is_weather:
            location = extract_location(search_query)
            search_results = weather_web_search(location, bocha_api_key=bocha_api_key)
            if not search_results:
                search_results = weather_search(search_query)
        else:
            search_results = web_search(search_query, bocha_api_key=bocha_api_key)

        if not search_results:
            print(f"[search] Round {round_num}: no results", flush=True)
            if round_num < max_rounds:
                search_query = user_msg.strip()
            continue

        print(f"[search] Round {round_num}: got {len(search_results)} chars", flush=True)

        # Build forceful retry prompt
        now_str = datetime.now().strftime("%Y年%m月%d日 %A")
        retry_system = (
            f"你是一个智能助手。以下是系统通过实时网络搜索获取到的最新信息。\n\n"
            f"当前时间: {now_str}\n\n"
            f"===== 搜索结果 =====\n{search_results}\n===== 搜索结果结束 =====\n\n"
            f"重要要求：\n"
            f"1. 你必须基于以上搜索结果来回答用户的问题。\n"
            f"2. 绝对不要说你无法搜索、没有联网、查不到或获取不到——数据已经在上面了。\n"
            f"3. 如果搜索结果中包含天气、新闻等实时数据，直接告诉用户具体内容。\n"
            f"4. 用自然口语化的方式回复，简洁明了。\n"
        )

        retry_messages = [{"role": "system", "content": retry_system}]
        retry_messages.extend(messages_history[-MAX_HISTORY * 2:])

        try:
            resp = requests.post(
                f"{api_base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": model_name,
                    "messages": retry_messages,
                    "temperature": 0.7,
                    "max_tokens": model_config.get("remote_max_tokens", DEFAULT_MAX_TOKENS),
                },
                timeout=60
            )
            resp.raise_for_status()
            data = resp.json()
            reply = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            reply = re.sub(r'[\ud800-\udfff]', '', reply)
            reply = re.sub(r'\[SEARCH:.*?\]\s*', '', reply).strip()

            if reply:
                # Always track the best reply we get
                if not best_reply or len(reply) > len(best_reply):
                    best_reply = reply

                if not response_is_evasive(reply):
                    print(f"[search] Round {round_num} success: {reply[:80]}", flush=True)
                    return reply

            print(f"[search] Round {round_num} still evasive", flush=True)

            # For round 2, try using the raw user message as search query
            if round_num < max_rounds:
                search_query = user_msg.strip()

        except Exception as e:
            print(f"[search] Round {round_num} API failed: {e}", flush=True)

    return best_reply


def generate_reply_remote(messages_history, system_prompt, model_config,
                          image_base64=None, image_mime="image/jpeg"):
    """Generate reply via remote OpenAI-compatible API with smart search.

    image_base64: optional base64-encoded image data string for the current message.
    image_mime: MIME type of the image (default image/jpeg).
    """
    api_base = model_config.get("api_base", "").rstrip("/")
    model_name = model_config.get("model", "")
    api_key = model_config.get("api_key", "")
    provider = model_config.get("remote_provider", "Remote API")

    override = model_config.get("system_prompt_override", "")
    if override:
        system_prompt = override

    # Add search instructions
    system_prompt = _build_system_prompt(system_prompt)

    # Build API messages — support both plain text and multimodal list content
    api_messages = [{"role": "system", "content": system_prompt}]

    history_slice = messages_history[-MAX_HISTORY * 2:]

    # If we have a current image, inject it into the last user message
    if image_base64:
        for i in range(len(history_slice) - 1, -1, -1):
            msg = history_slice[i]
            if msg.get("role") == "user":
                # Convert this message's content to multimodal format
                text = msg.get("content", "")
                if isinstance(text, list):
                    text_parts = [p.get("text", "") for p in text if p.get("type") == "text"]
                    text = " ".join(text_parts) or "请描述这张图片"
                if not text or not text.strip():
                    text = "请描述这张图片"
                # Build multimodal content
                data_url = f"data:{image_mime};base64,{image_base64}"
                multimodal_content = [
                    {"type": "text", "text": text},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ]
                # Replace this message in the slice
                history_slice = list(history_slice)
                history_slice[i] = {"role": "user", "content": multimodal_content}
                print(f"[{provider}] Image attached to user message ({len(image_base64)} bytes base64)")
                break

    # Pass through — content can be string or list (multimodal)
    for msg in history_slice:
        content = msg.get("content", "")
        if isinstance(content, list):
            # Already multimodal format — pass as-is
            api_messages.append({"role": msg["role"], "content": content})
        else:
            api_messages.append({"role": msg["role"], "content": content})

    try:
        print(f"[{provider}/{model_name}] Generating ({len(api_messages)} msgs)...", flush=True)
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
                "max_tokens": model_config.get("remote_max_tokens", DEFAULT_MAX_TOKENS),
            },
            timeout=60
        )
        resp.raise_for_status()
        data = resp.json()
        choice = data.get("choices", [{}])[0]
        reply = choice.get("message", {}).get("content", "").strip()

        # Clean prefixes
        for prefix in ["助手：", "AI：", "Assistant: ", "Bot: "]:
            if reply.startswith(prefix):
                reply = reply[len(prefix):].strip()

        reply = re.sub(r'[\ud800-\udfff]', '', reply)

        # Tool call detection (strip tool call output)
        if re.match(r'^(call|function|tool)\s*\n?\s*\{', reply, re.IGNORECASE):
            print("[search] Tool call output detected", flush=True)
            reply = ""
        elif re.match(r'^\s*\{.*"name"\s*:.*"arguments"', reply, re.DOTALL):
            print("[search] JSON tool call detected", flush=True)
            reply = ""

        # Get user message
        user_msg = ""
        for m in reversed(messages_history):
            if m.get("role") == "user":
                user_msg = m.get("content", "")
                break

        print(f"[search] User: {user_msg[:60]}", flush=True)
        print(f"[search] Reply: {reply[:80]}", flush=True)

        # ---- Smart search trigger logic ----
        search_done = False

        # Path 1: Model explicitly requests search via [SEARCH: ...] marker
        model_search_query = extract_search_marker(reply)
        if model_search_query:
            print("[search] >>> Model requested search", flush=True)
            reply_clean = re.sub(r'\[SEARCH:.*?\]\s*', '', reply).strip()
            search_results = web_search(model_search_query, bocha_api_key=model_config.get("bocha_api_key", ""))
            if search_results:
                now_str = datetime.now().strftime("%Y年%m月%d日 %A")
                retry_system = (
                    f"你是一个智能助手。以下是系统通过实时网络搜索获取到的最新信息。\n\n"
                    f"当前时间: {now_str}\n\n"
                    f"===== 搜索结果 =====\n{search_results}\n===== 搜索结果结束 =====\n\n"
                    f"重要要求：\n"
                    f"1. 你必须基于以上搜索结果来回答用户的问题。\n"
                    f"2. 绝对不要说你无法搜索、没有联网、查不到或获取不到——数据已经在上面了。\n"
                    f"3. 用自然口语化的方式回复，简洁明了。\n"
                )
                retry_msgs = [{"role": "system", "content": retry_system}]
                retry_msgs.extend(messages_history[-MAX_HISTORY * 2:])
                try:
                    resp2 = requests.post(
                        f"{api_base}/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json={"model": model_name, "messages": retry_msgs, "temperature": 0.7, "max_tokens": model_config.get("remote_max_tokens", DEFAULT_MAX_TOKENS)},
                        timeout=60
                    )
                    resp2.raise_for_status()
                    reply2 = resp2.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                    reply2 = re.sub(r'[\ud800-\udfff]', '', reply2)
                    reply2 = re.sub(r'\[SEARCH:.*?\]\s*', '', reply2).strip()
                    if reply2:
                        reply = reply2
                        print("[search] Model-search success", flush=True)
                    else:
                        reply = reply_clean if reply_clean else reply
                except Exception as e:
                    print(f"[search] Model-search retry failed: {e}", flush=True)
                    reply = reply_clean if reply_clean else reply
            else:
                reply = reply_clean if reply_clean else reply
            search_done = True

        # Path 2: Response is evasive -> auto search (no keyword required)
        if not search_done and response_is_evasive(reply):
            print("[search] >>> Evasive response, triggering auto search", flush=True)
            improved = _do_search_and_retry(
                user_msg, messages_history, system_prompt, model_config, max_rounds=2
            )
            if improved:
                reply = improved
            search_done = True

        # Path 3: Keyword match + response not great -> also try search
        if not search_done and needs_web_search(user_msg) and (not reply or len(reply) < 50):
            print("[search] >>> Keyword match + short reply, triggering search", flush=True)
            improved = _do_search_and_retry(
                user_msg, messages_history, system_prompt, model_config, max_rounds=1
            )
            if improved:
                reply = improved

        result = reply if reply else "抱歉，我没想好怎么回复。"

        # Final safety net
        if re.match(r'^(call|function|tool)\s*\n?\s*\{', result, re.IGNORECASE) or \
           re.match(r'^\s*\{.*"name"\s*:', result, re.DOTALL):
            result = "这个问题我帮你查了一下，不过暂时没找到准确的信息。"

        print(f"[{provider}] Final: {result[:80]}", flush=True)
        return result

    except requests.exceptions.ConnectionError:
        print(f"[error] Cannot connect to {api_base}", flush=True)
        return None
    except requests.exceptions.HTTPError as e:
        print(f"[error] API error: {e.response.status_code}", flush=True)
        return None
    except Exception as e:
        print(f"[error] Remote API failed: {e}", flush=True)
        return None


def generate_reply_ollama(messages_history, system_prompt, model_name="qwen3:8b",
                          model_config=None):
    """Generate reply via local Ollama"""
    api_messages = [{"role": "system", "content": system_prompt}]
    api_messages.extend(messages_history[-MAX_HISTORY * 2:])

    try:
        print(f"[Ollama/{model_name}] Generating...", flush=True)
        resp = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": model_name,
                "messages": api_messages,
                "stream": False,
                "options": {"temperature": 0.8, "num_predict": (model_config or {}).get("ollama_max_tokens", DEFAULT_MAX_TOKENS)}
            },
            timeout=120
        )
        resp.raise_for_status()
        data = resp.json()
        reply = data.get("message", {}).get("content", "").strip()

        # Extract from qwen3 thinking mode
        if "" in reply and "" in reply:
            reply = reply.split("")[-1].strip()

        for prefix in ["助手：", "AI：", "Assistant: ", "Bot: "]:
            if reply.startswith(prefix):
                reply = reply[len(prefix):].strip()

        return reply if reply else "抱歉，我没想好怎么回复。"

    except Exception as e:
        print(f"[error] Ollama failed: {e}", flush=True)
        return None


def _run_qodercli_streaming(cmd, clean_env, timeout=300):
    """Run qodercli with real-time stdout streaming to console.

    Prints each line from qodercli as it arrives (thinking process visible),
    while also capturing the full output for return.
    Returns (stdout_string, returncode, stderr_string).
    """
    # Hint the child process to reduce buffering
    stream_env = dict(clean_env)
    stream_env["FORCE_COLOR"] = "0"
    stream_env["NO_COLOR"] = "1"
    stream_env["PYTHONUNBUFFERED"] = "1"

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # merge stderr into stdout
            env=stream_env,
        )

        lines_out = []
        last_activity = [time.time()]  # mutable for closure

        def _read_output():
            try:
                for raw_line in proc.stdout:
                    last_activity[0] = time.time()
                    lines_out.append(raw_line)
                    text = raw_line.decode("utf-8", errors="replace").rstrip()
                    if text:
                        print(f"[qodercli] {text}", flush=True)
            except Exception:
                pass

        reader = threading.Thread(target=_read_output, daemon=True)
        reader.start()

        # Heartbeat: log every 30s if process is still running
        heartbeat_interval = 30
        next_heartbeat = time.time() + heartbeat_interval
        while True:
            remaining = timeout - (time.time() - last_activity[0])
            if remaining <= 0:
                # Check if process actually finished
                ret = proc.poll()
                if ret is not None:
                    break
                # Still running, kill on timeout
                proc.kill()
                reader.join(timeout=5)
                elapsed = int(time.time() - last_activity[0])
                print(f"[qodercli] Timed out after {timeout}s "
                      f"(last output {elapsed}s ago)", flush=True)
                return None, -1, ""

            wait_time = min(remaining, next_heartbeat - time.time())
            if wait_time > 0:
                try:
                    ret = proc.wait(timeout=wait_time)
                    break  # process finished
                except subprocess.TimeoutExpired:
                    pass  # still running, check heartbeat

            if time.time() >= next_heartbeat:
                alive_secs = int(time.time() - last_activity[0])
                total_secs = int(time.time() - (last_activity[0] - alive_secs))
                print(f"[qodercli] Still running... "
                      f"({alive_secs}s since last output, "
                      f"{len(lines_out)} lines so far)", flush=True)
                next_heartbeat += heartbeat_interval

        reader.join(timeout=5)
        combined = b"".join(lines_out).decode("utf-8", errors="replace")

        return combined, proc.returncode, ""

    except FileNotFoundError:
        print(f"[qodercli] Binary not found: {cmd[0]}", flush=True)
        return None, -1, ""
    except Exception as e:
        print(f"[qodercli] Error: {e}", flush=True)
        return None, -1, ""


def _find_qodercli():
    """Auto-detect qoderclicn binary path."""
    # Check explicit config first
    # Then try common install locations
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, ".qoder-cn", "bin", "qoderclicn", "qoderclicn.exe"),  # Windows
        os.path.join(home, ".qoder-cn", "bin", "qoderclicn"),  # Unix
        "qoderclicn",  # PATH lookup
        "qodercli",    # fallback PATH lookup
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    # Fall back to PATH resolution
    return "qoderclicn"


def _find_codexcli():
    """Auto-detect Codex CLI path."""
    candidates = [
        r"D:\AI\workspace\codex-cli.cmd",
        r"D:\AI\codex-cli.cmd",
        r"D:\AI\npm-global\codex.cmd",
        "codex",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return "codex"


def _format_cli_conversation(messages_history):
    """Format chat history into a single prompt for stateless CLIs."""
    parts = []
    for msg in messages_history[:-1]:
        role_label = "用户" if msg.get("role") == "user" else "助手"
        content = msg.get("content", "")
        if isinstance(content, list):
            text_parts = [p.get("text", "") for p in content if p.get("type") == "text"]
            content = " ".join(text_parts) or "[附件]"
        parts.append(f"{role_label}: {content}")

    current_msg = messages_history[-1].get("content", "") if messages_history else ""
    if isinstance(current_msg, list):
        text_parts = [p.get("text", "") for p in current_msg if p.get("type") == "text"]
        current_msg = " ".join(text_parts) or "请处理这个附件"
    parts.append(f"用户: {current_msg}")

    return "\n\n".join(parts) if len(parts) > 1 else current_msg


def _path_for_cli_prompt(path, workspace_dir):
    """Use workspace-relative paths in prompts when possible."""
    if not path:
        return path
    try:
        abs_path = os.path.abspath(path)
        abs_workspace = os.path.abspath(workspace_dir)
        rel_path = os.path.relpath(abs_path, abs_workspace)
        if not rel_path.startswith("..") and not os.path.isabs(rel_path):
            return rel_path
    except Exception:
        pass
    return path


def _looks_like_image_generation_request(text):
    if not text:
        return False
    lowered = text.lower()
    if any(word in lowered for word in ("generate image", "create image", "draw image", "make image")):
        return True
    verbs = ("生成", "画", "绘制", "做", "制作", "生图", "出图")
    nouns = ("图", "图片", "海报", "插画", "照片", "头像", "壁纸")
    return any(verb in text for verb in verbs) and any(noun in text for noun in nouns)


def _qodercli_config_dir(cli_path):
    """Derive the .qoder-cn config directory from the binary path.

    qodercli is typically at ~/.qoder-cn/bin/qoderclicn/qoderclicn.exe
    so the config dir is two levels up: ~/.qoder-cn
    This is needed for non-interactive/service environments where
    HOME/USERPROFILE may not resolve correctly.
    """
    if not cli_path or not os.path.isfile(cli_path):
        return None
    # Walk up: qoderclicn.exe -> qoderclicn -> bin -> .qoder-cn
    config_dir = os.path.dirname(os.path.dirname(os.path.dirname(cli_path)))
    if os.path.isdir(config_dir) and os.path.basename(config_dir) == ".qoder-cn":
        return config_dir
    return None


def generate_reply_qodercli(messages_history, system_prompt, model_config,
                            image_path=None):
    """Generate reply by invoking qoderclicn in non-interactive print mode.

    qodercli has built-in search/tool use, so we do NOT call the Bocha search engine.
    Conversation history is formatted into the prompt since each invocation is stateless.

    image_path: optional path to an image file to attach via --attachment flag.
    """
    # Resolve binary path
    cli_path = model_config.get("qodercli_path") or _find_qodercli()

    # Build a single prompt that includes conversation history
    parts = []
    for msg in messages_history[:-1]:  # all but the last (current) message
        role_label = "用户" if msg["role"] == "user" else "助手"
        # For multimodal history, extract text content
        content = msg.get("content", "")
        if isinstance(content, list):
            text_parts = [p.get("text", "") for p in content if p.get("type") == "text"]
            content = " ".join(text_parts) or "[图片]"
        parts.append(f"{role_label}: {content}")

    # The last message is the current user question
    current_msg = messages_history[-1].get("content", "") if messages_history else ""
    if isinstance(current_msg, list):
        text_parts = [p.get("text", "") for p in current_msg if p.get("type") == "text"]
        current_msg = " ".join(text_parts) or "请描述这张图片"
    parts.append(f"用户: {current_msg}")

    full_prompt = "\n\n".join(parts) if len(parts) > 1 else current_msg

    # Build command
    cmd = [cli_path, "-p"]

    if system_prompt:
        cmd += ["--system-prompt", system_prompt]

    max_tokens = model_config.get("qodercli_max_tokens", 4096)
    cmd += ["--max-output-tokens", str(max_tokens)]

    model = model_config.get("qodercli_model", "Qwen3.7-Max")
    cmd += ["-m", model]

    # Auto-approve all permission/tool prompts
    cmd += ["--permission-mode", "bypass_permissions"]

    # Add workspace output directory so Write tool can save files there
    workspace_dir = model_config.get("workspace_dir", r"D:\AI\workspace")
    if os.path.isdir(workspace_dir):
        cmd += ["--add-dir", workspace_dir]

    # Attach file if provided (image, document, spreadsheet, etc.)
    if image_path and os.path.isfile(image_path):
        cmd += ["--attachment", image_path]
        att_ext = os.path.splitext(image_path)[1].lower()
        print(f"[qodercli] Attaching file: {image_path} ({att_ext})")

        _IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg"}
        if att_ext in _IMAGE_EXTS:
            full_prompt += (
                "\n\n[注意: 你已收到一张图片附件。你可以直接查看这张图片的内容。"
                "如果用户要求对图片进行处理（如美化、编辑、分析等），"
                "请使用Python代码或其他工具来完成，不要拒绝。"
                "如果生成了新的图片文件，请在回复中只输出相对路径，不要输出盘符或绝对路径。]"
            )
        else:
            full_prompt += (
                "\n\n[注意: 你已收到一个文件附件。"
                "请使用Python代码读取这个文件并进行处理。"
                "如果生成了新的文件，请在回复中只输出相对路径，不要输出盘符或绝对路径。]"
            )

    cmd.append(full_prompt)

    # Clear SDK env vars that would force qodercli into SDK mode
    clean_env = {k: v for k, v in os.environ.items()
                 if k not in ("QODER_AGENT_SDK_ENTRYPOINT",
                              "QODER_SDK_AUTH_PAYLOAD_FILE",
                              "QODER_WORK_INTEGRATION_MODE",
                              "QODER_SCENE")}

    try:
        # Use longer timeout when processing file attachments
        cli_timeout = 600 if image_path else 300
        print(f"[qodercli] Invoking: {cli_path} -p (prompt length: {len(full_prompt)} chars, "
              f"timeout: {cli_timeout}s)")
        stdout, returncode, stderr = _run_qodercli_streaming(cmd, clean_env, timeout=cli_timeout)

        if stdout is None:
            return None

        if returncode != 0:
            print(f"[qodercli] Process exited with code {returncode}: {stderr[:500]}", flush=True)
            return None

        reply = stdout.strip()

        # Strip ANSI escape codes
        reply = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', reply)

        if not reply:
            print("[qodercli] Empty response from qodercli", flush=True)
            return None

        print(f"[qodercli] Final reply ({len(reply)} chars): {reply[:80]}...")
        return reply

    except Exception as e:
        print(f"[qodercli] Error: {e}", flush=True)
        return None


def _run_codexcli_streaming(cmd, clean_env, timeout=600, stdin_text=None,
                            stream_output=True):
    """Run Codex CLI, stream logs, and return stdout/returncode."""
    stream_env = dict(clean_env)
    stream_env["FORCE_COLOR"] = "0"
    stream_env["NO_COLOR"] = "1"
    stream_env["PYTHONUNBUFFERED"] = "1"

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=stream_env,
        )

        if stdin_text is not None:
            try:
                proc.stdin.write(stdin_text.encode("utf-8"))
                proc.stdin.close()
            except Exception:
                pass

        lines_out = []
        last_activity = [time.time()]

        def _read_output():
            try:
                for raw_line in proc.stdout:
                    last_activity[0] = time.time()
                    lines_out.append(raw_line)
                    text = raw_line.decode("utf-8", errors="replace").rstrip()
                    if text and stream_output:
                        print(f"[codexcli] {text}", flush=True)
            except Exception:
                pass

        reader = threading.Thread(target=_read_output, daemon=True)
        reader.start()

        heartbeat_interval = 30
        next_heartbeat = time.time() + heartbeat_interval
        while True:
            remaining = timeout - (time.time() - last_activity[0])
            if remaining <= 0:
                ret = proc.poll()
                if ret is not None:
                    break
                proc.kill()
                reader.join(timeout=5)
                elapsed = int(time.time() - last_activity[0])
                print(f"[codexcli] Timed out after {timeout}s "
                      f"(last output {elapsed}s ago)", flush=True)
                return None, -1

            wait_time = min(remaining, next_heartbeat - time.time())
            if wait_time > 0:
                try:
                    proc.wait(timeout=wait_time)
                    break
                except subprocess.TimeoutExpired:
                    pass

            if time.time() >= next_heartbeat:
                alive_secs = int(time.time() - last_activity[0])
                print(f"[codexcli] Still running... "
                      f"({alive_secs}s since last output, "
                      f"{len(lines_out)} lines so far)", flush=True)
                next_heartbeat += heartbeat_interval

        reader.join(timeout=5)
        combined = b"".join(lines_out).decode("utf-8", errors="replace")
        return combined, proc.returncode

    except FileNotFoundError:
        print(f"[codexcli] Binary not found: {cmd[0]}", flush=True)
        return None, -1
    except Exception as e:
        print(f"[codexcli] Error: {e}", flush=True)
        return None, -1


def _normalize_proxy_url(value, default_scheme="http"):
    """Normalize host:port proxy values to URLs."""
    if not value or not str(value).strip():
        return ""
    value = str(value).strip()
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", value):
        return value
    return f"{default_scheme}://{value}"


def _get_windows_proxy_from_registry():
    """Best-effort read of current Windows user proxy settings."""
    if platform.system().lower() != "windows":
        return {}
    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            enabled = winreg.QueryValueEx(key, "ProxyEnable")[0]
            if not enabled:
                return {}
            raw = winreg.QueryValueEx(key, "ProxyServer")[0]
    except Exception:
        return {}

    if not raw:
        return {}

    if "=" not in raw:
        proxy_url = _normalize_proxy_url(raw)
        return {"http": proxy_url, "https": proxy_url, "all": proxy_url}

    result = {"http": "", "https": "", "all": ""}
    for part in raw.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip().lower()
        value = value.strip()
        if key == "http":
            result["http"] = _normalize_proxy_url(value, "http")
        elif key == "https":
            result["https"] = _normalize_proxy_url(value, "http")
        elif key == "socks":
            result["all"] = _normalize_proxy_url(value, "socks5")

    result["https"] = result["https"] or result["http"]
    result["http"] = result["http"] or result["https"]
    result["all"] = result["all"] or result["http"]
    return {k: v for k, v in result.items() if v}


def _build_codexcli_env(model_config):
    """Build Codex CLI env with optional proxy overrides from model_config."""
    env = dict(os.environ)

    proxy = (
        model_config.get("codexcli_proxy")
        or model_config.get("terminal_proxy")
        or model_config.get("proxy")
    )
    http_proxy = (
        model_config.get("codexcli_http_proxy")
        or model_config.get("http_proxy")
        or model_config.get("HTTP_PROXY")
    )
    https_proxy = (
        model_config.get("codexcli_https_proxy")
        or model_config.get("https_proxy")
        or model_config.get("HTTPS_PROXY")
    )
    all_proxy = (
        model_config.get("codexcli_all_proxy")
        or model_config.get("all_proxy")
        or model_config.get("ALL_PROXY")
    )

    if proxy:
        proxy_url = _normalize_proxy_url(proxy)
        http_proxy = http_proxy or proxy_url
        https_proxy = https_proxy or proxy_url
        all_proxy = all_proxy or proxy_url

    use_windows_proxy = model_config.get("codexcli_use_windows_proxy", True)
    if use_windows_proxy and not (http_proxy or https_proxy or all_proxy):
        registry_proxy = _get_windows_proxy_from_registry()
        http_proxy = registry_proxy.get("http", "")
        https_proxy = registry_proxy.get("https", "")
        all_proxy = registry_proxy.get("all", "")

    no_proxy = (
        model_config.get("codexcli_no_proxy")
        or model_config.get("no_proxy")
        or model_config.get("NO_PROXY")
        or "localhost,127.0.0.1,::1"
    )

    proxy_values = {
        "HTTP_PROXY": _normalize_proxy_url(http_proxy) if http_proxy else "",
        "HTTPS_PROXY": _normalize_proxy_url(https_proxy) if https_proxy else "",
        "ALL_PROXY": _normalize_proxy_url(all_proxy) if all_proxy else "",
    }

    for key, value in proxy_values.items():
        if value:
            env[key] = value
            env[key.lower()] = value

    if no_proxy:
        env["NO_PROXY"] = str(no_proxy)
        env["no_proxy"] = str(no_proxy)

    return env


def _build_codexcli_cmd(cli_path, prompt, model_config, output_file,
                        image_path=None, plan_only=False):
    """Build Codex CLI exec command."""
    workspace_dir = model_config.get("workspace_dir", r"D:\AI\workspace")
    if not os.path.isdir(workspace_dir):
        workspace_dir = os.getcwd()

    cmd = [cli_path]

    permission_mode = model_config.get("codexcli_permission_mode", "bypass")
    dangerous_bypass = model_config.get("codexcli_dangerously_bypass", True)
    if permission_mode == "bypass" and not plan_only:
        if dangerous_bypass:
            cmd += ["--dangerously-bypass-approvals-and-sandbox"]
        else:
            approval = model_config.get("codexcli_approval_policy", "never")
            cmd += ["-a", approval]

    cmd += ["exec"]

    model = model_config.get("codexcli_model") or DEFAULT_CODEXCLI_MODEL
    if model:
        cmd += ["-m", model]

    reasoning_effort = (
        model_config.get("codexcli_reasoning_effort")
        or DEFAULT_CODEXCLI_REASONING_EFFORT
    )
    if reasoning_effort:
        cmd += ["-c", f'model_reasoning_effort="{reasoning_effort}"']

    sandbox = "read-only" if plan_only else model_config.get("codexcli_sandbox", "workspace-write")
    cmd += [
        "--skip-git-repo-check",
        "--color", "never",
        "-C", workspace_dir,
        "--add-dir", workspace_dir,
        "--output-last-message", output_file,
    ]
    if plan_only or not dangerous_bypass:
        cmd += ["--sandbox", sandbox]

    if image_path and os.path.isfile(image_path):
        absolute_image_path = os.path.abspath(image_path)
        attachment_dir = os.path.dirname(absolute_image_path)
        if attachment_dir and os.path.isdir(attachment_dir):
            cmd += ["--add-dir", attachment_dir]
        ext = os.path.splitext(image_path)[1].lower()
        if ext in {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}:
            # Codex resolves --image before applying -C, so a workspace-relative
            # value can be looked up from the service process directory instead.
            # Keep prompts relative, but make the CLI attachment unambiguous.
            cmd += ["--image", absolute_image_path]

    extra_args = model_config.get("codexcli_extra_args", [])
    if isinstance(extra_args, list):
        cmd += [str(x) for x in extra_args]

    cmd.append("-")
    return cmd


def _read_codexcli_output(output_file, stdout):
    """Prefer Codex CLI's final-message file, fall back to stdout cleanup."""
    reply = ""
    try:
        if output_file and os.path.exists(output_file):
            with open(output_file, "r", encoding="utf-8") as f:
                reply = f.read().strip()
    except Exception as e:
        print(f"[codexcli] Failed to read output file: {e}", flush=True)

    if not reply and stdout:
        reply = stdout.strip()
        reply = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', reply)
        # Best-effort cleanup when --output-last-message is unavailable/fails.
        markers = ["codex\n", "assistant\n"]
        for marker in markers:
            if marker in reply:
                reply = reply.rsplit(marker, 1)[-1].strip()
        reply = re.sub(r'\ntokens used[\s\S]*$', '', reply).strip()

    return reply if reply else None


def _build_codexcli_prompt(messages_history, system_prompt, image_path=None,
                           workspace_dir=None,
                           plan_only=False, execute_plan=None):
    conversation = _format_cli_conversation(messages_history)
    workspace_dir = workspace_dir or os.getcwd()
    prompt_parts = []
    if system_prompt:
        prompt_parts.append(f"系统指令：\n{system_prompt}")
    prompt_parts.append(f"对话记录：\n{conversation}")
    prompt = "\n\n".join(prompt_parts)

    if image_path and os.path.isfile(image_path):
        ext = os.path.splitext(image_path)[1].lower()
        prompt_path = _path_for_cli_prompt(image_path, workspace_dir)
        if ext in {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}:
            prompt += (
                f"\n\n附件图片路径：{prompt_path}\n"
                "请结合图片内容和用户要求回答。如果生成了新的图片文件，"
                "请保存到当前工作目录或其子目录，并在回复中只包含相对路径，不要输出盘符或绝对路径。"
            )
        else:
            prompt += (
                f"\n\n附件文件路径：{prompt_path}\n"
                "请读取该文件并按用户要求处理。如果需要生成新文件，"
                "请保存到当前工作目录或其子目录，并在回复中只包含相对路径，不要输出盘符或绝对路径。"
            )

    if _looks_like_image_generation_request(conversation):
        prompt += (
            "\n\n重要：如果你生成了图片，必须将最终图片文件保存到当前工作目录或其子目录，"
            "文件扩展名使用 .png、.jpg 或 .webp。最终回复中必须包含生成图片的相对路径，"
            "例如 `generated_image.png` 或 `outputs/generated_image.png`；不要只说已经生成，"
            "不要输出盘符或绝对路径。如果使用 imagegen 技能，生成后必须从 "
            "`$CODEX_HOME/generated_images/...` 复制最终图片到当前工作目录或其子目录。"
        )

    if plan_only:
        prompt += (
            "\n\n重要：如果需要执行文件读写、命令运行、网络访问或其他有权限风险的操作，"
            "请不要执行，只列出你准备执行的计划和需要的权限。"
            "如果不需要权限操作，可以直接给出最终回复。"
        )

    if execute_plan:
        prompt += (
            "\n\n用户已经确认允许执行。请根据以下计划完成任务，然后给出最终回复：\n"
            f"{execute_plan}"
        )

    return prompt


def generate_reply_codexcli(messages_history, system_prompt, model_config,
                            image_path=None):
    """Generate reply by invoking Codex CLI in non-interactive exec mode."""
    cli_path = model_config.get("codexcli_path") or _find_codexcli()
    workspace_dir = model_config.get("workspace_dir", r"D:\AI\workspace")
    if not os.path.isdir(workspace_dir):
        workspace_dir = os.getcwd()

    output_file = os.path.join(
        workspace_dir, f".codexcli_reply_{uuid.uuid4().hex}.txt"
    )
    prompt = _build_codexcli_prompt(
        messages_history, system_prompt, image_path=image_path,
        workspace_dir=workspace_dir
    )
    cmd = _build_codexcli_cmd(
        cli_path, prompt, model_config, output_file, image_path=image_path
    )

    clean_env = _build_codexcli_env(model_config)

    try:
        cli_timeout = model_config.get("codexcli_timeout", 900 if image_path else 600)
        if model_config.get("codexcli_print_prompt", True):
            print("[codexcli] Prompt begin >>>", flush=True)
            print(prompt, flush=True)
            print("[codexcli] <<< Prompt end", flush=True)
        print(f"[codexcli] Invoking: {cli_path} exec "
              f"(prompt length: {len(prompt)} chars, timeout: {cli_timeout}s)")
        stdout, returncode = _run_codexcli_streaming(
            cmd, clean_env, timeout=cli_timeout, stdin_text=prompt
        )
        if stdout is None:
            return None
        if returncode != 0:
            print(f"[codexcli] Process exited with code {returncode}", flush=True)
            return None

        reply = _read_codexcli_output(output_file, stdout)
        if not reply:
            print("[codexcli] Empty response from Codex CLI", flush=True)
            return None

        print(f"[codexcli] Final reply ({len(reply)} chars): {reply[:80]}...")
        return reply

    except Exception as e:
        print(f"[codexcli] Error: {e}", flush=True)
        return None
    finally:
        try:
            if os.path.exists(output_file):
                os.remove(output_file)
        except Exception:
            pass


def generate_rule_classification_codexcli(prompt, model_config):
    """Run an isolated, read-only Codex CLI rule-classification request."""
    cli_path = model_config.get("codexcli_path") or _find_codexcli()
    workspace_dir = model_config.get("workspace_dir", r"D:\AI\workspace")
    if not os.path.isdir(workspace_dir):
        workspace_dir = os.getcwd()

    output_file = os.path.join(
        workspace_dir, f".codexcli_rule_{uuid.uuid4().hex}.txt"
    )
    cmd = _build_codexcli_cmd(
        cli_path, prompt, model_config, output_file, plan_only=True
    )
    cmd[1:1] = ["-a", "never"]

    try:
        timeout = model_config.get("codexcli_rule_timeout", 120)
        print(f"[rule-router] Invoking Codex CLI classifier "
              f"(prompt length: {len(prompt)} chars, timeout: {timeout}s)",
              flush=True)
        stdout, returncode = _run_codexcli_streaming(
            cmd,
            _build_codexcli_env(model_config),
            timeout=timeout,
            stdin_text=prompt,
            stream_output=False,
        )
        if stdout is None or returncode != 0:
            print(f"[rule-router] Classifier failed with exit code {returncode}",
                  flush=True)
            return None

        output = _read_codexcli_output(output_file, stdout)
        if not output:
            print("[rule-router] Classifier returned an empty response", flush=True)
            return None

        print(f"[rule-router] Classifier returned {len(output)} chars", flush=True)
        return output
    except Exception as exc:
        print(f"[rule-router] Classifier error: {exc}", flush=True)
        return None
    finally:
        try:
            if os.path.exists(output_file):
                os.remove(output_file)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Qodercli "ask" mode: two-phase permission flow
# Phase 1 (plan): identify what permissions are needed
# Phase 2 (execute): re-run with bypass_permissions after user approval
# ---------------------------------------------------------------------------

def _build_qodercli_cmd(cli_path, system_prompt, prompt, model_config,
                       permission_mode, session_id=None, resume=False):
    """Build qodercli command list."""
    cmd = [cli_path, "-p", "--permission-mode", permission_mode]
    if system_prompt:
        cmd += ["--system-prompt", system_prompt]
    max_tokens = model_config.get("qodercli_max_tokens", 4096)
    cmd += ["--max-output-tokens", str(max_tokens)]
    model = model_config.get("qodercli_model", "Qwen3.7-Max")
    cmd += ["-m", model]
    if session_id:
        if resume:
            cmd += ["--resume", session_id]
        else:
            cmd += ["--session-id", session_id]
    cmd.append(prompt)
    return cmd


def _run_qodercli_subprocess(cmd):
    """Run qodercli subprocess with clean env, return stdout string or None."""
    clean_env = {k: v for k, v in os.environ.items()
                 if k not in ("QODER_AGENT_SDK_ENTRYPOINT",
                              "QODER_SDK_AUTH_PAYLOAD_FILE",
                              "QODER_WORK_INTEGRATION_MODE",
                              "QODER_SCENE")}
    stdout, returncode, stderr = _run_qodercli_streaming(cmd, clean_env, timeout=300)
    if stdout is None:
        return None
    if returncode != 0:
        print(f"[qodercli] exit={returncode}: {stderr[:500]}", flush=True)
        return None
    reply = stdout.strip()
    reply = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', reply)
    return reply if reply else None


def generate_reply_qodercli_plan(messages_history, system_prompt, model_config):
    """Phase 1 of ask mode: run qodercli with dont_ask to identify needed permissions.

    Returns a dict with either:
      - {"type": "reply", "content": "..."} — completed without needing permission
      - {"type": "permission_needed", "session_id": "...", "plan": "..."} — needs approval
      - {"type": "error", "content": "..."} — failed
    """
    cli_path = model_config.get("qodercli_path") or _find_qodercli()

    parts = []
    for msg in messages_history[:-1]:
        role_label = "用户" if msg["role"] == "user" else "助手"
        parts.append(f"{role_label}: {msg['content']}")
    current_msg = messages_history[-1]["content"] if messages_history else ""
    parts.append(f"用户: {current_msg}")
    full_prompt = "\n\n".join(parts) if len(parts) > 1 else current_msg

    # Wrap prompt to stop model before executing permission-requiring actions
    plan_prompt = (
        full_prompt
        + "\n\n[IMPORTANT: Before executing any actions that require permission "
        "(file operations, shell commands, web requests), STOP and list what you plan "
        "to do. Do NOT execute them yet. Just describe your plan.]"
    )

    session_id = str(uuid.uuid4())[:8]
    cmd = _build_qodercli_cmd(cli_path, system_prompt, plan_prompt, model_config,
                             permission_mode="dont_ask", session_id=session_id)

    print(f"[qodercli-ask] Phase 1: checking permissions (session={session_id})")
    output = _run_qodercli_subprocess(cmd)

    if not output:
        return {"type": "error", "content": "QoderCLI 没有返回结果"}

    # If output looks like a complete short answer (no plan indicators), treat as direct reply
    plan_indicators = ["plan", "步骤", "需要执行", "需要运行", "需要访问", "需要权限",
                       "will need", "need to", "plan to", "intend to",
                       "步骤如下", "操作如下", "将要", "将要执行", "需要以下"]
    is_plan = any(ind in output.lower() for ind in plan_indicators)

    if not is_plan and len(output) < 200:
        # Looks like a direct answer, no permission needed
        return {"type": "reply", "content": output}

    # Model described a plan — permission is needed
    return {"type": "permission_needed", "session_id": session_id, "plan": output}


def generate_reply_qodercli_execute(messages_history, system_prompt, model_config,
                                    session_id, user_approved):
    """Phase 2 of ask mode: resume session with bypass_permissions (or cancel).

    Args:
        session_id: session ID from generate_reply_qodercli_plan
        user_approved: True if user approved, False if denied
    """
    cli_path = model_config.get("qodercli_path") or _find_qodercli()

    if not user_approved:
        # User denied — send cancellation in the session
        cancel_prompt = "用户拒绝了操作权限。请告知用户操作已取消。"
        cmd = _build_qodercli_cmd(cli_path, system_prompt, cancel_prompt, model_config,
                                 permission_mode="dont_ask", session_id=session_id,
                                 resume=True)
        output = _run_qodercli_subprocess(cmd)
        return output or "操作已取消。"

    # User approved — resume session with bypass_permissions
    resume_prompt = "用户已批准所有权限。请执行你之前描述的计划。"
    cmd = _build_qodercli_cmd(cli_path, system_prompt, resume_prompt, model_config,
                             permission_mode="bypass_permissions", session_id=session_id,
                             resume=True)

    print(f"[qodercli-ask] Phase 2: executing with approval (session={session_id})")
    output = _run_qodercli_subprocess(cmd)
    return output or "操作已执行，但没有返回结果。"


def generate_reply_codexcli_plan(messages_history, system_prompt, model_config):
    """Phase 1 of ask mode: run Codex CLI in read-only planning mode."""
    cli_path = model_config.get("codexcli_path") or _find_codexcli()
    workspace_dir = model_config.get("workspace_dir", r"D:\AI\workspace")
    if not os.path.isdir(workspace_dir):
        workspace_dir = os.getcwd()

    output_file = os.path.join(
        workspace_dir, f".codexcli_plan_{uuid.uuid4().hex}.txt"
    )
    prompt = _build_codexcli_prompt(
        messages_history, system_prompt, workspace_dir=workspace_dir,
        plan_only=True
    )
    cmd = _build_codexcli_cmd(
        cli_path, prompt, model_config, output_file, plan_only=True
    )

    try:
        print("[codexcli-ask] Phase 1: checking permissions")
        stdout, returncode = _run_codexcli_streaming(
            cmd, _build_codexcli_env(model_config),
            timeout=model_config.get("codexcli_plan_timeout", 300),
            stdin_text=prompt,
        )
        if stdout is None or returncode != 0:
            return {"type": "error", "content": "Codex CLI 没有返回结果"}

        output = _read_codexcli_output(output_file, stdout)
        if not output:
            return {"type": "error", "content": "Codex CLI 没有返回结果"}

        plan_indicators = [
            "plan", "步骤", "需要执行", "需要运行", "需要访问", "需要权限",
            "will need", "need to", "plan to", "intend to",
            "步骤如下", "操作如下", "将要", "将要执行", "需要以下",
            "读取文件", "写入文件", "运行命令", "联网",
        ]
        lowered = output.lower()
        is_plan = any(ind in lowered for ind in plan_indicators)

        if not is_plan and len(output) < 300:
            return {"type": "reply", "content": output}

        session_id = str(uuid.uuid4())[:8]
        return {"type": "permission_needed", "session_id": session_id, "plan": output}

    finally:
        try:
            if os.path.exists(output_file):
                os.remove(output_file)
        except Exception:
            pass


def generate_reply_codexcli_execute(messages_history, system_prompt, model_config,
                                    plan, user_approved):
    """Phase 2 of ask mode: execute the approved Codex CLI plan."""
    if not user_approved:
        return "操作已取消。"

    cli_path = model_config.get("codexcli_path") or _find_codexcli()
    workspace_dir = model_config.get("workspace_dir", r"D:\AI\workspace")
    if not os.path.isdir(workspace_dir):
        workspace_dir = os.getcwd()

    output_file = os.path.join(
        workspace_dir, f".codexcli_execute_{uuid.uuid4().hex}.txt"
    )
    prompt = _build_codexcli_prompt(
        messages_history, system_prompt, workspace_dir=workspace_dir,
        execute_plan=plan
    )
    cmd = _build_codexcli_cmd(
        cli_path, prompt, model_config, output_file
    )

    try:
        print("[codexcli-ask] Phase 2: executing approved plan")
        stdout, returncode = _run_codexcli_streaming(
            cmd, _build_codexcli_env(model_config),
            timeout=model_config.get("codexcli_timeout", 900),
            stdin_text=prompt,
        )
        if stdout is None or returncode != 0:
            return None
        return _read_codexcli_output(output_file, stdout)

    finally:
        try:
            if os.path.exists(output_file):
                os.remove(output_file)
        except Exception:
            pass


def generate_reply(messages_history, system_prompt, model_config,
                   image_path=None, image_base64=None, image_mime="image/jpeg"):
    """Dispatch to the appropriate backend based on model_config mode.

    image_path: local file path to an image/file (used by qodercli/codexcli attachments).
    image_base64: base64-encoded image data string (used by remote API multimodal format).
    image_mime: MIME type of the image (default image/jpeg).
    """
    mode = model_config.get("mode", "ollama")
    if mode == "remote":
        return generate_reply_remote(messages_history, system_prompt, model_config,
                                     image_base64=image_base64, image_mime=image_mime)
    elif mode == "qodercli":
        return generate_reply_qodercli(messages_history, system_prompt, model_config,
                                       image_path=image_path)
    elif mode in ("codexcli", "codecli"):
        return generate_reply_codexcli(messages_history, system_prompt, model_config,
                                       image_path=image_path)
    else:
        model_name = model_config.get("model", "qwen3:8b")
        return generate_reply_ollama(messages_history, system_prompt, model_name,
                                     model_config=model_config)
