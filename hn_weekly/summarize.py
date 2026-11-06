"""LLM stages: per-story summary -> topic grouping -> topic merge -> section overviews -> week TL;DR."""

import asyncio
import json

from openai import AsyncOpenAI

from .config import SECTIONS

VIBES = ["enthusiastic", "skeptical", "divided", "informative"]


def _schema(name: str, props: dict) -> dict:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": {"type": "object", "properties": props,
                       "required": list(props), "additionalProperties": False},
        },
    }


STR = {"type": "string"}
STORY_FORMAT = _schema("story_summary", {
    "gist": STR,
    "pulse": STR,
    "vibe": {"type": "string", "enum": VIBES},
    "section": {"type": "string", "enum": SECTIONS},
    "topic_hint": STR,
    "entities": {"type": "array", "items": STR},
})
GROUP_FORMAT = _schema("topic_grouping", {
    "topics": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "section": {"type": "string", "enum": SECTIONS},
            "topic": STR,
            "story_ids": {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["section", "topic", "story_ids"],
        "additionalProperties": False,
    }},
})
MERGE_FORMAT = _schema("topic_merge", {
    "gist": STR, "pulse": STR, "vibe": {"type": "string", "enum": VIBES},
})
TEXT_FORMAT = _schema("text", {"text": STR})

STORY_PROMPT = """You summarize a Hacker News story and its discussion for a busy engineer's weekly digest.
Return:
- gist: 1-2 sentences on what the story/article actually is. Concrete, no hype.
- pulse: 2-3 sentences on what the HN community thinks: the consensus, the main pushback, and any notable expert/insider comment. Base this ONLY on the comments given.
- vibe: overall tone of the discussion.
- section: the single best-fitting section. Use "Launches & Repos" for Show HN posts, new open-source projects and product launches.
- topic_hint: 2-5 words naming the underlying event/storyline (e.g. "Ubuntu Snap controversy"), used to group related stories.
- entities: up to 5 key companies/products/people/projects named."""

GROUP_PROMPT = f"""You organize one week of top Hacker News stories into a digest.
Merge stories into one topic ONLY when they cover the SAME specific event, announcement, product, company action, or
controversy (stories may be from different days), e.g. "New 14-inch MacBook Pro with M6" + "New iMac with M6" (one Apple launch),
or an article plus the company's response to it.
Do NOT make category buckets. Two different breaches, two different model releases, or two different elections are
SEPARATE topics even though they share a theme. Sections already provide the categories.
When in doubt, keep stories separate. Most stories will be single-story topics; that is expected.
Every story id must appear in exactly one topic.
Topic name: short and specific (max 8 words), naming the actual thing (e.g. "Anthropic S-1 and IPO", never "AI news").
For a single-story topic, use a clean version of the story title.
Assign each topic one section from: {", ".join(SECTIONS)}."""

MERGE_PROMPT = """Several Hacker News stories this week were about the same topic. Combine their summaries into:
- gist: 2-3 sentences on what happened overall.
- pulse: 2-3 sentences on what the community thought across these discussions.
- vibe: overall tone."""

COMPACT_FORMAT = _schema("compact_boxes", {
    "boxes": {"type": "array", "items": {
        "type": "object",
        "properties": {"index": {"type": "integer"}, "title": STR, "gist": STR, "take": STR},
        "required": ["index", "title", "gist", "take"],
        "additionalProperties": False,
    }},
})

COMPACT_PROMPT = """You write tiny dashboard boxes for a weekly Hacker News digest. For EACH numbered topic return:
- index: the topic's number, unchanged
- title: max 5 words, names the actual thing (e.g. "Mistral Large 4 open weights")
- gist: max 15 words, what happened. Concrete facts, no filler like "This week".
- take: max 15 words, what HN commenters think, in a direct voice (e.g. "Impressive, but the license isn't truly open.")
No trailing periods on titles. Never exceed the word limits."""

