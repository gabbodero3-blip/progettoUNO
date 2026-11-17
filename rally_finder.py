#!/usr/bin/env python3
"""
Roadsurfer Rally finder
=======================

Trova tutte le tratte Roadsurfer Rally (noleggi di sola andata) disponibili in un
periodo, senza dover selezionare a mano ogni coppia partenza/arrivo sul sito.

Usa la stessa API pubblica che usa la pagina https://booking.roadsurfer.com/it/rally/

Esempi:
    python3 rally_finder.py 31/12/2026
        -> tutte le tratte che includono la notte 31/12 -> 01/01, con periodo e modello del mezzo
    python3 rally_finder.py 31/12/2026 --da Monaco
    python3 rally_finder.py --dal 2026-12-01 --al 2026-12-20
    python3 rally_finder.py --dal 2026-12-01 --al 2026-12-20 --da Milano --da Monaco
    python3 rally_finder.py --dal 2026-12-01 --al 2026-12-31 --a Barcellona --min-notti 5
    python3 rally_finder.py --dal 2026-12-01 --al 2026-12-31 --paese IT --html risultati.html

Nessuna dipendenza esterna: basta Python 3.8+.
"""

from __future__ import annotations

import argparse
import os
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

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

# Il sito limita le richieste (errore 429): le distanziamo e, se arriva un 429,
# tutti i thread si fermano per un po' prima di riprovare.
_lock = threading.Lock()
_next_slot = 0.0
MIN_INTERVAL = 0.25  # secondi minimi tra due richieste (modificabile con --pausa)


def _wait_turn() -> None:
    global _next_slot
    with _lock:
        now = time.monotonic()
        wait = _next_slot - now
        _next_slot = max(now, _next_slot) + MIN_INTERVAL
    if wait > 0:
        time.sleep(wait)


def _back_off(seconds: float) -> None:
    """Rallenta tutte le richieste per `seconds` secondi."""
    global _next_slot
    with _lock:
        _next_slot = max(_next_slot, time.monotonic() + seconds)


def api_get(path: str, alias: str, retries: int = 6):
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
        _wait_turn()
        try:
            with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as resp:
                return json.load(resp)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            if attempt == retries - 1:
                ERRORS.append(f"{path}: {exc}")
                return None
            delay = min(5 * 2 ** attempt, 60)  # 5, 10, 20, 40, 60 s
            if isinstance(exc, urllib.error.HTTPError) and exc.code in (429, 503):
                retry_after = exc.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    delay = max(delay, int(retry_after))
                print(f"  ... il sito chiede di rallentare, attendo {delay}s", file=sys.stderr)
                _back_off(delay)
            else:
                time.sleep(delay)
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
        print("      Riprova tra qualche minuto, magari con --pausa 1 --thread 2.", file=sys.stderr)
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
    vehicles: list = field(default_factory=list)  # [(modello, marca, prezzo)]

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


def fetch_vehicles(offers: list[Offer], args) -> None:
    """Per ogni finestra chiede al sito i mezzi disponibili (modello e prezzo)."""
    if not offers:
        return
    print(f"Recupero modello e prezzo di {len(offers)} disponibilita'...", file=sys.stderr)

    def lookup(o: Offer):
        params = urllib.parse.urlencode({
            "stations": json.dumps([[o.start.id, o.end.id]]),
            "range": json.dumps([o.window_from.isoformat(), o.window_to.isoformat()]),
            "currency": "EUR",
        })
        data = api_get(f"{args.lingua}/rally/search?{params}", "rally.search") or []
        found = []
        for item in data if isinstance(data, list) else []:
            if not item.get("available") or not item.get("model"):
                continue
            model = item["model"]
            price = ((item.get("availability") or {}).get("price_details") or {}).get("final")
            found.append((model.get("name") or item.get("name") or "?", model.get("subname") or "", price))
        o.vehicles = found

    with ThreadPoolExecutor(max_workers=args.thread) as pool:
        list(pool.map(lookup, offers))


def parse_night(text: str) -> date:
    """Accetta '31/12/2026', '2026-12-31' o '31/12/2026 -> 01/01/2027' (conta la prima data)."""
    first = text.replace("->", " ").replace(">", " ").split()[0]
    for f in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(first, f).date()
        except ValueError:
            pass
    raise argparse.ArgumentTypeError(f"data non valida: {text!r} (usa ad es. 31/12/2026)")


def vehicle_text(o: Offer) -> str:
    if not o.vehicles:
        return "non disponibile/n.d."
    parts = []
    for name, brand, price in o.vehicles:
        t = f"{name} ({brand})" if brand else name
        if price is not None:
            t += f" - {price:.0f} EUR"
        parts.append(t)
    return "; ".join(parts)


