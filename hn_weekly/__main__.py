"""hn-weekly: build a themed weekly digest of Hacker News.

Usage:
  python -m hn_weekly                    # previous ISO week
  python -m hn_weekly --week 2026-W44
  python -m hn_weekly --dry-run          # just list the picked stories (no OpenAI)
  python -m hn_weekly --rerender         # rebuild HTML from cached digest
  python -m hn_weekly --if-missing       # no-op if that week's page exists (used by launchd)
"""

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import httpx

from .config import SECTIONS, load_config
from .extract import build_raw
from .broadcast import stage_broadcast
from .heat import apply_heat
from .fetch import UA, collect_week, previous_week, week_days
from .render import render_week
from .summarize import Summarizer

TOP_BOXES = 20


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def read_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def write_json(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


async def gather_all(coros):
    results = await asyncio.gather(*coros, return_exceptions=True)
    errors = [r for r in results if isinstance(r, BaseException)]
    if errors:
        raise RuntimeError(f"{len(errors)} task(s) failed; first: {errors[0]!r}") from errors[0]
    return results


async def stage_stories(cfg, week: str, wdir: Path) -> list[dict]:
    p = wdir / "1_stories.json"
    if not p.exists():
        log(f"Collecting top {cfg.per_day}/day for {week} from Algolia…")
        write_json(p, await collect_week(week, cfg.per_day))
    data = read_json(p)
    return [s for day in data["days"].values() for s in day]


async def stage_raw(stories: list[dict], wdir: Path) -> dict[int, dict]:
    rdir = wdir / "2_raw"
    todo = [s for s in stories if not (rdir / f"{s['id']}.json").exists()]
    if todo:
        log(f"Fetching articles + comments for {len(todo)} stories…")
        sem = asyncio.Semaphore(5)
        async with httpx.AsyncClient(headers=UA) as client:
            async def one(s):
                async with sem:
                    try:
                        raw = await build_raw(client, s)
                    except Exception as e:  # keep going; summarize from title only
                        log(f"  ! {s['id']} fetch failed: {e!r}")
                        raw = {"id": s["id"], "article": "", "comments": [], "error": repr(e)}
                    write_json(rdir / f"{s['id']}.json", raw)
            await gather_all(one(s) for s in todo)
    return {s["id"]: read_json(rdir / f"{s['id']}.json") for s in stories}


async def stage_story_summaries(llm: Summarizer, stories, raws, wdir: Path) -> dict[int, dict]:
    sdir = wdir / "3_story"
    todo = [s for s in stories if not (sdir / f"{s['id']}.json").exists()]
    if todo:
        log(f"Summarizing {len(todo)} stories with {llm.model}…")
        done = 0

        async def one(s):
            nonlocal done
            write_json(sdir / f"{s['id']}.json", await llm.story(s, raws[s["id"]]))
            done += 1
            if done % 20 == 0:
                log(f"  {done}/{len(todo)}")
        await gather_all(one(s) for s in todo)
    return {s["id"]: read_json(sdir / f"{s['id']}.json") for s in stories}


async def stage_topics(llm: Summarizer, stories, summaries, wdir: Path) -> list[dict]:
    p = wdir / "4_topics.json"
    if p.exists():
        return read_json(p)
    log("Grouping stories into topics…")
    by_id = {s["id"]: s for s in stories}
    grouped = await llm.group(stories, summaries)

    async def build(t):
        members = [(by_id[i], summaries[i]) for i in t["story_ids"]]
        if len(members) == 1:
            m = members[0][1]
            text = {"gist": m["gist"], "pulse": m["pulse"], "vibe": m["vibe"]}
        else:
            text = await llm.merge(members)
        return {**t, **text, "importance": sum(s["points"] for s, _ in members)}

    topics = await gather_all(build(t) for t in grouped)
    merged = sum(1 for t in topics if len(t["story_ids"]) > 1)
    log(f"  {len(stories)} stories → {len(topics)} topics ({merged} multi-story)")
    write_json(p, topics)
    return topics


async def stage_digest(cfg, llm: Summarizer, week, stories, topics, wdir: Path) -> dict:
    p = wdir / "5_digest.json"
    if p.exists():
        return read_json(p)
    by_id = {s["id"]: s for s in stories}

    def lead(t):  # highest-points story represents the topic's links
        return max((by_id[i] for i in t["story_ids"]), key=lambda s: s["points"])

    ranked = sorted(topics, key=lambda t: -t["importance"])
    top, rest = ranked[:TOP_BOXES], ranked[TOP_BOXES:]

    log(f"Writing {len(top)} compact boxes + headline…")
    compact = await llm.compact(top)
    boxes = []
    for t, c in zip(top, compact):
        s = lead(t)
        threads = [{"title": x["title"], "url": x["url"],
                    "hn_url": f"https://news.ycombinator.com/item?id={x['id']}"}
                   for x in sorted((by_id[i] for i in t["story_ids"]), key=lambda x: -x["points"])]
        boxes.append({"section": t["section"], "title": c["title"], "gist": c["gist"], "take": c["take"],
                      "mood": t["vibe"], "url": s["url"], "hn_url": threads[0]["hn_url"],
                      "n_stories": len(threads), "threads": threads})
    headline = await llm.headline(boxes)

    sections = [{"name": name, "boxes": [b for b in boxes if b["section"] == name]} for name in SECTIONS]
    sections = [s for s in sections if s["boxes"]]
    more = [{"title": lead(t)["title"], "url": lead(t)["url"],
             "hn_url": f"https://news.ycombinator.com/item?id={lead(t)['id']}"} for t in rest]

    days = week_days(week)
    digest = {
        "week": week,
        "range": f"{days[0]:%b %-d} – {days[-1]:%b %-d, %Y}",
        "generated": f"{datetime.now():%Y-%m-%d %H:%M}",
        "model": llm.model,
        "per_day": cfg.per_day,
        "stats": {"stories": len(stories), "comments": sum(s["comments"] for s in stories)},
        "headline": headline,
        "sections": sections,
        "more": more,
    }
    write_json(p, digest)
    return digest


def notify_and_open(page: Path, digest: dict) -> None:
    msg = f"{digest['range']} · {digest['stats']['stories']} stories"
    subprocess.run(["osascript", "-e",
                    f'display notification "{msg}" with title "Your HN weekly is ready"'], check=False)
    subprocess.run(["open", str(page)], check=False)


async def main() -> int:
    ap = argparse.ArgumentParser(prog="hn_weekly")
    ap.add_argument("--week", help="ISO week like 2026-W44 (default: previous week)")
    ap.add_argument("--dry-run", action="store_true", help="list picked stories, no LLM calls")
    ap.add_argument("--rerender", action="store_true", help="re-render HTML from cached digest")
    ap.add_argument("--if-missing", action="store_true", help="exit quietly if the page exists")
    ap.add_argument("--no-open", action="store_true", help="don't open the page / notify")
    args = ap.parse_args()

    cfg = load_config()
    week = args.week or previous_week()
    wdir = cfg.week_dir(week)
    page = cfg.output_dir / f"{week}.html"

    if args.if_missing and page.exists():
        return 0
    if week_days(week)[-1] >= date.today():
        print(f"{week} hasn't finished yet; pick an earlier week.", file=sys.stderr)
        return 2

    if args.rerender:
        digest = apply_heat(read_json(wdir / "5_digest.json"), wdir)
        bpath = wdir / "7_broadcast.json"
        if bpath.exists() and (cfg.output_dir / f"audio/{week}.mp3").exists():
            digest["broadcast"] = read_json(bpath)
        log(f"Re-rendered {render_week(digest, cfg.output_dir)}")
        return 0

    stories = await stage_stories(cfg, week, wdir)
    if args.dry_run:
        for s in stories:
            if s["rank_in_day"] == 1:
                print(f"\n== {date.fromisoformat(s['day']):%A %b %-d} ==")
            print(f"{s['rank_in_day']:>3}. {s['points']:>5} pts {s['comments']:>5} c  {s['title'][:80]}")
        print(f"\n{len(stories)} stories. (No OpenAI calls made.)")
        return 0

    if not cfg.api_key:
        print(f"OPENAI_API_KEY missing: add it to {Path(__file__).resolve().parent.parent / '.env'}",
              file=sys.stderr)
        return 1

    llm = Summarizer(cfg.api_key, cfg.model)
    raws = await stage_raw(stories, wdir)
    summaries = await stage_story_summaries(llm, stories, raws, wdir)
    topics = await stage_topics(llm, stories, summaries, wdir)
    digest = apply_heat(await stage_digest(cfg, llm, week, stories, topics, wdir), wdir)
    try:
        digest["broadcast"] = await stage_broadcast(cfg, llm, digest, week, wdir, log)
    except Exception as e:  # audio is a bonus: never block the page on it
        log(f"  ! audio bulletin failed, publishing without it: {e!r}")
    out = render_week(digest, cfg.output_dir)
    log(f"Done: {out}")
    if not args.no_open:
        notify_and_open(out, digest)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
