# Roadsurfer Rally finder

Trova **tutte** le tratte [Roadsurfer Rally](https://booking.roadsurfer.com/it/rally/?currency=EUR)
disponibili in un periodo, senza dover selezionare a mano ogni coppia partenza/arrivo sul sito.

Lo script interroga la stessa API usata dalla pagina del Rally:

1. elenco delle stazioni di partenza;
2. per ogni partenza, le stazioni di arrivo possibili;
3. per ogni tratta, le finestre di date disponibili;

e mostra solo quelle che cadono nel periodo che scegli.

## Uso dal computer

Serve solo Python 3.8+ (nessuna libreria da installare).

```bash
# tutte le tratte tra il 1 e il 20 dicembre
python3 rally_finder.py --dal 2026-12-01 --al 2026-12-20

# solo partenze da Milano o Torino, con almeno 5 notti nel periodo
python3 rally_finder.py --dal 2026-12-01 --al 2026-12-31 --da Milano --da Torino --min-notti 5

# partenze dall'Italia verso la Spagna, salvando una pagina HTML filtrabile
python3 rally_finder.py --dal 2026-12-01 --al 2026-12-31 --paese-partenza IT --paese-arrivo ES --html risultati.html
```

Opzioni principali:

| Opzione | Significato |
|---|---|
| `--dal`, `--al` | periodo (AAAA-MM-GG). Default: da oggi a +60 giorni |
| `--da`, `--a` | filtro città di partenza / arrivo (anche parte del nome, ripetibile) |
| `--paese-partenza`, `--paese-arrivo` | filtro per codice paese (IT, DE, ES, FR, ...) |
| `--min-notti N` | almeno N notti della finestra devono cadere nel periodo |
| `--ritiro-nel-periodo` | solo finestre che **iniziano** dentro il periodo |
| `--html`, `--json`, `--markdown` | salva i risultati su file |

Ogni riga è una finestra in cui il camper è disponibile per quella tratta
("Dal" = primo giorno di ritiro, "Al" = ultimo giorno di riconsegna), proprio come
appare nel calendario del sito. La prenotazione si fa poi sul sito Roadsurfer.
L'elenco completo richiede 1-2 minuti: lo script distanzia le richieste per non essere bloccato dal sito (errore 429). Se il blocco capita comunque, usa `--pausa 1 --thread 2`.

## Uso dal telefono (GitHub Actions)

1. Apri la scheda **Actions** del repository → **Cerca tratte Roadsurfer Rally**.
2. **Run workflow**, inserisci le date (e, se vuoi, le città) e avvia.
3. A fine esecuzione la tabella dei risultati è nel **Summary** del run;
   la pagina HTML filtrabile è scaricabile tra gli **Artifacts** (`risultati-rally`).
