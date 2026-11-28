# Sky Radio Disney-codewoord

GitHub Actions luistert elke ochtend naar de publieke livestream van Sky Radio en vangt het Disney-codewoord van de dag. De eerste treffer komt meteen als stille ntfy-melding binnen. Het woord telt pas als geverifieerd nadat het **twee keer onafhankelijk** is gehoord; dan volgt de melding met hoge prioriteit, wordt `gevonden_codewoorden.json` in de Actions-cache gezet, en stopt de estafette.

Actieperiode: **27 november 2026 00:00** t/m **24 december 2026 23:59** (Europe/Amsterdam).

Buiten dat venster stopt `main.py` direct, tenzij je `--test` gebruikt.

## Wat er draait

1. Cron start om **06:00 Nederlandse tijd** (`05:00 UTC`; de hele actie valt in de wintertijd).
2. ffmpeg neemt de stream op in stukken van **45 seconden** met **10 seconden overlap** (elke 35 seconden een nieuwe start). De overgang van ‘Vandaag is dat codewoord:’ naar Mickey valt daardoor niet precies op een knip. Elk bestand wordt na de transcriptie verwijderd.
3. faster-whisper (`small`, `language=nl`, `compute_type=int8`, `beam_size=3`, `vad_filter=False`) maakt er Nederlandse tekst van. De initial prompt geeft de promo en een paar voorbeeldwoorden mee, zodat de korte hoge stem niet als geluidseffect verdwijnt. Het model blijft in de Hugging Face-cache van de workflow staan.
4. Zinnen met `codewoord`, `disney`, `disneyland`, `woord van vandaag` of `kerstcodewoord` worden uitgelezen. Na `vandaag is dat codewoord`, `dat codewoord`, `is dat codewoord`, `codewoord:` of `het codewoord` (ook met dubbele punt, komma of punt ertussen) pakt de listener het eerstvolgende woord dat geen stopwoord is (`de`, `het`, `een`, `is`, `dat`, `van`, `vandaag`, `mickey`, `disney`, …). Zet Whisper het woord in het segment direct daarna, dan telt dat ene woord mee. Een echo van de voorbeeldlijst uit de prompt wordt genegeerd.
5. Kleine spraak-naar-tekstfouten worden samengevoegd met `difflib.SequenceMatcher` (ratio ≥ 0.85). Dezelfde aankondiging in twee overlappende segmenten telt één keer: een nieuwe treffer moet minstens **60 seconden** na de vorige segmentstart vallen. Dat is langer dan de 35 seconden tussen twee starts, en kort genoeg om een latere herhaling apart te tellen.
6. Bij de eerste treffer — ook als een zin wel `codewoord` bevat maar er geen woord uit te halen is — gaat er meteen een stille melding uit (`Priority: low`, titel `🔎 Kandidaat gehoord (1/2): {WOORD}`, body de letterlijke zin). Bij de tweede onafhankelijke treffer gaat er één POST naar `https://ntfy.sh/{NTFY_TOPIC}`:
   - Title: `🎄 Geverifieerd Disney Codewoord: {WOORD}`
   - Priority: `high`
   - Tags: `sparkles,radio,white_check_mark`
   - Click: de Sky Radio-actiepagina
   - Body: het woord plus de twee uitgeschreven zinnen
7. Het script schrijft het woord naar `gevonden_codewoorden.json`. De workflow bewaart dat bestand in de Actions-cache en zet **geen** volgende runner klaar.
8. Staat het woord voor vandaag al in dat bestand, dan stopt een nieuwe run meteen.
9. Is het budget van **325 minuten** op en het woord nog niet bevestigd, dan bewaart de run de kandidaten in de cache en start via `workflow_dispatch` de volgende runner. Om 23:59 lokale tijd stopt de dag zonder nieuwe runner; de cron van de volgende ochtend begint opnieuw.

Tussen twee runners zit een korte wisselpauze (checkout, pip, cache). Binnen één runner is de opname continu.

## Repository en minuten

De repository is **publiek**. GitHub Actions op de standaard runners is daar gratis, en een luisterdag kan meerdere runners van ruim vijf uur gebruiken. Het codewoord staat niet in git. Elke run herstelt `gevonden_codewoorden.json` uit de Actions-cache en schrijft daarna een nieuwe cache weg, vóór een eventuele estafette. Die cache is alleen beschikbaar voor workflows in deze repository, niet in de publieke code. GitHub bewaart een cache tot zeven dagen na het laatste gebruik; de dagelijkse cron houdt hem tijdens de actie in leven.

`GITHUB_TOKEN` mag een `workflow_dispatch` starten. Deze workflow heeft daarvoor `actions: write`. Inhoud van de repo schrijven is niet nodig (`contents: read`).

Onder **Settings → Actions → General** staat *Require approval for all external contributors*. Een fork kan daardoor geen workflow starten die het `NTFY_TOPIC`-secret zou kunnen uitlezen, tot jij die run zelf goedkeurt.

## Secret `NTFY_TOPIC`

1. Installeer de [ntfy-app](https://ntfy.sh) (of gebruik de website).
2. Kies een willekeurige topicnaam van minstens 16 tekens, bijvoorbeeld `sky-k9m2x8p4q7w1z5r9`. Wie de naam kent kan meelezen. Zet de naam nergens in de repository.
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

Een echte luisterrun lokaal is dezelfde opdracht zonder `--test`, binnen de actieperiode. Het JSON-bestand blijft dan alleen op je computer staan.

## JSON

`gevonden_codewoorden.json` begint als `{}` en staat in `.gitignore`. Per datum komen er kandidaten, tellingen, de twee zinnen, en na bevestiging `verified` plus `notified` bij. In GitHub Actions geeft de cache dat bestand door aan de volgende estafette-runner.

## Handig om te weten

- De stream is `https://playerservices.streamtheworld.com/api/livestream-redirect/SKYRADIO.mp3`.
- Whisper draait met `beam_size=3`, `compute_type=int8` en zonder VAD-filter, zodat de hoge stem van Mickey in de transcriptie blijft. `DEBUG_TRANSCRIPTS=true` (of het vinkje bij handmatig starten) zet elke zin met `disney` of `codewoord` in de Actions-log. Standaard staat die optie uit.
- De emoji in de ntfy-titel wordt als UTF-8 verstuurd. `requests` kan alleen latin-1-headers aan; `main.py` zet de titel daarom om voor verzending.
