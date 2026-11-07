"""Render the digest JSON to a self-contained HTML page and regenerate the archive index."""

import html
import re
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES = Path(__file__).parent / "templates"
env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html", "j2"]))


def _day_label(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%a")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


env.filters["day"] = _day_label
env.filters["slug"] = _slug
env.filters["mmss"] = lambda t: f"{int(t // 60)}:{int(t % 60):02d}"


def render_week(digest: dict, output_dir: Path) -> Path:
    out = output_dir / f"{digest['week']}.html"
    out.write_text(env.get_template("week.html.j2").render(d=digest), encoding="utf-8")
    render_index(output_dir)
    return out


META = re.compile(r'<meta name="hn-(week|range|headline)" content="([^"]*)">')


def render_index(output_dir: Path) -> Path:
    """Build the archive from the week pages' own meta tags, so no JSON needs to be kept."""
    weeks = []
    for f in sorted(output_dir.glob("*-W*.html"), reverse=True):
        meta = {k: html.unescape(v) for k, v in META.findall(f.read_text(encoding="utf-8"))}
        if "week" in meta:
            weeks.append({"week": meta["week"], "range": meta.get("range", meta["week"]),
                          "headline": meta.get("headline", "")})
    out = output_dir / "index.html"
    out.write_text(env.get_template("index.html.j2").render(weeks=weeks), encoding="utf-8")
    return out
