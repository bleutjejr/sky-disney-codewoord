# Sky Radio Disney-codewoord

GitHub Actions luistert elke ochtend naar de publieke livestream van Sky Radio en vangt het Disney-codewoord van de dag. Het woord telt pas als het **twee keer onafhankelijk** is gehoord. Daarna komt er één ntfy-melding, wordt `gevonden_codewoorden.json` gecommit en gepusht, en stopt de estafette.

Actieperiode: **27 november 2026 00:00** t/m **24 december 2026 23:59** (Europe/Amsterdam).

Buiten dat venster stopt `main.py` direct, tenzij je `--test` gebruikt.

## Wat er draait

1. Cron start om **06:00 Nederlandse tijd** (`05:00 UTC`; de hele actie valt in de wintertijd).
2. ffmpeg neemt de stream op in stukken van **45 seconden** met **5 seconden overlap**. Een woord dat precies op de knip valt, zit daardoor in het volgende stuk. Elk bestand wordt na de transcriptie verwijderd.
3. faster-whisper (`small`, `language=nl`, `compute_type=int8`) maakt er Nederlandse tekst van. Het model blijft in de Hugging Face-cache van de workflow staan.
4. Zinnen met `codewoord`, `disney`, `disneyland`, `woord van vandaag` of `kerstcodewoord` worden uitgelezen. Het woord ná `codewoord is` / `codewoord:` (of `woord van vandaag is`) is de kandidaat.
5. Kleine spraak-naar-tekstfouten worden samengevoegd met `difflib.SequenceMatcher` (ratio ≥ 0.85). Dezelfde aankondiging in twee overlappende segmenten telt één keer: een nieuwe treffer moet minstens **60 seconden** na de vorige segmentstart vallen. Dat is langer dan de 40 seconden tussen twee starts, en kort genoeg om een latere herhaling apart te tellen.
6. Bij de tweede onafhankelijke treffer gaat er één POST naar `https://ntfy.sh/{NTFY_TOPIC}`:
   - Title: `🎄 Geverifieerd Disney Codewoord: {WOORD}`
   - Priority: `high`
   - Tags: `sparkles,radio,white_check_mark`
   - Click: de Sky Radio-actiepagina
   - Body: het woord plus de twee uitgeschreven zinnen
7. Het script schrijft het woord naar `gevonden_codewoorden.json`, commit en pusht dat bestand, en zet **geen** volgende runner klaar.
8. Staat het woord voor vandaag al in dat bestand, dan stopt een nieuwe run meteen.
9. Is het budget van **325 minuten** op en het woord nog niet bevestigd, dan pusht de run de kandidaten en start via `workflow_dispatch` de volgende runner. Om 23:59 lokale tijd stopt de dag zonder nieuwe runner; de cron van de volgende ochtend begint opnieuw.

Tussen twee runners zit een korte wisselpauze (checkout, pip, cache). Binnen één runner is de opname continu.

## Repository en minuten

De repository is **publiek**. GitHub Actions op de standaard runners is daar gratis, en een luisterdag kan meerdere runners van ruim vijf uur gebruiken. Het codewoord van de dag komt in `gevonden_codewoorden.json` en is daarmee leesbaar voor wie de repo opent. Het woord wordt toch op de radio omgeroepen.

`GITHUB_TOKEN` mag een `workflow_dispatch` starten (dat is de uitzondering op de regel dat deze token geen nieuwe workflows triggert). Deze workflow zet daarvoor `actions: write` en `contents: write`.

## Secret `NTFY_TOPIC`

1. Installeer de [ntfy-app](https://ntfy.sh) (of gebruik de website).
2. Kies een lang, lastig te raden topic, bijvoorbeeld `sky-disney-leon-7k3m`. Iedereen die de naam kent kan meelezen, dus behandel het als een wachtwoord.
3. Abonneer je telefoon op dat topic.
4. Zet in de GitHub-repo onder **Settings → Secrets and variables → Actions** een secret:
   - Name: `NTFY_TOPIC`
   - Value: alleen de topicnaam, zonder `https://ntfy.sh/`

## Eenmalig opzetten

```bash
git add .
git commit -m "Start de Sky Radio codewoord-listener."
git branch -M main
```

Maak een publieke repo op GitHub, voeg `origin` toe en push `main`. Zet daarna het secret. Actions moet aan staan (bij een nieuwe repo gebeurt dat vanzelf na de eerste push van `.github/workflows`).

Handmatig starten: **Actions → Disney-codewoord Sky Radio → Run workflow**.

## Lokaal testen

Je hebt Python 3.11+ en ffmpeg op `PATH` nodig.

```powershell
pip install -r requirements.txt
$env:NTFY_TOPIC = "jouw-topic"
python main.py --test
```

`--test` slaat de datumcheck en de radio over en stuurt meteen één testmelding met het woord `TEST`. Dat is geen echt codewoord.

Een echte luisterrun lokaal is dezelfde opdracht zonder `--test`, binnen de actieperiode. Zonder `git user.email` en zonder `origin` wordt de JSON wel opgeslagen, maar niet gepusht.

## JSON

`gevonden_codewoorden.json` begint als `{}`. Per datum komen er kandidaten, tellingen, de twee zinnen, en na bevestiging `verified` plus `notified` bij. Kandidaten worden ook gepusht tijdens een estafette, zodat de volgende runner de telling doorzet.

## Handig om te weten

- De stream is `https://playerservices.streamtheworld.com/api/livestream-redirect/SKYRADIO.mp3`.
- Whisper draait met `beam_size=1` en een stemactivatiefilter, zodat muziek niet de hele runner vult en geen verzonnen zinnen oplevert.
- De emoji in de ntfy-titel wordt als UTF-8 verstuurd. `requests` kan alleen latin-1-headers aan; `main.py` zet de titel daarom om voor verzending.
