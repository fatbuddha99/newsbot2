import json
import os
import re
import ssl
import traceback
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from html import unescape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, quote_plus, urlparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

MAX_ITEMS_PER_SOURCE = 10
MAX_TOTAL_ITEMS = 50
REQUEST_TIMEOUT = 5
DEDUPE_SIMILARITY_WORDS = 0.72
MAX_HEADLINES_FOR_LLM = 10
SHOW_TOP_STORIES = 15

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/147.0.0.0 Safari/537.36"
)
SEC_USER_AGENT = "newsbot/1.0 support@example.com"

ENABLE_LLM_ANALYSIS = True
GEMINI_MODELS = ["gemini-3-flash-preview", "gemini-3.1-pro-preview"]
DEFAULT_GEMINI_MODEL = GEMINI_MODELS[0]
DEEP_DIVE_MODEL = GEMINI_MODELS[0]
OPENAI_MODELS = ["gpt-5.2"]
DEFAULT_OPENAI_MODEL = OPENAI_MODELS[0]
LLM_MODES = {"auto", "gemini", "openai"}
MAX_DEEP_DIVE_ROWS = 8
CACHE_TTL_SECONDS = 300
DISK_RETENTION_DAYS = 30
SCAN_TTL_SECONDS = 900
INSIGHT_TTL_SECONDS = 900
GLOBAL_INSIGHT_TTL_SECONDS = 1800
BASE_ANALYSIS_TTL_SECONDS = 6 * 60 * 60
FULL_ANALYSIS_TTL_SECONDS = 24 * 60 * 60
CACHE_VERSION = "2026-04-25-earnings-reaction-v2"

NEWS_SOURCES = [
    {
        "name": "Reuters Business",
        "url": "https://news.google.com/rss/search?q=allinurl:reuters.com+business+-unveils+-luxury+-lifestyle&hl=en-US&gl=US&ceid=US:en",
    },
    {
        "name": "Political Signal",
        "url": "https://news.google.com/rss/search?q=Donald+Trump+post+OR+announcement+OR+statement+-rumor+-opinion&hl=en-US&gl=US&ceid=US:en",
    },
    {"name": "Yahoo Finance", "url": "https://finance.yahoo.com/news/rssindex"},
    {"name": "CNBC Finance", "url": "https://www.cnbc.com/id/10000664/device/rss/rss.html"},
    {"name": "MarketWatch", "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories"},
]

NOISE_PHRASES = [
    "flying car",
    "unveils",
    "luxury",
    "lifestyle",
    "concept",
    "opinion:",
    "how to",
    "best of",
    "rumor",
    "leak",
    "gift guide",
    "prediction",
    "forecast",
]

SIGNAL_PHRASES = [
    "fed",
    "interest rate",
    "inflation",
    "cpi",
    "earnings miss",
    "profit warning",
    "bankruptcy",
    "tariff",
    "sanction",
    "yield",
    "treasury",
    "ceo resigns",
    "chapter 11",
    "sec",
    "war",
    "strike",
]

SSL_CONTEXT = ssl.create_default_context()
TAG_RE = re.compile(r"<[^>]+>")
FRAME_QUARTER_RE = re.compile(r"CY(\d{4})Q([1-4])")
SEC_TICKER_CACHE = None
API_CACHE = {}
CACHE_FILE = BASE_DIR / ".signal_terminal_cache.json"
DISK_CACHE = {}
DISK_CACHE_LOCK = Lock()

warnings.filterwarnings("ignore", message=r".*utcnow\(\) is deprecated.*", category=DeprecationWarning)
warnings.filterwarnings("ignore", message=r".*utcfromtimestamp\(\) is deprecated.*", category=DeprecationWarning)


def redact_sensitive_text(text: str) -> str:
    if not text:
        return text
    redacted = text
    for env_key in ("OPENAI_API_KEY", "GEMINI_API_KEY"):
        secret = os.getenv(env_key)
        if secret:
            redacted = redacted.replace(secret, f"{env_key[:6]}***REDACTED***")
    redacted = re.sub(r"Bearer\s+[A-Za-z0-9_\-]+", "Bearer ***REDACTED***", redacted)
    redacted = re.sub(r"sk-[A-Za-z0-9_\-]+", "sk-***REDACTED***", redacted)
    return redacted


def log_runtime_error(scope: str, exc: Exception):
    print(redact_sensitive_text(f"[{scope}] {type(exc).__name__}: {exc}"), flush=True)
    trace = redact_sensitive_text(traceback.format_exc())
    if trace and trace.strip() != "NoneType: None":
        print(trace, flush=True)


def fetch_text(url: str, headers=None) -> str:
    req = Request(url, headers=headers or {"User-Agent": USER_AGENT})
    with urlopen(req, timeout=REQUEST_TIMEOUT, context=SSL_CONTEXT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_json(url: str, headers=None):
    return json.loads(fetch_text(url, headers=headers))


def load_disk_cache():
    global DISK_CACHE
    if not CACHE_FILE.exists():
        DISK_CACHE = {}
        return
    try:
        DISK_CACHE = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        DISK_CACHE = {}


def save_disk_cache():
    with DISK_CACHE_LOCK:
        CACHE_FILE.write_text(json.dumps(DISK_CACHE), encoding="utf-8")


def cache_key_text(key):
    return json.dumps({"v": CACHE_VERSION, "k": key}, sort_keys=True)


def cache_get(key):
    entry = API_CACHE.get(key)
    if not entry:
        return None
    if datetime.utcnow() > entry["expiresAt"]:
        API_CACHE.pop(key, None)
        return None
    return entry["value"]


def cache_set(key, value, ttl_seconds=CACHE_TTL_SECONDS):
    API_CACHE[key] = {
        "value": value,
        "expiresAt": datetime.utcnow() + timedelta(seconds=ttl_seconds),
    }
    return value


def disk_cache_get(key, ttl_seconds=None):
    entry = DISK_CACHE.get(cache_key_text(key))
    if not entry:
        return None
    created_at = entry.get("createdAt")
    if not created_at:
        return None
    try:
        created_dt = datetime.fromisoformat(created_at)
    except Exception:
        return None
    age = datetime.utcnow() - created_dt
    if age > timedelta(days=DISK_RETENTION_DAYS):
        return None
    if ttl_seconds is not None and age > timedelta(seconds=ttl_seconds):
        return None
    return entry.get("value")


def disk_cache_set(key, value):
    DISK_CACHE[cache_key_text(key)] = {
        "createdAt": datetime.utcnow().isoformat(),
        "value": value,
    }
    save_disk_cache()
    return value


def clean_html(text: str) -> str:
    if not text:
        return ""
    text = unescape(text)
    text = TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_rss(raw: str, source_name: str):
    root = ET.fromstring(raw)
    items = []
    for node in root.findall(".//item"):
        title = node.findtext("title") or ""
        link = node.findtext("link") or ""
        pub_date = node.findtext("pubDate") or ""
        if not title:
            continue
        items.append(
            {
                "title": clean_html(title),
                "link": link.strip(),
                "pubDate": pub_date,
                "source": source_name,
            }
        )
    return items[:MAX_ITEMS_PER_SOURCE]


def similarity(a: str, b: str) -> float:
    ta = set(a.lower().split())
    tb = set(b.lower().split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta), len(tb))


def phrase_in_text(phrase: str, text: str) -> bool:
    pattern = r"(?<!\w)" + re.escape(phrase).replace(r"\ ", r"\s+") + r"(?!\w)"
    return re.search(pattern, text) is not None


def dedupe(items):
    deduped = []
    for item in items:
        if any(similarity(item["title"], existing["title"]) > DEDUPE_SIMILARITY_WORDS for existing in deduped):
            continue
        deduped.append(item)
    return deduped


