"""Hacker News data collection: per-day top stories (Algolia) and comment trees."""

import asyncio
from datetime import date, datetime, timedelta, timezone

import httpx

ALGOLIA = "https://hn.algolia.com/api/v1"
FIREBASE = "https://hacker-news.firebaseio.com/v0"
UA = {"User-Agent": "hn-weekly/0.1 (personal weekly digest)"}


def previous_week(today: date | None = None) -> str:
    today = today or date.today()
    y, w, _ = (today - timedelta(days=7)).isocalendar()
    return f"{y}-W{w:02d}"


def week_days(week: str) -> list[date]:
    """'2026-W44' -> the 7 dates Mon..Sun."""
    monday = date.fromisoformat(f"{week}-1")  # ISO week date, Python 3.11+
    return [monday + timedelta(days=i) for i in range(7)]


def day_window(d: date) -> tuple[int, int]:
    """Start/end unix timestamps for a calendar day in the local timezone."""
    local_tz = datetime.now().astimezone().tzinfo
    start = datetime(d.year, d.month, d.day, tzinfo=local_tz)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())


async def get_json(client: httpx.AsyncClient, url: str, params: dict | None = None, tries: int = 4):
    for attempt in range(tries):
        try:
            r = await client.get(url, params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code < 500 and r.status_code != 429:
                r.raise_for_status()
        except (httpx.TransportError, httpx.TimeoutException):
            if attempt == tries - 1:
                raise
        await asyncio.sleep(2 ** attempt)
    raise RuntimeError(f"GET {url} failed after {tries} tries")


def _story_record(hit: dict) -> dict:
    title = hit.get("title") or ""
    url = hit.get("url") or ""
    return {
        "id": int(hit["objectID"]),
        "title": title,
        "url": url or f"https://news.ycombinator.com/item?id={hit['objectID']}",
        "has_external_url": bool(url),
        "points": hit.get("points") or 0,
        "comments": hit.get("num_comments") or 0,
        "author": hit.get("author"),
        "posted": datetime.fromtimestamp(hit["created_at_i"], timezone.utc).isoformat(),
        "is_show_hn": title.lower().startswith("show hn"),
        "is_repo": any(h in url for h in ("github.com/", "gitlab.com/", "codeberg.org/")),
        "story_text": hit.get("story_text") or "",
    }


async def top_stories_for_day(client: httpx.AsyncClient, d: date, n: int) -> list[dict]:
    start, end = day_window(d)
    hits, page = [], 0
    while True:
        data = await get_json(client, f"{ALGOLIA}/search", {
            "tags": "story",
            "numericFilters": f"created_at_i>={start},created_at_i<{end},points>20",
            "hitsPerPage": 200,
            "page": page,
        })
        hits += data["hits"]
        page += 1
        if page >= data.get("nbPages", 0):
            break
    stories = sorted((_story_record(h) for h in hits), key=lambda s: -s["points"])[:n]
    for i, s in enumerate(stories, 1):
        s["day"] = d.isoformat()
        s["rank_in_day"] = i
    return stories


async def collect_week(week: str, per_day: int) -> dict:
    async with httpx.AsyncClient(headers=UA) as client:
        days = week_days(week)
        results = await asyncio.gather(*(top_stories_for_day(client, d, per_day) for d in days))
    return {"week": week, "days": {d.isoformat(): r for d, r in zip(days, results)}}


async def comment_tree(client: httpx.AsyncClient, story_id: int) -> tuple[dict, list[int]]:
    """Full tree from Algolia plus HN's ranked top-level order from Firebase."""
    tree, fb = await asyncio.gather(
        get_json(client, f"{ALGOLIA}/items/{story_id}"),
        get_json(client, f"{FIREBASE}/item/{story_id}.json"),
    )
    return tree, (fb or {}).get("kids", [])