def print_night(offers: list[Offer], night: date) -> None:
    """Risultato per una notte: periodo possibile e modello, divisi per paese di partenza."""
    nxt = night + timedelta(days=1)
    title = f"notte {night.strftime('%d/%m/%Y')} -> {nxt.strftime('%d/%m/%Y')}"
    if not offers:
        print(f"\nNessuna tratta disponibile per la {title}.")
        return
    print(f"\n{len(offers)} tratte disponibili per la {title}, per paese di partenza.")
    print("Periodo = ritiro dal primo giorno, riconsegna entro l'ultimo (la notte richiesta e' sempre inclusa).")
    head = ("Partenza", "Arrivo", "Periodo possibile", "Notti", "Mezzo (prezzo noleggio)")
    rows_all = []
    for country, items in by_country(offers):
        rows_all.append((country, [
            (o.start.name, o.end.label,
             f"{fmt(o.window_from)} -> {fmt(o.window_to)}",
             str(o.window_nights), vehicle_text(o))
            for o in items
        ]))
    widths = [max(len(r[i]) for _, rows in rows_all for r in rows + [head]) for i in range(len(head))]
    line = lambda r: "  ".join(c.ljust(widths[j]) for j, c in enumerate(r)).rstrip()
    for country, rows in rows_all:
        print(f"\n=== {country.upper()} - {len(rows)} ===")
        print(line(head))
        print("  ".join("-" * w for w in widths))
        for r in rows:
            print(line(r))


# ------------------------------------------------------------------------ output

WEEKDAYS = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]


def fmt(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d.strftime('%d/%m/%Y')}"


def by_country(offers: list[Offer]) -> list[tuple[str, list[Offer]]]:
    """Raggruppa per paese di partenza (Italia per prima, poi in ordine alfabetico)."""
    groups: dict[tuple[str, str], list[Offer]] = {}
    for o in offers:
        groups.setdefault((o.start.country, o.start.country_name or o.start.country or "?"), []).append(o)
    ordered = sorted(groups.items(), key=lambda kv: (kv[0][0] != "IT", kv[0][1]))
    return [(f"{name} ({cc})" if cc else name, items) for (cc, name), items in ordered]


def print_table(offers: list[Offer], args) -> None:
    if not offers:
        print(f"\nNessuna tratta disponibile tra il {fmt(args.dal)} e il {fmt(args.al)}.")
        return
    print(f"\n{len(offers)} disponibilita' tra il {fmt(args.dal)} e il {fmt(args.al)}, per paese di partenza:")
    with_models = bool(args.modelli)
    head = ("Partenza", "Arrivo", "Dal", "Al", "Notti") + (("Mezzo (prezzo noleggio)",) if with_models else ())
    row = lambda o: (o.start.name, o.end.label, fmt(o.window_from), fmt(o.window_to), str(o.window_nights)) + (
        (vehicle_text(o),) if with_models else ())
    rows = [row(o) for o in offers]
    widths = [max(len(r[i]) for r in rows + [head]) for i in range(len(head))]
    line = lambda r: "  ".join(c.ljust(widths[j]) for j, c in enumerate(r)).rstrip()
    for country, items in by_country(offers):
        print(f"\n=== {country.upper()} - {len(items)} disponibilita' ===")
        print(line(head))
        print("  ".join("-" * w for w in widths))
        for o in items:
            print(line(row(o)))


def write_markdown(offers: list[Offer], args, path: str) -> None:
    lines = [
        "# Roadsurfer Rally - tratte disponibili",
        "",
        f"Periodo: **{fmt(args.dal)} - {fmt(args.al)}** - trovate **{len(offers)}** disponibilita'.",
        f"Prenota su: {RALLY_PAGE.format(lang=args.lingua)}",
        "",
    ]
    for country, items in by_country(offers):
        lines += [f"## {country} - {len(items)}", "", "| Partenza | Arrivo | Dal | Al | Notti |", "|---|---|---|---|---|"]
        for o in items:
            lines.append(
                f"| {o.start.name} | {o.end.label} | {fmt(o.window_from)} | {fmt(o.window_to)} | {o.window_nights} |"
            )
        lines.append("")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def write_html(offers: list[Offer], args, path: str, preset_period: bool = True) -> None:
    """Pagina interattiva (pensata per il telefono): filtri per date e risultati per paese di partenza."""
    stations = {}
    for o in offers:
        for st in (o.start, o.end):
            stations[st.id] = [st.name, st.country, st.country_name]
    data = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dal": args.dal.isoformat() if preset_period else "",
        "al": args.al.isoformat() if preset_period else "",
        "url": RALLY_PAGE.format(lang=args.lingua),
        "stations": stations,
        "offers": [[o.start.id, o.end.id, o.window_from.isoformat(), o.window_to.isoformat()] for o in offers],
    }
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(PAGE_TEMPLATE.replace("__DATA__", blob))


