#!/usr/bin/env python3
"""Počasie SHMÚ – webová stránka s aktuálnymi meraniami automatických staníc.

Server každé 2 minúty skontroluje otvorený dátový server SHMÚ
(https://opendata.shmu.sk/meteorology/climate/now/data/), stiahne nové
5-minútové súbory s minútovými meraniami a zobrazí ich na stránke
http://localhost:8000. Každých 20 minút hľadá aj nový beh predpovedného
modelu ALADIN (meteorology/weather/nwp/aladin/sk/4.5km) pre mapy predpovede.

Potrebuje len Python 3.9 alebo novší, žiadne ďalšie balíčky.

Spustenie:   python server.py
Voľby:       python server.py --help
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import re
import ssl
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlparse
from urllib.request import Request, urlopen

import aladin
import historia as hist
import radar
import wunderground as wu
import zrazkomery as zr

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"
STATIONS_FILE = HERE / "stanice.json"
CERT_FILE = HERE / "certs" / "shmu-chain.pem"

OBS_PATH = "/meteorology/climate/now/data"
NWP_PATH = "/meteorology/weather/nwp/aladin/sk/4.5km"
NWP_FILE_RE = re.compile(r"^al-grib_sk_(\d{3})-(\d{8})-(\d{4})-nwp-\.grb$")
RUN_DIR_RE = re.compile(r"^\d{4}/$")
# Hodinové kroky do +48 h, potom po 3 hodinách do +102 h (len beh o 00 UTC ide tak ďaleko).
NWP_HOURS = tuple(range(0, 49)) + tuple(range(51, 103, 3))
NWP_MIN_HOURS = 72
USER_AGENT = "pocasie-shmu/1.0 (osobna stranka s meraniami SHMU)"

# Čas merania ("minuta") je v SEČ, polia ukladané pre grafy: pozri historia.py
SEC = hist.SEC
SERIES_FIELDS = hist.SERIES_FIELDS
IDX = hist.IDX
parse_minuta = hist.parse_minuta

FILE_RE = re.compile(r"^aws1min - \d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2}\.json$")
DAY_RE = re.compile(r"^\d{8}/$")
HREF_RE = re.compile(r'<a\s+href="([^"?/][^"]*)"', re.IGNORECASE)


def log(msg: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


# --------------------------------------------------------------------------
# Sťahovanie zo servera SHMÚ
# --------------------------------------------------------------------------

def make_ssl_context() -> ssl.SSLContext:
    """Bežné overovanie certifikátov + chýbajúci medzicertifikát SHMÚ.

    Server opendata.shmu.sk neposiela celý reťazec certifikátov, preto je
    jeho medzicertifikát (Sectigo DV R36) a koreňový certifikát (Sectigo
    R46) pribalený v priečinku certs/. Overovanie sa nikdy nevypína.
    """
    ctx = ssl.create_default_context()
    try:
        import certifi  # nepovinné, pomáha na macOS

        ctx.load_verify_locations(certifi.where())
    except Exception:
        pass
    if CERT_FILE.exists():
        ctx.load_verify_locations(cafile=str(CERT_FILE))
    return ctx


class Shmu:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")
        self.ctx = make_ssl_context() if self.base.startswith("https") else None

    def get(self, path: str, tries: int = 2) -> bytes:
        url = self.base + path
        last: Exception | None = None
        for attempt in range(tries):
            try:
                req = Request(url, headers={"User-Agent": USER_AGENT})
                with urlopen(req, timeout=40, context=self.ctx) as resp:
                    return resp.read()
            except (HTTPError, URLError, TimeoutError, OSError) as err:
                last = err
                time.sleep(2 + attempt * 3)
        raise ConnectionError(f"Nepodarilo sa stiahnuť {url}: {last}")

    def listing(self, path: str) -> list[str]:
        html = self.get(path.rstrip("/") + "/").decode("utf-8", "replace")
        return [unquote(h) for h in HREF_RE.findall(html)]

    def recent_files(self) -> list[str]:
        """Cesty k súborom z dvoch najnovších denných priečinkov, od najstaršieho."""
        days = sorted((e for e in self.listing(OBS_PATH) if DAY_RE.match(e)), reverse=True)
        if not days:
            raise ConnectionError("Na serveri SHMÚ sa nenašli denné priečinky s dátami.")
        files: list[tuple[str, str]] = []
        for day in days[:2]:
            folder = f"{OBS_PATH}/{day.rstrip('/')}"
            for name in self.listing(folder):
                if FILE_RE.match(name):
                    files.append((name, f"{folder}/{name}"))
        files.sort()  # názov obsahuje dátum a čas, abecedné poradie = časové
        return [path for _, path in files]

    def snapshot(self, path: str) -> list[dict]:
        folder, _, name = path.rpartition("/")
        doc = json.loads(self.get(f"{folder}/{quote(name)}"))
        records = doc.get("data") if isinstance(doc, dict) else None
        if not isinstance(records, list):
            raise ValueError(f"Neočakávaný formát súboru {name}")
        return [r for r in records if isinstance(r, dict)]


# --------------------------------------------------------------------------
# Úložisko meraní v pamäti
# --------------------------------------------------------------------------

def num(value):
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


class Store:
    def __init__(self, history_hours: int, days: int = 8):
        self.lock = threading.Lock()
        self.keep_seconds = (history_hours + 1) * 3600
        self.keep_days = days
        self.series: dict[int, dict[int, tuple]] = {}  # stanica -> {čas: hodnoty}
        self.hours: dict[int, dict[int, list]] = {}    # stanica -> {hodina: súhrn} pre grafy na niekoľko dní
        self.latest_raw: dict[int, tuple[int, dict]] = {}  # posledný celý záznam
        self.processed: set[str] = set()
        self.version = 0
        self.newest_file: str | None = None
        self.status = {
            "stav": "start",
            "chyba": None,
            "poslednaKontrola": None,
            "historia": {"hotovo": 0, "spolu": 0},
        }
        self._cache: dict[str, tuple[int, bytes]] = {}

    def ingest(self, path: str, records: list[dict]) -> int:
        added = 0
        with self.lock:
            for rec in records:
                try:
                    sid = int(rec.get("ind_kli"))
                except (TypeError, ValueError):
                    continue
                ts = parse_minuta(rec.get("minuta"))
                if ts is None:
                    continue
                values = tuple(num(rec.get(f)) for f in SERIES_FIELDS)
                self.series.setdefault(sid, {})[ts] = values
                prev = self.latest_raw.get(sid)
                if prev is None or ts >= prev[0]:
                    self.latest_raw[sid] = (ts, rec)
                added += 1
            self.processed.add(path)
            name = path.rpartition("/")[2]
            if self.newest_file is None or name > self.newest_file.rpartition("/")[2]:
                self.newest_file = path
            self._prune()
            self.version += 1
        return added

    def _prune(self) -> None:
        """Minúty staršie než história grafov sa presunú do hodinových súhrnov."""
        newest = max((max(s) for s in self.series.values() if s), default=0)
        cutoff = newest - self.keep_seconds
        for sid, s in self.series.items():
            old = [t for t in s if t < cutoff]
            if not old:
                continue
            groups: dict[int, list] = {}
            for ts in old:
                groups.setdefault(ts // 3600 * 3600, []).append(s.pop(ts))
            hours = self.hours.setdefault(sid, {})
            for h, rows in groups.items():
                hours[h] = hist.combine([hist.summarize(rows), hours.get(h)])
        for hours in self.hours.values():
            for ts in [t for t in hours if t < newest - self.keep_days * 86400]:
                del hours[ts]

    def cached(self, key: str, build) -> bytes:
        with self.lock:
            hit = self._cache.get(key)
            if hit and hit[0] == self.version:
                return hit[1]
            body = json.dumps(build(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self._cache[key] = (self.version, body)
            if len(self._cache) > 200:
                self._cache.clear()
            return body


# --------------------------------------------------------------------------
# Katalóg staníc (stanice.json), znovu načítaný pri zmene súboru
# --------------------------------------------------------------------------

class Stations:
    def __init__(self, path: Path):
        self.path = path
        self.mtime = None
        self.data: dict[int, dict] = {}

    def get(self) -> dict[int, dict]:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return self.data
        if mtime != self.mtime:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.data = {int(k): v for k, v in raw.items() if k.isdigit() and isinstance(v, dict)}
                self.mtime = mtime
            except (ValueError, OSError) as err:
                log(f"Súbor stanice.json sa nedá načítať ({err}), používam predchádzajúci zoznam.")
        return self.data


# --------------------------------------------------------------------------
# Výpočty pre API
# --------------------------------------------------------------------------

def window_sum(points: list[tuple[int, tuple]], start: int, end: int, idx: int):
    total, have = 0.0, 0
    for ts, v in points:
        if start < ts <= end and v[idx] is not None:
            total += v[idx]
            have += 1
    return total, have


def today_summary(points: list[tuple[int, tuple]], last_ts: int) -> dict | None:
    """Dnešné minimum, maximum a úhrn zrážok od miestnej polnoci (rovnaký tvar ako historia.daily)."""
    start = hist.day_start(last_ts)
    t_i, r_i, g_i = IDX["t"], IDX["zra_uhrn"], IDX["vie_max_rych"]
    lo = hi = gust = None
    rain, n_rain, n = 0.0, 0, 0
    for ts, v in points:
        if ts < start:
            continue
        n += 1
        t = v[t_i]
        if t is not None:
            if lo is None or t < lo[0]:
                lo = (t, ts)
            if hi is None or t > hi[0]:
                hi = (t, ts)
        if v[g_i] is not None and (gust is None or v[g_i] > gust):
            gust = v[g_i]
        if v[r_i] is not None:
            rain += v[r_i]
            n_rain += 1
    if not n:
        return None
    span = max(600, last_ts + 60 - start) / 60
    return {"den": hist.day_label(start), "zaciatok": start, "tMin": lo and lo[0], "tMax": hi and hi[0],
            "tMinCas": lo and lo[1], "tMaxCas": hi and hi[1], "naraz": gust,
            "zrazky": round(rain, 1) if n_rain else None,
            "pokrytie": round(min(1, n / span), 2), "pokrytieZrazok": round(min(1, n_rain / span), 2)}


def build_current(store: Store, stations: Stations) -> dict:
    catalog = stations.get()
    out = []
    newest_all = 0
    for sid, series in store.series.items():
        if not series:
            continue
        points = sorted(series.items())
        last_ts = points[-1][0]
        newest_all = max(newest_all, last_ts)
        latest: dict[str, float | None] = {}
        for ts, v in reversed(points):
            if last_ts - ts > 3600:
                break
            for name, i in IDX.items():
                if name not in latest and v[i] is not None:
                    latest[name] = v[i]
            if len(latest) == len(IDX):
                break

        t_idx = IDX["t"]
        temps = [v[t_idx] for ts, v in points if v[t_idx] is not None and last_ts - ts <= 24 * 3600]
        t_hour_ago = None
        for ts, v in points:
            if ts >= last_ts - 3600 and v[t_idx] is not None:
                if ts <= last_ts - 3000:  # aspoň 50 minút staré
                    t_hour_ago = v[t_idx]
                break

        rain_i = IDX["zra_uhrn"]
        r1, c1 = window_sum(points, last_ts - 3600, last_ts, rain_i)
        r24, c24 = window_sum(points, last_ts - 86400, last_ts, rain_i)
        today = today_summary(points, last_ts)

        raw_ts, raw = store.latest_raw.get(sid, (None, {}))
        soil = {d: num(raw.get(f"t_pod{d}")) for d in (5, 10, 20, 50, 100)}
        meta = catalog.get(sid, {})
        out.append({
            "id": sid,
            "nazov": meta.get("nazov"),
            "lat": meta.get("lat"),
            "lon": meta.get("lon"),
            "vyska": meta.get("vyska"),
            "vyskaTlak": meta.get("vyskaTlak"),
            "vyskaZdroj": meta.get("vyskaZdroj"),
            "cas": last_ts,
            "hodnoty": latest,
            "zmenaTeploty1h": None if t_hour_ago is None or "t" not in latest else round(latest["t"] - t_hour_ago, 1),
            "tMin24": min(temps) if temps else None,
            "tMax24": max(temps) if temps else None,
            "dnes": today,
            "zrazky1h": None if c1 == 0 else {"mm": round(r1, 2), "pokrytie": round(min(c1 / 60, 1), 2)},
            "zrazky24h": None if c24 == 0 else {"mm": round(r24, 2), "pokrytie": round(min(c24 / 1440, 1), 2)},
            "poda": {k: v for k, v in soil.items() if v is not None},
            "historiaOd": points[0][0],
        })
    out.sort(key=lambda s: s["id"])
    return {
        "najnovsieMeranie": newest_all or None,
        "subor": store.newest_file.rpartition("/")[2] if store.newest_file else None,
        "stanice": out,
    }


circular_mean = hist.circular_mean


def build_station(store: Store, stations: Stations, sid: int) -> dict | None:
    series = store.series.get(sid)
    if not series:
        return None
    meta = stations.get().get(sid, {})
    groups: dict[int, list[tuple]] = {}
    for ts, v in series.items():
        groups.setdefault(ts // 600 * 600, []).append(v)
    bins = [(k, hist.summarize(groups[k])) for k in sorted(groups)]
    hours = sorted(store.hours.get(sid, {}).items())
    cols = hist.series_columns(bins, 600)   # čas = stred 10-minútového intervalu
    raw_ts, raw = store.latest_raw.get(sid, (None, {}))
    return {
        "id": sid,
        "nazov": meta.get("nazov"),
        "vyska": meta.get("vyska"),
        "vyskaTlak": meta.get("vyskaTlak"),
        "lat": meta.get("lat"),
        "lon": meta.get("lon"),
        "rady": cols,
        "radyHod": hist.series_columns(hours, 3600),
        "dni": hist.daily([(ts, b, 3600) for ts, b in hours] + [(ts, b, 600) for ts, b in bins],
                          until=max(series) + 60),
        "poslednyZaznam": {"cas": raw_ts, "hodnoty": raw},
    }


# --------------------------------------------------------------------------
# Sťahovanie na pozadí
# --------------------------------------------------------------------------

def _half_hour(path: str) -> bool:
    """Súbor s meraniami končiacimi o celej alebo o pol hodine (vzorka pre staršie dni)."""
    m = re.search(r" \d{2}-(\d{2})-\d{2}\.json$", path)
    return bool(m) and int(m.group(1)) % 30 == 0


class Collector(threading.Thread):
    def __init__(self, shmu: Shmu, store: Store, history_hours: int, full_hours: int, poll: int):
        super().__init__(daemon=True)
        self.shmu, self.store = shmu, store
        self.history_files = history_hours * 12
        self.full_files = full_hours * 12
        self.poll = poll
        self.backfilled = False
        self.days_done = False
        self.older: set[str] = set()
        self.older_days: set[str] = set()

    def fetch(self, path: str) -> None:
        try:
            records = self.shmu.snapshot(path)
        except Exception as err:  # jeden zlý súbor nezastaví ostatné
            log(f"Preskakujem {path.rpartition('/')[2]}: {err}")
            self.store.processed.add(path)
            return
        self.store.ingest(path, records)

    def backfill(self, files: list[str]) -> None:
        window = files[-self.history_files:]
        newest = window[-1:]
        full = window[-self.full_files:-1]
        older = window[:-self.full_files][::-1][::6]  # každý 6. súbor = raz za 30 min
        plan = [p for p in newest + full[::-1] + older if p not in self.store.processed]
        self.store.status["historia"] = {"hotovo": 0, "spolu": len(plan)}
        log(f"Sťahujem históriu: {len(plan)} súborov (najnovší ako prvý).")
        if newest:
            self.fetch(newest[0])
            self.store.status["historia"]["hotovo"] = 1
            self.store.status["stav"] = "historia"
        with ThreadPoolExecutor(max_workers=3) as pool:
            for _ in pool.map(self.fetch, plan[1:]):
                self.store.status["historia"]["hotovo"] += 1
        self.backfilled = True
        log("História načítaná.")

    def backfill_days(self, files: list[str]) -> None:
        """Po častiach doplní staršie dni pre grafy: súbor každých 30 minút, najviac 24 súborov naraz."""
        if self.days_done or self.store.keep_days <= 1:
            return
        oldest = hist.day_label(time.time() - (self.store.keep_days + 1) * 86400).replace("-", "")
        recent = set(files[-self.history_files:])
        plan = [p for p in files if p not in recent and p not in self.older and _half_hour(p)][::-1]
        if len(plan) < 24:
            recent_days = {p.rsplit("/", 2)[-2] for p in files}
            days = sorted((e.rstrip("/") for e in self.shmu.listing(OBS_PATH) if DAY_RE.match(e)), reverse=True)
            for day in days:
                if day < oldest or len(plan) >= 24:
                    break
                if day in recent_days or day in self.older_days:
                    continue
                folder = f"{OBS_PATH}/{day}"
                todo = sorted((f"{folder}/{n}" for n in self.shmu.listing(folder) if FILE_RE.match(n)), reverse=True)
                todo = [p for p in todo if _half_hour(p) and p not in self.older]
                if not todo:
                    self.older_days.add(day)
                plan.extend(todo)
        plan = plan[:24]
        if not plan:
            self.days_done = True
            log("História dní načítaná.")
            return
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(self.fetch, plan))
        self.older.update(plan)

    def run(self) -> None:
        while True:
            ok = True
            try:
                files = self.shmu.recent_files()
                if not files:
                    raise ConnectionError("Na serveri SHMÚ zatiaľ nie sú žiadne súbory s meraniami.")
                if not self.backfilled:
                    self.backfill(files)
                else:
                    new = [p for p in files[-36:] if p not in self.store.processed]
                    for path in new:
                        self.fetch(path)
                    if new:
                        log(f"Nové merania: {', '.join(p.rpartition('/')[2] for p in new)}")
                    self.store.processed &= set(files)
                    try:
                        self.backfill_days(files)
                    except Exception as err:
                        log(f"História dní: {err}")
                self.store.status.update(stav="ok", chyba=None)
            except Exception as err:
                ok = False
                self.store.status.update(stav="chyba", chyba=str(err))
                log(f"Chyba: {err}")
            self.store.status["poslednaKontrola"] = int(time.time())
            time.sleep(self.poll if ok else 60)


# --------------------------------------------------------------------------
# Zrážkomerné stanice a radar
# --------------------------------------------------------------------------

class GaugeCollector(threading.Thread):
    """Každé 2 minúty nové súbory zrážkomerov; pri štarte posledných 24 h, staršie dni postupne."""

    def __init__(self, shmu: Shmu, poll: int):
        super().__init__(daemon=True)
        self.shmu, self.poll = shmu, poll
        self.gauges = zr.Gauges()
        self.lock = threading.Lock()
        self.status = {"stav": "start", "chyba": None, "poslednaKontrola": None}
        self.version = 0
        self._cache: tuple[int, bytes] | None = None

    def body(self) -> bytes:
        with self.lock:
            if not self._cache or self._cache[0] != self.version:
                lst, newest = self.gauges.build(zr.load_catalog())
                doc = {"stav": self.status, "najnovsieMeranie": newest or None, "stanice": lst}
                self._cache = (self.version, json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            return self._cache[1]

    def run(self) -> None:
        first = True
        while True:
            # sťahuje sa do kópie, aby stránka medzitým čítala ucelené dáta
            shadow = zr.Gauges(self.gauges.to_state(sorted(self.gauges.processed)))
            shadow.older, shadow.older_days = set(self.gauges.older), set(self.gauges.older_days)
            _files, status = zr.update(self.shmu, shadow, max_files=288 if first else 72, older=12, log=log)
            with self.lock:
                self.gauges = shadow
                self.status = status
                self.version += 1
            first = False
            time.sleep(self.poll if status["stav"] == "ok" else 60)


class RadarCollector(threading.Thread):
    """Každé 2,5 minúty nové snímky radaru do priečinka _radar (posledné 2 hodiny)."""

    def __init__(self, shmu: Shmu, folder: Path, poll: int = 150):
        super().__init__(daemon=True)
        self.shmu, self.folder, self.poll = shmu, folder, poll
        self.meta: dict = {"snimky": [], "mriezka": None, "stav": {"stav": "start", "chyba": None}}

    def run(self) -> None:
        while True:
            self.meta = radar.update(self.shmu, self.folder, log=log)
            time.sleep(self.poll if self.meta["stav"]["stav"] == "ok" else 60)


# --------------------------------------------------------------------------
# Predpoveď ALADIN
# --------------------------------------------------------------------------

# pole: (jednotka, popis)
FORECAST_FIELDS = {
    "t": ("°C", "Teplota vzduchu 2 m"),
    "zrazky": ("mm/h", "Zrážky"),
    "oblacnost": ("%", "Oblačnosť"),
    "vietor": ("m/s", "Priemerný vietor 10 m"),
    "naraz": ("m/s", "Nárazy vetra"),
    "tlak": ("hPa", "Tlak na hladine mora"),
}


class Forecast:
    def __init__(self):
        self.lock = threading.Lock()
        self.version = 0
        self.run: int | None = None       # čas behu modelu (epoch, UTC)
        self.source: str | None = None
        self.hours: tuple = ()
        self.steps: list[dict] = []       # {h, cas, okno, polia: {meno: array alebo None}}
        self.active: list[int] = []       # indexy bodov mriežky s hodnotami
        self.status = {"stav": "start", "chyba": None, "poslednaKontrola": None, "stiahnute": 0, "spolu": 0}
        self._cache: dict[str, tuple[int, bytes]] = {}

    def cached(self, key: str, build) -> bytes:
        with self.lock:
            hit = self._cache.get(key)
            if hit and hit[0] == self.version:
                return hit[1]
        body = json.dumps(build(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with self.lock:
            if len(self._cache) > 50:
                self._cache.clear()
            self._cache[key] = (self.version, body)
        return body


def _speed(u, v):
    if u is None or v is None:
        return None
    return aladin.to_array(math.hypot(a, b) for a, b in zip(u, v))


def decode_forecast_file(payload: bytes):
    """Z jedného hodinového súboru vyberie polia pre mapy (len aktívne body)."""
    found, mask, okno, run = {}, None, None, None
    for msg in aladin.iter_messages(payload):
        found[msg["kluc"]] = msg["hodnoty"]
        mask = mask or msg["maska"]
        run = run or msg["beh"]
        if msg["kluc"] == aladin.TP:
            okno = msg["okno"]
    t = found.get(aladin.T2M)
    tp = found.get(aladin.TP)
    tcc = found.get(aladin.TCC)
    p = found.get(aladin.PRMSL)
    fields = {
        "t": None if t is None else aladin.to_array(v - 273.15 for v in t),
        "zrazky": None if tp is None or not okno else aladin.to_array(max(0.0, v) / okno for v in tp),
        "oblacnost": None if tcc is None else aladin.to_array(min(100.0, max(0.0, v * 100)) for v in tcc),
        "vietor": _speed(found.get(aladin.U10), found.get(aladin.V10)),
        "naraz": _speed(found.get(aladin.GUST_U), found.get(aladin.GUST_V)),
        "tlak": None if p is None else aladin.to_array(v / 100 for v in p),
    }
    return fields, mask, okno, run


class ForecastCollector(threading.Thread):
    def __init__(self, shmu: Shmu, fc: Forecast, poll: int = 1200):
        super().__init__(daemon=True)
        self.shmu, self.fc, self.poll = shmu, fc, poll

    def pick_run(self):
        """Najnovší beh, ktorý má súvislé kroky aspoň do +72 h."""
        days = sorted((e for e in self.shmu.listing(NWP_PATH) if DAY_RE.match(e)), reverse=True)
        for day in days[:2]:
            day_path = f"{NWP_PATH}/{day.rstrip('/')}"
            runs = sorted((e for e in self.shmu.listing(day_path) if RUN_DIR_RE.match(e)), reverse=True)
            for run in runs:
                run_path = f"{day_path}/{run.rstrip('/')}"
                available = {}
                for name in self.shmu.listing(run_path):
                    m = NWP_FILE_RE.match(name)
                    if m:
                        available[int(m.group(1))] = name
                usable = []
                for h in NWP_HOURS:
                    if h not in available:
                        break
                    usable.append(h)
                if usable and usable[-1] >= NWP_MIN_HOURS:
                    run_dt = datetime.strptime(day.rstrip("/") + run.rstrip("/"), "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
                    return run_path, int(run_dt.timestamp()), [(h, available[h]) for h in usable]
        raise ConnectionError("Na serveri SHMÚ sa nenašiel úplný beh modelu ALADIN.")

    def load(self, run_path: str, run_ts: int, files: list) -> None:
        self.fc.status.update(stav="stahujem", stiahnute=0, spolu=len(files))
        log(f"Sťahujem predpoveď ALADIN {run_path.rsplit('/', 2)[-2]}/{run_path.rsplit('/', 1)[-1]}: {len(files)} súborov.")

        def fetch(item):
            h, name = item
            payload = self.shmu.get(f"{run_path}/{quote(name)}")
            decoded = decode_forecast_file(payload)
            self.fc.status["stiahnute"] += 1
            return h, decoded

        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(fetch, files))
        steps, mask = [], None
        for h, (fields, m, okno, _run) in results:
            mask = mask or m
            steps.append({"h": h, "cas": run_ts + h * 3600, "okno": okno, "polia": fields})
        if mask is None:
            raise ValueError("Súbory predpovede neobsahujú žiadne polia.")
        with self.fc.lock:
            self.fc.run, self.fc.source = run_ts, run_path
            self.fc.hours = tuple(h for h, _ in files)
            self.fc.steps = steps
            self.fc.active = [k for k, b in enumerate(mask) if b]
            self.fc.version += 1
        log(f"Predpoveď ALADIN načítaná ({len(steps)} krokov do +{steps[-1]['h']} h).")

    def run(self) -> None:
        while True:
            ok = True
            try:
                run_path, run_ts, files = self.pick_run()
                hours = tuple(h for h, _ in files)
                if run_path != self.fc.source or hours != self.fc.hours:
                    self.load(run_path, run_ts, files)
                self.fc.status.update(stav="ok", chyba=None)
            except Exception as err:
                ok = False
                self.fc.status.update(stav="chyba", chyba=str(err))
                log(f"Predpoveď: {err}")
            self.fc.status["poslednaKontrola"] = int(time.time())
            time.sleep(self.poll if ok else 300)


def _grid_corners():
    lat, lon = [], []
    for j in range(aladin.NY + 1):
        for i in range(aladin.NX + 1):
            la, lo = aladin.grid_latlon(i - 0.5, j - 0.5)
            lat.append(round(la, 4))
            lon.append(round(lo, 4))
    return {"lat": lat, "lon": lon}


GRID_CORNERS = _grid_corners()


def build_forecast_meta(fc: Forecast) -> dict:
    with fc.lock:
        return {
            "beh": fc.run,
            "kroky": [{"h": s["h"], "cas": s["cas"], "okno": s["okno"]} for s in fc.steps],
            "polia": {k: {"jednotka": u, "nazov": n} for k, (u, n) in FORECAST_FIELDS.items()},
            "mriezka": {"nx": aladin.NX, "ny": aladin.NY, "aktivne": fc.active, "rohy": GRID_CORNERS},
        }


def build_forecast_field(fc: Forecast, name: str) -> dict:
    # zrážky na 0,01 mm/h, aby slabý dážď mal na mape plynulý okraj; ostatné na 0,1
    mul = 100 if name == "zrazky" else 10
    with fc.lock:
        values = []
        for s in fc.steps:
            arr = s["polia"].get(name)
            values.append(None if arr is None else [round(v * mul) for v in arr])
        return {"pole": name, "jednotka": FORECAST_FIELDS[name][0], "nasobok": mul, "hodnoty": values}


def build_forecast_point(fc: Forecast, lat: float, lon: float) -> dict | None:
    with fc.lock:
        if not fc.steps:
            return None
        i0, j0 = aladin.nearest_index(lat, lon)
        best, best_d = None, None
        for pos, k in enumerate(fc.active):
            d = (k % aladin.NX - i0) ** 2 + (k // aladin.NX - j0) ** 2
            if best_d is None or d < best_d:
                best, best_d = pos, d
        k = fc.active[best]
        clat, clon = aladin.grid_latlon(k % aladin.NX, k // aladin.NX)
        series = {"cas": [s["cas"] for s in fc.steps], "okno": [s["okno"] for s in fc.steps]}
        for name in FORECAST_FIELDS:
            series[name] = [None if s["polia"].get(name) is None else round(s["polia"][name][best], 2) for s in fc.steps]
        return {"beh": fc.run, "lat": round(clat, 4), "lon": round(clon, 4), "rady": series}


_POS_CACHE: dict = {}


def model_temperature(fc: Forecast | None, lat: float, lon: float, ts: int):
    """Teplota z ALADIN-u v najbližšom bode mriežky a najbližšom čase (do 90 minút)."""
    if fc is None:
        return None
    with fc.lock:
        if not fc.steps:
            return None
        key = (fc.version, round(lat, 3), round(lon, 3))
        pos = _POS_CACHE.get(key)
        if pos is None:
            i0, j0 = aladin.nearest_index(lat, lon)
            pos = min(range(len(fc.active)),
                      key=lambda p: (fc.active[p] % aladin.NX - i0) ** 2 + (fc.active[p] // aladin.NX - j0) ** 2)
            if len(_POS_CACHE) > 500:
                _POS_CACHE.clear()
            _POS_CACHE[key] = pos
        step = min(fc.steps, key=lambda s: abs(s["cas"] - ts))
        if abs(step["cas"] - ts) > 5400 or step["polia"].get("t") is None:
            return None
        return step["polia"]["t"][pos]


def nearest_shmu_temperature(store: Store, stations: Stations, lat: float, lon: float, ts: int):
    """(teplota, km, názov) najbližšej stanice SHMÚ do 30 km s meraním do hodiny od ts."""
    catalog = stations.get()
    best = None
    with store.lock:
        for sid, (rts, rec) in store.latest_raw.items():
            meta = catalog.get(sid) or {}
            if meta.get("lat") is None or abs(rts - ts) > 3600:
                continue
            t = num(rec.get("t"))
            if t is None:
                continue
            dy = (meta["lat"] - lat) * 111.2
            dx = (meta["lon"] - lon) * 111.2 * math.cos(math.radians(lat))
            km = math.hypot(dx, dy)
            if km <= 30 and (best is None or km < best[1]):
                best = (t, km, meta.get("nazov") or str(sid))
    return best


# --------------------------------------------------------------------------
# Webový server
# --------------------------------------------------------------------------

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def make_handler(store: Store, stations: Stations, fc: Forecast | None, wux: dict,
                 gauges: GaugeCollector | None = None, rad: RadarCollector | None = None):
    wset, wstore, wclient, wcoll = wux["settings"], wux["store"], wux["client"], wux["collector"]

    def wu_list() -> bytes:
        def build():
            return wu.build_list(wstore, lambda la, lo, ts: model_temperature(fc, la, lo, ts),
                                 lambda la, lo, ts: nearest_shmu_temperature(store, stations, la, lo, ts))
        key = (wstore.version, fc.version if fc else 0, store.version, int(time.time() // 60))
        if wux.get("_key") != key:
            wux["_key"], wux["_body"] = key, json.dumps(build(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return wux["_body"]

    def wu_status() -> bytes:
        return json.dumps(wu.build_status(wset, wstore, wclient), ensure_ascii=False).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        server_version = "PocasieSHMU/1.0"

        def log_message(self, fmt, *args):  # tichý prístupový log
            pass

        def send_body(self, code: int, body: bytes, ctype: str, cache: str = "no-store") -> None:
            compressible = ctype.startswith(("application/json", "text/"))
            if compressible and len(body) > 1400 and "gzip" in self.headers.get("Accept-Encoding", ""):
                body = gzip.compress(body, 5)
                self.send_response(code)
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
            else:
                self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def send_json(self, body: bytes, code: int = 200) -> None:
            self.send_body(code, body, CONTENT_TYPES[".json"])

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            path = urlparse(self.path).path
            try:
                if path == "/api/current":
                    body = store.cached("current", lambda: build_current(store, stations))
                    status = json.dumps(store.status, ensure_ascii=False).encode("utf-8")
                    return self.send_json(b'{"stav":' + status + b',"amaterske":' + wu_list() +
                                          b',"wuStav":' + wu_status() + b"," + body[1:])
                m = re.fullmatch(r"/api/station/wu-([A-Za-z0-9]{3,24})", path)
                if m:
                    data = wu.build_series(wstore, m.group(1).upper())
                    if data is None:
                        return self.send_json(b'{"chyba":"Stanica nema ziadne merania."}', 404)
                    return self.send_json(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                if path == "/api/wu":
                    return self.send_json(wu_status())
                m = re.fullmatch(r"/api/station/(\d+)", path)
                if m:
                    sid = int(m.group(1))
                    with store.lock:
                        exists = sid in store.series
                    if not exists:
                        return self.send_json(b'{"chyba":"Stanica nema ziadne merania."}', 404)
                    return self.send_json(store.cached(f"st{sid}", lambda: build_station(store, stations, sid)))
                if path == "/api/status":
                    return self.send_json(json.dumps(store.status, ensure_ascii=False).encode("utf-8"))
                if path == "/api/zrazkomery":
                    if gauges is None:
                        return self.send_json('{"chyba":"Zrážkomery sú vypnuté."}'.encode("utf-8"), 404)
                    return self.send_json(gauges.body())
                if path == "/api/radar":
                    if rad is None:
                        return self.send_json('{"chyba":"Radar je vypnutý."}'.encode("utf-8"), 404)
                    return self.send_json(json.dumps(rad.meta, ensure_ascii=False).encode("utf-8"))
                m = re.fullmatch(r"/api/radar/(\d{12}\.bin)", path)
                if m and rad is not None and (rad.folder / m.group(1)).is_file():
                    return self.send_body(200, (rad.folder / m.group(1)).read_bytes(), "application/octet-stream",
                                          "public, max-age=86400")
                if path.startswith("/api/forecast"):
                    return self.forecast(path)
                if path in ("/", ""):
                    path = "/index.html"
                target = (STATIC_DIR / path.lstrip("/")).resolve()
                if STATIC_DIR not in target.parents or not target.is_file():
                    return self.send_body(404, "Stránka sa nenašla.".encode("utf-8"), "text/plain; charset=utf-8")
                ctype = CONTENT_TYPES.get(target.suffix, "application/octet-stream")
                return self.send_body(200, target.read_bytes(), ctype, "no-cache")
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            path = urlparse(self.path).path
            try:
                if path not in ("/api/wu/nastavenia", "/api/wu/odpojit"):
                    return self.send_json('{"chyba":"Neznáma požiadavka."}'.encode("utf-8"), 404)
                problem = self.local_request_problem()
                if problem:
                    return self.send_json(json.dumps({"chyba": problem}, ensure_ascii=False).encode("utf-8"), 403)
                length = int(self.headers.get("Content-Length") or 0)
                if length > 4096:
                    return self.send_json('{"chyba":"Požiadavka je príliš veľká."}'.encode("utf-8"), 413)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    data = json.loads(raw or b"{}")
                    if not isinstance(data, dict):
                        raise ValueError
                except ValueError:
                    return self.send_json('{"chyba":"Neplatné údaje."}'.encode("utf-8"), 400)
                if path == "/api/wu/odpojit":
                    wset.clear()
                    wcoll.wake.set()
                    log("Weather Underground: nastavenia zmazané.")
                    return self.send_json(wu_status())
                err = self.save_wu(data)
                if err:
                    return self.send_json(json.dumps({"chyba": err}, ensure_ascii=False).encode("utf-8"), 400)
                wcoll.wake.set()
                log(f"Weather Underground: uložená stanica {wset.data.get('stanica')}.")
                return self.send_json(wu_status())
            except (BrokenPipeError, ConnectionResetError):
                pass

        def local_request_problem(self):
            """Nastavenia smie meniť len prehliadač na tomto počítači a len z tejto stránky."""
            if self.client_address[0] not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                return "Nastavenia možno meniť len na počítači, na ktorom beží server."
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return "Neplatný typ požiadavky."
            origin = self.headers.get("Origin")
            host = self.headers.get("Host", "")
            if origin and urlparse(origin).netloc != host:
                return "Požiadavka neprišla z tejto stránky."
            return None

        def save_wu(self, data: dict):
            kluc = str(data.get("kluc") or "").strip()
            stanica = str(data.get("stanica") or "").strip().upper()
            dalsie_raw = data.get("dalsie") or ""
            if isinstance(dalsie_raw, list):
                dalsie_raw = " ".join(str(x) for x in dalsie_raw)
            dalsie = [x.upper() for x in re.split(r"[\s,;]+", str(dalsie_raw)) if x]
            if kluc and not wu.KEY_RE.match(kluc):
                return "Kľúč k API má mať 32 znakov (číslice 0–9 a písmená a–f)."
            if not kluc and not wset.data.get("kluc"):
                return "Zadajte kľúč k API."
            if not wu.STATION_RE.match(stanica):
                return "ID stanice má tvar ako IBRATI123 (len písmená a číslice)."
            bad = [x for x in dalsie if not wu.STATION_RE.match(x)]
            if bad:
                return f"Neplatné ID stanice: {', '.join(bad[:3])}"
            if len(dalsie) > wu.MAX_OTHERS:
                return f"Ďalších staníc môže byť najviac {wu.MAX_OTHERS}."
            wset.update(kluc or None, stanica, dalsie, bool(data.get("okolie", True)))
            return None

        def forecast(self, path: str):
            if fc is None:
                return self.send_json('{"chyba":"Predpoveď je vypnutá (--bez-predpovede)."}'.encode("utf-8"), 404)
            status = json.dumps(fc.status, ensure_ascii=False).encode("utf-8")
            if path == "/api/forecast":
                body = fc.cached("meta", lambda: build_forecast_meta(fc))
                return self.send_json(b'{"stav":' + status + b"," + body[1:])
            if path == "/api/forecast/bod":
                q = dict(p.split("=", 1) for p in urlparse(self.path).query.split("&") if "=" in p)
                try:
                    lat, lon = float(q["lat"]), float(q["lon"])
                except (KeyError, ValueError):
                    return self.send_json('{"chyba":"Chýba lat alebo lon."}'.encode("utf-8"), 400)
                key = f"bod{lat:.3f},{lon:.3f}"
                data = fc.cached(key, lambda: build_forecast_point(fc, lat, lon))
                return self.send_json(data, 200 if data != b"null" else 404)
            m = re.fullmatch(r"/api/forecast/pole/(\w+)", path)
            if m and m.group(1) in FORECAST_FIELDS:
                return self.send_json(fc.cached(f"pole{m.group(1)}", lambda: build_forecast_field(fc, m.group(1))))
            return self.send_json('{"chyba":"Neznáma požiadavka."}'.encode("utf-8"), 404)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Webová stránka s meraniami automatických staníc SHMÚ.")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)), help="port webovej stránky (predvolene 8000)")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"),
                        help="adresa, na ktorej server počúva (0.0.0.0 = aj pre ostatné zariadenia v sieti)")
    parser.add_argument("--hodiny", type=int, default=24, help="koľko hodín histórie držať pre grafy (predvolene 24)")
    parser.add_argument("--plne-hodiny", type=int, default=2,
                        help="koľko posledných hodín stiahnuť pri štarte úplne; staršie po 30 min (predvolene 2)")
    parser.add_argument("--interval", type=int, default=120, help="ako často kontrolovať nové dáta v sekundách (predvolene 120)")
    parser.add_argument("--otvorit", action="store_true", help="po spustení otvoriť stránku v prehliadači")
    parser.add_argument("--bez-predpovede", action="store_true", help="nesťahovať predpoveď ALADIN (asi 11 MB za 6 hodín)")
    parser.add_argument("--dni", type=int, default=8,
                        help="koľko dní histórie pre grafy a denné maximá; staršie dni sa dopĺňajú postupne (predvolene 8)")
    parser.add_argument("--bez-radaru", action="store_true", help="nesťahovať snímky radaru")
    parser.add_argument("--bez-zrazkomerov", action="store_true", help="nesťahovať údaje zrážkomerných staníc")
    parser.add_argument("--wu-subor", default=str(HERE / "wunderground.json"), help=argparse.SUPPRESS)
    parser.add_argument("--zdroj", default=os.environ.get("SHMU_BASE_URL", "https://opendata.shmu.sk"), help=argparse.SUPPRESS)
    args = parser.parse_args()

    if sys.version_info < (3, 9):
        sys.exit("Potrebný je Python 3.9 alebo novší.")

    store = Store(args.hodiny, max(1, args.dni))
    stations = Stations(STATIONS_FILE)
    stations.get()
    Collector(Shmu(args.zdroj), store, args.hodiny, max(1, args.plne_hodiny), max(30, args.interval)).start()
    fc = None
    if not args.bez_predpovede:
        fc = Forecast()
        ForecastCollector(Shmu(args.zdroj), fc).start()
    gauges = rad = None
    if not args.bez_zrazkomerov:
        gauges = GaugeCollector(Shmu(args.zdroj), max(30, args.interval))
        gauges.start()
    if not args.bez_radaru:
        rad = RadarCollector(Shmu(args.zdroj), HERE / "_radar")
        rad.start()

    wu_file = Path(args.wu_subor)
    wset = wu.Settings(wu_file)
    wclient = wu.Client(wu_file.with_name(wu_file.stem + "_pocitadlo.json"),
                        make_ssl_context() if wu.WU_BASE.startswith("https") else None)
    wstore = wu.Store()
    wcoll = wu.Collector(wset, wstore, wclient, log=log)
    wcoll.start()
    wux = {"settings": wset, "store": wstore, "client": wclient, "collector": wcoll}
    if wset.configured:
        log(f"Weather Underground: pripájam stanicu {wset.data.get('stanica')}.")

    try:
        httpd = ThreadingHTTPServer((args.host, args.port), make_handler(store, stations, fc, wux, gauges, rad))
    except OSError as err:
        sys.exit(f"Port {args.port} sa nedá použiť ({err}). Skúste napr.: python server.py --port 8080")
    shown = "localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host
    url = f"http://{shown}:{args.port}"
    log(f"Stránka beží na {url}  (ukončenie: Ctrl+C)")
    if args.otvorit:
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("Koniec.")


if __name__ == "__main__":
    main()