def apply_filtering(items, query=None, focus_mode=True):
    scored = []
    query_text = (query or "").lower().strip()

    for item in items:
        text = item["title"].lower()
        score = 0
        matches = []

        for phrase in NOISE_PHRASES:
            if phrase_in_text(phrase, text):
                score -= 10
                matches.append(f"noise:{phrase}")

        for phrase in SIGNAL_PHRASES:
            if phrase_in_text(phrase, text):
                score += 5
                matches.append(f"signal:{phrase}")

        if query_text and query_text in text:
            score += 15
            matches.append(f"query:{query_text}")

        enriched = dict(item)
        enriched["signalScore"] = score
        enriched["scoreMatches"] = matches

        if focus_mode and score < 0:
            continue

        scored.append(enriched)

    scored.sort(key=lambda item: (item["signalScore"], item["source"], item["title"]), reverse=True)
    return scored[:MAX_TOTAL_ITEMS]


def build_sources(query=None):
    if not query:
        return NEWS_SOURCES
    encoded = quote_plus(query)
    return [
        {
            "name": "Ticker Search",
            "url": f"https://news.google.com/rss/search?q={encoded}+when:24h&hl=en-US&gl=US&ceid=US:en",
        },
        {
            "name": "Reuters Search",
            "url": f"https://news.google.com/rss/search?q=allinurl:reuters.com+{encoded}&hl=en-US&gl=US&ceid=US:en",
        },
    ]


def build_analysis_payload(items, query=None, focus_mode=True):
    lines = []
    for index, item in enumerate(items[:MAX_HEADLINES_FOR_LLM], 1):
        matches = ", ".join(item.get("scoreMatches", [])) or "none"
        lines.append(
            f"{index}. [source={item['source']}] [score={item['signalScore']}] "
            f"[matches={matches}] {item['title']}"
        )
    return (
        f"Search query: {query or 'GLOBAL'}\n"
        f"Focus mode: {'ON' if focus_mode else 'OFF'}\n"
        "Prioritized headlines:\n"
        + "\n".join(lines)
    )


def llm_error(provider, model, error_text):
    return {
        "ok": False,
        "provider": provider,
        "model": model,
        "text": "",
        "error": error_text,
    }


def extract_openai_text(response):
    if hasattr(response, "output_text") and response.output_text:
        return response.output_text

    parts = []
    for item in getattr(response, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            text_value = getattr(content, "text", None)
            if text_value:
                parts.append(text_value)
    return "\n".join(parts).strip()


def call_gemini(prompt, model):
    try:
        from google import genai
    except ImportError:
        return llm_error("gemini", model, "google-genai is not installed. Run: pip install google-genai")

    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not api_key:
        return llm_error("gemini", model, "GEMINI_API_KEY is missing.")

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(model=model, contents=prompt)
        text = getattr(response, "text", "") or ""
        return {
            "ok": True,
            "provider": "gemini",
            "model": model,
            "text": text.strip(),
            "error": "",
        }
    except Exception as exc:
        log_runtime_error(f"llm:gemini:{model}", exc)
        return llm_error("gemini", model, f"LLM ERROR: {exc}")


def call_openai(prompt, model):
    try:
        from openai import OpenAI
        import httpx
    except ImportError:
        return llm_error("openai", model, "openai is not installed. Run: pip install openai")

    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return llm_error("openai", model, "OPENAI_API_KEY is missing.")

    http_client = None
    try:
        http_client = httpx.Client(http2=False, timeout=httpx.Timeout(30.0, connect=10.0))
        client = OpenAI(api_key=api_key, timeout=30.0, max_retries=1, http_client=http_client)
        response = client.responses.create(model=model, input=prompt)
        text = extract_openai_text(response)
        return {
            "ok": True,
            "provider": "openai",
            "model": model,
            "text": text.strip(),
            "error": "",
        }
    except Exception as exc:
        log_runtime_error(f"llm:openai:{model}", exc)
        return llm_error("openai", model, f"LLM ERROR: {exc}")
    finally:
        if http_client is not None:
            http_client.close()


def normalize_llm_mode(mode):
    normalized = (mode or "auto").strip().lower()
    return normalized if normalized in LLM_MODES else "auto"


def call_llm_with_fallback(prompt, gemini_model=DEFAULT_GEMINI_MODEL, openai_model=DEFAULT_OPENAI_MODEL, mode="auto"):
    mode = normalize_llm_mode(mode)
    if mode == "gemini":
        return call_gemini(prompt, gemini_model)
    if mode == "openai":
        return call_openai(prompt, openai_model)

    primary = call_gemini(prompt, gemini_model)
    if primary.get("ok"):
        return primary

    fallback = call_openai(prompt, openai_model)
    if fallback.get("ok"):
        fallback["fallbackFrom"] = primary.get("provider")
        return fallback

    return {
        "ok": False,
        "provider": fallback.get("provider", primary.get("provider", "none")),
        "model": fallback.get("model", primary.get("model", "")),
        "text": "",
        "error": f"{primary.get('error', 'Gemini failed')} | Fallback: {fallback.get('error', 'OpenAI failed')}",
    }


def analyze_with_llm(headlines_context, query=None, focus_mode=True, llm_mode="auto"):
    prompt = (
        "You are a real-time macro, political-risk, and market signal analyst. "
        "You are given prioritized headlines with heuristic signal scores and matched trigger phrases. "
        "Use that ranking to focus on the most market-moving items first.\n\n"
        "Return a concise intelligence brief in exactly this format:\n"
        "Primary signal:\n"
        "- One or two sentences on the dominant takeaway.\n\n"
        "Main themes:\n"
        "- 3 bullet points max.\n\n"
        "Why it matters now:\n"
        "- 2 bullet points max.\n\n"
        "Likely market impact:\n"
        "- Rates:\n"
        "- Equities/sectors:\n"
        "- Commodities/FX/crypto:\n\n"
        "Bull case:\n"
        "- 2 bullet points max.\n\n"
        "Bear case:\n"
        "- 2 bullet points max.\n\n"
        "What to watch next:\n"
        "- 4 bullet points max, as concrete catalysts or data points.\n\n"
        "Confidence:\n"
        "- Choose High, Medium, or Low and explain briefly.\n\n"
        "Rules:\n"
        "- Be crisp, concrete, and market-oriented.\n"
        "- Prefer signal over recap.\n"
        "- If the query is ticker-specific, center the analysis on that company or ticker first.\n"
        "- If the evidence is mixed, say so clearly.\n"
        "- Do not mention that you are an AI.\n\n"
        f"{headlines_context}"
    )
    return call_llm_with_fallback(prompt, mode=llm_mode)


def load_sec_tickers():
    global SEC_TICKER_CACHE
    if SEC_TICKER_CACHE is None:
        url = "https://www.sec.gov/files/company_tickers.json"
        data = fetch_json(url, headers={"User-Agent": SEC_USER_AGENT})
        SEC_TICKER_CACHE = {
            entry["ticker"].upper(): {
                "ticker": entry["ticker"].upper(),
                "cik": str(entry["cik_str"]).zfill(10),
                "title": entry["title"],
            }
            for entry in data.values()
        }
    return SEC_TICKER_CACHE


def resolve_equity_query(query: str):
    if not query:
        return None

    url = f"https://query1.finance.yahoo.com/v1/finance/search?q={quote_plus(query)}"
    payload = fetch_json(url)
    query_upper = query.strip().upper()

    candidates = []
    for quote in payload.get("quotes", []):
        if quote.get("quoteType") != "EQUITY":
            continue
        symbol = (quote.get("symbol") or "").upper()
        score = 0
        if symbol == query_upper:
            score += 100
        if (quote.get("shortname") or "").upper() == query_upper:
            score += 50
        if query_upper in symbol:
            score += 20
        score += 10 if quote.get("isYahooFinance") else 0
        candidates.append(
            {
                "symbol": symbol,
                "shortName": quote.get("shortname") or quote.get("longname") or symbol,
                "longName": quote.get("longname") or quote.get("shortname") or symbol,
                "sector": quote.get("sectorDisp") or quote.get("sector") or "",
                "industry": quote.get("industryDisp") or quote.get("industry") or "",
                "exchange": quote.get("exchDisp") or quote.get("exchange") or "",
                "score": score,
            }
        )

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item["score"], item["symbol"]), reverse=True)
    return candidates[0]


