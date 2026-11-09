#!/usr/bin/env python3
"""
Roadsurfer Rally finder
=======================

Trova tutte le tratte Roadsurfer Rally (noleggi di sola andata) disponibili in un
periodo, senza dover selezionare a mano ogni coppia partenza/arrivo sul sito.

Usa la stessa API pubblica che usa la pagina https://booking.roadsurfer.com/it/rally/

Esempi:
    python3 rally_finder.py --dal 2026-12-01 --al 2026-12-20
    python3 rally_finder.py --dal 2026-12-01 --al 2026-12-20 --da Milano --da Monaco
    python3 rally_finder.py --dal 2026-12-01 --al 2026-12-31 --a Barcellona --min-notti 5
    python3 rally_finder.py --dal 2026-12-01 --al 2026-12-31 --paese IT --html risultati.html

Nessuna dipendenza esterna: basta Python 3.8+.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta

BASE = "https://booking.roadsurfer.com/api"
RALLY_PAGE = "https://booking.roadsurfer.com/{lang}/rally/?currency=EUR"
USER_AGENT = "Mozilla/5.0 (rally-finder)"

try:  # usa i certificati di certifi se installato (risolve errori SSL su alcuni Mac)
    import certifi
    import ssl
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CONTEXT = None


# --------------------------------------------------------------------------- API

ERRORS: list[str] = []


def api_get(path: str, alias: str, retries: int = 3):
    """GET su /api/<path>. Il server richiede l'header X-Requested-Alias."""
    req = urllib.request.Request(
        f"{BASE}/{path}",
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "X-Requested-Alias": alias,
        },
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as resp:
                return json.load(resp)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            if attempt == retries - 1:
                ERRORS.append(f"{path}: {exc}")
                return None
            time.sleep(2 ** attempt)
    return None


def explain_errors() -> None:
    """Stampa gli errori di rete in modo comprensibile."""
    if not ERRORS:
        return
    print(f"\n!! {len(ERRORS)} richieste al sito sono fallite. Primo errore:", file=sys.stderr)
    print(f"   {ERRORS[0]}", file=sys.stderr)
    text = " ".join(ERRORS).upper()
    if "CERTIFICATE_VERIFY_FAILED" in text or "SSL" in text:
        print("   -> Problema di certificati SSL di Python.", file=sys.stderr)
        print("      Su Mac: apri la cartella Applicazioni > Python 3.x e fai doppio clic su", file=sys.stderr)
        print("      'Install Certificates.command', poi rilancia lo script.", file=sys.stderr)
        print("      In alternativa: python3 -m pip install certifi", file=sys.stderr)
    elif "403" in text or "429" in text:
        print("   -> Il sito ha rifiutato le richieste (forse troppe insieme).", file=sys.stderr)
        print("      Riprova tra qualche minuto, magari con --thread 2.", file=sys.stderr)
    else:
        print("   -> Controlla la connessione (VPN, proxy, firewall aziendale) e riprova.", file=sys.stderr)


@dataclass
class Station:
    id: int
    name: str
    city: str
    country: str
    country_name: str

    @property
    def label(self) -> str:
        return f"{self.name} ({self.country})"

    def matches(self, text: str) -> bool:
        t = text.lower()
        return t in self.name.lower() or t in self.city.lower() or t == str(self.id)


@dataclass
class Offer:
    start: Station
    end: Station
    window_from: date
    window_to: date
    usable_from: date
    usable_to: date

    @property
    def window_nights(self) -> int:
        return (self.window_to - self.window_from).days

    @property
    def usable_nights(self) -> int:
        return (self.usable_to - self.usable_from).days


def to_station(raw: dict) -> Station:
    city = raw.get("city") or {}
    return Station(
        id=raw["id"],
        name=raw.get("name", str(raw["id"])),
        city=city.get("name", ""),
        country=city.get("country", ""),
        country_name=city.get("country_translated") or city.get("country_name", ""),
    )


def parse_day(value: str) -> date:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date()


# ------------------------------------------------------------------------ search

