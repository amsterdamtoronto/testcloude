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
from datetime import datetime, timezone
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


def build_payload(videos: list[dict], taken_at_utc: datetime) -> dict:
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
    top = sorted(videos, key=lambda v: v["viewCount"], reverse=True)[:3]
    for v in top:
        v_er = (
            (v["likeCount"] + v["commentCount"]) / v["viewCount"] * 100
            if v["viewCount"]
            else 0.0
        )
        v["engagementRate"] = round(v_er, 2)

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
        "top": [
            {
                "videoId": v["videoId"],
                "title": v["title"],
                "publishedAt": v["publishedAt"],
                "viewCount": v["viewCount"],
                "viewCountCompact": fmt_compact(v["viewCount"]),
                "engagementRate": v["engagementRate"],
                "thumbnail": v["thumbnail"],
                "url": v["url"],
            }
            for v in top
        ],
        "videos": sorted(videos, key=lambda v: v["publishedAt"], reverse=True),
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
    finally:
        conn.close()

    payload = build_payload(videos, taken_at)
    write_json_atomic(payload)
    log.info(
        "Wrote %s — videos=%d views=%d ER=%.2f%%",
        JSON_PATH,
        payload["totals"]["videos"],
        payload["totals"]["views"],
        payload["totals"]["engagementRate"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
