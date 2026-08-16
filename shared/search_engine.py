"""
shared/search_engine.py - Web search (Bocha API + Bing China + Baidu + weather)
Optimized for China mainland access.
"""
import re
import requests
from datetime import datetime, timedelta


# ---- Search intent keywords (kept for quick-match, no longer sole trigger) ----
SEARCH_KEYWORDS = [
    "weather", "news", "latest", "search", "check", "look up",
    "realtime", "today", "now", "recent", "just now", "yesterday", "tomorrow",
    "how much", "stock price", "exchange rate", "score", "trending", "ranking",
    "online", "baidu", "google",
    # Chinese
    "天气", "新闻", "最新", "搜索", "查一下", "帮我查", "搜一下",
    "实时", "今天", "现在", "最近", "刚刚", "昨天", "明天",
    "多少钱", "股价", "汇率", "比分", "热搜", "排行",
    "联网", "网上", "百度", "谷歌", "搜索", "查找",
    "发生了什么", "怎么了", "出事", "突发",
]

# ---- Evasive response detection ----
EVASIVE_PHRASES = [
    "无法获取", "没有联网", "看不到", "无法搜索", "没有实时",
    "无法提供实时", "没有网络", "无法查询", "不支持联网",
    "我无法", "抱歉，我无法", "抱歉，我不能", "作为AI", "作为人工智能",
    "没有访问", "无法访问", "信息截止",
    "knowledge cutoff", "训练数据",
    "无法确定", "不太清楚具体", "不确定具体",
    "查不到", "查不了", "没查到", "不知道具体", "不太清楚",
    "不太了解", "没发查", "没法查", "看不到具体",
    "不联网", "上不了网", "网卡了", "断网了",
    "我这边没", "我这没法", "我这边看不了",
    "建议你自己", "你还是自己", "自己查一下",
    "没有实时", "没有最新", "获取不到",
    "网不太好", "网络不太好", "连不上网", "网速不行",
    "没法帮你", "帮不了你", "帮不到你",
    "你自己看", "你自己查", "自己瞧一眼", "自己看看",
    "看看窗外", "瞧一眼手机", "看看手机",
    "不太确定", "我说不准", "说不好",
    "I don't have access", "I cannot browse", "I'm unable to search",
    "I don't have real-time", "my knowledge is limited to",
    "I cannot provide current", "as an AI",
]

EVASIVE_PATTERNS = [
    r'(?:查|搜|看|找|获取).{0,3}(?:不到|不了|不出来)',
    r'(?:没有|没).{0,3}(?:联网|网络|实时|最新)',
    r'(?:我|这边).{0,10}(?:无法|没法|不能|帮不了|看不了|查不了)',
    r'(?:网|网络).{0,3}(?:不太好|不好|不行|有问题|断了)',
    r'(?:你自己|自己|你还是).{0,5}(?:看看|查查|搜搜|瞧|打开)',
]

# ---- Search command words (to strip from query) ----
SEARCH_COMMAND_WORDS = [
    "联网查询", "联网搜索", "帮我查一下", "帮我搜一下",
    "帮我查", "帮我搜", "查一下", "搜一下",
    "搜索一下", "查询一下", "一下",
    "联网", "搜索", "查询", "查找", "搜索",
    "请问", "请帮我", "帮我看", "帮我看看",
]

# ---- Weather keywords ----
WEATHER_KEYWORDS = ["天气", "气温", "温度", "下雨", "下雪", "多少度", "穿什么", "带伞", "防晒"]

# ---- [SEARCH: ...] marker detection ----
SEARCH_MARKER_RE = re.compile(r'\[SEARCH:\s*(.+?)\]', re.IGNORECASE)


def needs_web_search(user_message):
    """Check if user message has search intent (keyword-based, supplementary)"""
    msg = user_message.lower()
    matched = [kw for kw in SEARCH_KEYWORDS if kw in msg]
    if matched:
        print(f"[search] Keywords matched: {matched}", flush=True)
    return bool(matched)


def response_is_evasive(reply):
    """Check if model response is evasive or unable to answer.
    For longer replies (>100 chars), require 2+ matches to reduce false positives
    (e.g., model references search results but contains one borderline phrase).
    """
    if not reply or len(reply) < 15:
        print(f"[search] Reply too short ({len(reply or '')} chars), evasive", flush=True)
        return True
    matched = [p for p in EVASIVE_PHRASES if p in reply]
    if matched:
        # For longer substantive replies, one borderline phrase is OK
        if len(reply) > 100 and len(matched) < 2:
            print(f"[search] Mild phrases {matched} in long reply ({len(reply)} chars), not evasive", flush=True)
            return False
        print(f"[search] Evasive phrases: {matched}", flush=True)
        return True
    for pattern in EVASIVE_PATTERNS:
        if re.search(pattern, reply):
            # For longer replies, also be more lenient with pattern matches
            if len(reply) > 100:
                print(f"[search] Mild pattern {pattern} in long reply ({len(reply)} chars), not evasive", flush=True)
                return False
            print(f"[search] Evasive pattern: {pattern}", flush=True)
            return True
    return False