HEADLINE_PROMPT = """Write ONE sentence (max 25 words) capturing the biggest things on Hacker News this week,
based on the boxes below. No preamble, no "This week on Hacker News"."""


class Summarizer:
    def __init__(self, api_key: str, model: str, concurrency: int = 8):
        self.client = AsyncOpenAI(api_key=api_key, max_retries=5)
        self.model = model
        self.sem = asyncio.Semaphore(concurrency)

    async def _call(self, system: str, user: str, fmt: dict) -> dict:
        async with self.sem:
            r = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                response_format=fmt,
            )
        return json.loads(r.choices[0].message.content)

    async def story(self, story: dict, raw: dict) -> dict:
        comments = "\n\n".join(
            f"[{c['author']}] {c['text']}" +
            "".join(f"\n  ↳ [{r['author']}] {r['text']}" for r in c["replies"])
            for c in raw["comments"]
        ) or "(no comments)"
        user = (f"TITLE: {story['title']}\nURL: {story['url']}\n"
                f"POINTS: {story['points']}  COMMENTS: {story['comments']}\n\n"
                f"ARTICLE TEXT:\n{raw['article'] or '(unavailable)'}\n\nTOP COMMENTS:\n{comments}")
        out = await self._call(STORY_PROMPT, user, STORY_FORMAT)
        return {"id": story["id"], **out}

    async def group(self, stories: list[dict], summaries: dict[int, dict]) -> list[dict]:
        lines = []
        for s in stories:
            m = summaries[s["id"]]
            lines.append(json.dumps({
                "id": s["id"], "day": s["day"], "section": m["section"], "title": s["title"],
                "gist": m["gist"], "topic_hint": m["topic_hint"], "entities": m["entities"],
            }, ensure_ascii=False))
        out = await self._call(GROUP_PROMPT, "\n".join(lines), GROUP_FORMAT)
        return validate_grouping(out["topics"], stories, summaries)

    async def merge(self, members: list[tuple[dict, dict]]) -> dict:
        user = "\n\n".join(f"TITLE: {s['title']}\nGIST: {m['gist']}\nPULSE: {m['pulse']}"
                           for s, m in members)
        return await self._call(MERGE_PROMPT, user, MERGE_FORMAT)

    async def compact(self, topics: list[dict]) -> list[dict]:
        """Short title/gist/take per topic, returned in the same order as `topics`."""
        user = "\n\n".join(f"#{i}\nTOPIC: {t['topic']}\nGIST: {t['gist']}\nPULSE: {t['pulse']}"
                           for i, t in enumerate(topics))
        boxes = {b["index"]: b for b in (await self._call(COMPACT_PROMPT, user, COMPACT_FORMAT))["boxes"]}
        # Fall back to the long text for any topic the model skipped.
        return [boxes.get(i) or {"title": t["topic"], "gist": t["gist"], "take": t["pulse"]}
                for i, t in enumerate(topics)]

    async def headline(self, boxes: list[dict]) -> str:
        user = "\n".join(f"- {b['title']}: {b['gist']}" for b in boxes)
        return (await self._call(HEADLINE_PROMPT, user, TEXT_FORMAT))["text"]


def validate_grouping(topics: list[dict], stories: list[dict], summaries: dict[int, dict]) -> list[dict]:
    """Every story exactly once: drop unknown/duplicate ids, re-add missing ones as singletons."""
    known = {s["id"] for s in stories}
    seen: set[int] = set()
    clean = []
    for t in topics:
        ids = []
        for i in t["story_ids"]:
            if i in known and i not in seen:
                seen.add(i)
                ids.append(i)
        if ids:
            clean.append({"section": t["section"], "topic": t["topic"], "story_ids": ids})
    for s in stories:
        if s["id"] not in seen:
            clean.append({"section": summaries[s["id"]]["section"], "topic": s["title"],
                          "story_ids": [s["id"]]})
    return clean