def get_company_facts(symbol: str):
    tickers = load_sec_tickers()
    ticker_info = tickers.get(symbol.upper())
    if not ticker_info:
        return None, None
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{ticker_info['cik']}.json"
    return fetch_json(url, headers={"User-Agent": SEC_USER_AGENT}), ticker_info


def pick_quarter_series(units):
    quarter_values = {}
    for entries in units.values():
        for entry in entries:
            frame = entry.get("frame") or ""
            match = FRAME_QUARTER_RE.fullmatch(frame)
            if not match:
                continue
            frame_key = f"CY{match.group(1)}Q{match.group(2)}"
            existing = quarter_values.get(frame_key)
            if existing is None or entry.get("filed", "") > existing.get("filed", ""):
                quarter_values[frame_key] = entry

    items = []
    for frame_key, entry in quarter_values.items():
        match = FRAME_QUARTER_RE.fullmatch(frame_key)
        items.append(
            {
                "frame": frame_key,
                "year": int(match.group(1)),
                "quarter": int(match.group(2)),
                "periodEnd": entry.get("end"),
                "filed": entry.get("filed"),
                "value": entry.get("val"),
            }
        )

    items.sort(key=lambda item: (item["year"], item["quarter"]))
    return items


def extract_metric_series(company_facts, concept_candidates):
    us_gaap = company_facts.get("facts", {}).get("us-gaap", {})
    for concept in concept_candidates:
        if concept in us_gaap:
            return pick_quarter_series(us_gaap[concept].get("units", {}))
    return []


def fetch_price_series(symbol: str):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote_plus(symbol)}?range=3y&interval=1d&includeAdjustedClose=true"
    payload = fetch_json(url)
    result = payload.get("chart", {}).get("result", [{}])[0]
    timestamps = result.get("timestamp", [])
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    opens = quote.get("open") or []
    closes = quote.get("close") or []

    prices = []
    for ts, open_price, close in zip(timestamps, opens, closes):
        if close is None:
            continue
        prices.append(
            {
                "date": datetime.utcfromtimestamp(ts).date(),
                "open": float(open_price) if open_price is not None else None,
                "close": float(close),
            }
        )
    return prices


def fetch_current_quote(symbol: str):
    url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={quote_plus(symbol)}"
    try:
        payload = fetch_json(url)
    except Exception:
        payload = {}
    results = payload.get("quoteResponse", {}).get("result", [])
    quote = results[0] if results else {}
    current_price = quote.get("regularMarketPrice")
    previous_close = quote.get("regularMarketPreviousClose")

    if current_price is None or previous_close is None:
        try:
            prices = fetch_price_series(symbol)
        except Exception:
            prices = []
        if prices:
            latest_close = prices[-1]["close"]
            current_price = latest_close if current_price is None else current_price
            previous_close = prices[-2]["close"] if previous_close is None and len(prices) > 1 else previous_close

    return {
        "currentPrice": current_price,
        "previousClose": previous_close,
        "marketCap": quote.get("marketCap"),
        "currency": quote.get("currency"),
    }


def closing_price_on_or_before(prices, period_end):
    target = datetime.strptime(period_end, "%Y-%m-%d").date()
    valid = [item for item in prices if item["date"] <= target]
    if not valid:
        return None
    return valid[-1]["close"]


def trading_day_before(prices, event_date):
    target = datetime.strptime(event_date, "%Y-%m-%d").date()
    valid = [item for item in prices if item["date"] < target]
    return valid[-1] if valid else None


def trading_day_on_or_after(prices, event_date):
    target = datetime.strptime(event_date, "%Y-%m-%d").date()
    valid = [item for item in prices if item["date"] >= target]
    return valid[0] if valid else None


def format_billions(raw_value):
    if raw_value is None:
        return "N/A"
    if abs(raw_value) < 1_000_000_000:
        return f"${raw_value / 1_000_000:.1f}M"
    return f"${raw_value / 1_000_000_000:.2f}B"


def format_eps(raw_value):
    if raw_value is None:
        return "N/A"
    return f"${raw_value:.2f}"


def format_price(raw_value):
    if raw_value is None:
        return "N/A"
    return f"${raw_value:.2f}"


def format_pe(raw_value):
    if raw_value is None:
        return "N/A"
    return f"{raw_value:.1f}x"


def pct_change(current, previous):
    if current is None or previous in (None, 0):
        return None
    return ((current - previous) / abs(previous)) * 100.0


def quarter_label(year, quarter):
    return f"Q{quarter} {year}"


def build_trend_note(rows, index):
    row = rows[index]
    notes = []

    if row.get("revenueYoY") is not None:
        direction = "up" if row["revenueYoY"] >= 0 else "down"
        notes.append(f"Revenue {direction} {abs(row['revenueYoY']):.1f}% YoY")
    if row.get("epsYoY") is not None:
        direction = "up" if row["epsYoY"] >= 0 else "down"
        notes.append(f"EPS {direction} {abs(row['epsYoY']):.1f}% YoY")

    if index > 0:
        prev = rows[index - 1]
        if row.get("pe") is not None and prev.get("pe") is not None:
            if row["pe"] < prev["pe"] - 0.25:
                notes.append("P/E compressed vs prior quarter")
            elif row["pe"] > prev["pe"] + 0.25:
                notes.append("P/E expanded vs prior quarter")

        if row.get("closePrice") is not None and prev.get("closePrice") is not None:
            price_delta = row["closePrice"] - prev["closePrice"]
            if price_delta < 0 and row.get("eps") is not None and prev.get("eps") is not None and row["eps"] > prev["eps"]:
                notes.append("Price fell while EPS rose")
            elif price_delta > 0 and row.get("eps") is not None and prev.get("eps") is not None and row["eps"] > prev["eps"]:
                notes.append("Price and EPS both improved")

    return "; ".join(notes[:3]) or "Baseline quarter in view"


def build_financial_rows(symbol: str):
    company_facts, sec_info = get_company_facts(symbol)
    if not company_facts:
        return None, sec_info

    revenue_series = extract_metric_series(
        company_facts,
        [
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
            "SalesRevenueNet",
        ],
    )
    eps_series = extract_metric_series(
        company_facts,
        [
            "EarningsPerShareDiluted",
            "DilutedEarningsPerShare",
            "EarningsPerShareBasicAndDiluted",
        ],
    )

    revenue_by_frame = {item["frame"]: item for item in revenue_series}
    eps_by_frame = {item["frame"]: item for item in eps_series}
    common_frames = sorted(set(revenue_by_frame) & set(eps_by_frame), key=lambda frame: (int(frame[2:6]), int(frame[-1])))
    common_frames = common_frames[-MAX_DEEP_DIVE_ROWS:]

    prices = fetch_price_series(symbol)
    rows = []
    for frame in common_frames:
        revenue_item = revenue_by_frame[frame]
        eps_item = eps_by_frame[frame]
        end_date = eps_item["periodEnd"]
        filed_date = max(revenue_item.get("filed") or "", eps_item.get("filed") or "")
        close_price = closing_price_on_or_before(prices, end_date)
        pre_report_day = trading_day_before(prices, filed_date) if filed_date else None
        report_day = trading_day_on_or_after(prices, filed_date) if filed_date else None
        rows.append(
            {
                "frame": frame,
                "label": quarter_label(eps_item["year"], eps_item["quarter"]),
                "periodEnd": end_date,
                "filedDate": filed_date,
                "revenueRaw": revenue_item["value"],
                "eps": float(eps_item["value"]) if eps_item["value"] is not None else None,
                "closePrice": close_price,
                "preReportPrice": pre_report_day["close"] if pre_report_day else None,
                "reportOpenPrice": report_day.get("open") if report_day else None,
                "reportClosePrice": report_day.get("close") if report_day else None,
            }
        )

    for idx, row in enumerate(rows):
        if idx >= 3:
            trailing_eps = sum(item["eps"] for item in rows[idx - 3 : idx + 1] if item["eps"] is not None)
            row["ttmEps"] = trailing_eps
            row["pe"] = (row["closePrice"] / trailing_eps) if row.get("closePrice") and trailing_eps else None
            row["preReportPe"] = (row["preReportPrice"] / trailing_eps) if row.get("preReportPrice") and trailing_eps else None
            row["reportClosePe"] = (row["reportClosePrice"] / trailing_eps) if row.get("reportClosePrice") and trailing_eps else None
        else:
            row["ttmEps"] = None
            row["pe"] = None
            row["preReportPe"] = None
            row["reportClosePe"] = None

        prev_year_idx = idx - 4
        if prev_year_idx >= 0:
            row["revenueYoY"] = pct_change(row["revenueRaw"], rows[prev_year_idx]["revenueRaw"])
            row["epsYoY"] = pct_change(row["eps"], rows[prev_year_idx]["eps"])
        else:
            row["revenueYoY"] = None
            row["epsYoY"] = None

    for idx in range(len(rows)):
        rows[idx]["trendNote"] = build_trend_note(rows, idx)
        rows[idx]["revenue"] = format_billions(rows[idx]["revenueRaw"])
        rows[idx]["epsDisplay"] = format_eps(rows[idx]["eps"])
        rows[idx]["closePriceDisplay"] = format_price(rows[idx]["closePrice"])
        rows[idx]["peDisplay"] = format_pe(rows[idx]["pe"])

    return rows, sec_info


