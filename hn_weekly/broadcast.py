"""The 2-minute audio bulletin: an anchor script (LLM) read aloud by OpenAI TTS.

Stage 6 writes `6_script.json`: segments, each tied to a box (or to none).
Stage 7 synthesizes each segment as raw PCM, so every segment's duration is exact.
It joins them with short pauses, encodes one CBR MP3, and writes `7_broadcast.json`
with start/end times. The page uses those times to highlight the box being read.
"""

import asyncio
import json
import re
from pathlib import Path

import lameenc
from openai import AsyncOpenAI

from .summarize import Summarizer, _schema, STR

SAMPLE_RATE = 24_000          # OpenAI TTS PCM: 24kHz, 16-bit, mono
BYTES_PER_SEC = SAMPLE_RATE * 2
PAUSE = {"open": 0.4, "story": 0.5, "rapid": 0.25, "launches": 0.25, "wrap": 0.5, "signoff": 0.0}
KINDS = ["open", "story", "rapid", "launches", "wrap", "signoff"]
WORD_TARGET, WORD_MAX = 235, 260   # measured ~120 wpm → ≈ 2:00 of audio
WRAP_MAX = 30
N_TOP, N_QUICK = 3, 5

SCRIPT_FORMAT = _schema("bulletin", {
    "segments": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": KINDS},
            "box_id": {"type": "integer"},
            "title": STR,
            "text": STR,
        },
        "required": ["kind", "box_id", "title", "text"],
        "additionalProperties": False,
    }},
})

SCRIPT_PROMPT = f"""You are the anchor of "HN Weekly", a 2-minute spoken news bulletin about last week on Hacker News.
Viewers see a slide with the details while you speak, so say only the essentials, like a TV headline bulletin.
Write the script as segments, in this order:
1. open (box_id -1): ONE line, e.g. "This is HN Weekly, your week on Hacker News in two minutes." ~12 words.
2. story: exactly one per TOP box, in the given order. Exactly two sentences: what happened, then what
   Hacker News thinks (the main reaction or disagreement). ~45 words each.
3. rapid: exactly one per QUICK box, in order (launches included). ONE short sentence of fact plus the HN verdict,
   e.g. "Mistral released Large 4 weights, and HN cheered the open license." ~12 words each.
Never move a box to a different kind of segment, and never cover boxes that are not listed.
4. wrap (box_id -1): the whole week in ONE sentence, drawn from ALL HIGHLIGHTS (not just the stories above).
   Write it like a columnist's verdict: name the two or three concrete themes that ran through many highlights
   (e.g. "software quietly overstepping user consent", "decade-old bugs coming back to bite") and the mood.
   Do not list individual stories, do not repeat the HEADLINE, and avoid vague words like "shifts", "issues",
   "amid", "landscape". Max 25 words. No lead-in: the anchor already says "In one line" before it.
5. signoff (box_id -1): e.g. "That's the week. See you next Monday." ~7 words.
Rules:
- Total about {WORD_TARGET} words. NEVER exceed {WORD_MAX}. Cut adjectives, filler and transitions.
- This is read aloud by a text-to-speech voice. Write for the ear: short sentences, no markdown, no emoji, no URLs,
  no parentheses. Write numbers, money and abbreviations the way they are spoken
  ("five point six billion dollars", "C-V-E", "M6 chip").
- Neutral, crisp radio tone. No hype, and do not invent facts beyond what is given.
- "title" is a 2-5 word on-screen label for the segment (e.g. "Binance loses $5.6B").
- box_id must be one of the ids given, or -1 for open/wrap/signoff."""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("“", '"').replace("”", '"').replace("’", "'")).strip().lower()


def _words(segments) -> int:
    return sum(len(s["text"].split()) for s in segments)


def _story_id(hn_url: str) -> int:
    return int(hn_url.rsplit("=", 1)[1])


def build_inputs(digest: dict, wdir: Path) -> tuple[str, dict[str, list[int]]]:
    """Prompt input: top and quick boxes to read, plus every highlight for the one-line wrap."""
    topics = json.loads((wdir / "4_topics.json").read_text())
    topic_of = {i: t for t in topics for i in t["story_ids"]}
    boxes = sorted((b for s in digest["sections"] for b in s["boxes"]), key=lambda b: b["id"])

    def describe(b):
        t = topic_of[_story_id(b["hn_url"])]
        return (f"[box {b['id']}] {b['title']} (section: {b['section']}, mood: {b['mood']})\n"
                f"  what: {t['gist']}\n  hn: {t['pulse']}")

    top, rapid = boxes[:N_TOP], boxes[N_TOP:N_TOP + N_QUICK]
    highlights = [f"- {b['title']} ({b['section']}, {b['mood']}): {b['gist']}" for b in boxes]

    prompt = (f"WEEK: {digest['range']}\nHEADLINE: {digest['headline']}\n\n"
              "TOP:\n" + "\n".join(map(describe, top)) +
              "\n\nQUICK:\n" + "\n".join(map(describe, rapid)) +
              "\n\nALL HIGHLIGHTS:\n" + "\n".join(highlights))
    rundown = {"story": [b["id"] for b in top], "rapid": [b["id"] for b in rapid],
               "launches": [], "wrap": [-1]}
    return prompt, rundown