def find_offers(args) -> tuple[list[Offer], int]:
    lang = args.lingua
    print("Scarico l'elenco delle stazioni...", file=sys.stderr)
    raw_stations = api_get(f"{lang}/rally/stations", "rally.startStations") or []
    if not raw_stations:
        print("Impossibile scaricare l'elenco delle stazioni dal sito.", file=sys.stderr)
        return [], 0
    stations = {s["id"]: to_station(s) for s in raw_stations}
    starts = [
        stations[s["id"]]
        for s in raw_stations
        if s.get("one_way") and s.get("enabled") and s.get("public", True)
    ]
    if args.da:
        starts = [s for s in starts if any(s.matches(t) for t in args.da)]
    if args.paese_partenza:
        wanted = {p.upper() for p in args.paese_partenza}
        starts = [s for s in starts if s.country in wanted]
    if not starts:
        print("Nessuna stazione di partenza corrisponde ai filtri.", file=sys.stderr)
        return [], 0

    print(f"Cerco le destinazioni di {len(starts)} stazioni di partenza...", file=sys.stderr)

    def routes_for(st: Station):
        data = api_get(f"{lang}/rally/stations/{st.id}", "rally.fetchRoutes") or {}
        return st, data.get("returns") or []

    pairs: list[tuple[Station, Station]] = []
    with ThreadPoolExecutor(max_workers=args.thread) as pool:
        for st, returns in pool.map(routes_for, starts):
            for rid in returns:
                end = stations.get(rid) or Station(rid, f"Stazione {rid}", "", "", "")
                if args.a and not any(end.matches(t) for t in args.a):
                    continue
                if args.paese_arrivo and end.country not in {p.upper() for p in args.paese_arrivo}:
                    continue
                pairs.append((st, end))

    print(f"Controllo le date di {len(pairs)} tratte...", file=sys.stderr)

    def timeframes_for(pair):
        st, end = pair
        data = api_get(f"{lang}/rally/timeframes/{st.id}-{end.id}", "rally.timeframes") or []
        return st, end, data

    offers: list[Offer] = []
    with ThreadPoolExecutor(max_workers=args.thread) as pool:
        for st, end, frames in pool.map(timeframes_for, pairs):
            for fr in frames:
                try:
                    w_from, w_to = parse_day(fr["startDate"]), parse_day(fr["endDate"])
                except (KeyError, ValueError):
                    continue
                # parte della finestra che cade nel periodo richiesto
                u_from, u_to = max(w_from, args.dal), min(w_to, args.al)
                if (u_to - u_from).days < args.min_notti:
                    continue
                if args.ritiro_nel_periodo and not (args.dal <= w_from <= args.al):
                    continue
                offers.append(Offer(st, end, w_from, w_to, u_from, u_to))

    offers.sort(key=lambda o: (o.window_from, o.start.name, o.end.name))
    return offers, len(pairs)


# ------------------------------------------------------------------------ output

WEEKDAYS = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]


def fmt(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d.strftime('%d/%m/%Y')}"


def print_table(offers: list[Offer], args) -> None:
    if not offers:
        print(f"\nNessuna tratta disponibile tra il {fmt(args.dal)} e il {fmt(args.al)}.")
        return
    print(f"\n{len(offers)} disponibilita' tra il {fmt(args.dal)} e il {fmt(args.al)}:\n")
    rows = [("Partenza", "Arrivo", "Dal", "Al", "Notti")]
    for o in offers:
        rows.append((o.start.label, o.end.label, fmt(o.window_from), fmt(o.window_to), str(o.window_nights)))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for i, r in enumerate(rows):
        print("  ".join(c.ljust(widths[j]) for j, c in enumerate(r)))
        if i == 0:
            print("  ".join("-" * w for w in widths))