def build_deep_dive_metrics(rows):
    if not rows:
        return {}
    latest = rows[-1]
    first = rows[0]
    pe_values = [row["pe"] for row in rows if row.get("pe") is not None]
    metrics = {
        "latestQuarter": latest["label"],
        "latestRevenue": latest["revenue"],
        "latestEps": latest["epsDisplay"],
        "latestPrice": latest["closePriceDisplay"],
        "latestPe": latest["peDisplay"],
        "revenueGrowth8Q": pct_change(latest["revenueRaw"], first["revenueRaw"]),
        "epsGrowth8Q": pct_change(latest["eps"], first["eps"]),
        "peCompression8Q": pct_change(latest["pe"], first["pe"]) if latest.get("pe") and first.get("pe") else None,
        "minPe": min(pe_values) if pe_values else None,
        "maxPe": max(pe_values) if pe_values else None,
    }
    score, label, summary = compute_growth_profile(rows)
    metrics["growthScore"] = score
    metrics["growthLabel"] = label
    metrics["growthSummary"] = summary
    return metrics


def compute_growth_profile(rows):
    if not rows:
        return 5, "Neutral", "Not enough trend data yet to judge whether the business is accelerating or deteriorating."

    latest = rows[-1]
    rev_yoy = latest.get("revenueYoY")
    eps_yoy = latest.get("epsYoY")

    rev_seq = None
    eps_seq = None
    if len(rows) >= 2:
        prev = rows[-2]
        rev_seq = pct_change(latest.get("revenueRaw"), prev.get("revenueRaw"))
        eps_seq = pct_change(latest.get("eps"), prev.get("eps"))

    score = 5.0

    def add_component(value, strong=12, good=5, weak=-5, bad=-12, weight=1.0):
        nonlocal score
        if value is None:
            return
        if value >= strong:
            score += 1.5 * weight
        elif value >= good:
            score += 0.75 * weight
        elif value <= bad:
            score -= 1.5 * weight
        elif value <= weak:
            score -= 0.75 * weight

    add_component(rev_yoy, weight=1.1)
    add_component(eps_yoy, weight=1.4)
    add_component(rev_seq, strong=6, good=2, weak=-2, bad=-6, weight=0.8)
    add_component(eps_seq, strong=8, good=3, weak=-3, bad=-8, weight=1.0)

    score = max(1, min(10, round(score)))

    if score >= 8:
        label = "Excellent"
        summary = "Growth looks strong: both revenue and earnings trends are supporting a healthy expansion profile."
    elif score >= 6:
        label = "Good"
        summary = "Growth looks constructive: the business appears to be improving more than it is deteriorating."
    elif score >= 4:
        label = "Neutral"
        summary = "Growth looks mixed or stable: the company is not clearly accelerating, but it is not obviously breaking down either."
    else:
        label = "Weak"
        summary = "Growth looks weak: recent revenue and/or earnings trends suggest deterioration rather than healthy expansion."

    return score, label, summary


def classify_earnings_reaction(row):
    rev_yoy = row.get("revenueYoY")
    eps_yoy = row.get("epsYoY")

    if rev_yoy is not None and eps_yoy is not None:
        if rev_yoy > 0 and eps_yoy > 0:
            return "Double-positive quarter"
        if rev_yoy > 0 and eps_yoy <= 0:
            return "Beat top line / weak bottom line"
        if rev_yoy <= 0 and eps_yoy > 0:
            return "Miss top line / beat bottom line"
        return "Missed top and bottom line"
    if rev_yoy is not None:
        return "Top-line positive" if rev_yoy > 0 else "Top-line soft"
    if eps_yoy is not None:
        return "Bottom-line positive" if eps_yoy > 0 else "Bottom-line soft"
    return "Limited report history"


def build_earnings_analysis(rows, metrics):
    if not rows:
        return {"summary": "No earnings reaction data available yet.", "events": []}

    events = []
    start_index = max(0, len(rows) - 3)
    for idx in range(len(rows) - 1, start_index - 1, -1):
        row = rows[idx]
        before_report = row.get("preReportPrice")
        report_open = row.get("reportOpenPrice")
        report_close = row.get("reportClosePrice")
        gap_pct = pct_change(report_open, before_report) if report_open is not None and before_report is not None else None
        close_reaction = pct_change(report_close, before_report) if report_close is not None and before_report is not None else None
        pe_reaction = pct_change(row.get("reportClosePe"), row.get("preReportPe")) if row.get("reportClosePe") is not None and row.get("preReportPe") is not None else None

        if gap_pct is None:
            price_label = "No earnings-day reaction history"
        elif gap_pct >= 4:
            price_label = "Gap up"
        elif gap_pct >= 1:
            price_label = "Opened up"
        elif gap_pct <= -4:
            price_label = "Gap down"
        elif gap_pct <= -1:
            price_label = "Opened down"
        else:
            price_label = "Flat open"

        if gap_pct is not None and close_reaction is not None:
            if gap_pct < 0 and close_reaction > 0:
                price_label = "Gap down then reversed up"
            elif gap_pct > 0 and close_reaction < 0:
                price_label = "Gap up then faded"
            elif gap_pct > 0 and close_reaction > gap_pct + 1.0:
                price_label = "Gap up and held"
            elif gap_pct < 0 and close_reaction < gap_pct - 1.0:
                price_label = "Gap down and stayed weak"

        if pe_reaction is None:
            pe_label = "P/E unavailable"
        elif pe_reaction >= 3:
            pe_label = "P/E expanded"
        elif pe_reaction <= -3:
            pe_label = "P/E compressed"
        else:
            pe_label = "P/E stable"

        events.append(
            {
                "quarter": row.get("label"),
                "reportRead": classify_earnings_reaction(row),
                "priceReactionPct": gap_pct,
                "closeReactionPct": close_reaction,
                "priceReactionLabel": price_label,
                "peReactionPct": pe_reaction,
                "peReactionLabel": pe_label,
            }
        )

    latest_event = events[0] if events else None
    since_last_er = metrics.get("moveVsLastQuarter")
    since_last_er_text = f"{since_last_er:.1f}%" if since_last_er is not None else "N/A"
    current_price = metrics.get("currentPrice", "N/A")
    current_pe = metrics.get("currentPe", "N/A")

    summary = []
    if latest_event:
        latest_line = f"The latest reported quarter ({latest_event['quarter']}) reads as {latest_event['reportRead'].lower()}."
        if latest_event.get("priceReactionPct") is not None:
            latest_line += f" The earnings-day reaction was {latest_event['priceReactionLabel'].lower()} ({latest_event['priceReactionPct']:.1f}% gap/open move"
            if latest_event.get("closeReactionPct") is not None:
                latest_line += f", {latest_event['closeReactionPct']:.1f}% by the close"
            latest_line += ")."
        if latest_event.get("peReactionLabel") and latest_event["peReactionLabel"] != "P/E unavailable":
            latest_line += f" The multiple {latest_event['peReactionLabel'].lower()}."
        summary.append(latest_line)
    summary.append(f"Current price is {current_price}, which is {since_last_er_text} since the last reported quarter-end, with current TTM P/E at {current_pe}.")

    return {"summary": " ".join(summary).strip(), "events": events}


