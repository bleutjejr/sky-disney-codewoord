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
# Alleen stijl en de cue. Geen concrete zelfstandige naamwoorden: Whisper
# hallucineert die anders als codewoord bij muziek of onverstaanbare audio.
WHISPER_INITIAL_PROMPT = os.environ.get(
    "WHISPER_INITIAL_PROMPT",
    "Sky Radio Disney actie. Vandaag is dat codewoord:",
)
# Streng tegen verzinsels. Een segment onder deze avg_logprob telt niet mee.
WHISPER_NO_SPEECH_THRESHOLD = 0.6
WHISPER_LOG_PROB_THRESHOLD = -1.0
WHISPER_COMPRESSION_RATIO_THRESHOLD = 2.4
WHISPER_MIN_AVG_LOGPROB = -1.0

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

# Eenmalige breuk met de Actions-cache. Epoch 4 wist 2026-11-28 (Zang en
# GELDERLAND) precies één keer, ook als een oudere cache toch binnenkomt.
# De workflow zelf gebruikt cache-sleutel codewoorden-v4.
STATE_EPOCH = 4
EPOCH_RESETS: dict[int, tuple[date, ...]] = {
    2: (date(2026, 11, 28),),
    4: (date(2026, 11, 28),),
}

# nl_core_news_sm heeft geen woordvectoren, dus token.is_oov is daar altijd
# True en zou ook Fantasie afwijzen. md is het kleinste Nederlandse model
# waarbij is_oov betekent: dit woord staat niet in het vocabulaire.
SPACY_MODEL = "nl_core_news_md"

# Whisper-hallucinaties, Engelse songtekst en zender-/opvulwoorden.
# "disney" staat hier bewust niet bij: dat kan zelf het codewoord zijn.
BLACKLIST = {
    "gelderland",
    "tv",
    "ondertiteling",
    "amara",
    "bedankt",
    "kijken",
    "zang",
    "song",
    "music",
    "christmas",
    "love",
    "sky",
    "radio",
    "app",
    "disneyland",
    "codewoord",
    "vandaag",
    "ga",
    "naar",
    "vul",
    "veel",
    "vol",
    "daar",
    "in",
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
