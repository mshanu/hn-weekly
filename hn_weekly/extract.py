"""Turn raw HN data + article pages into compact LLM input."""

import asyncio
import html
import re

import httpx
import trafilatura

from .fetch import comment_tree

ARTICLE_CHARS = 10_000   # ~2.5k tokens
COMMENT_CHARS = 20_000   # ~5k tokens
TOP_THREADS = 12
REPLIES_PER_THREAD = 2
BROWSER_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


def clean_html(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<p>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


async def fetch_article(story: dict) -> str:
    fallback = clean_html(story.get("story_text"))
    if not story["has_external_url"]:
        return fallback
    try:
        async with httpx.AsyncClient(headers=BROWSER_UA, follow_redirects=True, timeout=20) as c:
            r = await c.get(story["url"])
        ctype = r.headers.get("content-type", "")
        if r.status_code != 200 or "html" not in ctype:
            return fallback
        text = await asyncio.to_thread(trafilatura.extract, r.text, include_comments=False)
        return (text or fallback)[:ARTICLE_CHARS]
    except Exception:
        return fallback


def flatten_comments(tree: dict, ranked_ids: list[int]) -> list[dict]:
    by_id = {c["id"]: c for c in tree.get("children", [])}
    # HN ranked order first, then any Algolia children Firebase didn't list.
    order = [i for i in ranked_ids if i in by_id] + [i for i in by_id if i not in set(ranked_ids)]
    threads, budget = [], COMMENT_CHARS
    for cid in order:
        c = by_id[cid]
        text = clean_html(c.get("text"))
        if not text:  # deleted / dead
            continue
        replies = [
            {"author": r.get("author"), "text": clean_html(r.get("text"))[:800]}
            for r in c.get("children", []) if r.get("text")
        ][:REPLIES_PER_THREAD]
        thread = {"author": c.get("author"), "text": text[:2000], "replies": replies}
        size = len(thread["text"]) + sum(len(r["text"]) for r in replies)
        if size > budget:
            break
        budget -= size
        threads.append(thread)
        if len(threads) >= TOP_THREADS:
            break
    return threads


async def build_raw(client: httpx.AsyncClient, story: dict) -> dict:
    article, (tree, ranked) = await asyncio.gather(
        fetch_article(story), comment_tree(client, story["id"])
    )
    return {"id": story["id"], "article": article, "comments": flatten_comments(tree, ranked)}