def enrich_live_metrics(metrics, rows, quote):
    if not rows:
        return metrics

    latest = rows[-1]
    current_price = quote.get("currentPrice")
    current_pe = None
    if current_price is not None and latest.get("ttmEps"):
        current_pe = current_price / latest["ttmEps"]

    last_quarter_price = latest.get("closePrice")
    move_vs_last_quarter = pct_change(current_price, last_quarter_price) if current_price is not None and last_quarter_price else None

    metrics.update(
        {
            "currentPrice": format_price(current_price),
            "currentPriceRaw": current_price,
            "currentPe": format_pe(current_pe),
            "currentPeRaw": current_pe,
            "lastQuarterPrice": latest.get("closePriceDisplay"),
            "lastQuarterPe": latest.get("peDisplay"),
            "moveVsLastQuarter": move_vs_last_quarter,
        }
    )
    return metrics


def parse_json_object(text):
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.DOTALL)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        cleaned = cleaned[start : end + 1]
    return json.loads(cleaned)


def build_fallback_sections(company, metrics):
    name = company.get("longName") or company.get("shortName") or company.get("symbol") or "This company"
    sector = company.get("sector") or "its sector"
    industry = company.get("industry") or "its niche"
    latest_rev = metrics.get("latestRevenue", "N/A")
    latest_eps = metrics.get("latestEps", "N/A")
    latest_pe = metrics.get("latestPe", "N/A")
    revenue_growth = metrics.get("revenueGrowth8Q")
    eps_growth = metrics.get("epsGrowth8Q")
    pe_change = metrics.get("peCompression8Q")
    return {
        "business": f"{name} operates in {sector}, primarily in {industry}. It is the operating business behind the ticker and the main source of the financial history shown below.",
        "money": f"It makes money by selling products and services within its core industry. The latest quarter in this view shows revenue of {latest_rev} and diluted EPS of {latest_eps}.",
        "moat": "The moat depends on brand, scale, switching costs, data, distribution, or regulated positioning. The financial view should be read alongside competitive pressure and customer concentration.",
        "financialPerformance": f"Over the displayed eight-quarter window, revenue growth is {revenue_growth:.1f}% and EPS growth is {eps_growth:.1f}%." if revenue_growth is not None and eps_growth is not None else "The table shows the company’s quarter-by-quarter revenue, EPS, quarter-end price, and trailing P/E trend.",
        "earningsAnalysis": (metrics.get("earningsAnalysis") or {}).get("summary", "Earnings reaction analysis is unavailable."),
        "valuationFrame": "Use the P/E compression framework only when earnings are still growing while the valuation multiple has materially fallen. If fundamentals are weakening alongside the lower multiple, the setup may be a value trap rather than a healthy compression story.",
        "fairValue": "Fair value is not the same thing as the last traded price. If the market is changing the narrative around the company or the whole sector, perceived value can be re-rated higher or lower before the fundamentals fully catch up.",
        "projectionRisk": f"Quarter-end valuation was {metrics.get('latestPrice', 'N/A')} at {latest_pe}, while the live market is now around {metrics.get('currentPrice', 'N/A')} at {metrics.get('currentPe', 'N/A')}. " + (f"P/E moved {pe_change:.1f}% across the window, which can indicate multiple compression or expansion beyond earnings alone." if pe_change is not None else "Future upside depends on earnings durability, guidance, and market sentiment."),
        "headlineContext": "Latest headlines should be used as context to judge whether the multiple change is driven by temporary sentiment, a genuine business slowdown, or a possible re-rating setup.",
    }


def build_pe_compression_frame(metrics):
    rev = metrics.get("revenueGrowth8Q")
    eps = metrics.get("epsGrowth8Q")
    pe = metrics.get("peCompression8Q")
    latest_pe = metrics.get("latestPe", "N/A")

    if pe is None or eps is None:
        return {
            "applicable": False,
            "stage": "Insufficient data",
            "summary": "There is not enough trailing valuation history to place this company in the PE-compression cycle yet.",
        }

    if pe <= -20 and eps > 0 and (rev is None or rev >= 0):
        if pe <= -35 and eps > 10:
            return {
                "applicable": True,
                "stage": "Stage 4 to Stage 6 setup",
                "summary": f"Earnings are still growing while the multiple has compressed materially. That fits the classic PE compression framework and may be approaching a re-rating setup if sentiment stabilizes. In fair-value terms, the market may be reassessing what this business is worth rather than simply marking it at a static cheap price. Current TTM P/E is {latest_pe}.",
            }
        return {
            "applicable": True,
            "stage": "Stage 4 PE compression",
            "summary": f"The business still appears to be growing, but the market has taken the valuation multiple down. That is consistent with PE compression rather than a broken earnings story. Fair value may be higher than the current price if the market is temporarily underpricing quality or durability. Current TTM P/E is {latest_pe}.",
        }

    if pe < 0 and eps > 0:
        return {
            "applicable": True,
            "stage": "Late Stage 3 / early Stage 4",
            "summary": f"Growth looks more normal than explosive, and the multiple is easing lower. That fits the transition from stretched expectations toward a more mature valuation range. Fair value is likely being renegotiated by the market as the story shifts from hypergrowth to durable compounder. Current TTM P/E is {latest_pe}.",
        }

    if pe >= 10 and eps > 0:
        return {
            "applicable": True,
            "stage": "Stage 2 or Stage 6 re-rating",
            "summary": f"The multiple has expanded while earnings are still improving. That can reflect renewed optimism, stronger momentum, or an early re-rating from a previously compressed base. In fair-value terms, the market may be lifting the value area because the narrative, sector importance, or quality perception has changed. Current TTM P/E is {latest_pe}.",
        }

    return {
        "applicable": False,
        "stage": "No clean PE compression signal",
        "summary": "The multiple change does not cleanly fit the healthy PE compression playbook. This may be a mixed setup where business quality, guidance, or sentiment need closer inspection.",
    }


def infer_cycle_stage(metrics, has_financial_history=True):
    if not has_financial_history:
        return {
            "stage": "Stage 1 to Stage 2 perception-driven",
            "summary": "The company appears to be in an earlier perception-driven phase where narrative, strategic relevance, and future optionality matter more than stable trailing earnings.",
        }

    frame = metrics.get("peCompressionFrame") or {}
    if frame.get("applicable"):
        return {"stage": frame.get("stage"), "summary": frame.get("summary")}

    pe = metrics.get("peCompression8Q")
    eps = metrics.get("epsGrowth8Q")
    if pe is not None and eps is not None and pe >= 10 and eps > 0:
        return {
            "stage": "Stage 2 or Stage 6 re-rating",
            "summary": "The market is expanding the multiple while earnings still improve, which suggests either stretched expectations or a genuine re-rating from a depressed base.",
        }
    if eps is not None and eps > 0:
        return {
            "stage": "Stage 5 mature compounder",
            "summary": "The company looks more like a mature compounder where value depends on durability, consistency, and the possibility of future re-rating rather than hypergrowth expectations.",
        }
    return {
        "stage": "Mixed / unclear stage",
        "summary": "The available data does not place the company cleanly in one valuation stage, so narrative analysis should carry more weight than the current multiple alone.",
    }


