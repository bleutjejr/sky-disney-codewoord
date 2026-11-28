"""Instellingen voor de Sky Radio Disney-codewoord listener."""

from __future__ import annotations

import os
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "gevonden_codewoorden.json"


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "ja", "on"}

TIMEZONE = ZoneInfo("Europe/Amsterdam")
CAMPAIGN_START = date(2026, 11, 27)
CAMPAIGN_END = date(2026, 12, 24)

STREAM_URL = os.environ.get(
    "STREAM_URL",
    "https://playerservices.streamtheworld.com/api/livestream-redirect/SKYRADIO.mp3",
)
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_BASE = os.environ.get("NTFY_BASE", "https://ntfy.sh").rstrip("/")
CLICK_URL = (
    "https://www.skyradio.nl/acties/win-een-betoverende-kerst-in-disneyland-paris"
)

WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")
WHISPER_LANGUAGE = "nl"
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")
WHISPER_BEAM_SIZE = int(os.environ.get("WHISPER_BEAM_SIZE", "3"))
# Uit: de korte, hoge stem van Mickey wordt anders als geluidseffect weggeknipt.
WHISPER_VAD_FILTER = _env_flag("WHISPER_VAD_FILTER", False)
WHISPER_INITIAL_PROMPT = os.environ.get(
    "WHISPER_INITIAL_PROMPT",
    "Sky Radio Disney kerst actie. Vandaag is dat codewoord: Parijs, fantasie, "
    "betoverend, sneeuw, kasteel, kerstboom, rendier.",
)

SEGMENT_SECONDS = int(os.environ.get("SEGMENT_SECONDS", "45"))
# 45s opname, elke 35s een nieuwe start. De 10s overlap dekt de overgang
# van de presentator naar de stem van Mickey.
OVERLAP_SECONDS = int(os.environ.get("OVERLAP_SECONDS", "10"))
# Iets onder de 330 minuten, zodat afronden en de cache-stap binnen de job-timeout passen.
RUN_BUDGET_SECONDS = int(os.environ.get("RUN_BUDGET_SECONDS", str(325 * 60)))
# Langer dan de 35s tussen segmentstarts. Dezelfde aankondiging in twee
# overlappende segmenten telt één keer; een latere herhaling telt weer apart.
MIN_CONFIRMATION_GAP_SECONDS = int(os.environ.get("MIN_CONFIRMATION_GAP_SECONDS", "60"))
FUZZY_RATIO = float(os.environ.get("FUZZY_RATIO", "0.85"))
CONFIRMATIONS_REQUIRED = int(os.environ.get("CONFIRMATIONS_REQUIRED", "2"))
# Zet op true om elke zin met "disney" of "codewoord" in de Actions-log te zetten.
DEBUG_TRANSCRIPTS = _env_flag("DEBUG_TRANSCRIPTS", False)

KEYWORDS = (
    "codewoord",
    "disney",
    "disneyland",
    "woord van vandaag",
    "kerstcodewoord",
)

# Grammaticale woorden die geen codewoord zijn. Korte zelfstandige naamwoorden
# zoals "ster" of "sneeuw" blijven juist wel geldig.
STOPWORDS = {
    "de",
    "het",
    "een",
    "van",
    "vandaag",
    "dag",
    "voor",
    "naar",
    "met",
    "dat",
    "die",
    "deze",
    "dit",
    "ons",
    "onze",
    "jouw",
    "je",
    "jij",
    "u",
    "is",
    "luidt",
    "disney",
    "disneyland",
    "mickey",
    "mikkie",
    "mouse",
    "mickeymouse",
    "radio",
    "sky",
    "skyradio",
    "codewoord",
    "kerstcodewoord",
    "woord",
    "hetzelfde",
    "volgende",
    "ook",
    "nog",
    "niet",
    "wel",
    "maar",
    "want",
    "dus",
    "als",
    "dan",
    "bij",
    "uit",
    "aan",
    "tot",
    "over",
    "door",
    "en",
    "of",
    "op",
    "in",
    "te",
    "om",
    "luister",
    "noteer",
    "noteren",
}


def now_amsterdam() -> datetime:
    return datetime.now(TIMEZONE)


def campaign_bounds() -> tuple[datetime, datetime]:
    start = datetime.combine(CAMPAIGN_START, time(0, 0), tzinfo=TIMEZONE)
    end = datetime.combine(CAMPAIGN_END, time(23, 59, 59), tzinfo=TIMEZONE)
    return start, end


def in_campaign(moment: datetime) -> bool:
    start, end = campaign_bounds()
    return start <= moment <= end


def end_of_listening_day(day: date) -> datetime:
    """Laatste moment waarop deze kalenderdag nog beluisterd mag worden."""
    day_end = datetime.combine(day, time(23, 59, 59), tzinfo=TIMEZONE)
    return min(day_end, campaign_bounds()[1])
