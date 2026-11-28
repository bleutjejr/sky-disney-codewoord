#!/usr/bin/env python3
"""Luister naar Sky Radio en bevestig het Disney-codewoord van de dag.

De opname loopt in segmenten van 45 seconden met 10 seconden overlap
(elke 35 seconden een nieuwe start). Het codewoord is het losse woord direct
na een van de twee promo's, en telt alleen als het één Nederlands zelfstandig
naamwoord is. Een kandidaat is pas geverifieerd nadat hij twee keer
onafhankelijk is gehoord (fuzzy-drempel 0.85, minimale tussenpauze 60 seconden).
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

# Het eerstvolgende losse woord direct na de twee promo-varianten.
# Whisper schrijft "vul" soms als "veel" of "vol".
PATTERN_DAT = re.compile(
    r"(?:vandaag\s+is\s+)?dat\s+codewoord[.,:!?\s]+([a-zA-ZÀ-ÿ-]+)",
    re.IGNORECASE,
)
PATTERN_IN = re.compile(
    r"(?:vul|veel|vol)?\s*daar\s+dit\s+codewoord\s+in[.,:!?\s]+([a-zA-ZÀ-ÿ-]+)",
    re.IGNORECASE,
)
_NOUNS = frozenset({"NOUN", "PROPN"})

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
    logging.getLogger("spacy").setLevel(logging.ERROR)


def normalize_word(word: str) -> str:
    return word.casefold().replace("-", "").replace("'", "").strip()


def words_similar(left: str, right: str) -> float:
    return SequenceMatcher(None, normalize_word(left), normalize_word(right)).ratio()


def collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def load_nlp():
    import spacy

    log.info("spaCy laden: %s", config.SPACY_MODEL)
    return spacy.load(config.SPACY_MODEL)


def _display_word(raw: str) -> str:
    return raw.strip().strip(".,!?;:\"“”‘’()[]{}").strip("-'")


def judge_word(nlp, raw: str) -> tuple[bool, str, str]:
    """Controleer één geëxtraheerd woord. Geeft (geldig, weergave, reden)."""
    word = _display_word(raw)
    key = normalize_word(word)
    if not key:
        return False, word, "afgewezen: geen zelfstandig naamwoord"
    if key in config.BLACKLIST:
        return False, word, "afgewezen: blacklist"
    if key == "disney":
        return True, word, "geaccepteerd"
    doc = nlp(word)
    tokens = [token for token in doc if not token.is_space and not token.is_punct]
    if len(tokens) != 1 or tokens[0].pos_ not in _NOUNS:
        return False, word, "afgewezen: geen zelfstandig naamwoord"
    if tokens[0].is_oov:
        return False, word, "afgewezen: niet in het Nederlandse vocabulaire"
    return True, word, "geaccepteerd"


def _sentence_spans(text: str) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    start = 0
    for match in re.finditer(r"(?<=[.!?])\s+|\n+", text):
        sentence = collapse(text[start:match.start()])
        if sentence:
            spans.append((start, match.start(), sentence))
        start = match.end()
    sentence = collapse(text[start:])
    if sentence:
        spans.append((start, len(text), sentence))
    return spans


def _pattern_matches(text: str) -> list[re.Match]:
    found: list[re.Match] = []
    seen: set[tuple[int, int]] = set()
    for pattern in (PATTERN_DAT, PATTERN_IN):
        for match in pattern.finditer(text):
            span = (match.start(1), match.end(1))
            if span in seen:
                continue
            seen.add(span)
            found.append(match)
    found.sort(key=lambda match: match.start(1))
    return found


def review_transcript(nlp, text: str) -> list[tuple[str, str]]:
    """Log elke zin met 'codewoord' en geef de geaccepteerde (woord, zin)-paren."""
    spans = _sentence_spans(text)
    if not spans and "codewoord" in text.casefold():
        spans = [(0, len(text), collapse(text))]
    matches = _pattern_matches(text)
    accepted: list[tuple[str, str]] = []
    for start, end, sentence in spans:
        if "codewoord" not in sentence.casefold():
            continue
        sentence_matches = [match for match in matches if start <= match.start() < end]
        if not sentence_matches:
            log.info("Zin: %s | geëxtraheerd: — | afgewezen: geen woord na het patroon", sentence)
            continue
        seen_words: set[str] = set()
        for match in sentence_matches:
            raw = match.group(1)
            key = normalize_word(raw)
            if key in seen_words:
                continue
            seen_words.add(key)
            ok, word, reason = judge_word(nlp, raw)
            if ok:
                log.info("Zin: %s | geëxtraheerd: %s | geaccepteerd", sentence, word)
                accepted.append((word, sentence))
            else:
                log.info("Zin: %s | geëxtraheerd: %s | %s", sentence, word or raw, reason)
    return accepted


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
    """Lage-prioriteit melding bij de eerste geldige treffer."""
    title = f"🔎 Kandidaat (1/2): {word}"
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
    for segment in segments:
        text = usable_segment_text(segment)
        if text is None:
            continue
        parts.append(text)
    return parts


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


def process_loop(
    audio: queue.Queue,
    stop: threading.Event,
    state: dict,
    day: date,
    verified: threading.Event,
    fatal: list[BaseException],
) -> None:
    try:
        nlp = load_nlp()
        model = load_model()
    except Exception as exc:
        fatal.append(exc)
        stop.set()
        log.exception("Model laden mislukt.")
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
            if not text or "codewoord" not in text.casefold():
                continue
            heard_at = item["started"]
            verified_now = False
            for word, phrase in review_transcript(nlp, text):
                if register_hearing(state, day, word, phrase, heard_at):
                    verified_now = True
                    break
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


def apply_state_epoch(state: dict) -> None:
    """Wis de dagen van een nieuwe cache-epoch één keer.

    Staat de epoch al in het toestandsbestand, dan blijft een later gevonden
    woord van die dag staan. Het gewiste woord wordt niet gelogd.
    """
    current = state.get("_epoch")
    if not isinstance(current, int):
        current = 0
    if current >= config.STATE_EPOCH:
        return
    cleared: list[str] = []
    for epoch in range(current + 1, config.STATE_EPOCH + 1):
        for day in config.EPOCH_RESETS.get(epoch, ()):
            key = day.isoformat()
            if key in state:
                state.pop(key)
                cleared.append(key)
    state["_epoch"] = config.STATE_EPOCH
    save_state(state)
    if cleared:
        log.info("Eenmalige reset (epoch %s) voor %s.", config.STATE_EPOCH, ", ".join(cleared))
    else:
        log.info("Cache-epoch %s vastgelegd.", config.STATE_EPOCH)


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
        state = load_state()
        apply_state_epoch(state)
        if args.reset_today:
            clear_today(state, now.date())

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