def analyze_deep_dive_with_gemini(company, rows, metrics, headlines_context="", model=DEEP_DIVE_MODEL, llm_mode="auto"):
    rows_payload = [
        {
            "quarter": row["label"],
            "revenue": row["revenue"],
            "dilutedEPS": row["epsDisplay"],
            "ttmPE": row["peDisplay"],
            "closePrice": row["closePriceDisplay"],
            "trendNote": row["trendNote"],
        }
        for row in rows
    ]
    prompt = (
        "You are a disciplined equity research analyst.\n"
        "Use the company metadata and 8-quarter table below to produce a JSON object with exactly these keys:\n"
        'business, money, moat, financialPerformance, earningsAnalysis, valuationFrame, fairValue, projectionRisk, headlineContext.\n'
        "Each value should be 2-4 concise sentences, investor-friendly, plain English, and grounded in the supplied data.\n"
        "Focus on business model clarity, how the company earns revenue, moat vs competition, interpretation of revenue/EPS/price/P-E trends, latest headline context, and balanced projection plus risk.\n"
        "For earningsAnalysis, review the supplied earnings reaction summary for the past 3 reported quarters. Treat double-positive revenue/EPS growth as a heuristic analog to a double beat, top-line positive with weak EPS as beat top line / weak bottom line, and negative revenue/EPS trends as miss-like. Explain whether price moved up/down and whether P/E expanded or compressed.\n"
        "When the data shows earnings are still growing while P/E has fallen, explicitly use the PE compression framework:\n"
        "1) growth at all costs, 2) stretched expectations, 3) normalizing growth, 4) PE compression, 5) mature compounder, 6) re-rating/bounce-back.\n"
        "If the setup does not truly fit PE compression, say that clearly and do not force it.\n"
        "Distinguish value play vs value trap where relevant.\n"
        "Use this fair-value principle: value does not equal the last traded price. Fair value can move when the market re-rates a company or its sector because the narrative, strategic importance, customer set, capital flows, or competitive positioning changes.\n"
        "Treat re-rating as a shift in perceived value, not just a stock going up. For example, a speculative company can become an infrastructure or strategic-capability story, which changes the market's value area even before revenue fully catches up.\n"
        "In the fairValue field, explain whether current price may be below, near, or above fair value under the current narrative, and how that fair value could move if the sector is being re-rated.\n"
        "In the projectionRisk field, explicitly compare quarter-end price/P-E to current price/current P-E when live metrics are available, and explain whether the market is already paying a higher multiple after the event.\n"
        "Do not include markdown fences. Return valid JSON only.\n\n"
        f"Company:\n{json.dumps(company, indent=2)}\n\n"
        f"Metrics:\n{json.dumps(metrics, indent=2, default=str)}\n\n"
        f"Quarter table:\n{json.dumps(rows_payload, indent=2)}\n\n"
        f"Latest headlines context:\n{headlines_context or 'No latest headlines available.'}"
    )

    try:
        llm_result = call_llm_with_fallback(prompt, gemini_model=model, openai_model=DEFAULT_OPENAI_MODEL, mode=llm_mode)
        if not llm_result.get("ok"):
            raise RuntimeError(llm_result.get("error", "LLM unavailable"))
        text = llm_result.get("text", "")
        sections = parse_json_object(text)
        return {
            "ok": True,
            "provider": llm_result.get("provider"),
            "model": llm_result.get("model"),
            "sections": sections,
            "error": "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "provider": "fallback",
            "model": model,
            "sections": build_fallback_sections(company, metrics),
            "error": f"LLM ERROR: {exc}",
        }


def build_deep_dive(query, llm_mode="auto"):
    llm_mode = normalize_llm_mode(llm_mode)
    cache_key = ("deep-dive-full", (query or "").strip().upper(), llm_mode)
    cached = cache_get(cache_key)
    if cached:
        return cached
    disk_cached = disk_cache_get(cache_key, ttl_seconds=FULL_ANALYSIS_TTL_SECONDS)
    if disk_cached:
        return cache_set(cache_key, disk_cached, ttl_seconds=FULL_ANALYSIS_TTL_SECONDS)

    base_payload = build_deep_dive_base(query)
    if not base_payload.get("ok"):
        return base_payload

    company_payload = base_payload["company"]
    metrics = base_payload["metrics"]
    rows = list(reversed(base_payload["rows"]))
    latest_headlines = base_payload.get("latestHeadlines", [])
    latest_headline_lines = [
        f"- [{item['source']}] {item['title']} (SIG {item['signalScore']})" for item in latest_headlines
    ]
    headlines_context = "\n".join(latest_headline_lines)
    narrative = analyze_deep_dive_with_gemini(company_payload, rows, metrics, headlines_context=headlines_context, llm_mode=llm_mode)
    payload = {
        "ok": True,
        "company": company_payload,
        "metrics": metrics,
        "rows": base_payload["rows"],
        "latestHeadlines": latest_headlines,
        "headlineAnalysis": base_payload.get("headlineAnalysis", {}),
        "narrative": narrative,
    }
    disk_cache_set(cache_key, payload)
    return cache_set(cache_key, payload, ttl_seconds=FULL_ANALYSIS_TTL_SECONDS)


def build_deep_dive_base(query):
    cache_key = ("deep-dive-base", (query or "").strip().upper())
    cached = cache_get(cache_key)
    if cached:
        return cached
    disk_cached = disk_cache_get(cache_key, ttl_seconds=BASE_ANALYSIS_TTL_SECONDS)
    if disk_cached:
        return cache_set(cache_key, disk_cached, ttl_seconds=BASE_ANALYSIS_TTL_SECONDS)

    company = resolve_equity_query(query or "")
    if not company:
        return {
            "ok": False,
            "error": "Enter a valid ticker or company query to build the financial deep dive.",
        }

    with ThreadPoolExecutor(max_workers=3) as executor:
        rows_future = executor.submit(build_financial_rows, company["symbol"])
        quote_future = executor.submit(fetch_current_quote, company["symbol"])
        headlines_future = executor.submit(scan_news, company["symbol"], True, False)
        rows, sec_info = rows_future.result()
        quote = quote_future.result()
        headline_scan = headlines_future.result()

    if not rows:
        return {
            "ok": False,
            "error": f"No usable SEC quarter data was found for {company['symbol']}.",
        }

    company_payload = {
        "symbol": company["symbol"],
        "shortName": company.get("shortName") or company["symbol"],
        "longName": company.get("longName") or sec_info.get("title") if sec_info else company["symbol"],
        "sector": company.get("sector") or "",
        "industry": company.get("industry") or "",
        "exchange": company.get("exchange") or "",
    }
    metrics = build_deep_dive_metrics(rows)
    metrics["peCompressionFrame"] = build_pe_compression_frame(metrics)
    metrics = enrich_live_metrics(metrics, rows, quote)
    metrics["earningsAnalysis"] = build_earnings_analysis(rows, metrics)
    payload = {
        "ok": True,
        "company": company_payload,
        "metrics": metrics,
        "rows": rows[::-1],
        "latestHeadlines": headline_scan.get("items", [])[:5],
        "headlineAnalysis": headline_scan.get("analysis", {}),
    }
    disk_cache_set(cache_key, payload)
    return cache_set(cache_key, payload, ttl_seconds=BASE_ANALYSIS_TTL_SECONDS)


