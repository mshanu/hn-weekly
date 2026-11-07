# HN Weekly

A Monday digest of last week's Hacker News, built from the top 20 stories of each day. It is
summarized with OpenAI, grouped into topics, and shown as a page of minimal boxes with heat
indicators and the community's take on each topic.

## How it works

```
GitHub Actions (Mon 02:30 UTC = 08:00 IST)
  └─ python -m hn_weekly
       1. collect   top 20 stories/day           (HN Algolia API)
       2. enrich    article text + top comments  (Algolia items + HN Firebase ranking)
       3. summarize one OpenAI call per story
       4. group     stories about the same event → topics
       5. compact   20 hottest topics → boxes + one-line headline
       6. script    ~580-word anchor script (top 3, rapid fire, launches, verbatim comment of the week)
       7. audio     OpenAI TTS per segment (voice: coral) → one ~5-minute MP3 + exact segment timings
       render       site/<week>.html (player highlights the box being read) + site/index.html
  └─ commit site/*.html · publish MP3 as release digest-<week> · deploy site/ (+ last 12 weeks' audio) to Pages
```

The stage JSON is written to `DATA_DIR`. In CI that is a temporary folder that is thrown away
after each run. Only the finished HTML pages are committed. The archive page is rebuilt from the
`<meta name="hn-…">` tags in those pages.

Audio is stored as GitHub Release assets, which keeps it out of git history. Each deploy pulls the
last 12 weeks' MP3s into the site.

Cost: GitHub is free (public repo). OpenAI is about $0.40 a week: about $0.30 for summaries and
$0.10 for the script and voice.

## Setup (one-time)

1. Add the repo secret `OPENAI_API_KEY`. Optionally, set the repo variable `OPENAI_MODEL`.
2. Settings → Pages → Source: **GitHub Actions**.

## Common tasks

| Task | How |
|---|---|
| Build a past week | Actions → weekly-digest → Run workflow → `week = 2026-W43` |
| Rebuild an existing week | Delete `site/<week>.html`, commit, then run the workflow for that week |
| Change the schedule | Edit `cron` in `.github/workflows/weekly.yml` (it is in UTC) |
| Run locally | `cp .env.example .env` (add your key), then `OUTPUT_DIR=site uv run python -m hn_weekly` |
| Preview the picked stories (no OpenAI calls) | `uv run python -m hn_weekly --dry-run` |

Settings (environment variables or `.env`): `OPENAI_API_KEY`, `OPENAI_MODEL`, `OUTPUT_DIR`,
`DATA_DIR`, `PER_DAY`, `OPENAI_TTS_MODEL`, `TTS_VOICE`, `TTS_STYLE`.