PAGE_TEMPLATE = r"""<!doctype html>
<html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Tratte Rally</title>
<style>
:root { --bg:#f6f7f7; --card:#fff; --fg:#1b1f1e; --muted:#5f6b69; --line:#dfe4e3; --accent:#2f7f77; --accent-fg:#fff; --chip:#e8efee; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg:#121514; --card:#1b1f1e; --fg:#eef1f0; --muted:#9aa6a4; --line:#2b3130; --accent:#6dbcb3; --accent-fg:#0c1716; --chip:#252b2a; } }
:root[data-theme="dark"] { --bg:#121514; --card:#1b1f1e; --fg:#eef1f0; --muted:#9aa6a4; --line:#2b3130; --accent:#6dbcb3; --accent-fg:#0c1716; --chip:#252b2a; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:16px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
main { max-width:760px; margin:0 auto; padding:16px 16px 48px; }
h1 { font-size:1.45rem; margin:4px 0 2px; }
.sub { color:var(--muted); font-size:.875rem; margin:0 0 14px; }
a { color:var(--accent); }
.filters { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:12px; display:grid; grid-template-columns:1fr 1fr; gap:10px; }
.filters label { display:flex; flex-direction:column; gap:4px; font-size:.8rem; color:var(--muted); min-width:0; }
.filters .wide { grid-column:1 / -1; }
input, button { font:inherit; color:var(--fg); }
input { width:100%; min-width:0; height:44px; padding:0 10px; border:1px solid var(--line); border-radius:10px; background:var(--bg); }
.row { display:flex; gap:8px; }
.row input { flex:1; }
button { height:44px; padding:0 14px; border-radius:10px; border:1px solid var(--line); background:var(--chip); cursor:pointer; white-space:nowrap; }
button.link { height:auto; padding:0; border:0; background:none; color:var(--accent); text-decoration:underline; font-size:.875rem; }
.chips { display:flex; gap:8px; overflow-x:auto; padding:12px 0 4px; scrollbar-width:none; }
.chips::-webkit-scrollbar { display:none; }
.chip { height:38px; border-radius:19px; font-size:.9rem; flex:none; }
.chip[aria-pressed="true"] { background:var(--accent); border-color:var(--accent); color:var(--accent-fg); font-weight:600; }
.count { display:flex; justify-content:space-between; align-items:baseline; gap:12px; margin:10px 2px; font-size:.9rem; color:var(--muted); }
.count b { color:var(--fg); }
details { background:var(--card); border:1px solid var(--line); border-radius:14px; margin-bottom:12px; overflow:hidden; }
summary { list-style:none; cursor:pointer; padding:13px 14px; display:flex; align-items:center; gap:10px; font-weight:650; font-size:1.05rem; }
summary::-webkit-details-marker { display:none; }
summary .n { margin-left:auto; font-weight:500; font-size:.85rem; color:var(--muted); background:var(--chip); border-radius:12px; padding:2px 10px; }
summary::after { content:"▾"; color:var(--muted); }
details:not([open]) summary::after { content:"▸"; }
.city { padding:7px 14px 5px; font-size:.78rem; font-weight:650; letter-spacing:.04em; text-transform:uppercase; color:var(--muted); background:var(--bg); border-top:1px solid var(--line); }
.offer { padding:10px 14px; border-top:1px solid var(--line); display:flex; justify-content:space-between; align-items:center; gap:12px; }
.offer .to { font-weight:600; }
.offer .when { color:var(--muted); font-size:.875rem; }
.offer .nights { flex:none; font-size:.8rem; font-variant-numeric:tabular-nums; background:var(--chip); border-radius:10px; padding:3px 9px; }
.empty { background:var(--card); border:1px dashed var(--line); border-radius:14px; padding:22px 16px; text-align:center; color:var(--muted); }
.book { display:block; text-align:center; margin-top:18px; padding:13px; border-radius:12px; background:var(--accent); color:var(--accent-fg); font-weight:650; text-decoration:none; }
</style></head><body><main>
<h1>Tratte Roadsurfer Rally</h1>
<p class="sub" id="sub"></p>

<section class="filters" aria-label="Filtri">
  <label>Dal<input type="date" id="dal"></label>
  <label>Al<input type="date" id="al"></label>
  <label class="wide">Deve includere la notte del
    <span class="row"><input type="date" id="notte"><button type="button" id="capodanno">Capodanno</button></span>
  </label>
  <label>Notti minime nel periodo<input type="number" id="min" min="1" max="30" inputmode="numeric" placeholder="1"></label>
  <label>Città (partenza o arrivo)<input type="search" id="q" placeholder="es. Milano" autocomplete="off"></label>
  <div class="wide"><button type="button" class="link" id="reset">Azzera i filtri</button></div>
</section>

<div class="chips" id="chips" role="group" aria-label="Paese di partenza"></div>
<div class="count"><span id="count"></span><button type="button" class="link" id="toggle">Chiudi tutti</button></div>
<div id="out"></div>
<a class="book" id="book" target="_blank" rel="noopener">Prenota sul sito Roadsurfer</a>
</main>
<script id="data" type="application/json">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('data').textContent);
const $ = id => document.getElementById(id);
const F = ['dal','al','notte','min','q'];
const day = s => { const [y,m,d] = s.split('-').map(Number); return Date.UTC(y, m-1, d) / 864e5; };
const iso = n => new Date(n * 864e5).toISOString().slice(0, 10);
const fmtD = s => new Date(s + 'T12:00:00Z').toLocaleDateString('it-IT', {weekday:'short', day:'numeric', month:'short', timeZone:'UTC'});
const flag = cc => /^[A-Z]{2}$/.test(cc || '') ? String.fromCodePoint(...[...cc].map(c => 127397 + c.charCodeAt(0))) : '';
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const offers = D.offers.map(([s, e, f, t]) => ({s: D.stations[s], e: D.stations[e], f, t, nf: day(f), nt: day(t)}));
let country = '';
let store = {};
try { store = JSON.parse(localStorage.getItem('rally-filtri') || '{}'); } catch (e) {}

const upd = new Date(D.generated);
$('sub').textContent = 'Aggiornato il ' + upd.toLocaleDateString('it-IT', {day:'numeric', month:'long'}) +
  ' alle ' + upd.toLocaleTimeString('it-IT', {hour:'2-digit', minute:'2-digit'}) + ' · ' + offers.length + ' disponibilità in totale';
$('book').href = D.url;
$('dal').value = D.dal || store.dal || '';
$('al').value = D.al || store.al || '';
for (const k of ['notte','min','q']) $(k).value = store[k] || '';
country = store.country || '';

function matches(o) {
  const dal = $('dal').value, al = $('al').value, notte = $('notte').value;
  const min = Math.max(1, parseInt($('min').value, 10) || 1);
  const q = $('q').value.trim().toLowerCase();
  const from = Math.max(o.nf, dal ? day(dal) : -Infinity), to = Math.min(o.nt, al ? day(al) : Infinity);
  if (to - from < min) return false;
  if (notte && !(o.nf <= day(notte) && o.nt > day(notte))) return false;
  if (q && !(o.s[0] + ' ' + o.e[0]).toLowerCase().includes(q)) return false;
  return true;
}

function render() {
  const list = offers.filter(matches);
  const groups = new Map();
  for (const o of list) {
    const key = o.s[1] || '?';
    if (!groups.has(key)) groups.set(key, {cc: key, name: o.s[2] || key, items: []});
    groups.get(key).items.push(o);
  }
  const sorted = [...groups.values()].sort((a, b) => (a.cc !== 'IT') - (b.cc !== 'IT') || a.name.localeCompare(b.name, 'it'));
  if (country && !groups.has(country)) country = '';

  $('chips').innerHTML = [{cc: '', name: 'Tutti', items: list}, ...sorted].map(g =>
    `<button type="button" class="chip" data-cc="${esc(g.cc)}" aria-pressed="${g.cc === country}">${flag(g.cc)} ${esc(g.name)} · ${g.items.length}</button>`).join('');

  const shown = sorted.filter(g => !country || g.cc === country);
  const n = shown.reduce((a, g) => a + g.items.length, 0);
  $('count').innerHTML = n ? `<b>${n}</b> disponibilità da <b>${shown.length}</b> ${shown.length === 1 ? 'paese' : 'paesi'}` : '';
  $('toggle').hidden = shown.length < 2;

  $('out').innerHTML = shown.length ? shown.map(g => {
    const cities = new Map();
    for (const o of g.items) { if (!cities.has(o.s[0])) cities.set(o.s[0], []); cities.get(o.s[0]).push(o); }
    const body = [...cities.entries()].sort((a, b) => a[0].localeCompare(b[0], 'it')).map(([city, items]) =>
      `<div class="city">Da ${esc(city)}</div>` + items.sort((a, b) => a.nf - b.nf || a.e[0].localeCompare(b.e[0], 'it')).map(o =>
        `<div class="offer"><div><div class="to">→ ${esc(o.e[0])} ${flag(o.e[1])}</div>` +
        `<div class="when">${fmtD(o.f)} → ${fmtD(o.t)}</div></div><span class="nights">${o.nt - o.nf} notti</span></div>`).join('')).join('');
    return `<details open><summary>${flag(g.cc)} ${esc(g.name)}<span class="n">${g.items.length}</span></summary>${body}</details>`;
  }).join('') : '<div class="empty">Nessuna tratta disponibile con questi filtri.<br>Le tratte Rally vengono pubblicate poco per volta: riprova più avanti o allarga il periodo.</div>';
  $('toggle').textContent = 'Chiudi tutti';

  const save = {country};
  for (const k of F) save[k] = $(k).value;
  try { localStorage.setItem('rally-filtri', JSON.stringify(save)); } catch (e) {}
}

for (const k of F) $(k).addEventListener('input', render);
$('chips').addEventListener('click', e => { const b = e.target.closest('.chip'); if (b) { country = b.dataset.cc; render(); } });
$('reset').addEventListener('click', () => { for (const k of F) $(k).value = ''; country = ''; render(); });
$('capodanno').addEventListener('click', () => {
  const now = new Date(), y = now.getFullYear();
  $('notte').value = y + '-12-31'; render();
});
$('toggle').addEventListener('click', () => {
  const all = [...document.querySelectorAll('#out details')], open = all.some(d => d.open);
  all.forEach(d => d.open = !open); $('toggle').textContent = open ? 'Apri tutti' : 'Chiudi tutti';
});
render();
</script>
</body></html>
"""


