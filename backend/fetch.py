"""Fetch FastMotion x Kugoo collab videos and build dashboard data.json.

Reads YOUTUBE_API_KEY from env. Lists all videos on the channel, keeps only
those whose description contains the marker substring (default: "kugoo.ru"),
stores a historical snapshot in SQLite, and writes frontend/data.json with
the aggregated metrics consumed by the dashboard.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
import tempfile
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


CHANNEL_ID = os.environ.get("YOUTUBE_CHANNEL_ID", "UCvy7FIEQYztchmNfCROlgDw")
MARKER = os.environ.get("COLLAB_MARKER", "kugoo.ru").lower()
MIN_PUBLISHED_AT = os.environ.get("COLLAB_SINCE", "2025-04-14T00:00:00Z")

ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = ROOT / "backend"
FRONTEND_DIR = ROOT / "docs"
LOG_DIR = BACKEND_DIR / "logs"
DB_PATH = BACKEND_DIR / "data.db"
JSON_PATH = FRONTEND_DIR / "data.json"

MSK = ZoneInfo("Europe/Moscow")

ISO_DUR_RE = re.compile(
    r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$"
)

URL_RE = re.compile(r"https?://|www\.|\.ru\b|\.com\b|t\.me", re.IGNORECASE)
WORD_RE = re.compile(r"[\wёЁ]+", re.UNICODE)

POSITIVE_WORDS = {
    "топ", "огонь", "круто", "крутой", "крутое", "классно", "класс",
    "отлично", "отличный", "отличное", "отличная", "спасибо", "благодарю",
    "респект", "красава", "красавчик", "красавчики", "советую", "рекомендую",
    "лучший", "лучшее", "лучшая", "лучшие", "супер", "шикарно", "шикарный",
    "годно", "годный", "кайф", "кайфово", "обожаю", "нравится", "нравиться",
    "понравилось", "понравился", "понравилась", "полезно", "полезный",
    "полезная", "информативно", "информативный", "мощно", "мощный",
    "мощная", "толково", "толковый", "молодец", "молодцы", "молодца",
    "браво", "ждем", "ждём", "поддержка", "качество", "качественно",
    "качественный", "доволен", "довольна", "довольны", "топчик", "годнота",
    "топово", "имба", "имбовый", "огнище", "великолепно", "великолепный",
    "обзорчик", "контент",
}

POSITIVE_PHRASES = (
    "лайк подписка",
    "лайк за",
    "спасибо за",
    "благодарю за",
    "уважаю",
    "красавчик",
    "очень круто",
    "очень полезно",
    "очень нравится",
    "топ контент",
)

POSITIVE_EMOJI = "🔥👍❤️🧡💛💚💙💜🤍💯💪🤩😍👌✊🙌⚡✨🥰😎😻⭐"

NEGATIVE_WORDS = {
    "говно", "отстой", "развод", "обман", "обманули", "хуже", "плохо",
    "плохой", "плохая", "мусор", "барахло", "дерьмо", "фигня", "херня",
    "развели", "кинули", "бесит", "ненавижу", "разочарован", "разочарование",
    "тухло", "тухлый", "посредственно", "ужасно", "ужас", "фу", "галимый",
    "галимое", "сломалось", "сломался", "сломалась", "жалею", "пожалел",
    "ломается", "хлам", "выкинул", "выкинула", "беда", "беда", "печаль",
    "халтура", "халтурный", "брак", "бракованный",
}

NEGATIVE_PHRASES = (
    "не советую",
    "не рекомендую",
    "не покупайте",
    "не стоит",
    "лучше не",
    "не работает",
    "не качество",
    "не понравил",
    "пустая трата",
    "выброшенные деньги",
)

NEGATIVE_EMOJI = "💩👎🤮😡🤡🥱😒"

KUGOO_TOKENS = {
    "kugoo", "куго", "кугу", "kuga", "куга",
}
DNS_TOKENS = {
    "dns", "днс", "dns-shop", "днс-шоп",
}
PRODUCT_TOKENS = {
    "самокат", "самокаты", "самокатик", "самокате", "самокаты",
    "электросамокат", "электросамокаты",
    "мотоцикл", "мотоциклы", "электромотоцикл", "электромотоциклы",
    "мопед", "мопеды", "электромопед", "электромопеды",
    "велосипед", "велосипеды", "электровелосипед", "велик",
    "аппарат", "аппараты", "транспорт", "обзор", "обзоры", "обзорчик",
    "тест", "тесты", "тест-драйв",
}

SPAM_PHRASES = (
    "подпишись", "подписывайтесь", "подпишитесь", "канал у меня",
    "мой канал", "заходи ко мне", "посмотри мои", "посмотрите мои",
    "промокод", "телеграм", "telegram", "тг канал", "тг-канал",
    "пишите в лс", "пишите в личку", "в директ", "напиши в директ",
    "бонус по промокоду", "по промокоду",
)

# Meta-commentary about the brand deal itself rather than genuine consumer
# feedback. We never want these in either Kugoo or DNS quote block.
META_PHRASES = (
    "реклам",          # реклама, рекламу, рекламируешь, ...
    "проплат",         # проплатили, проплачено, ...
    "проспонсир",      # проспонсировали
    "спонсор",         # спонсор, спонсорство, спонсирует
    "интеграц",        # интеграция, интеграцию
    "коллаб",          # коллаба, коллабы
    "занесл",          # занесли, занесла
    "сколько заплат",
    "сколько отвал",
    "сколько занесл",
    "за рекламу",
    "не бесплатно",
    "купи рекламу",
    "купили рекламу",
    "купил рекламу",
)

REPEAT_CHAR_RE = re.compile(r"(.)\1{3,}", re.UNICODE)
EMOJI_HINT_RE = re.compile(
    r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF]"
)
NEG_BIGRAM_RE = re.compile(
    r"\bне\s+(?:" + "|".join(sorted({
        "круто","классно","советую","рекомендую","нравится","нравиться",
        "понравилось","понравился","понравилась","полезно","полезный",
        "огонь","шикарно","супер","топ","годно","качественно","качественный",
    }, key=len, reverse=True)) + r")\b",
    re.IGNORECASE | re.UNICODE,
)

COMMENT_MIN_LEN = 25
COMMENT_MAX_LEN = 240
COMMENT_MAX_EMOJI = 5
QUOTES_LIMIT = 8
RECENCY_HALFLIFE_DAYS = 90

# Per-brand thresholds. DNS is rarer in this collab's comment pool so we
# accept lower-liked / lower-scored quotes; the require_brand filter still
# keeps quality up.
KUGOO_MIN_LIKES = 3
KUGOO_MIN_SCORE = 2
DNS_MIN_LIKES = 1
DNS_MIN_SCORE = 1

HISTORY_DAYS = 30
HISTORY_POINTS = 100
TOP_ER_LIMIT = 3
SNAPSHOT_RETENTION_DAYS = 90

TELEGRAM_CHANNEL = os.environ.get("TG_CHANNEL", "Jilong_Rus")
TELEGRAM_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)


def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("fetch")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = RotatingFileHandler(
        LOG_DIR / "fetch.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


log = setup_logging()


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            taken_at TEXT NOT NULL,
            video_id TEXT NOT NULL,
            title TEXT NOT NULL,
            published_at TEXT NOT NULL,
            view_count INTEGER NOT NULL,
            like_count INTEGER NOT NULL,
            comment_count INTEGER NOT NULL,
            duration_seconds INTEGER NOT NULL,
            thumbnail TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_snapshots_video ON snapshots(video_id, taken_at)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tg_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            taken_at TEXT NOT NULL,
            channel TEXT NOT NULL,
            subscribers INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tg_snapshots_chan ON tg_snapshots(channel, taken_at)"
    )
    conn.commit()
    return conn


def parse_iso_duration(value: str) -> int:
    m = ISO_DUR_RE.match(value or "")
    if not m:
        return 0
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


def fmt_duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def fmt_compact(n: int) -> str:
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 10_000:
        return f"{n / 1_000:.1f}K"
    if n >= 1_000:
        return f"{n / 1_000:.2f}K"
    return str(n)


def get_uploads_playlist(youtube, channel_id: str) -> str:
    resp = (
        youtube.channels()
        .list(part="contentDetails", id=channel_id, maxResults=1)
        .execute()
    )
    items = resp.get("items", [])
    if not items:
        raise RuntimeError(f"Channel {channel_id} not found")
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]


def list_video_ids(youtube, playlist_id: str) -> list[str]:
    ids: list[str] = []
    token: str | None = None
    pages = 0
    while True:
        resp = (
            youtube.playlistItems()
            .list(
                part="contentDetails",
                playlistId=playlist_id,
                maxResults=50,
                pageToken=token,
            )
            .execute()
        )
        for item in resp.get("items", []):
            vid = item.get("contentDetails", {}).get("videoId")
            published = item.get("contentDetails", {}).get("videoPublishedAt")
            if not vid:
                continue
            if published and published < MIN_PUBLISHED_AT:
                continue
            ids.append(vid)
        token = resp.get("nextPageToken")
        pages += 1
        if not token:
            break
    log.info("Listed %d video ids across %d pages", len(ids), pages)
    return ids


def fetch_video_details(youtube, video_ids: list[str]) -> list[dict]:
    out: list[dict] = []
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i : i + 50]
        resp = (
            youtube.videos()
            .list(part="snippet,statistics,contentDetails", id=",".join(batch))
            .execute()
        )
        for item in resp.get("items", []):
            snippet = item.get("snippet", {})
            stats = item.get("statistics", {})
            content = item.get("contentDetails", {})
            description = snippet.get("description", "") or ""
            if MARKER not in description.lower():
                continue
            duration_s = parse_iso_duration(content.get("duration", ""))
            if duration_s < 60:
                continue
            thumbs = snippet.get("thumbnails", {}) or {}
            thumb = (
                thumbs.get("medium")
                or thumbs.get("high")
                or thumbs.get("default")
                or {}
            ).get("url", "")
            out.append(
                {
                    "videoId": item["id"],
                    "title": snippet.get("title", ""),
                    "publishedAt": snippet.get("publishedAt", ""),
                    "viewCount": int(stats.get("viewCount", 0)),
                    "likeCount": int(stats.get("likeCount", 0)),
                    "commentCount": int(stats.get("commentCount", 0)),
                    "duration": fmt_duration(duration_s),
                    "durationSeconds": duration_s,
                    "thumbnail": thumb,
                    "url": f"https://www.youtube.com/watch?v={item['id']}",
                }
            )
    return out


def save_snapshot(conn: sqlite3.Connection, taken_at: str, videos: list[dict]) -> None:
    rows = [
        (
            taken_at,
            v["videoId"],
            v["title"],
            v["publishedAt"],
            v["viewCount"],
            v["likeCount"],
            v["commentCount"],
            v["durationSeconds"],
            v["thumbnail"],
        )
        for v in videos
    ]
    conn.executemany(
        """
        INSERT INTO snapshots
            (taken_at, video_id, title, published_at, view_count,
             like_count, comment_count, duration_seconds, thumbnail)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()