def extract_search_marker(reply):
    """Extract search query from [SEARCH: ...] marker in model output"""
    match = SEARCH_MARKER_RE.search(reply or "")
    if match:
        query = match.group(1).strip()
        print(f"[search] Model requested search: {query}", flush=True)
        return query
    return None


def extract_search_query(user_message):
    """Extract clean search query from user message"""
    query = user_message.strip()
    for cmd in SEARCH_COMMAND_WORDS:
        query = query.replace(cmd, "")
    query = query.strip(" ,，.。!！?？、")
    if len(query) < 3:
        query = user_message.strip()
    return query


def is_weather_query(user_message):
    """Check if query is about weather"""
    return any(kw in user_message for kw in WEATHER_KEYWORDS)


def extract_location(query):
    """Extract location name from weather query"""
    cleaned = query
    for word in ["天气", "气温", "温度", "今天", "明天", "昨天", "后天",
                 "多少度", "下雨", "下雪", "穿什么", "带伞", "防晒",
                 "查询", "搜索", "查一下", "联网", "的"]:
        cleaned = cleaned.replace(word, "")
    cleaned = re.sub(r'\d{4}年\d{2}月\d{2}日', '', cleaned)
    cleaned = cleaned.strip(" ,，.。!！?？、")
    return cleaned if len(cleaned) >= 2 else query


# ============================================================
# Weather search (wttr.in)
# ============================================================

def weather_search(query, max_results=5):
    """Use wttr.in free weather API"""
    location = extract_location(query)
    print(f"[weather] Location: {location}", flush=True)
    try:
        resp = requests.get(
            f"https://wttr.in/{location}?format=j1&lang=zh",
            timeout=20
        )
        resp.raise_for_status()
        data = resp.json()

        current = data.get("current_condition", [{}])[0]
        today = data.get("weather", [{}])[0]
        tomorrow = data.get("weather", [{}])[1] if len(data.get("weather", [])) > 1 else {}

        weather_desc = current.get("lang_zh", [{}])[0].get(
            "value", current.get("weatherDesc", [{}])[0].get("value", "N/A")
        )

        lines = [
            f"【{location}实时天气】",
            f"当前温度: {current.get('temp_C', '?')}°C, 体感: {current.get('FeelsLikeC', '?')}°C",
            f"天气: {weather_desc}",
            f"湿度: {current.get('humidity', '?')}%, 风速: {current.get('windspeedKmph', '?')} km/h",
        ]

        if today:
            lines.append(f"\n【今日预报 ({today.get('date', '今天')})】")
            lines.append(f"最高: {today.get('maxtempC', '?')}°C, 最低: {today.get('mintempC', '?')}°C")
            for h in today.get("hourly", []):
                time_val = h.get("time", "0").zfill(4)
                hour = time_val[:2] + ":" + time_val[2:] if len(time_val) >= 4 else time_val
                desc = h.get("lang_zh", [{}])[0].get("value", h.get("weatherDesc", [{}])[0].get("value", ""))
                lines.append(f"  {hour} - {h.get('tempC', '?')}°C, {desc}")

        if tomorrow:
            lines.append(f"\n【明日预报 ({tomorrow.get('date', '明天')})】")
            lines.append(f"最高: {tomorrow.get('maxtempC', '?')}°C, 最低: {tomorrow.get('mintempC', '?')}°C")

        result = "\n".join(lines)
        print(f"[weather] OK, {len(result)} chars", flush=True)
        return result
    except Exception as e:
        print(f"[weather] Failed: {e}", flush=True)
        return ""


def weather_web_search(location, bocha_api_key=""):
    """Search for weather info via web search (Bocha/Bing) - fallback when wttr.in fails"""
    query = f"{location} 今天天气预报 温度 降水"
    print(f"[weather] Web search fallback: {query}", flush=True)
    return web_search(query, max_results=5, bocha_api_key=bocha_api_key)


# ============================================================
# Search backends (China-optimized waterfall)
# ============================================================