def build_narrative_shift_fallback(company, stage_info, latest_headlines):
    headline_titles = [item["title"] for item in latest_headlines[:5]]
    headline_blob = " ".join(headline_titles).lower()
    symbol = company.get("symbol", "")
    name = company.get("longName") or company.get("shortName") or symbol or "The company"
    sector = company.get("sector") or "its sector"
    industry = company.get("industry") or "its industry"

    def has(*terms):
        return any(term in headline_blob for term in terms)

    event_text = " / ".join(headline_titles[:3]) if headline_titles else "No fresh event headlines were available."

    if stage_info.get("stage", "").startswith("Stage 1 to Stage 2"):
        old_narrative = (
            f"{name} was mostly being treated as a perception-driven story in {sector}, where optionality and narrative mattered more than stable operating proof."
        )
    elif "PE compression" in stage_info.get("stage", "") or "Stage 4" in stage_info.get("stage", ""):
        old_narrative = (
            f"{name} looked like a compressed-multiple name: the market respected the business enough to follow it, but was not willing to pay the old valuation because expectations had cooled."
        )
    elif "mature compounder" in stage_info.get("stage", "").lower():
        old_narrative = (
            f"{name} was being viewed more like a mature compounder in {industry}: durable, but not obviously on the verge of a major narrative reset."
        )
    else:
        old_narrative = (
            f"The prior narrative around {name} was anchored to its existing category, current execution, and the value area investors were already using."
        )

    possible_new_parts = []
    if has("nvidia", "nvda", "ai", "ai factories", "data center", "infrastructure"):
        possible_new_parts.append(
            "the market may be recasting it as an AI infrastructure or strategic-capacity story rather than a niche or speculative name"
        )
    if has("los alamos", "department of energy", "air force", "lab", "government"):
        possible_new_parts.append(
            "institutional or government validation may be shifting it from concept-stage speculation toward strategic relevance"
        )
    if has("earnings", "guidance", "forecast", "profit", "revenue"):
        possible_new_parts.append(
            "the event may be moving the story from disappointment or skepticism toward operational improvement and renewed credibility"
        )
    if has("partnership", "join forces", "collaboration", "agreement", "deal"):
        possible_new_parts.append(
            "partnership-driven validation may be changing how the market thinks about future demand, distribution, or ecosystem relevance"
        )
    if has("nuclear", "energy", "power", "fuel"):
        possible_new_parts.append(
            "the company may be getting pulled into a broader energy-security or power-availability narrative instead of being judged only on current fundamentals"
        )

    if not possible_new_parts:
        possible_new = (
            f"The main question is whether the latest event changes how investors classify {name}. If the market starts using a different framework for the company or the sector, a re-rating becomes more plausible."
        )
    else:
        possible_new = f"For {name}, " + "; ".join(possible_new_parts[:3]) + "."

    if has("nvidia", "ai", "data center", "los alamos", "government", "partnership"):
        fair_value_change = (
            f"Fair value may be moving because the market could be assigning {name} to a more important narrative bucket. In a Jim Dalton sense, price is only today’s auction, while value migrates if the market starts treating the company as strategically more relevant."
        )
        rerating_verdict = (
            "This looks more like a possible re-rating than a simple price reaction, because the headlines point to a category shift rather than just a one-off event."
        )
    elif has("earnings", "guidance", "profit", "forecast"):
        fair_value_change = (
            f"Fair value may shift if the event changes confidence in future earning power, margins, or product competitiveness. The key is whether the market now sees a more durable business trajectory, not just a single strong print."
        )
        rerating_verdict = (
            "This is a possible re-rating if the event resets expectations and the market begins valuing future execution differently. Otherwise it may fade into a normal post-event reaction."
        )
    else:
        fair_value_change = (
            "Fair value only changes if the market truly updates its view of the company’s future role, earnings power, or strategic importance. A headline without narrative migration is just a reaction, not a new value area."
        )
        rerating_verdict = (
            "This is still inconclusive. The move could become a re-rating, but it needs evidence that the market is changing the story rather than merely trading the event."
        )

    return {
        "currentStage": stage_info.get("stage", "Unclear"),
        "oldNarrative": old_narrative,
        "event": event_text,
        "possibleNewNarrative": possible_new,
        "fairValueChange": fair_value_change,
        "reratingVerdict": rerating_verdict,
        "confirms": f"Follow-through would mean the next headlines, guidance, or partnerships keep reinforcing the new story for {name}, and the stock holds a higher value area instead of giving back the move quickly.",
        "invalidates": f"The narrative is invalidated if the move fades, the supposed catalyst does not alter real positioning, or investors fall back to the old framework for {name} and {sector}.",
    }


def analyze_narrative_shift_with_gemini(company, metrics, stage_info, latest_headlines, headline_analysis, model=DEEP_DIVE_MODEL, llm_mode="auto"):
    latest_headline_lines = [
        {
            "source": item.get("source"),
            "title": item.get("title"),
            "signalScore": item.get("signalScore"),
            "link": item.get("link"),
        }
        for item in latest_headlines[:5]
    ]

    prompt = (
        "You are an event-driven market analyst using a 6-stage growth and valuation framework.\n"
        "Decide whether the latest event or headline cluster may change the narrative and trigger a re-rating.\n"
        "Important principle: value does not equal current price. Fair value can migrate when the market reclassifies the company or sector.\n"
        "Early stages (1 and 2) are heavily driven by perception and narrative. Later stages can still re-rate when a new event changes how the market sees the business.\n"
        "Return valid JSON only with exactly these keys:\n"
        "currentStage, oldNarrative, event, possibleNewNarrative, fairValueChange, reratingVerdict, confirms, invalidates.\n"
        "Keep each field concise but concrete. Mention whether this looks like noise, temporary reaction, possible re-rating, or confirmed value migration.\n"
        "Do not just restate a generic framework. Compare the old narrative to a possible new narrative created by the actual headlines.\n"
        "If the headlines suggest a category shift, say exactly what the old category was and what the new category may be.\n"
        "Example: not just 'price moved on news', but 'the market may be moving from broken legacy chipmaker to AI-capable turnaround' or 'from speculative nuclear concept to AI infrastructure energy enabler.'\n\n"
        f"Company:\n{json.dumps(company, indent=2)}\n\n"
        f"Stage info:\n{json.dumps(stage_info, indent=2)}\n\n"
        f"Metrics:\n{json.dumps(metrics, indent=2, default=str)}\n\n"
        f"Latest headlines:\n{json.dumps(latest_headline_lines, indent=2)}\n\n"
        f"Latest market insight summary:\n{json.dumps(headline_analysis, indent=2, default=str)}"
    )

    try:
        llm_result = call_llm_with_fallback(prompt, gemini_model=model, openai_model=DEFAULT_OPENAI_MODEL, mode=llm_mode)
        if not llm_result.get("ok"):
            raise RuntimeError(llm_result.get("error", "LLM unavailable"))
        text = llm_result.get("text", "")
        sections = parse_json_object(text)
        return {
            "ok": True,
            "provider": llm_result.get("provider"),
            "model": llm_result.get("model"),
            "sections": sections,
            "error": "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "provider": "fallback",
            "model": model,
            "sections": build_narrative_shift_fallback(company, stage_info, latest_headlines),
            "error": f"LLM ERROR: {exc}",
        }


def build_narrative_shift(query, llm_mode="auto"):
    llm_mode = normalize_llm_mode(llm_mode)
    cache_key = ("narrative-shift-full", (query or "").strip().upper(), llm_mode)
    cached = cache_get(cache_key)
    if cached:
        return cached
    disk_cached = disk_cache_get(cache_key, ttl_seconds=FULL_ANALYSIS_TTL_SECONDS)
    if disk_cached:
        return cache_set(cache_key, disk_cached, ttl_seconds=FULL_ANALYSIS_TTL_SECONDS)

    base_payload = build_narrative_shift_base(query)
    if not base_payload.get("ok"):
        return base_payload

    analysis = analyze_narrative_shift_with_gemini(
        base_payload["company"],
        base_payload["metrics"],
        base_payload["stageInfo"],
        base_payload["latestHeadlines"],
        base_payload.get("headlineAnalysis", {}),
        llm_mode=llm_mode,
    )
    payload = {
        "ok": True,
        "company": base_payload["company"],
        "stageInfo": base_payload["stageInfo"],
        "metrics": base_payload["metrics"],
        "latestHeadlines": base_payload["latestHeadlines"],
        "headlineAnalysis": base_payload.get("headlineAnalysis", {}),
        "analysis": analysis,
    }
    disk_cache_set(cache_key, payload)
    return cache_set(cache_key, payload, ttl_seconds=FULL_ANALYSIS_TTL_SECONDS)


