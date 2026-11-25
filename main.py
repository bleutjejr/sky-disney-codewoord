#!/usr/bin/env python3
"""Luister naar Sky Radio en bevestig het Disney-codewoord van de dag.

De opname loopt in segmenten van 45 seconden met 5 seconden overlap.
Een kandidaat telt pas als geverifieerd nadat hij twee keer onafhankelijk
is gehoord (fuzzy-drempel 0.85, minimale tussenpauze 90 seconden).
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

ARTICLE = r"(?:de|het|een|'t)"
OPTIONAL_QUOTE = r"[\"“”']?"
WORD = r"([A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'\-]{2,40})"
# Vulwoorden tussen "codewoord" en "is", maar eet "is"/"luidt" zelf niet op.
FILLER = r"(?:\s+(?!is\b|luidt\b|:)[A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,4}"

PATTERNS = (
    re.compile(
        rf"\b(?:kerst)?codewoord\b{FILLER}\s*(?:is|luidt|:)\s+(?:{ARTICLE}\s+)?{OPTIONAL_QUOTE}{WORD}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\bwoord\s+van\s+vandaag\b\s*(?:is|luidt|:)\s+(?:{ARTICLE}\s+)?{OPTIONAL_QUOTE}{WORD}",
        re.IGNORECASE,
    ),
)

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


def _candidates_in(chunks: list[str]) -> dict[str, tuple[str, str]]:
    found: dict[str, tuple[str, str]] = {}
    for chunk in chunks:
        if not has_keyword(chunk):
            continue
        for pattern in PATTERNS:
            for match in pattern.finditer(chunk):
                word = clean_candidate(match.group(1))
                if word is None:
                    continue
                key = normalize_word(word)
                previous = found.get(key)
                # Bewaar de kortste zin die het woord nog helemaal bevat.
                if previous is None or len(chunk) < len(previous[1]):
                    found[key] = (word, chunk)
    return found


def extract_candidates(text: str) -> list[tuple[str, str]]:
    """Geef (woord, zin) paren terug. Eén hit per genormaliseerd woord."""
    sentences = split_sentences(text)
    found = _candidates_in(sentences)
    if found:
        return list(found.values())
    whole = collapse(text)
    if not whole:
        return []
    return list(_candidates_in([whole]).values())


def blank_day() -> dict:
    return {
        "verified": None,
        "verified_at": None,
        "notified": False,
        "phrases": [],
        "candidates": [],
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


def git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def publish_state(message: str) -> None:
    """Commit en push alleen gevonden_codewoorden.json. Faalt zichtbaar door."""
    inside = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=config.ROOT,
        capture_output=True,
        text=True,
        env=git_env(),
    )
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        log.warning("Geen git-werkboom; JSON blijft alleen op schijf staan.")
        return

    identity = subprocess.run(
        ["git", "config", "--get", "user.email"],
        cwd=config.ROOT,
        capture_output=True,
        text=True,
        env=git_env(),
    )
    if identity.returncode != 0 or not identity.stdout.strip():
        log.warning(
            "git user.email ontbreekt, commit overgeslagen. "
            "In GitHub Actions zet de workflow de identiteit."
        )
        return

    add = subprocess.run(
        ["git", "add", "--", config.STATE_PATH.name],
        cwd=config.ROOT,
        capture_output=True,
        text=True,
        env=git_env(),
    )
    if add.returncode != 0:
        raise RuntimeError(add.stderr.strip() or "git add mislukt")

    staged = subprocess.run(
        ["git", "diff", "--cached", "--quiet", "--", config.STATE_PATH.name],
        cwd=config.ROOT,
        env=git_env(),
    )
    if staged.returncode == 0:
        log.info("Geen wijziging in gevonden_codewoorden.json om te committen.")
        return
    if staged.returncode != 1:
        raise RuntimeError("git diff kon de staging-status niet bepalen")

    commit = subprocess.run(
        ["git", "commit", "-m", message],
        cwd=config.ROOT,
        capture_output=True,
        text=True,
        env=git_env(),
    )
    if commit.returncode != 0:
        raise RuntimeError(commit.stderr.strip() or commit.stdout.strip() or "git commit mislukt")
    log.info("Commit gemaakt: %s", message)

    remote = subprocess.run(
        ["git", "remote"],
        cwd=config.ROOT,
        capture_output=True,
        text=True,
        env=git_env(),
    )
    if remote.returncode != 0 or "origin" not in remote.stdout.split():
        log.warning("Geen origin-remote; push overgeslagen.")
        return

    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=config.ROOT,
        capture_output=True,
        text=True,
        check=True,
        env=git_env(),
    ).stdout.strip()

    last_error = ""
    for attempt in range(1, 4):
        push = subprocess.run(
            ["git", "push", "origin", "HEAD"],
            cwd=config.ROOT,
            capture_output=True,
            text=True,
            env=git_env(),
        )
        if push.returncode == 0:
            log.info("gevonden_codewoorden.json gepusht.")
            return
        last_error = (push.stderr or push.stdout).strip()
        log.warning("Push poging %s mislukt: %s", attempt, last_error)
        pull = subprocess.run(
            ["git", "pull", "--rebase", "origin", branch],
            cwd=config.ROOT,
            capture_output=True,
            text=True,
            env=git_env(),
        )
        if pull.returncode != 0:
            last_error = (pull.stderr or pull.stdout).strip()
            log.warning("Rebase mislukt: %s", last_error)
            break
    raise RuntimeError(last_error or "git push mislukt")


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


def send_ntfy(title: str, body: str) -> None:
    import requests

    topic = normalize_topic(config.NTFY_TOPIC)
    headers = {
        "Title": utf8_header(title),
        "Priority": "high",
        "Tags": "sparkles,radio,white_check_mark",
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
            log.info("ntfy-notificatie verstuurd.")
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
        "Whisper laden: model=%s compute=%s device=%s threads=%s",
        config.WHISPER_MODEL,
        config.WHISPER_COMPUTE_TYPE,
        config.WHISPER_DEVICE,
        threads,
    )
    return WhisperModel(
        config.WHISPER_MODEL,
        device=config.WHISPER_DEVICE,
        compute_type=config.WHISPER_COMPUTE_TYPE,
        cpu_threads=threads,
    )


def transcribe(model, path: Path) -> str:
    # beam_size 1 en VAD houden de CPU bij met de livestream.
    # Zonder VAD transcribeert Whisper minutenlange muziek en hallucineert tekst.
    segments, _info = model.transcribe(
        str(path),
        language=config.WHISPER_LANGUAGE,
        beam_size=1,
        best_of=1,
        condition_on_previous_text=False,
        temperature=0.0,
        vad_filter=True,
    )
    return collapse(" ".join(segment.text for segment in segments))


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
                return False
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
    if not confirmed:
        try:
            publish_state(f"Bewaar kandidaat-codewoorden voor {day.isoformat()}")
        except Exception:
            log.exception("Kandidaat opslaan in git mislukt; de telling blijft lokaal bewaard.")
        return False

    publish_state(f"Registreer geverifieerd Disney-codewoord voor {day.isoformat()}")
    send_verified(display, phrases)
    with STATE_LOCK:
        bucket = ensure_day(state, day)
        bucket["notified"] = True
        save_state(state)
    publish_state(f"Markeer Disney-codewoord voor {day.isoformat()} als gemeld")
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
                text = transcribe(model, path)
            except Exception:
                log.exception("Transcriptie van %s mislukt.", path.name)
                continue
            if not text:
                log.info("Leeg transcript voor %s.", path.name)
                continue
            if not has_keyword(text):
                log.info("Geen trefwoord in %s (%s tekens).", path.name, len(text))
                continue
            log.info("Trefwoord in %s: %s", path.name, text)
            for word, phrase in extract_candidates(text):
                if register_hearing(state, day, word, phrase, item["started"]):
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
    publish_state(f"Markeer Disney-codewoord voor {day.isoformat()} als gemeld")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Vang het Disney-codewoord van Sky Radio.")
    parser.add_argument(
        "--test",
        action="store_true",
        help="Stuur direct een ntfy-test en stop, ook buiten de actieperiode.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = build_parser().parse_args(argv)
    relay = False
    try:
        if args.test:
            send_test_notification()
            return 0

        now = config.now_amsterdam()
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