def fetch_comments(youtube, videos: list[dict]) -> list[dict]:
    out: list[dict] = []
    for v in videos:
        try:
            resp = (
                youtube.commentThreads()
                .list(
                    part="snippet",
                    videoId=v["videoId"],
                    order="relevance",
                    maxResults=100,
                    textFormat="plainText",
                )
                .execute()
            )
        except HttpError as e:
            status = getattr(getattr(e, "resp", None), "status", None)
            if status in (403, 404):
                log.info("Comments unavailable for %s (HTTP %s)", v["videoId"], status)
                continue
            log.warning("commentThreads error for %s: %s", v["videoId"], e)
            continue
        for item in resp.get("items", []):
            top = (
                item.get("snippet", {})
                .get("topLevelComment", {})
                .get("snippet", {})
            )
            if not top:
                continue
            cid = item.get("snippet", {}).get("topLevelComment", {}).get("id", "")
            out.append(
                {
                    "commentId": cid,
                    "text": (top.get("textDisplay") or "").strip(),
                    "author": top.get("authorDisplayName", ""),
                    "authorChannelId": (top.get("authorChannelId") or {}).get("value", ""),
                    "likeCount": int(top.get("likeCount", 0)),
                    "publishedAt": top.get("publishedAt", ""),
                    "videoId": v["videoId"],
                    "videoTitle": v["title"],
                    "videoPublishedAt": v["publishedAt"],
                }
            )
    log.info("Fetched %d raw comments across %d videos", len(out), len(videos))
    return out


