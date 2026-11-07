import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_DIR / ".env")

SECTIONS = [
    "AI & ML",
    "Dev Tools & Programming",
    "Launches & Repos",
    "Security & Privacy",
    "Business & Startups",
    "Science & Hardware",
    "Society, Policy & Culture",
    "Other",
]


DEFAULT_TTS_STYLE = (
    "Crisp radio news anchor. Brisk, upbeat pace, confident and neutral, with clear diction. "
    "Slight emphasis on numbers and names. Short natural pause between sentences."
)


@dataclass(frozen=True)
class Config:
    output_dir: Path
    data_dir: Path
    per_day: int
    model: str
    api_key: str | None
    tts_model: str
    tts_voice: str
    tts_style: str

    def week_dir(self, week: str) -> Path:
        return self.data_dir / week


def load_config() -> Config:
    output_dir = Path(os.path.expanduser(os.getenv("OUTPUT_DIR", "~/HNWeekly")))
    return Config(
        output_dir=output_dir,
        # Stage JSON cache. In CI this points at a temp dir and is discarded after the run.
        data_dir=Path(os.path.expanduser(os.getenv("DATA_DIR", str(output_dir / "data")))),
        per_day=int(os.getenv("PER_DAY", "20")),
        model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        api_key=os.getenv("OPENAI_API_KEY") or None,
        tts_model=os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts"),
        tts_voice=os.getenv("TTS_VOICE", "coral"),
        tts_style=os.getenv("TTS_STYLE", DEFAULT_TTS_STYLE),
    )
