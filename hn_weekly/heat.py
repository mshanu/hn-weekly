"""Heat = how much action a topic/section created: points + comments across its threads.

Computed at render time from the cached stage files, so it needs no LLM calls and
applies to already-generated weeks too.
"""

import json
from pathlib import Path


def _story_id(hn_url: str) -> int:
    return int(hn_url.rsplit("=", 1)[1])


def apply_heat(digest: dict, wdir: Path) -> dict:
    stories = {s["id"]: s for day in json.loads((wdir / "1_stories.json").read_text())["days"].values()
               for s in day}
    topics = json.loads((wdir / "4_topics.json").read_text())
    topic_of = {i: t for t in topics for i in t["story_ids"]}

    def engagement(ids):
        return sum(stories[i]["points"] + stories[i]["comments"] for i in ids)

    # Section heat counts every story in the section that week, not just the boxed ones.
    section_heat: dict[str, int] = {}
    for t in topics:
        section_heat[t["section"]] = section_heat.get(t["section"], 0) + engagement(t["story_ids"])

    boxes = [b for s in digest["sections"] for b in s["boxes"]]
    for b in boxes:
        b["heat"] = engagement(topic_of[_story_id(b["hn_url"])]["story_ids"])

    # Flames by rank among the week's boxes: top quarter 3, next ~third 2, rest 1.
    order = sorted(boxes, key=lambda b: -b["heat"])
    n = len(order)
    for rank, b in enumerate(order):
        b["id"] = rank  # stable id = heat rank; the audio bulletin refers to boxes by it
        b["flames"] = 3 if rank < max(1, round(n * 0.25)) else 2 if rank < round(n * 0.6) else 1

    hottest = max(section_heat.values(), default=1)
    for s in digest["sections"]:
        s["heat"] = section_heat.get(s["name"], 0)
        s["heat_pct"] = round(100 * s["heat"] / hottest)
        s["boxes"].sort(key=lambda b: -b["heat"])
    digest["sections"].sort(key=lambda s: -s["heat"])
    return digest