def _has_phrase(text_lower: str, phrases) -> bool:
    return any(p in text_lower for p in phrases)


def classify_comment(
    text: str, brand_tokens: set[str],
    require_brand: bool = False,
    min_score: int = 2,
) -> tuple[bool, int]:
    """Return (is_positive, score). Strict filter: must be brand-relevant
    (and optionally product-related), free of spam/negation/sarcasm markers,
    and score above threshold."""
    if not text:
        return False, 0
    lower = text.lower()
    words = set(WORD_RE.findall(lower))

    # Hard rejects
    if words & NEGATIVE_WORDS:
        return False, 0
    if _has_phrase(lower, NEGATIVE_PHRASES):
        return False, 0
    if any(ch in text for ch in NEGATIVE_EMOJI):
        return False, 0
    if _has_phrase(lower, SPAM_PHRASES):
        return False, 0
    if _has_phrase(lower, META_PHRASES):
        return False, 0
    if NEG_BIGRAM_RE.search(lower):
        return False, 0
    if REPEAT_CHAR_RE.search(text):
        return False, 0
    if lower.startswith("не "):
        return False, 0

    # Reject emoji floods
    emoji_count = len(EMOJI_HINT_RE.findall(text))
    if emoji_count > COMMENT_MAX_EMOJI:
        return False, 0
    text_without_emoji = EMOJI_HINT_RE.sub("", text).strip()
    if len(text_without_emoji) < COMMENT_MIN_LEN:
        return False, 0

    # Reject shouting (>50% uppercase letters)
    letters = [c for c in text if c.isalpha()]
    if len(letters) >= 12:
        upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
        if upper_ratio > 0.5:
            return False, 0

    # Brand / product relevance
    has_brand = any(b in lower for b in brand_tokens)
    has_product = bool(words & PRODUCT_TOKENS)
    if require_brand:
        if not has_brand:
            return False, 0
    else:
        if not (has_brand or has_product):
            return False, 0

    # Score
    pos_hits = len(words & POSITIVE_WORDS)
    phrase_hits = sum(1 for p in POSITIVE_PHRASES if p in lower)
    pos_emoji_hits = sum(1 for ch in text if ch in POSITIVE_EMOJI)
    score = pos_hits + phrase_hits + min(pos_emoji_hits, 2)
    if has_brand:
        score += 1
    if score < min_score:
        return False, 0
    return True, score