def problems(segments: list[dict], rundown: dict[str, list[int]]) -> list[str]:
    """Human-readable issues to feed back to the model for one retry."""
    out = []
    if (w := _words(segments)) > WORD_MAX:
        out.append(f"It is {w} words; the hard limit is {WORD_MAX}. Aim for {WORD_TARGET}.")
    for kind, ids in rundown.items():
        got = [s["box_id"] for s in segments if s["kind"] == kind]
        if got != ids:
            out.append(f"'{kind}' segments must cover exactly boxes {ids} in that order; you gave {got}.")
    for s in segments:
        if s["kind"] == "wrap" and len(s["text"].split()) > WRAP_MAX:
            out.append(f"The wrap is {len(s['text'].split())} words; keep it to one sentence of at most 25.")
    return out


def validate_script(segments: list[dict], digest: dict) -> list[dict]:
    valid_ids = {b["id"] for s in digest["sections"] for b in s["boxes"]}
    out = []
    for s in segments:
        if not s["text"].strip():
            continue
        if s["box_id"] not in valid_ids or s["kind"] == "wrap":
            s["box_id"] = -1
        if s["kind"] == "wrap":
            s["text"] = re.sub(r"^\s*in one line\W*", "", s["text"], flags=re.I)
        out.append(s)
    return out


async def write_script(llm: Summarizer, digest: dict, wdir: Path) -> list[dict]:
    prompt, rundown = build_inputs(digest, wdir)
    segs = (await llm._call(SCRIPT_PROMPT, prompt, SCRIPT_FORMAT))["segments"]
    if issues := problems(segs, rundown):
        print("  script retry:", "; ".join(issues), flush=True)
        segs = (await llm._call(
            SCRIPT_PROMPT, prompt + "\n\nYOUR PREVIOUS DRAFT:\n" + json.dumps(segs, ensure_ascii=False) +
            "\n\nFix these problems and return the full corrected script:\n- " + "\n- ".join(issues),
            SCRIPT_FORMAT))["segments"]
    return validate_script(segs, digest)


async def synthesize(cfg, segments: list[dict]) -> list[bytes]:
    client = AsyncOpenAI(api_key=cfg.api_key, max_retries=5)
    sem = asyncio.Semaphore(4)

    async def one(seg):
        async with sem:
            r = await client.audio.speech.create(
                model=cfg.tts_model, voice=cfg.tts_voice,
                input=("In one line: " + seg["text"]) if seg["kind"] == "wrap" else seg["text"],
                instructions=cfg.tts_style, response_format="pcm")
            pcm = r.content
            return pcm[: len(pcm) // 2 * 2]  # whole 16-bit samples only
    return await asyncio.gather(*(one(s) for s in segments))


def encode_mp3(pcm: bytes) -> bytes:
    enc = lameenc.Encoder()
    enc.set_bit_rate(64)
    enc.set_in_sample_rate(SAMPLE_RATE)
    enc.set_channels(1)
    enc.set_quality(2)
    return bytes(enc.encode(pcm) + enc.flush())


def load_broadcast(bpath: Path, wdir: Path) -> dict:
    return json.loads(bpath.read_text())


async def stage_broadcast(cfg, llm: Summarizer, digest: dict, week: str, wdir: Path, log) -> dict | None:
    audio_rel = f"audio/{week}.mp3"
    audio_path = cfg.output_dir / audio_rel
    bpath = wdir / "7_broadcast.json"
    if bpath.exists() and audio_path.exists():
        return load_broadcast(bpath, wdir)

    spath = wdir / "6_script.json"
    if spath.exists():
        segments = json.loads(spath.read_text())
    else:
        log("Writing the anchor script…")
        segments = await write_script(llm, digest, wdir)
        spath.write_text(json.dumps(segments, ensure_ascii=False, indent=2))
    log(f"  script: {len(segments)} segments, {_words(segments)} words")

    log(f"Recording audio ({cfg.tts_voice})…")
    clips = await synthesize(cfg, segments)
    pcm, t, timeline = bytearray(), 0.0, []
    for seg, clip in zip(segments, clips):
        dur = len(clip) / BYTES_PER_SEC
        timeline.append({"kind": seg["kind"], "box_id": seg["box_id"], "title": seg["title"],
                         "text": seg["text"], "start": round(t, 2), "end": round(t + dur, 2)})
        pause = PAUSE.get(seg["kind"], 0.4)
        pcm += clip + bytes(int(pause * SAMPLE_RATE) * 2)
        t += dur + pause

    audio_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path.write_bytes(encode_mp3(bytes(pcm)))
    broadcast = {"audio": audio_rel, "duration": round(t, 1), "voice": cfg.tts_voice, "segments": timeline}
    bpath.write_text(json.dumps(broadcast, ensure_ascii=False, indent=2))
    log(f"  audio: {int(t // 60)}:{int(t % 60):02d}, {audio_path.stat().st_size / 1e6:.1f} MB → {audio_path}")
    return broadcast