def build_narrative_shift_base(query):
    cache_key = ("narrative-shift-base", (query or "").strip().upper())
    cached = cache_get(cache_key)
    if cached:
        return cached
    disk_cached = disk_cache_get(cache_key, ttl_seconds=BASE_ANALYSIS_TTL_SECONDS)
    if disk_cached:
        return cache_set(cache_key, disk_cached, ttl_seconds=BASE_ANALYSIS_TTL_SECONDS)

    company = resolve_equity_query(query or "")
    if not company:
        return {
            "ok": False,
            "error": "Enter a valid ticker or company query to analyze narrative shift.",
        }

    with ThreadPoolExecutor(max_workers=3) as executor:
        rows_future = executor.submit(build_financial_rows, company["symbol"])
        quote_future = executor.submit(fetch_current_quote, company["symbol"])
        headlines_future = executor.submit(scan_news, company["symbol"], True, False)
        rows, sec_info = rows_future.result()
        quote = quote_future.result()
        headline_scan = headlines_future.result()

    has_financial_history = bool(rows)
    metrics = build_deep_dive_metrics(rows) if rows else {}
    if rows:
        metrics["peCompressionFrame"] = build_pe_compression_frame(metrics)
        metrics = enrich_live_metrics(metrics, rows, quote)
    stage_info = infer_cycle_stage(metrics, has_financial_history=has_financial_history)

    company_payload = {
        "symbol": company["symbol"],
        "shortName": company.get("shortName") or company["symbol"],
        "longName": company.get("longName") or sec_info.get("title") if sec_info else company["symbol"],
        "sector": company.get("sector") or "",
        "industry": company.get("industry") or "",
        "exchange": company.get("exchange") or "",
    }

    payload = {
        "ok": True,
        "company": company_payload,
        "stageInfo": stage_info,
        "metrics": metrics,
        "latestHeadlines": headline_scan.get("items", [])[:5],
        "headlineAnalysis": headline_scan.get("analysis", {}),
    }
    disk_cache_set(cache_key, payload)
    return cache_set(cache_key, payload, ttl_seconds=BASE_ANALYSIS_TTL_SECONDS)


def scan_news(query=None, focus_mode=True, include_analysis=True, llm_mode="auto"):
    normalized_query = (query or "").strip().upper()
    llm_mode = normalize_llm_mode(llm_mode)
    cache_key = ("scan", normalized_query, bool(focus_mode), bool(include_analysis), llm_mode)
    scan_ttl = GLOBAL_INSIGHT_TTL_SECONDS if include_analysis and not normalized_query else INSIGHT_TTL_SECONDS if include_analysis else SCAN_TTL_SECONDS
    cached = cache_get(cache_key)
    if cached:
        return cached
    disk_cached = disk_cache_get(cache_key, ttl_seconds=scan_ttl)
    if disk_cached:
        return cache_set(cache_key, disk_cached, ttl_seconds=scan_ttl)

    base_cache_key = ("scan-base", normalized_query, bool(focus_mode))
    base_cached = cache_get(base_cache_key)
    if not base_cached:
        disk_base_cached = disk_cache_get(base_cache_key, ttl_seconds=SCAN_TTL_SECONDS)
        if disk_base_cached:
            base_cached = cache_set(base_cache_key, disk_base_cached, ttl_seconds=SCAN_TTL_SECONDS)

    if base_cached:
        filtered = list(base_cached.get("filtered", []))
        visible_items = list(base_cached.get("items", []))
        source_errors = list(base_cached.get("sourceErrors", []))
        sources = list(base_cached.get("sources", []))
    else:
        sources = build_sources(query)
        all_items = []
        source_errors = []

        with ThreadPoolExecutor(max_workers=min(5, len(sources) or 1)) as executor:
            futures = {executor.submit(fetch_text, source["url"]): source for source in sources}
            for future in as_completed(futures):
                source = futures[future]
                try:
                    raw = future.result()
                    all_items.extend(parse_rss(raw, source["name"]))
                except Exception as exc:
                    source_errors.append({"source": source["name"], "error": str(exc)})

        processed = dedupe(all_items)
        filtered = apply_filtering(processed, query=query, focus_mode=focus_mode)
        visible_items = filtered[:SHOW_TOP_STORIES]

        base_payload = {
            "query": query or "",
            "focusMode": focus_mode,
            "totalItems": len(filtered),
            "shownItems": len(visible_items),
            "items": visible_items,
            "filtered": filtered,
            "sourceErrors": source_errors,
            "sources": [source["name"] for source in sources],
        }
        disk_cache_set(base_cache_key, base_payload)
        cache_set(base_cache_key, base_payload, ttl_seconds=SCAN_TTL_SECONDS)

    filtered = list(filtered)
    visible_items = list(visible_items)

    analysis = {
        "ok": False,
        "provider": "none",
        "model": "",
        "text": "",
        "error": "AI analysis skipped.",
    }
    if include_analysis and ENABLE_LLM_ANALYSIS and visible_items:
        headlines_context = build_analysis_payload(visible_items, query=query, focus_mode=focus_mode)
        analysis = analyze_with_llm(headlines_context, query=query, focus_mode=focus_mode, llm_mode=llm_mode)

    payload = {
        "query": query or "",
        "focusMode": focus_mode,
        "llmMode": llm_mode,
        "provider": analysis.get("provider", "none"),
        "analysis": analysis,
        "totalItems": len(filtered),
        "shownItems": len(visible_items),
        "items": visible_items,
        "sourceErrors": source_errors,
        "sources": sources if sources and isinstance(sources[0], str) else [source["name"] for source in sources],
    }
    disk_cache_set(cache_key, payload)
    return cache_set(cache_key, payload, ttl_seconds=scan_ttl)


class NewsTerminalHandler(BaseHTTPRequestHandler):
    server_version = "NewsTerminal/1.0"

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            return self.serve_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        if path == "/static/styles.css":
            return self.serve_file(STATIC_DIR / "styles.css", "text/css; charset=utf-8")
        if path == "/static/app.js":
            return self.serve_file(STATIC_DIR / "app.js", "application/javascript; charset=utf-8")
        if path == "/api/scan":
            params = parse_qs(parsed.query)
            query = (params.get("query", [""])[0] or "").strip() or None
            focus_mode = params.get("focus", ["1"])[0] != "0"
            include_analysis = params.get("analysis", ["1"])[0] != "0"
            llm_mode = normalize_llm_mode(params.get("llm", ["auto"])[0])
            payload = scan_news(query=query, focus_mode=focus_mode, include_analysis=include_analysis, llm_mode=llm_mode)
            return self.send_json(payload)
        if path == "/api/deep-dive":
            params = parse_qs(parsed.query)
            query = (params.get("query", [""])[0] or "").strip() or None
            llm_mode = normalize_llm_mode(params.get("llm", ["auto"])[0])
            payload = build_deep_dive(query, llm_mode=llm_mode)
            return self.send_json(payload)
        if path == "/api/deep-dive-base":
            params = parse_qs(parsed.query)
            query = (params.get("query", [""])[0] or "").strip() or None
            payload = build_deep_dive_base(query)
            return self.send_json(payload)
        if path == "/api/narrative-shift":
            params = parse_qs(parsed.query)
            query = (params.get("query", [""])[0] or "").strip() or None
            llm_mode = normalize_llm_mode(params.get("llm", ["auto"])[0])
            payload = build_narrative_shift(query, llm_mode=llm_mode)
            return self.send_json(payload)
        if path == "/api/narrative-shift-base":
            params = parse_qs(parsed.query)
            query = (params.get("query", [""])[0] or "").strip() or None
            payload = build_narrative_shift_base(query)
            return self.send_json(payload)

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def serve_file(self, path: Path, content_type: str):
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "Missing file")
            return

        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload, status=HTTPStatus.OK):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format_string, *args):
        return


def main():
    load_disk_cache()
    host = os.getenv("NEWS_TERMINAL_HOST", "0.0.0.0")
    port = int(os.getenv("NEWS_TERMINAL_PORT", "8000"))
    httpd = ThreadingHTTPServer((host, port), NewsTerminalHandler)
    print(f"News terminal running at http://{host}:{port}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