def _search_bocha(query, max_results=5, api_key=""):
    """Bocha Search API - domestic Chinese search, best reliability from China"""
    if not api_key:
        return ""
    try:
        resp = requests.post(
            "https://api.bochaai.com/v1/web-search",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "query": query,
                "freshness": "noLimit",
                "summary": True,
                "count": max_results,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        pages = data.get("data", {}).get("webPages", {}).get("value", [])
        if not pages:
            return ""

        lines = []
        for i, page in enumerate(pages[:max_results], 1):
            title = page.get("name", "")
            snippet = page.get("summary", page.get("snippet", ""))
            href = page.get("url", "")
            lines.append(f"[{i}] {title}\n{snippet}\n{href}")

        result = "\n\n".join(lines)
        print(f"[search] Bocha: {len(pages)} results", flush=True)
        return result
    except Exception as e:
        print(f"[search] Bocha failed: {e}", flush=True)
        return ""


def _search_ddgs_bing(query, max_results=5):
    """DuckDuckGo-search library with Bing backend (works from China)"""
    try:
        from ddgs import DDGS
        with DDGS(timeout=10) as ddgs:
            results = list(ddgs.text(
                query, max_results=max_results,
                region="cn-zh", backend="bing"
            ))
        if not results:
            return ""
        print(f"[search] DDG/Bing: {len(results)} results", flush=True)
        lines = []
        for i, r in enumerate(results, 1):
            lines.append(f"[{i}] {r.get('title', '')}\n{r.get('body', '')}\n{r.get('href', '')}")
        return "\n\n".join(lines)
    except Exception as e:
        print(f"[search] DDG/Bing failed: {e}", flush=True)
        return ""


def _search_bing(query, max_results=5):
    """Bing China scraping (cn.bing.com)"""
    try:
        from urllib.parse import quote_plus
        url = f"https://cn.bing.com/search?q={quote_plus(query)}&ensearch=0"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/125.0.0.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        html = resp.text

        titles = re.findall(
            r'<h2[^>]*><a[^>]*href="([^"]+)"[^>]*>(.*?)</a></h2>',
            html, re.DOTALL
        )
        snippets = re.findall(
            r'<div class="b_caption"><p[^>]*>(.*?)</p>',
            html, re.DOTALL
        )

        results = []
        for i, (href, title_raw) in enumerate(titles[:max_results]):
            title = re.sub(r'<[^>]+>', '', title_raw).strip()
            snippet = re.sub(r'<[^>]+>', '', snippets[i]).strip() if i < len(snippets) else ""
            if title and len(title) > 3:
                results.append(f"[{len(results)+1}] {title}\n{snippet}\n{href}")

        if results:
            print(f"[search] Bing scrape: {len(results)} results", flush=True)
            return "\n\n".join(results)
        return ""
    except Exception as e:
        print(f"[search] Bing scrape failed: {e}", flush=True)
        return ""


def _search_baidu(query, max_results=5):
    """Baidu search scraping"""
    try:
        from urllib.parse import quote_plus
        url = f"https://www.baidu.com/s?ie=utf-8&wd={quote_plus(query)}&pn=0"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/125.0.0.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept": "text/html,application/xhtml+xml",
        }
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        html = resp.text

        # Extract titles and links from Baidu results
        titles = re.findall(
            r'<h3[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            html, re.DOTALL
        )
        # Extract snippets (Baidu uses various class names, try common patterns)
        snippets = re.findall(
            r'<span class="content-right_[^"]*">(.*?)</span>',
            html, re.DOTALL
        )
        if not snippets:
            snippets = re.findall(
                r'<div class="c-abstract[^"]*">(.*?)</div>',
                html, re.DOTALL
            )

        results = []
        for i, (href, title_raw) in enumerate(titles[:max_results]):
            title = re.sub(r'<[^>]+>', '', title_raw).strip()
            snippet = re.sub(r'<[^>]+>', '', snippets[i]).strip() if i < len(snippets) else ""
            if title and len(title) > 3:
                results.append(f"[{len(results)+1}] {title}\n{snippet}\n{href}")

        if results:
            print(f"[search] Baidu: {len(results)} results", flush=True)
            return "\n\n".join(results)
        return ""
    except Exception as e:
        print(f"[search] Baidu failed: {e}", flush=True)
        return ""


# ============================================================
# Main search entry point (waterfall)
# ============================================================

def web_search(query, max_results=5, bocha_api_key=""):
    """Search waterfall: Bocha -> DDG/Bing -> Bing scrape -> Baidu"""
    # 1. Bocha (best for China, needs API key)
    results = _search_bocha(query, max_results, api_key=bocha_api_key)
    if results:
        return results

    # 2. DDG with Bing backend
    results = _search_ddgs_bing(query, max_results)
    if results:
        return results

    # 3. cn.bing.com scraping
    results = _search_bing(query, max_results)
    if results:
        return results

    # 4. Baidu scraping
    results = _search_baidu(query, max_results)
    if results:
        return results

    print("[search] All backends failed", flush=True)
    return ""


def inject_dates(query):
    """Replace relative dates (today/tomorrow/etc) with concrete dates"""
    today = datetime.now()
    date_map = {
        "今天": today.strftime("%Y年%m月%d日"),
        "明天": (today + timedelta(days=1)).strftime("%Y年%m月%d日"),
        "昨天": (today - timedelta(days=1)).strftime("%Y年%m月%d日"),
        "后天": (today + timedelta(days=2)).strftime("%Y年%m月%d日"),
    }
    for rel, abs_date in date_map.items():
        if rel in query:
            query = query.replace(rel, abs_date)
            break
    return query