# -------------------------------------------------------------------------- main

def parse_args(argv=None):
    today = date.today()
    p = argparse.ArgumentParser(
        description="Trova le tratte Roadsurfer Rally disponibili in un periodo.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Esempi:")[1] if "Esempi:" in __doc__ else None,
    )
    p.add_argument("notte", nargs="?", type=parse_night, metavar="NOTTE",
                   help="una notte che ti interessa, es. 31/12/2026: mostra tutte le tratte che la includono, "
                        "il periodo possibile e il modello del mezzo")
    p.add_argument("--modelli", action="store_true",
                   help="per le ricerche per periodo, recupera anche modello e prezzo (piu' lento)")
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
    p.add_argument("--sito", metavar="CARTELLA",
                   help="crea CARTELLA/index.html con TUTTE le tratte pubblicate (date filtrabili dalla pagina)")
    p.add_argument("--markdown", metavar="FILE", help="aggiunge i risultati in Markdown a FILE")
    p.add_argument("--json", metavar="FILE", help="salva i risultati in JSON")
    p.add_argument("--lingua", default="it", help="lingua dei nomi delle stazioni (default it)")
    p.add_argument("--thread", type=int, default=3, help="richieste in parallelo (default 3)")
    p.add_argument("--pausa", type=float, default=0.25,
                   help="secondi minimi tra una richiesta e l'altra (default 0.25; aumenta se ricevi errori 429)")
    args = p.parse_args(argv)
    if args.notte:
        # una tratta include la notte se il ritiro e' entro quel giorno e la riconsegna dal giorno dopo
        args.dal, args.al = args.notte, args.notte + timedelta(days=1)
        args.min_notti, args.ritiro_nel_periodo = 1, False
    if args.al is None:
        args.al = args.dal + timedelta(days=730 if args.sito else 60)
    if args.al < args.dal:
        p.error("--al deve essere uguale o successivo a --dal")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    global MIN_INTERVAL
    MIN_INTERVAL = max(0.0, args.pausa)
    offers, n_pairs = find_offers(args)
    if args.notte or args.modelli:
        fetch_vehicles(offers, args)
    explain_errors()
    if ERRORS and not offers:
        print("\nRicerca non riuscita: i risultati non sono affidabili (vedi errore sopra).")
        return 1
    if args.sito:
        if ERRORS:
            print("\nSito NON aggiornato: alcune tratte non sono state controllate.")
            return 1
        os.makedirs(args.sito, exist_ok=True)
        write_html(offers, args, os.path.join(args.sito, "index.html"), preset_period=False)
        print(f"\nSito creato in {args.sito}/index.html con {len(offers)} disponibilita'.")
        return 0
    if args.notte:
        print_night(offers, args.notte)
    else:
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