def write_markdown(offers: list[Offer], args, path: str) -> None:
    lines = [
        "# Roadsurfer Rally - tratte disponibili",
        "",
        f"Periodo: **{fmt(args.dal)} - {fmt(args.al)}** - trovate **{len(offers)}** disponibilita'.",
        f"Prenota su: {RALLY_PAGE.format(lang=args.lingua)}",
        "",
    ]
    if offers:
        lines += ["| Partenza | Arrivo | Dal | Al | Notti |", "|---|---|---|---|---|"]
        for o in offers:
            lines.append(
                f"| {o.start.label} | {o.end.label} | {fmt(o.window_from)} | {fmt(o.window_to)} | {o.window_nights} |"
            )
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def write_html(offers: list[Offer], args, path: str) -> None:
    generated = datetime.now().strftime("%d/%m/%Y %H:%M")
    rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(o.start.label)}</td>"
        f"<td>{html.escape(o.end.label)}</td>"
        f"<td data-v='{o.window_from.isoformat()}'>{fmt(o.window_from)}</td>"
        f"<td data-v='{o.window_to.isoformat()}'>{fmt(o.window_to)}</td>"
        f"<td data-v='{o.window_nights:03d}'>{o.window_nights}</td>"
        "</tr>"
        for o in offers
    )
    doc = f"""<!doctype html>
<html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Roadsurfer Rally</title>
<style>
:root {{ --bg:#fff; --fg:#1d1d1f; --muted:#666; --line:#e3e3e3; --head:#f4f6f6; --accent:#3f8f87; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#141414; --fg:#eee; --muted:#aaa; --line:#2c2c2c; --head:#1e1e1e; --accent:#65a8a1; }} }}
body {{ margin:0; padding:16px; background:var(--bg); color:var(--fg); font:15px/1.4 system-ui,sans-serif; }}
h1 {{ font-size:1.4rem; margin:0 0 4px; }} p {{ color:var(--muted); margin:0 0 12px; }}
a {{ color:var(--accent); }}
input {{ width:100%; max-width:420px; padding:8px 10px; margin-bottom:12px; border:1px solid var(--line); border-radius:8px; background:var(--bg); color:var(--fg); font:inherit; box-sizing:border-box; }}
.wrap {{ overflow-x:auto; }}
table {{ border-collapse:collapse; width:100%; min-width:560px; }}
th,td {{ text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); white-space:nowrap; }}
th {{ background:var(--head); cursor:pointer; position:sticky; top:0; user-select:none; }}
tr:hover td {{ background:var(--head); }}
</style></head><body>
<h1>Roadsurfer Rally</h1>
<p>Periodo {fmt(args.dal)} - {fmt(args.al)} &middot; {len(offers)} disponibilit&agrave; &middot; aggiornato il {generated}
&middot; <a href="{RALLY_PAGE.format(lang=args.lingua)}" target="_blank" rel="noopener">prenota sul sito</a></p>
<input id="q" placeholder="Filtra per citt&agrave; (es. Milano, Barcellona)..." autocomplete="off">
<div class="wrap"><table id="t">
<thead><tr><th>Partenza</th><th>Arrivo</th><th>Dal</th><th>Al</th><th>Notti</th></tr></thead>
<tbody>
{rows}
</tbody></table></div>
<script>
const tb=document.querySelector('#t tbody');
document.getElementById('q').addEventListener('input',e=>{{const q=e.target.value.toLowerCase();
  for(const r of tb.rows) r.style.display=r.textContent.toLowerCase().includes(q)?'':'none';}});
document.querySelectorAll('th').forEach((th,i)=>{{let asc=true;th.onclick=()=>{{
  const rows=[...tb.rows];const v=r=>r.cells[i].dataset.v||r.cells[i].textContent;
  rows.sort((a,b)=>v(a).localeCompare(v(b))*(asc?1:-1));asc=!asc;rows.forEach(r=>tb.appendChild(r));}};}});
</script>
</body></html>
"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)


# -------------------------------------------------------------------------- main

def parse_args(argv=None):
    today = date.today()
    p = argparse.ArgumentParser(
        description="Trova le tratte Roadsurfer Rally disponibili in un periodo.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Esempi:")[1] if "Esempi:" in __doc__ else None,
    )
    p.add_argument("--dal", type=date.fromisoformat, default=today,
                   help="inizio periodo AAAA-MM-GG (default: oggi)")
    p.add_argument("--al", type=date.fromisoformat, default=None,
                   help="fine periodo AAAA-MM-GG (default: dal + 60 giorni)")
    p.add_argument("--da", action="append", metavar="CITTA",
                   help="filtra la partenza (nome o parte del nome, ripetibile)")
    p.add_argument("--a", action="append", metavar="CITTA",
                   help="filtra l'arrivo (nome o parte del nome, ripetibile)")
    p.add_argument("--paese-partenza", action="append", metavar="CC",
                   help="codice paese della partenza, es. IT, DE (ripetibile)")
    p.add_argument("--paese-arrivo", action="append", metavar="CC",
                   help="codice paese dell'arrivo, es. ES, FR (ripetibile)")
    p.add_argument("--min-notti", type=int, default=1,
                   help="minimo di notti della finestra che devono cadere nel periodo (default 1)")
    p.add_argument("--ritiro-nel-periodo", action="store_true",
                   help="mostra solo le finestre che INIZIANO dentro il periodo")
    p.add_argument("--html", metavar="FILE", help="salva anche una pagina HTML con i risultati")
    p.add_argument("--markdown", metavar="FILE", help="aggiunge i risultati in Markdown a FILE")
    p.add_argument("--json", metavar="FILE", help="salva i risultati in JSON")
    p.add_argument("--lingua", default="it", help="lingua dei nomi delle stazioni (default it)")
    p.add_argument("--thread", type=int, default=8, help="richieste in parallelo (default 8)")
    args = p.parse_args(argv)
    if args.al is None:
        args.al = args.dal + timedelta(days=60)
    if args.al < args.dal:
        p.error("--al deve essere uguale o successivo a --dal")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    offers, n_pairs = find_offers(args)
    explain_errors()
    if ERRORS and not offers:
        print("\nRicerca non riuscita: i risultati non sono affidabili (vedi errore sopra).")
        return 1
    print_table(offers, args)
    if ERRORS:
        print("\nAttenzione: alcune tratte non sono state controllate (vedi errore sopra).")
    if args.html:
        write_html(offers, args, args.html)
        print(f"\nPagina HTML salvata in {args.html}", file=sys.stderr)
    if args.markdown:
        write_markdown(offers, args, args.markdown)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(
                [
                    {
                        "partenza": o.start.label, "partenza_id": o.start.id,
                        "arrivo": o.end.label, "arrivo_id": o.end.id,
                        "dal": o.window_from.isoformat(), "al": o.window_to.isoformat(),
                        "notti": o.window_nights,
                    }
                    for o in offers
                ],
                fh, ensure_ascii=False, indent=1,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