def _recency_weight(video_published_iso: str, now_utc: datetime) -> float:
    """Returns a multiplier in [0.3, 2.0]: newer video → higher weight.
    Half-life of RECENCY_HALFLIFE_DAYS keeps relevance gradual.
    """
    if not video_published_iso:
        return 1.0
    try:
        pub = datetime.fromisoformat(video_published_iso.replace("Z", "+00:00"))
    except ValueError:
        return 1.0
    age_days = max(0, (now_utc - pub).days)
    weight = 2.0 * (0.5 ** (age_days / RECENCY_HALFLIFE_DAYS))
    return max(0.3, weight)


def pick_quotes(
    comments: list[dict],
    channel_id: str,
    now_utc: datetime,
    brand_tokens: set[str],
    require_brand: bool = False,
    label: str = "",
    min_likes: int = 3,
    min_score: int = 2,
) -> list[dict]:
    seen_authors: set[str] = set()
    candidates: list[dict] = []
    raw_brand_mentions = 0
    for c in comments:
        text = c["text"]
        if not text:
            continue
        if c.get("authorChannelId") == channel_id:
            continue
        if any(b in text.lower() for b in brand_tokens):
            raw_brand_mentions += 1
        if URL_RE.search(text):
            continue
        n = len(text)
        if n < COMMENT_MIN_LEN or n > COMMENT_MAX_LEN:
            continue
        if c["likeCount"] < min_likes:
            continue
        ok, score = classify_comment(
            text, brand_tokens, require_brand=require_brand, min_score=min_score
        )
        if not ok:
            continue
        rec = _recency_weight(c.get("videoPublishedAt", ""), now_utc)
        effective = c["likeCount"] * rec + score * 0.5
        candidates.append({**c, "score": score, "effective": effective})

    log.info(
        "  %s: %d raw mentions found in %d total comments",
        label or "brand", raw_brand_mentions, len(comments),
    )
    candidates.sort(key=lambda c: c["effective"], reverse=True)
    picked: list[dict] = []
    for c in candidates:
        if c["author"] in seen_authors:
            continue
        seen_authors.add(c["author"])
        picked.append(
            {
                "text": c["text"],
                "author": c["author"],
                "likeCount": c["likeCount"],
                "publishedAt": c["publishedAt"],
                "videoId": c["videoId"],
                "videoTitle": c["videoTitle"],
                "url": f"https://www.youtube.com/watch?v={c['videoId']}&lc={c['commentId']}",
            }
        )
        if len(picked) >= QUOTES_LIMIT:
            break
    log.info(
        "Picked %d %s quotes from %d candidates (of %d raw)",
        len(picked), label or "brand", len(candidates), len(comments),
    )
    return picked


