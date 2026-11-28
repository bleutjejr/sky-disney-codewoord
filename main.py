#!/usr/bin/env python3
"""Luister naar Sky Radio en bevestig het Disney-codewoord van de dag.

De opname loopt in segmenten van 45 seconden met 10 seconden overlap
(elke 35 seconden een nieuwe start). Een kandidaat telt pas als geverifieerd
nadat hij twee keer onafhankelijk is gehoord (fuzzy-drempel 0.85, minimale
tussenpauze 60 seconden). De eerste treffer gaat meteen als stille ntfy-melding
de deur uit.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

import config

log = logging.getLogger("codewoord")

# Scheiding tussen de vaste woorden van de promo: spaties, of : , . ertussen.
_GAP = r"[\s\"“”‘’'’:.,;!?\-–—]+"
_CODEWOORD = r"\b(?:kerst)?codewoord\b"
# Langste vorm eerst, zodat "vandaag is dat codewoord" niet uit elkaar valt
# in het kortere "dat codewoord" of los "codewoord".
CUE_RE = re.compile(
    rf"(?P<cue>"
    rf"\bvandaag{_GAP}is{_GAP}dat{_GAP}{_CODEWOORD}"
    rf"|\bis{_GAP}dat{_GAP}{_CODEWOORD}"
    rf"|\bdat{_GAP}{_CODEWOORD}"
    rf"|\b(?:het|de){_GAP}{_CODEWOORD}"
    rf"|\bwoord{_GAP}van{_GAP}vandaag\b"
    rf"|{_CODEWOORD}"
    rf")",
    re.IGNORECASE,
)
TOKEN_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'\-]{0,40}")
# Hoeveel tokens ná de cue we nog naar het codewoord zoeken.
CUE_LOOKAHEAD = 10

MIN_WAV_BYTES = 44 + 16000 * 2 * 5
STATE_LOCK = threading.Lock()


class AmsterdamFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        moment = datetime.fromtimestamp(record.created, config.TIMEZONE)
        return moment.strftime(datefmt or "%Y-%m-%d %H:%M:%S")


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        AmsterdamFormatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def normalize_word(word: str) -> str:
    return word.casefold().replace("-", "").replace("'", "").strip()


def words_similar(left: str, right: str) -> float:
    return SequenceMatcher(None, normalize_word(left), normalize_word(right)).ratio()


def collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [collapse(part) for part in parts if collapse(part)]


def has_keyword(text: str) -> bool:
    folded = text.casefold()
    return any(keyword in folded for keyword in config.KEYWORDS)


def clean_candidate(raw: str) -> str | None:
    word = raw.strip(".,!?;:\"“”‘’()[]{}").strip("-'")
    if len(normalize_word(word)) < 3:
        return None
    if normalize_word(word) in config.STOPWORDS:
        return None
    if not re.fullmatch(r"[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'\-]{2,40}", word):
        return None
    return word


def cue_specificity(cue: str) -> int:
    """Hogere score = dichter bij de echte promo 'Vandaag is dat codewoord:'."""
    folded = collapse(cue).casefold()
    if "vandaag" in folded and "is" in folded and "codewoord" in folded:
        return 3
    if re.search(r"\bdat\b", folded) and "codewoord" in folded:
        return 2
    if "woord van vandaag" in folded or folded.endswith(":"):
        return 2
    return 1


def next_meaningful_word(text: str, start: int, limit: int = CUE_LOOKAHEAD) -> tuple[str, int] | None:
    """Eerste woord na start dat geen stopwoord of leesteken is."""
    seen = 0
    for match in TOKEN_RE.finditer(text, start):
        seen += 1
        if seen > limit:
            return None
        cleaned = clean_candidate(match.group(0))
        if cleaned is None:
            continue
        return cleaned, match.end()
    return None


def phrase_around(text: str, cue_start: int, word_end: int) -> str:
    """De promozin: vanaf de cue (of het laatste leesteken ervoor) tot en met het woord."""
    left = max(0, cue_start - 80)
    for index in range(cue_start - 1, left - 1, -1):
        if text[index] in ".!?\n":
            left = index + 1
            break
    return collapse(text[left:word_end])[:400]


def _remember(found: dict[str, tuple[int, str, str]], word: str, phrase: str, score: int) -> None:
    key = normalize_word(word)
    previous = found.get(key)
    if previous is None or score > previous[0] or (score == previous[0] and len(phrase) > len(previous[2])):
        found[key] = (score, word, phrase)


def candidates_in_text(text: str) -> tuple[dict[str, tuple[int, str, str]], bool]:
    """Zoek codewoorden na een cue. Tweede waarde: er was een cue zonder woord."""
    found: dict[str, tuple[int, str, str]] = {}
    bare_cue = False
    for match in CUE_RE.finditer(text):
        score = cue_specificity(match.group("cue"))
        # Een dubbele punt direct na de cue hoort bij de promo, ook bij los "codewoord:".
        tail = text[match.end() : match.end() + 4]
        if ":" in tail:
            score = max(score, 2)
        hit = next_meaningful_word(text, match.end())
        if hit is None:
            bare_cue = True
            continue
        word, word_end = hit
        _remember(found, word, phrase_around(text, match.start(), word_end), score)
    return found, bare_cue


def _prefer_specific(found: dict[str, tuple[int, str, str]]) -> list[tuple[str, str]]:
    if not found:
        return []
    best = max(item[0] for item in found.values())
    # Een vage 'het codewoord' mag een latere, precieze promo niet overstemmen.
    if best >= 2:
        found = {key: item for key, item in found.items() if item[0] >= 2}
    return [(word, phrase) for _score, word, phrase in found.values()]


def _sentence_with(text: str, needle: str) -> str:
    target = needle.casefold()
    for sentence in split_sentences(text):
        if target in sentence.casefold():
            return sentence
    return collapse(text)[:400]


def extract_from_parts(parts: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """Geef (woord, zin) en zinnen waarin 'codewoord' viel maar geen woord volgde.

    Whisper zet de stem van Mickey soms in het segment direct na de cue.
    Die twee segmenten worden samen gelezen, en ook de samengevoegde tekst.
    """
    cleaned = [collapse(part) for part in parts if collapse(part)]
    found: dict[str, tuple[int, str, str]] = {}
    unresolved: list[str] = []

    for index, part in enumerate(cleaned):
        part_found, bare = candidates_in_text(part)
        for key, item in part_found.items():
            previous = found.get(key)
            if previous is None or item[0] > previous[0]:
                found[key] = item
        if bare and index + 1 < len(cleaned):
            follower = cleaned[index + 1]
            tokens = TOKEN_RE.findall(follower)
            content = [token for token in tokens if clean_candidate(token)]
            # Mickey zegt één woord; een langer volgend segment is gewone presentatie.
            if 0 < len(tokens) <= 4 and len(content) == 1:
                hit = next_meaningful_word(follower, 0, limit=3)
                if hit is not None:
                    word, word_end = hit
                    phrase = collapse(f"{part} {follower[:word_end]}")[:400]
                    _remember(found, word, phrase, 3)
                    bare = False
                    log.info("Codewoord uit het Whisper-segment direct na de cue: %s", word)
        if bare and CUE_RE.search(part):
            unresolved.append(_sentence_with(part, "codewoord") if "codewoord" in part.casefold() else collapse(part)[:400])

    joined = collapse(" ".join(parts))
    joined_found, joined_bare = candidates_in_text(joined)
    for key, item in joined_found.items():
        previous = found.get(key)
        if previous is None or item[0] > previous[0] or (
            item[0] == previous[0] and "codewoord" in item[2].casefold() and "codewoord" not in previous[2].casefold()
        ):
            found[key] = item

    winners = _prefer_specific(found)
    if winners:
        return winners, []
    if not unresolved and joined_bare and "codewoord" in joined.casefold():
        unresolved.append(_sentence_with(joined, "codewoord"))
    # Eén melding per chunk is genoeg; de kortste zin met het woord codewoord.
    unresolved = [sentence for sentence in unresolved if "codewoord" in sentence.casefold()]
    if not unresolved:
        return [], []
    unresolved.sort(key=len)
    return [], [unresolved[0]]


def blank_day() -> dict:
    return {
        "verified": None,
        "verified_at": None,
        "notified": False,
        "phrases": [],
        "candidates": [],
        "unresolved_alerts": [],
    }


def load_state() -> dict:
    if not config.STATE_PATH.exists():
        return {}
    try:
        data = json.loads(config.STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"gevonden_codewoorden.json is ongeldig: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit("gevonden_codewoorden.json moet een JSON-object zijn.")
    return data


def save_state(state: dict) -> None:
    payload = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    temporary = config.STATE_PATH.with_suffix(".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(config.STATE_PATH)


def ensure_day(state: dict, day: date) -> dict:
    key = day.isoformat()
    current = state.get(key)
    if not isinstance(current, dict):
        current = blank_day()
        state[key] = current
    current.setdefault("verified", None)
    current.setdefault("verified_at", None)
    current.setdefault("notified", False)
    current.setdefault("phrases", [])
    current.setdefault("candidates", [])
    current.setdefault("unresolved_alerts", [])
    return current


def choose_display(hearings: list[dict]) -> str:
    tally: dict[str, int] = {}
    for hearing in hearings:
        heard = hearing["heard_as"]
        tally[heard] = tally.get(heard, 0) + 1
    best = hearings[-1]["heard_as"]
    best_score = (-1, -1)
    for index, hearing in enumerate(hearings):
        word = hearing["heard_as"]
        score = (tally[word], index)
        if score >= best_score:
            best_score = score
            best = word
    return best


def find_candidate(candidates: list[dict], word: str) -> tuple[dict | None, float]:
    best = None
    best_ratio = 0.0
    for candidate in candidates:
        ratio = words_similar(candidate["word"], word)
        for hearing in candidate.get("hearings", []):
            ratio = max(ratio, words_similar(hearing.get("heard_as", ""), word))
        if ratio >= config.FUZZY_RATIO and ratio > best_ratio:
            best = candidate
            best_ratio = ratio
    return best, best_ratio


def utf8_header(value: str) -> str:
    """requests verstuurt headers als latin-1. Deze omweg levert op de lijn UTF-8."""
    return value.encode("utf-8").decode("latin-1")


def normalize_topic(raw: str) -> str:
    topic = raw.strip()
    for prefix in ("https://ntfy.sh/", "http://ntfy.sh/", "ntfy.sh/"):
        if topic.startswith(prefix):
            topic = topic[len(prefix) :]
    topic = topic.strip().strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", topic):
        raise SystemExit(
            "NTFY_TOPIC ontbreekt of is ongeldig. Gebruik 1-128 tekens: letters, cijfers, _ of -."
        )
    return topic


def send_ntfy(
    title: str,
    body: str,
    tags: str = "sparkles,radio,white_check_mark",
    priority: str = "high",
) -> None:
    import requests

    topic = normalize_topic(config.NTFY_TOPIC)
    headers = {
        "Title": utf8_header(title),
        "Priority": priority,
        "Tags": tags,
        "Click": config.CLICK_URL,
        "Content-Type": "text/plain; charset=utf-8",
    }
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            response = requests.post(
                f"{config.NTFY_BASE}/{topic}",
                data=body.encode("utf-8"),
                headers=headers,
                timeout=30,
            )
            if response.status_code >= 300:
                raise RuntimeError(
                    f"ntfy antwoordde {response.status_code}: {response.text[:300]}"
                )
            log.info("ntfy-notificatie verstuurd (priority %s).", priority)
            return
        except Exception as exc:
            last_error = exc
            log.warning("ntfy-poging %s mislukt: %s", attempt, exc)
            if attempt < 3:
                time.sleep(5)
    raise RuntimeError(f"ntfy bleef falen: {last_error}") from last_error


def notification_body(word: str, phrases: list[str]) -> str:
    lines = [word, ""]
    for index, phrase in enumerate(phrases[:2], start=1):
        lines.append(f"{index}. {phrase}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def send_verified(word: str, phrases: list[str]) -> None:
    title = f"🎄 Geverifieerd Disney Codewoord: {word}"
    send_ntfy(title, notification_body(word, phrases))


def send_candidate_alert(word: str, phrase: str) -> None:
    """Stille melding bij de eerste treffer, of als het woord niet te isoleren was."""
    title = f"🔎 Kandidaat gehoord (1/{config.CONFIRMATIONS_REQUIRED}): {word}"
    send_ntfy(title, phrase.strip() + "\n", tags="mag,radio", priority="low")


def send_test_notification() -> None:
    word = "TEST"
    title = f"🎄 Geverifieerd Disney Codewoord: {word}"
    body = (
        f"{word}\n\n"
        "1. Dit is een test van de ntfy-notificatie. Er is niet naar Sky Radio geluisterd.\n\n"
        "2. Zie je deze melding, dan staan topic, titel, prioriteit, tags en click-link goed.\n"
    )
    send_ntfy(title, body)


def set_relay(enabled: bool) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    value = "true" if enabled else "false"
    log.info("Estafette-relay: %s", value)
    if not output:
        return
    with open(output, "a", encoding="utf-8") as handle:
        handle.write(f"relay={value}\n")


def ffmpeg_command(path: Path) -> list[str]:
    return [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-rw_timeout",
        "15000000",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "5",
        "-user_agent",
        "Mozilla/5.0 (compatible; SkyCodewoordListener/1.0)",
        "-i",
        config.STREAM_URL,
        "-t",
        str(config.SEGMENT_SECONDS),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "wav",
        str(path),
    ]


def start_ffmpeg(path: Path) -> subprocess.Popen:
    kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(ffmpeg_command(path), **kwargs)


def stop_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        log.warning("ffmpeg stopte niet binnen 5 seconden.")


def record_loop(
    audio: queue.Queue,
    stop: threading.Event,
    verified: threading.Event,
    temp_dir: Path,
    fatal: list[BaseException],
) -> None:
    step = config.SEGMENT_SECONDS - config.OVERLAP_SECONDS
    if step <= 0:
        raise RuntimeError("OVERLAP_SECONDS moet kleiner zijn dan SEGMENT_SECONDS")
    next_start = time.monotonic()
    sequence = 0
    active: list[dict] = []

    def reap() -> None:
        still: list[dict] = []
        for item in active:
            proc = item["proc"]
            if proc.poll() is None:
                still.append(item)
                continue
            path: Path = item["path"]
            size = path.stat().st_size if path.exists() else 0
            failed = proc.returncode != 0 or size < MIN_WAV_BYTES
            if verified.is_set() or failed:
                if failed and not verified.is_set():
                    log.warning(
                        "Opname %s mislukt (exit %s, %s bytes).",
                        path.name,
                        proc.returncode,
                        size,
                    )
                path.unlink(missing_ok=True)
                continue
            log.info(
                "Segment %s klaar (%.0f KB, start %s).",
                path.name,
                path.stat().st_size / 1024,
                item["started"].strftime("%H:%M:%S"),
            )
            audio.put(item)
        active[:] = still

    try:
        while not stop.is_set():
            reap()
            now = time.monotonic()
            if now < next_start:
                stop.wait(min(0.5, next_start - now))
                continue
            sequence += 1
            path = temp_dir / f"seg-{sequence:05d}.wav"
            started = config.now_amsterdam()
            proc = start_ffmpeg(path)
            active.append({"proc": proc, "path": path, "started": started})
            log.info(
                "Opname %s gestart (%ss, overlap %ss).",
                path.name,
                config.SEGMENT_SECONDS,
                config.OVERLAP_SECONDS,
            )
            next_start = time.monotonic() + step
        if verified.is_set():
            return
        # Laat een lopende opname aflopen zodat het laatste stuk niet wordt afgekapt.
        for item in list(active):
            remaining = config.SEGMENT_SECONDS + 15
            try:
                item["proc"].wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                stop_process(item["proc"])
        reap()
    except Exception as exc:
        fatal.append(exc)
        stop.set()
        log.exception("Opname gestopt.")
    finally:
        for item in active:
            stop_process(item["proc"])
            item["path"].unlink(missing_ok=True)


def load_model():
    from faster_whisper import WhisperModel

    threads = max(1, min(4, os.cpu_count() or 1))
    log.info(
        "Whisper laden: model=%s compute=%s device=%s threads=%s beam=%s vad=%s",
        config.WHISPER_MODEL,
        config.WHISPER_COMPUTE_TYPE,
        config.WHISPER_DEVICE,
        threads,
        config.WHISPER_BEAM_SIZE,
        config.WHISPER_VAD_FILTER,
    )
    return WhisperModel(
        config.WHISPER_MODEL,
        device=config.WHISPER_DEVICE,
        compute_type=config.WHISPER_COMPUTE_TYPE,
        cpu_threads=threads,
    )


def usable_segment_text(segment) -> str | None:
    """Tekst van een Whisper-segment, of None als de kans te laag is.

    De afgewezen tekst wordt niet teruggegeven en dus ook niet gelogd.
    """
    text = collapse(getattr(segment, "text", None) or "")
    if not text:
        return None
    avg_logprob = getattr(segment, "avg_logprob", None)
    if avg_logprob is not None and avg_logprob < config.WHISPER_MIN_AVG_LOGPROB:
        return None
    return text


def transcribe(model, path: Path) -> list[str]:
    """Transcribeer één segment. VAD uit, zodat de hoge stem van Mickey blijft staan.

    Segmenten met een te lage kans (muziek, effecten, onverstaanbare stem) vallen
    weg. De tekst daarvan gaat niet naar de log, zodat een verzinsel niet blijft hangen.
    """
    started = time.monotonic()
    segments, _info = model.transcribe(
        str(path),
        language=config.WHISPER_LANGUAGE,
        beam_size=config.WHISPER_BEAM_SIZE,
        best_of=1,
        condition_on_previous_text=False,
        temperature=0.0,
        vad_filter=config.WHISPER_VAD_FILTER,
        initial_prompt=config.WHISPER_INITIAL_PROMPT,
        no_speech_threshold=config.WHISPER_NO_SPEECH_THRESHOLD,
        log_prob_threshold=config.WHISPER_LOG_PROB_THRESHOLD,
        compression_ratio_threshold=config.WHISPER_COMPRESSION_RATIO_THRESHOLD,
    )
    parts: list[str] = []
    dropped = 0
    for segment in segments:
        text = usable_segment_text(segment)
        if text is None:
            if collapse(getattr(segment, "text", None) or ""):
                dropped += 1
            continue
        parts.append(text)
    log.info(
        "Transcriptie %s in %.1fs (%s whisper-segmenten, %s genegeerd wegens lage kans, beam=%s, vad=%s).",
        path.name,
        time.monotonic() - started,
        len(parts),
        dropped,
        config.WHISPER_BEAM_SIZE,
        config.WHISPER_VAD_FILTER,
    )
    return parts


def log_debug_sentences(text: str) -> None:
    if not config.DEBUG_TRANSCRIPTS:
        return
    for sentence in split_sentences(text):
        folded = sentence.casefold()
        if "disney" in folded or "codewoord" in folded:
            log.info("DEBUG transcript: %s", sentence)


def register_hearing(
    state: dict,
    day: date,
    word: str,
    phrase: str,
    heard_at: datetime,
) -> bool:
    """Registreer één onafhankelijke treffer. True zodra het woord geverifieerd is."""
    with STATE_LOCK:
        bucket = ensure_day(state, day)
        if bucket.get("verified"):
            return False
        candidate, ratio = find_candidate(bucket["candidates"], word)
        if candidate is None:
            candidate = {"word": word, "count": 0, "hearings": []}
            bucket["candidates"].append(candidate)
            ratio = 1.0
        last = candidate["hearings"][-1] if candidate["hearings"] else None
        if last is not None:
            previous = datetime.fromisoformat(last["at"])
            gap = heard_at - previous
            if gap < timedelta(seconds=config.MIN_CONFIRMATION_GAP_SECONDS):
                log.info(
                    "Kandidaat '%s' binnen %s seconden opnieuw gehoord; telt niet apart (overlap).",
                    candidate["word"],
                    int(gap.total_seconds()),
                )
                if candidate.get("preliminary_notified"):
                    return False
                display = candidate["word"]
                phrase = candidate["hearings"][0]["phrase"]
                first_time = True
                confirmed = False
                phrases = [phrase]
            else:
                first_time = False
        else:
            first_time = False
        if last is None or first_time is False:
            candidate["hearings"].append(
                {
                    "at": heard_at.isoformat(timespec="seconds"),
                    "phrase": phrase,
                    "heard_as": word,
                }
            )
            candidate["count"] = len(candidate["hearings"])
            candidate["word"] = choose_display(candidate["hearings"])
            count = candidate["count"]
            display = candidate["word"]
            confirmed = count >= config.CONFIRMATIONS_REQUIRED
            phrases = [hearing["phrase"] for hearing in candidate["hearings"][:2]]
            if confirmed:
                bucket["verified"] = display
                bucket["verified_at"] = heard_at.isoformat(timespec="seconds")
                bucket["notified"] = False
                bucket["phrases"] = phrases
            first_time = count == 1 and not candidate.get("preliminary_notified")
            save_state(state)
            log.info(
                "Kandidaat '%s' gehoord als '%s' (ratio %.2f), telling %s/%s. Zin: %s",
                display,
                word,
                ratio,
                count,
                config.CONFIRMATIONS_REQUIRED,
                phrase,
            )
    if first_time:
        try:
            send_candidate_alert(display, phrase)
        except Exception:
            log.exception("Eerste melding mislukt; luisteren gaat door.")
        else:
            with STATE_LOCK:
                candidate["preliminary_notified"] = True
                save_state(state)
            log.info("Lage-prioriteit melding verstuurd (1/%s): %s", config.CONFIRMATIONS_REQUIRED, display)
    if not confirmed:
        return False

    send_verified(display, phrases)
    with STATE_LOCK:
        bucket = ensure_day(state, day)
        bucket["notified"] = True
        save_state(state)
    log.info("Codewoord geverifieerd en gemeld: %s", display)
    return True


def register_unresolved(state: dict, day: date, phrase: str, heard_at: datetime) -> None:
    """Lage-prioriteit melding als een zin 'codewoord' bevat maar geen woord oplevert."""
    with STATE_LOCK:
        bucket = ensure_day(state, day)
        if bucket.get("verified"):
            return
        alerts = bucket.setdefault("unresolved_alerts", [])
        for note in alerts:
            previous = datetime.fromisoformat(note["at"])
            if heard_at - previous < timedelta(seconds=config.MIN_CONFIRMATION_GAP_SECONDS):
                earlier = collapse(note.get("phrase", "")).casefold()
                if SequenceMatcher(None, earlier, collapse(phrase).casefold()).ratio() >= 0.85:
                    log.info("Zin met codewoord lijkt op een recente melding; overlap telt niet opnieuw.")
                    return
        alerts.append({"at": heard_at.isoformat(timespec="seconds"), "phrase": phrase})
        if len(alerts) > 40:
            del alerts[:-40]
        save_state(state)
    try:
        send_candidate_alert("?", phrase)
    except Exception:
        log.exception("Melding zonder geïsoleerd woord mislukt; luisteren gaat door.")
        with STATE_LOCK:
            alerts = ensure_day(state, day).get("unresolved_alerts") or []
            if alerts and alerts[-1].get("phrase") == phrase:
                alerts.pop()
                save_state(state)
        return
    log.info("Lage-prioriteit melding verstuurd voor een zin zonder geïsoleerd woord.")


def process_loop(
    audio: queue.Queue,
    stop: threading.Event,
    state: dict,
    day: date,
    verified: threading.Event,
    fatal: list[BaseException],
) -> None:
    try:
        model = load_model()
    except Exception as exc:
        fatal.append(exc)
        stop.set()
        log.exception("Whisper laden mislukt.")
        return
    while not verified.is_set():
        try:
            item = audio.get(timeout=0.5)
        except queue.Empty:
            if stop.is_set():
                return
            continue
        path: Path = item["path"]
        try:
            if verified.is_set():
                continue
            try:
                parts = transcribe(model, path)
            except Exception:
                log.exception("Transcriptie van %s mislukt.", path.name)
                continue
            text = collapse(" ".join(parts))
            if not text:
                log.info("Leeg transcript voor %s.", path.name)
                continue
            log_debug_sentences(text)
            if not has_keyword(text):
                log.info("Geen trefwoord in %s (%s tekens).", path.name, len(text))
                continue
            candidates, unresolved = extract_from_parts(parts)
            if not candidates and not unresolved:
                log.info("Trefwoord in %s, geen codewoord-zin (%s tekens).", path.name, len(text))
                continue
            heard_at = item["started"]
            verified_now = False
            for word, phrase in candidates:
                if register_hearing(state, day, word, phrase, heard_at):
                    verified_now = True
                    break
            if not candidates:
                for phrase in unresolved:
                    register_unresolved(state, day, phrase, heard_at)
            if verified_now:
                verified.set()
                stop.set()
                return
        except Exception as exc:
            bucket = state.get(day.isoformat(), {})
            if isinstance(bucket, dict) and bucket.get("verified") and not bucket.get("notified"):
                fatal.append(exc)
                stop.set()
                log.exception("Melden van het geverifieerde codewoord mislukt.")
                return
            log.exception("Verwerking van %s mislukt.", path.name)
        finally:
            path.unlink(missing_ok=True)
            audio.task_done()


def drain_audio(audio: queue.Queue) -> None:
    while True:
        try:
            item = audio.get_nowait()
        except queue.Empty:
            return
        item["path"].unlink(missing_ok=True)
        audio.task_done()


def listen(state: dict, day: date) -> str:
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg staat niet op PATH. Installeer ffmpeg en probeer opnieuw.")
    normalize_topic(config.NTFY_TOPIC)

    stop = threading.Event()
    verified = threading.Event()
    fatal: list[BaseException] = []
    audio: queue.Queue = queue.Queue()
    temp_dir = Path(tempfile.mkdtemp(prefix="skyradio-"))
    deadline = time.monotonic() + config.RUN_BUDGET_SECONDS
    day_end = config.end_of_listening_day(day)
    log.info(
        "Luisteren voor %s tot %s (budget %s minuten).",
        day.isoformat(),
        day_end.strftime("%H:%M"),
        config.RUN_BUDGET_SECONDS // 60,
    )

    recorder = threading.Thread(
        target=record_loop,
        name="recorder",
        args=(audio, stop, verified, temp_dir, fatal),
        daemon=True,
    )
    processor = threading.Thread(
        target=process_loop,
        name="processor",
        args=(audio, stop, state, day, verified, fatal),
        daemon=True,
    )
    recorder.start()
    processor.start()
    reason = "budget"
    heartbeat = time.monotonic()
    try:
        while not stop.is_set():
            now = config.now_amsterdam()
            if now >= day_end:
                reason = "end_of_day"
                log.info("Einde van de luisterdag (%s). Geen nieuwe runner.", day.isoformat())
                stop.set()
                break
            if time.monotonic() >= deadline:
                reason = "budget"
                log.info("Runner-budget bereikt. Estafette kan de volgende runner starten.")
                stop.set()
                break
            if time.monotonic() - heartbeat >= 600:
                log.info("Nog aan het luisteren. Wachtrij: %s segmenten.", audio.qsize())
                heartbeat = time.monotonic()
            stop.wait(1)
        processor.join(timeout=600)
        if processor.is_alive():
            log.warning("Transcriptie liep nog na het stoppen; resterende segmenten worden verwijderd.")
        recorder.join(timeout=90)
        if verified.is_set():
            reason = "verified"
        elif fatal:
            raise fatal[0]
        return reason
    finally:
        stop.set()
        drain_audio(audio)
        shutil.rmtree(temp_dir, ignore_errors=True)


def clear_today(state: dict, day: date) -> None:
    """Wis kandidaten en een eventuele bevestiging van deze kalenderdag.

    Het eerdere woord wordt niet gelogd. De workflow schrijft dit bestand daarna
    terug naar de Actions-cache, zodat de volgende run de lege teller erft.
    """
    state[day.isoformat()] = blank_day()
    save_state(state)
    log.info("Teller van %s gewist.", day.isoformat())


def retry_pending_notification(state: dict, day: date) -> None:
    bucket = ensure_day(state, day)
    word = bucket.get("verified")
    phrases = list(bucket.get("phrases") or [])
    if not word or bucket.get("notified"):
        return
    if len(phrases) < 2:
        raise SystemExit(
            f"Codewoord {word} staat vast, maar er zijn geen twee zinnen opgeslagen om te melden."
        )
    send_verified(word, phrases)
    bucket["notified"] = True
    save_state(state)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Vang het Disney-codewoord van Sky Radio.")
    parser.add_argument(
        "--test",
        action="store_true",
        help="Stuur direct een ntfy-test en stop, ook buiten de actieperiode.",
    )
    parser.add_argument(
        "--reset-today",
        action="store_true",
        help="Wis de teller van vandaag en luister opnieuw. Het vorige woord wordt niet gelogd.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = build_parser().parse_args(argv)
    relay = False
    try:
        now = config.now_amsterdam()
        if args.reset_today:
            clear_today(load_state(), now.date())

        if args.test:
            send_test_notification()
            return 0

        if not config.in_campaign(now):
            start, end = config.campaign_bounds()
            log.info(
                "Buiten de actieperiode (%s t/m %s, Europe/Amsterdam). Stop.",
                start.strftime("%Y-%m-%d %H:%M"),
                end.strftime("%Y-%m-%d %H:%M"),
            )
            return 0

        day = now.date()
        state = load_state()
        bucket = ensure_day(state, day)
        if bucket.get("verified") and bucket.get("notified"):
            log.info(
                "Codewoord voor %s is al bevestigd: %s. Stop zonder nieuwe runner.",
                day.isoformat(),
                bucket["verified"],
            )
            return 0
        if bucket.get("verified") and not bucket.get("notified"):
            log.info("Codewoord staat vast maar is nog niet gemeld. Notificatie opnieuw sturen.")
            retry_pending_notification(state, day)
            return 0

        existing = len(bucket.get("candidates") or [])
        if existing:
            log.info("Eerder opgeslagen kandidaten voor %s: %s", day.isoformat(), existing)

        reason = listen(state, day)
        if reason == "budget":
            relay = config.now_amsterdam() < config.end_of_listening_day(day) - timedelta(minutes=2)
        else:
            relay = False
        return 0
    except KeyboardInterrupt:
        log.info("Afgebroken.")
        return 1
    except Exception:
        log.exception("Listener gestopt door een fout.")
        return 1
    finally:
        set_relay(relay)


if __name__ == "__main__":
    sys.exit(main())