def _aggregate_snapshots(conn: sqlite3.Connection, since_iso: str) -> list[dict]:
    """Sum per-snapshot totals across videos, ordered by time."""
    cur = conn.execute(
        """
        SELECT taken_at,
               SUM(view_count)    AS views,
               SUM(like_count)    AS likes,
               SUM(comment_count) AS comments
        FROM snapshots
        WHERE taken_at >= ?
        GROUP BY taken_at
        ORDER BY taken_at
        """,
        (since_iso,),
    )
    return [
        {"ts": r[0], "views": int(r[1] or 0),
         "likes": int(r[2] or 0), "comments": int(r[3] or 0)}
        for r in cur.fetchall()
    ]


def _resample(points: list[dict], target: int) -> list[dict]:
    """Reduce points to ~target count by uniform stride, keeping first and last."""
    if len(points) <= target:
        return points
    step = len(points) / target
    picked = [points[int(i * step)] for i in range(target)]
    if picked[-1] is not points[-1]:
        picked.append(points[-1])
    return picked


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def compute_history(conn: sqlite3.Connection, now_utc: datetime) -> dict:
    since = (now_utc - timedelta(days=HISTORY_DAYS)).isoformat()
    points = _aggregate_snapshots(conn, since)
    return {
        "rangeDays": HISTORY_DAYS,
        "points": _resample(points, HISTORY_POINTS),
        "rawCount": len(points),
    }


def compute_deltas(
    conn: sqlite3.Connection, current: dict, now_utc: datetime
) -> dict:
    """Compare current totals against snapshots ~24h and ~7d ago."""
    def find_at(hours: int) -> dict | None:
        target = (now_utc - timedelta(hours=hours)).isoformat()
        cur = conn.execute(
            """
            SELECT taken_at,
                   SUM(view_count)    AS views,
                   SUM(like_count)    AS likes,
                   SUM(comment_count) AS comments
            FROM snapshots
            WHERE taken_at <= ?
            GROUP BY taken_at
            ORDER BY taken_at DESC
            LIMIT 1
            """,
            (target,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {"ts": row[0], "views": int(row[1] or 0),
                "likes": int(row[2] or 0), "comments": int(row[3] or 0)}

    def delta_for(prev: dict | None) -> dict | None:
        if not prev:
            return None
        return {
            "views": current["views"] - prev["views"],
            "likes": current["likes"] - prev["likes"],
            "comments": current["comments"] - prev["comments"],
            "since": prev["ts"],
        }

    return {
        "h24": delta_for(find_at(24)),
        "d7": delta_for(find_at(168)),
    }


def fmt_signed(n: int | None) -> str | None:
    if n is None:
        return None
    sign = "+" if n >= 0 else "−"
    return f"{sign}{fmt_compact(abs(n))}"


TG_PATTERNS = [
    # <span class="counter_value">12 345</span><span class="counter_type">subscribers</span>
    re.compile(
        r'class=["\']counter_value["\'][^>]*>([^<]+)</[^>]+>\s*'
        r'<[^>]+class=["\']counter_type["\'][^>]*>\s*subscriber',
        re.IGNORECASE,
    ),
    # <div class="tgme_header_counter">12 345 subscribers</div>
    re.compile(
        r'class=["\']tgme_header_counter["\'][^>]*>\s*([\d\s .,KkКк]+)\s*subscriber',
        re.IGNORECASE,
    ),
    # generic ">12 345 subscribers<"
    re.compile(r'>\s*([\d\s .,KkКк]+)\s*subscribers?\s*<', re.IGNORECASE),
]


def _parse_subscriber_count(raw: str) -> int | None:
    raw = raw.strip()
    if not raw:
        return None
    if re.search(r'[KkКк]', raw):
        num = re.sub(r'[^\d.]', '', raw.replace(',', '.'))
        try:
            return int(float(num) * 1000)
        except (ValueError, TypeError):
            return None
    digits = re.sub(r'\D', '', raw)
    return int(digits) if digits else None


def fetch_telegram_subscribers(channel: str) -> tuple[int | None, str]:
    """Returns (count_or_none, diagnostic_string).
    The diagnostic is short and safe to surface in data.json."""
    for url in (f"https://t.me/s/{channel}", f"https://t.me/{channel}"):
        req = urllib.request.Request(url, headers={
            "User-Agent": TELEGRAM_UA,
            "Accept-Language": "en-US,en;q=0.9",
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                code = resp.status
                html = resp.read().decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as e:
            log.warning("Telegram HTTP %s for %s", e.code, url)
            if e.code in (429, 503):
                return None, f"HTTP {e.code} (rate-limited)"
            continue
        except Exception as e:
            log.warning("Telegram fetch error %s: %s", url, e)
            return None, f"network: {type(e).__name__}"

        for p in TG_PATTERNS:
            m = p.search(html)
            if m:
                n = _parse_subscriber_count(m.group(1))
                if n is not None and n > 0:
                    return n, f"ok via {url}"
        # As a last resort, scan around any "subscriber" occurrence.
        idx = html.lower().find("subscriber")
        if idx > 0:
            window = html[max(0, idx - 80):idx]
            mnum = re.search(r'([\d][\d\s .,KkКк]{0,10})\s*$', window)
            if mnum:
                n = _parse_subscriber_count(mnum.group(1))
                if n is not None and n > 0:
                    return n, f"ok via fallback near 'subscriber' on {url}"
        title_m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
        title = (title_m.group(1).strip() if title_m else "")[:80]
        has_subscriber_word = "subscriber" in html.lower()
        has_join_btn = "join channel" in html.lower() or "tgme_action_button" in html.lower()
        last_diag = (
            f"parse fail | title={title!r} | "
            f"bytes={len(html)} | has_subscriber_word={has_subscriber_word} | "
            f"has_join_btn={has_join_btn}"
        )
        log.warning("Could not parse subscribers from %s (HTTP %s, %d bytes, title=%r)",
                    url, code, len(html), title)
    return None, locals().get("last_diag", "parse failed")


def save_tg_snapshot(conn: sqlite3.Connection, taken_at: str, channel: str, subs: int) -> None:
    conn.execute(
        "INSERT INTO tg_snapshots(taken_at, channel, subscribers) VALUES (?, ?, ?)",
        (taken_at, channel, subs),
    )
    conn.commit()


def compute_tg_history(conn: sqlite3.Connection, channel: str, now_utc: datetime) -> dict:
    since = (now_utc - timedelta(days=HISTORY_DAYS)).isoformat()
    cur = conn.execute(
        """
        SELECT taken_at, subscribers FROM tg_snapshots
        WHERE channel = ? AND taken_at >= ?
        ORDER BY taken_at
        """,
        (channel, since),
    )
    raw = [{"ts": r[0], "subscribers": int(r[1])} for r in cur.fetchall()]
    return {
        "rangeDays": HISTORY_DAYS,
        "points": _resample(raw, HISTORY_POINTS),
        "rawCount": len(raw),
    }


def compute_tg_deltas(
    conn: sqlite3.Connection, channel: str, current: int, now_utc: datetime
) -> dict:
    def find_at(hours: int) -> dict | None:
        target = (now_utc - timedelta(hours=hours)).isoformat()
        cur = conn.execute(
            """
            SELECT taken_at, subscribers FROM tg_snapshots
            WHERE channel = ? AND taken_at <= ?
            ORDER BY taken_at DESC LIMIT 1
            """,
            (channel, target),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {"ts": row[0], "subscribers": int(row[1])}

    def delta_for(prev):
        if not prev:
            return None
        return {
            "abs": current - prev["subscribers"],
            "signed": fmt_signed(current - prev["subscribers"]),
            "since": prev["ts"],
        }

    return {
        "h24": delta_for(find_at(24)),
        "d7":  delta_for(find_at(168)),
    }


def build_payload(
    videos: list[dict],
    taken_at_utc: datetime,
    kugoo_quotes: list[dict],
    dns_quotes: list[dict],
    history: dict,
    deltas: dict,
    telegram: dict | None = None,
) -> dict:
    total_videos = len(videos)
    total_views = sum(v["viewCount"] for v in videos)
    total_likes = sum(v["likeCount"] for v in videos)
    total_comments = sum(v["commentCount"] for v in videos)
    avg_views = total_views // total_videos if total_videos else 0
    er = (
        (total_likes + total_comments) / total_views * 100
        if total_views
        else 0.0
    )
    for v in videos:
        v_er = (
            (v["likeCount"] + v["commentCount"]) / v["viewCount"] * 100
            if v["viewCount"]
            else 0.0
        )
        v["engagementRate"] = round(v_er, 2)

    top = sorted(videos, key=lambda v: v["viewCount"], reverse=True)[:3]
    top_er = sorted(videos, key=lambda v: v["engagementRate"], reverse=True)[:TOP_ER_LIMIT]

    def card(v: dict) -> dict:
        return {
            "videoId": v["videoId"],
            "title": v["title"],
            "publishedAt": v["publishedAt"],
            "viewCount": v["viewCount"],
            "viewCountCompact": fmt_compact(v["viewCount"]),
            "engagementRate": v["engagementRate"],
            "thumbnail": v["thumbnail"],
            "url": v["url"],
        }

    video_drops = sorted(
        [{"ts": v["publishedAt"], "videoId": v["videoId"], "title": v["title"]} for v in videos],
        key=lambda d: d["ts"],
    )

    def fmt_delta(d: dict | None, key: str) -> dict | None:
        if not d:
            return None
        val = d[key]
        return {"abs": val, "signed": fmt_signed(val), "since": d["since"]}

    taken_at_msk = taken_at_utc.astimezone(MSK)
    return {
        "channelId": CHANNEL_ID,
        "channelHandle": "@fastmotionelectric",
        "marker": MARKER,
        "updatedAtUtc": taken_at_utc.isoformat(),
        "updatedAtMsk": taken_at_msk.strftime("%d.%m.%Y %H:%M МСК"),
        "totals": {
            "videos": total_videos,
            "views": total_views,
            "viewsCompact": fmt_compact(total_views),
            "likes": total_likes,
            "likesCompact": fmt_compact(total_likes),
            "comments": total_comments,
            "commentsCompact": fmt_compact(total_comments),
            "avgViews": avg_views,
            "avgViewsCompact": fmt_compact(avg_views),
            "engagementRate": round(er, 2),
        },
        "deltas": {
            "h24": {
                "views":    fmt_delta(deltas.get("h24"), "views"),
                "likes":    fmt_delta(deltas.get("h24"), "likes"),
                "comments": fmt_delta(deltas.get("h24"), "comments"),
            },
            "d7": {
                "views":    fmt_delta(deltas.get("d7"), "views"),
                "likes":    fmt_delta(deltas.get("d7"), "likes"),
                "comments": fmt_delta(deltas.get("d7"), "comments"),
            },
        },
        "top": [card(v) for v in top],
        "topByEr": [card(v) for v in top_er],
        "videoDrops": video_drops,
        "history": history,
        "videos": sorted(videos, key=lambda v: v["publishedAt"], reverse=True),
        "quotes": {
            "kugoo": kugoo_quotes,
            "dns": dns_quotes,
        },
        "telegram": telegram or {},
    }


def write_json_atomic(payload: dict) -> None:
    FRONTEND_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix="data.", suffix=".json", dir=FRONTEND_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, JSON_PATH)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def main() -> int:
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        log.error("YOUTUBE_API_KEY is not set")
        return 2

    taken_at = datetime.now(timezone.utc)
    log.info("Run started at %s", taken_at.isoformat())

    try:
        youtube = build("youtube", "v3", developerKey=api_key, cache_discovery=False)
        playlist_id = get_uploads_playlist(youtube, CHANNEL_ID)
        log.info("Uploads playlist: %s", playlist_id)
        video_ids = list_video_ids(youtube, playlist_id)
        videos = fetch_video_details(youtube, video_ids)
        log.info("Matched %d collab videos (marker=%s)", len(videos), MARKER)
    except HttpError as e:
        log.exception("YouTube API HttpError: %s", e)
        log.warning("Keeping previous data.json")
        return 1
    except Exception as e:
        log.exception("Fetch failed: %s", e)
        log.warning("Keeping previous data.json")
        return 1

    if not videos:
        log.warning("No videos matched marker — keeping previous data.json")
        return 1

    conn = init_db()
    try:
        save_snapshot(conn, taken_at.isoformat(), videos)
        cutoff = (taken_at - timedelta(days=SNAPSHOT_RETENTION_DAYS)).isoformat()
        cur = conn.execute("DELETE FROM snapshots WHERE taken_at < ?", (cutoff,))
        if cur.rowcount:
            log.info("Pruned %d snapshots older than %d days",
                     cur.rowcount, SNAPSHOT_RETENTION_DAYS)
        conn.commit()
        current_totals = {
            "views":    sum(v["viewCount"] for v in videos),
            "likes":    sum(v["likeCount"] for v in videos),
            "comments": sum(v["commentCount"] for v in videos),
        }
        history = compute_history(conn, taken_at)
        deltas = compute_deltas(conn, current_totals, taken_at)
        log.info(
            "History: %d points (over %d days); deltas: h24=%s d7=%s",
            len(history["points"]), history["rangeDays"],
            "yes" if deltas.get("h24") else "no",
            "yes" if deltas.get("d7") else "no",
        )

        telegram_payload: dict = {
            "channel": TELEGRAM_CHANNEL,
            "url": f"https://t.me/{TELEGRAM_CHANNEL}",
            "subscribers": None,
            "subscribersCompact": "—",
            "deltas": {"h24": None, "d7": None},
            "history": {"rangeDays": HISTORY_DAYS, "points": [], "rawCount": 0},
        }
        subs, tg_diag = fetch_telegram_subscribers(TELEGRAM_CHANNEL)
        telegram_payload["debug"] = tg_diag
        if subs is not None:
            save_tg_snapshot(conn, taken_at.isoformat(), TELEGRAM_CHANNEL, subs)
            tg_cutoff = (taken_at - timedelta(days=SNAPSHOT_RETENTION_DAYS)).isoformat()
            conn.execute(
                "DELETE FROM tg_snapshots WHERE taken_at < ?", (tg_cutoff,)
            )
            conn.commit()
            telegram_payload.update({
                "subscribers": subs,
                "subscribersCompact": fmt_compact(subs),
                "deltas": compute_tg_deltas(conn, TELEGRAM_CHANNEL, subs, taken_at),
                "history": compute_tg_history(conn, TELEGRAM_CHANNEL, taken_at),
            })
            log.info("Telegram @%s: %d subscribers (%s)",
                     TELEGRAM_CHANNEL, subs, tg_diag)
        else:
            log.warning("Telegram @%s: subscribers unavailable (%s)",
                        TELEGRAM_CHANNEL, tg_diag)
    finally:
        conn.close()

    try:
        raw_comments = fetch_comments(youtube, videos)
        kugoo_quotes = pick_quotes(
            raw_comments, CHANNEL_ID, taken_at, KUGOO_TOKENS,
            require_brand=False, label="kugoo",
            min_likes=KUGOO_MIN_LIKES, min_score=KUGOO_MIN_SCORE,
        )
        dns_quotes = pick_quotes(
            raw_comments, CHANNEL_ID, taken_at, DNS_TOKENS,
            require_brand=True, label="dns",
            min_likes=DNS_MIN_LIKES, min_score=DNS_MIN_SCORE,
        )
    except Exception as e:
        log.warning("Comment fetch failed (continuing without quotes): %s", e)
        kugoo_quotes, dns_quotes = [], []

    payload = build_payload(
        videos, taken_at, kugoo_quotes, dns_quotes, history, deltas,
        telegram=telegram_payload,
    )
    write_json_atomic(payload)
    log.info(
        "Wrote %s — videos=%d views=%d ER=%.2f%% kugoo=%d dns=%d",
        JSON_PATH,
        payload["totals"]["videos"],
        payload["totals"]["views"],
        payload["totals"]["engagementRate"],
        len(payload["quotes"]["kugoo"]),
        len(payload["quotes"]["dns"]),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
