#!/usr/bin/env python3
"""Statická verzia stránky pre GitHub Pages.

Tento skript spúšťa GitHub Actions asi každých 10 minút (pozri
.github/workflows/pocasie.yml). Pri každom spustení:

1. načíta uložený stav z predchádzajúceho behu (priečinok --stav),
2. stiahne nové 5-minútové súbory meraní zo SHMÚ,
3. ak SHMÚ zverejnil nový beh modelu ALADIN, stiahne ho a spracuje,
4. ak je nastavený kľúč Weather Underground, obnoví vlastnú a okolité stanice,
5. zapíše stránku a dátové súbory do priečinka --vystup a nový stav do --stav.

Stránka potom nepotrebuje žiadny server, len hosting statických súborov.

Spustenie na vlastnom počítači (na skúšku):
    python generuj.py --stav stav --vystup _site
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import re
import shutil
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import aladin
import historia as hist
import radar
import server as srv
import wunderground as wu
import zrazkomery as zr

HERE = Path(__file__).resolve().parent
KEEP_MINUTES = 80 * 60        # minútové merania: posledných 80 minút
KEEP_BINS = 25 * 3600         # 10-minútové súhrny: posledných 25 hodín
KEEP_DAYS = 8                 # hodinové súhrny pre grafy na niekoľko dní
OLDER_PER_RUN = 24            # koľko starších súborov (história dní) stiahnuť za jeden beh
MAX_FILES_PER_RUN = 72        # strop sťahovania, keby GitHub dlho nespúšťal
ZR_OLDER_PER_RUN = 36         # zrážkomery: koľko starších súborov (úhrny za predošlé dni) za jeden beh
WU_RUNS_PER_DAY = 144
FORECAST_FORMAT = 2           # zvýšiť pri zmene tvaru uložených polí predpovede (vynúti nové spracovanie)

# ICON-EU (DWD) cez Open-Meteo: mriežka bodov nad Slovenskom
OPEN_METEO = os.environ.get("OPEN_METEO_URL", "https://api.open-meteo.com")
ICON_KROK = 0.125             # stupne; natívna mriežka ICON-EU je 0,0625°, každý druhý bod stačí a šetrí limit
ICON_LAT0, ICON_LON0 = 47.625, 16.75
ICON_LAT1, ICON_LON1 = 49.75, 22.625
ICON_DAVKA = 100              # bodov v jednej požiadavke
ICON_PREMENNE = {             # Open-Meteo → pole na stránke
    "temperature_2m": "t", "precipitation": "zrazky", "cloud_cover": "oblacnost",
    "wind_speed_10m": "vietor", "wind_gusts_10m": "naraz", "pressure_msl": "tlak",
}
ICON_FORMAT = 1
WU_BUDGET = 1200


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def dump(path: Path, obj, gz: bool = False) -> None:
    data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if gz:
        data = gzip.compress(data, 6)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def load(path: Path, default):
    try:
        raw = path.read_bytes()
        if path.suffix == ".gz":
            raw = gzip.decompress(raw)
        return json.loads(raw)
    except (OSError, ValueError, EOFError):
        return default


# --------------------------------------------------------------------------
# Merania SHMÚ: minúty za poslednú hodinu + 10-minútové súhrny za 25 hodín
# --------------------------------------------------------------------------

I_T, I_RAIN = hist.I_T, hist.I_RAIN
# súhrn košíka: t, vlh, tlak, vietor, náraz max, smer, zrážky súčet, zrážky minút, žiarenie, t min, t max, minút spolu
B_RAIN, B_RAIN_N, B_TMIN, B_TMAX, B_N = hist.B_RAIN, hist.B_RAIN_N, hist.B_TMIN, hist.B_TMAX, hist.B_N
summarize = hist.summarize


class Observations:
    def __init__(self, state: dict):
        self.minutes: dict[int, dict[int, tuple]] = {
            int(sid): {int(r[0]): tuple(r[1:]) for r in rows} for sid, rows in state.get("minuty", {}).items()}
        self.bins: dict[int, dict[int, list]] = {
            int(sid): {int(ts): v for ts, v in b.items()} for sid, b in state.get("kosiky", {}).items()}
        # hodinové súhrny pre grafy na niekoľko dní (staršie než 10-minútové košíky)
        self.hours: dict[int, dict[int, list]] = {
            int(sid): {int(ts): v for ts, v in b.items()} for sid, b in state.get("hodiny", {}).items()}
        self.older: set[str] = set(state.get("starsie", []))       # spracované staršie súbory (história dní)
        self.older_days: set[str] = set(state.get("starsieDni", []))  # dni, ktoré sú už doplnené
        self.latest_raw: dict[int, tuple[int, dict]] = {
            int(sid): (int(v[0]), v[1]) for sid, v in state.get("posledny", {}).items()}
        self.processed: set[str] = set(state.get("spracovane", []))
        self.newest_file: str | None = state.get("najnovsiSubor")

    def ingest(self, path: str, records: list[dict], track: bool = True) -> None:
        for rec in records:
            try:
                sid = int(rec.get("ind_kli"))
            except (TypeError, ValueError):
                continue
            ts = srv.parse_minuta(rec.get("minuta"))
            if ts is None:
                continue
            self.minutes.setdefault(sid, {})[ts] = tuple(srv.num(rec.get(f)) for f in srv.SERIES_FIELDS)
            prev = self.latest_raw.get(sid)
            if prev is None or ts >= prev[0]:
                self.latest_raw[sid] = (ts, rec)
        if not track:   # staršie súbory pre históriu dní nemenia "najnovší súbor"
            return
        self.processed.add(path)
        name = path.rpartition("/")[2]
        if self.newest_file is None or name > self.newest_file.rpartition("/")[2]:
            self.newest_file = path

    def finish(self) -> None:
        """Prepočíta 10-minútové súhrny z minút a odstráni staré dáta."""
        newest = max((max(m) for m in self.minutes.values() if m), default=0)
        if not newest:
            return
        for sid, mins in self.minutes.items():
            groups: dict[int, list] = {}
            for ts, v in mins.items():
                groups.setdefault(ts // 600 * 600, []).append(v)
            stored = self.bins.setdefault(sid, {})
            for b, rows in groups.items():
                if b not in stored or len(rows) >= stored[b][B_N]:
                    stored[b] = summarize(rows)
        for sid in list(self.minutes):
            m = self.minutes[sid]
            for ts in [t for t in m if t < newest - KEEP_MINUTES]:
                del m[ts]
            if not m:
                del self.minutes[sid]
        for sid in list(self.bins):
            b = self.bins[sid]
            hist.roll_into_hours(b, self.hours.setdefault(sid, {}), newest - KEEP_BINS)
            if not b:
                del self.bins[sid]
        for sid in list(self.hours):
            h = self.hours[sid]
            for ts in [t for t in h if t < newest - KEEP_DAYS * 86400]:
                del h[ts]
            if not h:
                del self.hours[sid]

    def to_state(self, recent_files: list[str]) -> dict:
        keep = set(recent_files)
        oldest = hist.day_label(time.time() - (KEEP_DAYS + 1) * 86400).replace("-", "")
        return {
            "minuty": {str(s): [[ts, *v] for ts, v in sorted(m.items())] for s, m in self.minutes.items()},
            "kosiky": {str(s): {str(ts): v for ts, v in sorted(b.items())} for s, b in self.bins.items()},
            "hodiny": {str(s): {str(ts): v for ts, v in sorted(b.items())} for s, b in self.hours.items()},
            "posledny": {str(s): [ts, rec] for s, (ts, rec) in self.latest_raw.items()},
            "spracovane": sorted(p for p in self.processed if p in keep),
            "starsie": sorted(p for p in self.older if _file_day(p) >= oldest),
            "starsieDni": sorted(d for d in self.older_days if d >= oldest),
            "najnovsiSubor": self.newest_file,
        }

    def day_items(self, sid: int) -> list[tuple[int, list, int]]:
        return ([(ts, b, 3600) for ts, b in sorted(self.hours.get(sid, {}).items())] +
                [(ts, b, 600) for ts, b in sorted(self.bins.get(sid, {}).items())])

    # výstupy v rovnakom tvare ako /api/current a /api/station/<id> lokálneho servera
    def current(self, catalog: dict[int, dict]) -> tuple[list[dict], int]:
        out, newest_all = [], 0
        for sid in sorted(set(self.minutes) | set(self.bins)):
            mins = sorted(self.minutes.get(sid, {}).items())
            bins = sorted(self.bins.get(sid, {}).items())
            if not mins and not bins:
                continue
            last_ts = mins[-1][0] if mins else bins[-1][0] + 540
            newest_all = max(newest_all, last_ts)
            latest: dict = {}
            for ts, v in reversed(mins):
                if last_ts - ts > 3600:
                    break
                for name, i in srv.IDX.items():
                    if name not in latest and v[i] is not None:
                        latest[name] = v[i]
            t_hour = None
            for ts, v in mins:
                if ts >= last_ts - 3600 and v[I_T] is not None:
                    if ts <= last_ts - 3000:
                        t_hour = v[I_T]
                    break
            r1 = [v[I_RAIN] for ts, v in mins if last_ts - 3600 < ts <= last_ts and v[I_RAIN] is not None]
            day = [b for ts, b in bins if ts > last_ts - 86400]
            r24n = sum(b[B_RAIN_N] for b in day)
            r24 = sum(b[B_RAIN] or 0 for b in day)
            tmins = [b[B_TMIN] for b in day if b[B_TMIN] is not None]
            tmaxs = [b[B_TMAX] for b in day if b[B_TMAX] is not None]
            today = hist.daily([(ts, b, 600) for ts, b in bins if ts >= hist.day_start(last_ts)], until=last_ts + 60)
            raw = self.latest_raw.get(sid, (None, {}))[1]
            soil = {d: srv.num(raw.get(f"t_pod{d}")) for d in (5, 10, 20, 50, 100)}
            meta = catalog.get(sid, {})
            out.append({
                "id": sid, "nazov": meta.get("nazov"), "lat": meta.get("lat"), "lon": meta.get("lon"),
                "vyska": meta.get("vyska"), "vyskaTlak": meta.get("vyskaTlak"), "vyskaZdroj": meta.get("vyskaZdroj"),
                "cas": last_ts, "hodnoty": latest,
                "zmenaTeploty1h": None if t_hour is None or "t" not in latest else round(latest["t"] - t_hour, 1),
                "tMin24": min(tmins) if tmins else None, "tMax24": max(tmaxs) if tmaxs else None,
                "dnes": today[-1] if today else None,
                "zrazky1h": {"mm": round(sum(r1), 2), "pokrytie": round(min(len(r1) / 60, 1), 2)} if r1 else None,
                "zrazky24h": {"mm": round(r24, 2), "pokrytie": round(min(r24n / 1440, 1), 2)} if r24n else None,
                "poda": {k: v for k, v in soil.items() if v is not None},
                "historiaOd": bins[0][0] if bins else (mins[0][0] if mins else None),
            })
        return out, newest_all

    def series(self, sid: int, catalog: dict[int, dict]) -> dict:
        cols = hist.series_columns(sorted(self.bins.get(sid, {}).items()), 600)
        hours = hist.series_columns(sorted(self.hours.get(sid, {}).items()), 3600)
        meta = catalog.get(sid, {})
        mins = self.minutes.get(sid)
        return {"id": sid, "nazov": meta.get("nazov"), "vyska": meta.get("vyska"), "vyskaTlak": meta.get("vyskaTlak"),
                "lat": meta.get("lat"), "lon": meta.get("lon"), "rady": cols, "radyHod": hours,
                "dni": hist.daily(self.day_items(sid), until=max(mins) + 60 if mins else None)}


def update_observations(shmu: srv.Shmu, obs: Observations) -> tuple[list[str], dict]:
    status = {"stav": "ok", "chyba": None, "poslednaKontrola": int(time.time()), "historia": {"hotovo": 0, "spolu": 0}}
    try:
        files = shmu.recent_files()
    except Exception as err:
        status.update(stav="chyba", chyba=str(err))
        log(f"SHMÚ: {err}")
        return sorted(obs.processed), status
    window = files[-288:]  # posledných 24 hodín
    if not obs.processed:
        # prvé spustenie: posledné 2 hodiny celé, staršie po 30 minútach (ako lokálny server)
        plan = window[-24:][::-1] + window[:-24][::-1][::6]
    else:
        # nové súbory, najnovší ako prvý; vynechané staršie sa dopĺňajú postupne v ďalších behoch
        plan = [p for p in window if p not in obs.processed][::-1]
    plan = plan[:MAX_FILES_PER_RUN]
    log(f"SHMÚ: {len(plan)} nových súborov s meraniami.")
    ok = 0
    for path in plan:
        try:
            obs.ingest(path, shmu.snapshot(path))
            ok += 1
        except Exception as err:
            log(f"SHMÚ: preskakujem {path.rpartition('/')[2]}: {err}")
            obs.processed.add(path)
    status["historia"] = {"hotovo": ok, "spolu": len(plan)}
    if obs.processed and len(plan) < MAX_FILES_PER_RUN // 2:
        try:
            backfill_days(shmu, obs, files)
        except Exception as err:
            log(f"SHMÚ: história dní: {err}")
    obs.finish()
    return files, status


def _file_day(path: str) -> str:
    """Deň z cesty k súboru (názov denného priečinka RRRRMMDD)."""
    return path.rsplit("/", 2)[-2] if path.count("/") >= 2 else ""


def _half_hour(path: str) -> bool:
    """Súbor s meraniami končiacimi o celej alebo o pol hodine (vzorka pre staršie dni)."""
    m = re.search(r" \d{2}-(\d{2})-\d{2}\.json$", path)
    return bool(m) and int(m.group(1)) % 30 == 0


def backfill_days(shmu: srv.Shmu, obs: Observations, files: list[str]) -> None:
    """Postupne doplní históriu za posledných KEEP_DAYS dní: súbor každých 30 minút.

    Každý beh stiahne najviac OLDER_PER_RUN súborov, takže celý týždeň sa doplní
    asi za 2 hodiny a potom sa už nesťahuje nič navyše.
    """
    oldest = hist.day_label(time.time() - (KEEP_DAYS + 1) * 86400).replace("-", "")
    recent = set(files[-288:])
    plan = [p for p in files[:-288] if _half_hour(p) and p not in obs.older and p not in recent][::-1]
    if len(plan) < OLDER_PER_RUN:
        days = sorted((e.rstrip("/") for e in shmu.listing(srv.OBS_PATH) if srv.DAY_RE.match(e)), reverse=True)
        recent_days = {_file_day(p) for p in files}
        for day in days:
            if day < oldest or len(plan) >= OLDER_PER_RUN:
                break
            if day in recent_days or day in obs.older_days:
                continue
            folder = f"{srv.OBS_PATH}/{day}"
            todo = [f"{folder}/{n}" for n in shmu.listing(folder) if srv.FILE_RE.match(n)]
            todo = [p for p in sorted(todo, reverse=True) if _half_hour(p) and p not in obs.older]
            if not todo:
                obs.older_days.add(day)
            plan.extend(todo)
    plan = plan[:OLDER_PER_RUN]
    if not plan:
        return
    log(f"SHMÚ: dopĺňam históriu dní, {len(plan)} starších súborov.")
    for path in plan:
        try:
            obs.ingest(path, shmu.snapshot(path), track=False)
        except Exception as err:
            log(f"SHMÚ: preskakujem {path.rpartition('/')[2]}: {err}")
        obs.older.add(path)


# --------------------------------------------------------------------------
# Predpoveď ALADIN
# --------------------------------------------------------------------------

class ForecastFiles:
    """Spracovaná predpoveď uložená v stave (meta + 6 polí)."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.meta = load(folder / "predpoved.json", None)
        self.fields: dict[str, dict] = {}
        self._pos: dict = {}

    def field(self, name: str):
        if name not in self.fields:
            self.fields[name] = load(self.folder / f"predpoved-{name}.json", None)
        return self.fields[name]

    def pos(self, lat: float, lon: float):
        key = (round(lat, 3), round(lon, 3))
        if key not in self._pos:
            act = self.meta["mriezka"]["aktivne"]
            i0, j0 = aladin.nearest_index(lat, lon)
            self._pos[key] = min(range(len(act)), key=lambda p: (act[p] % aladin.NX - i0) ** 2 + (act[p] // aladin.NX - j0) ** 2)
        return self._pos[key]

    def point(self, lat: float, lon: float) -> dict | None:
        if not self.meta or not self.meta.get("kroky"):
            return None
        p = self.pos(lat, lon)
        k = self.meta["mriezka"]["aktivne"][p]
        clat, clon = aladin.grid_latlon(k % aladin.NX, k // aladin.NX)
        rady = {"cas": [s["cas"] for s in self.meta["kroky"]], "okno": [s["okno"] for s in self.meta["kroky"]]}
        for name in srv.FORECAST_FIELDS:
            f = self.field(name)
            if not f:
                continue
            mul = f.get("nasobok", 10)
            rady[name] = [None if step is None else round(step[p] / mul, 2) for step in f["hodnoty"]]
        return {"beh": self.meta.get("beh"), "lat": round(clat, 4), "lon": round(clon, 4), "rady": rady}

    def temperature(self, lat: float, lon: float, ts: int):
        if not self.meta or not self.meta.get("kroky"):
            return None
        f = self.field("t")
        if not f:
            return None
        steps = self.meta["kroky"]
        k = min(range(len(steps)), key=lambda i: abs(steps[i]["cas"] - ts))
        if abs(steps[k]["cas"] - ts) > 5400 or f["hodnoty"][k] is None:
            return None
        return f["hodnoty"][k][self.pos(lat, lon)] / f.get("nasobok", 10)


def update_forecast(shmu: srv.Shmu, folder: Path, state: dict) -> dict:
    fc = srv.Forecast()
    coll = srv.ForecastCollector(shmu, fc)
    status = {"stav": "ok", "chyba": None, "poslednaKontrola": int(time.time())}
    try:
        run_path, run_ts, files = coll.pick_run()
        hours = [h for h, _ in files]
        have = (folder / "predpoved.json").exists() and all((folder / f"predpoved-{n}.json").exists() for n in srv.FORECAST_FIELDS)
        if state.get("zdroj") == run_path and state.get("hodiny") == hours and have and state.get("format") == FORECAST_FORMAT:
            log("ALADIN: beh sa nezmenil.")
        else:
            coll.load(run_path, run_ts, files)
            dump(folder / "predpoved.json", srv.build_forecast_meta(fc))
            for name in srv.FORECAST_FIELDS:
                dump(folder / f"predpoved-{name}.json", srv.build_forecast_field(fc, name))
            state.update(zdroj=run_path, hodiny=hours, beh=run_ts, format=FORECAST_FORMAT)
        status.update(stiahnute=len(hours), spolu=len(hours))
    except Exception as err:
        status.update(stav="chyba", chyba=str(err))
        log(f"ALADIN: {err}")
    return status


# --------------------------------------------------------------------------
# ICON-EU z Open-Meteo
# --------------------------------------------------------------------------

def _inside(ring, lon, lat) -> bool:
    c = False
    for k in range(len(ring)):
        x1, y1 = ring[k]
        x2, y2 = ring[k - 1]
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            c = not c
    return c


def icon_grid() -> dict:
    """Pravidelná mriežka zarovnaná s ICON-EU; aktívne sú body nad Slovenskom a ich susedia."""
    rings = load(srv.STATIC_DIR / "slovensko.json", {}).get("obrys", [])
    nx = int(round((ICON_LON1 - ICON_LON0) / ICON_KROK)) + 1
    ny = int(round((ICON_LAT1 - ICON_LAT0) / ICON_KROK)) + 1
    active = set()
    for j in range(ny):
        for i in range(nx):
            lat, lon = ICON_LAT0 + j * ICON_KROK, ICON_LON0 + i * ICON_KROK
            if any(_inside(r, lon, lat) for r in rings):
                for dj in (-1, 0, 1):
                    for di in (-1, 0, 1):
                        if 0 <= i + di < nx and 0 <= j + dj < ny:
                            active.add((j + dj) * nx + i + di)
    return {"typ": "latlon", "nx": nx, "ny": ny, "lat0": ICON_LAT0, "lon0": ICON_LON0,
            "dlat": ICON_KROK, "dlon": ICON_KROK, "aktivne": sorted(active)}


def _get_json(url: str, tries: int = 2):
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen
    last = None
    for attempt in range(tries):
        try:
            with urlopen(Request(url, headers={"User-Agent": srv.USER_AGENT}), timeout=60) as resp:
                return json.loads(resp.read())
        except HTTPError as err:
            last = err
            if err.code == 429 and attempt + 1 < tries:
                time.sleep(30)
                continue
            if err.code < 500:
                break
        except Exception as err:  # sieť
            last = err
        time.sleep(3)
    raise ConnectionError(f"Open-Meteo: {last}")


def update_icon(folder: Path, state: dict) -> dict:
    status = {"stav": "ok", "chyba": None, "poslednaKontrola": int(time.time())}
    have = (folder / "predpoved.json").exists() and all((folder / f"predpoved-{n}.json").exists() for n in srv.FORECAST_FIELDS)
    try:
        meta = _get_json(f"{OPEN_METEO}/data/dwd_icon_eu/static/meta.json")
        init = int(meta["last_run_initialisation_time"])
    except Exception as err:
        meta, init = None, None
        status["chyba"] = f"Čas behu ICON sa nepodarilo zistiť: {err}"
    now = time.time()
    last = state.get("stiahnute", 0)
    fresh_run = init is not None and init != state.get("beh") and datetime.fromtimestamp(init, timezone.utc).hour % 6 == 0
    stale = now - last > 7 * 3600
    if have and state.get("format") == ICON_FORMAT and not fresh_run and not stale:
        log("ICON: beh sa nezmenil.")
        return status
    grid = icon_grid()
    act = grid["aktivne"]
    pts = [(grid["lat0"] + (k // grid["nx"]) * grid["dlat"], grid["lon0"] + (k % grid["nx"]) * grid["dlon"]) for k in act]
    hourly = ",".join(ICON_PREMENNE)
    series = []
    try:
        for b in range(0, len(pts), ICON_DAVKA):
            chunk = pts[b:b + ICON_DAVKA]
            url = (f"{OPEN_METEO}/v1/forecast?latitude={','.join(f'{la:.4f}' for la, _ in chunk)}"
                   f"&longitude={','.join(f'{lo:.4f}' for _, lo in chunk)}&hourly={hourly}&models=icon_eu"
                   f"&elevation={','.join(['nan'] * len(chunk))}"  # bez prepočtu na výšku terénu: hodnoty modelu
                   f"&wind_speed_unit=ms&forecast_hours=121&timeformat=unixtime&cell_selection=nearest")
            doc = _get_json(url)
            series.extend(doc if isinstance(doc, list) else [doc])
            time.sleep(1)
    except Exception as err:
        status.update(stav="chyba", chyba=str(err))
        log(f"ICON: {err}")
        return status
    if len(series) != len(pts):
        status.update(stav="chyba", chyba="Open-Meteo vrátil iný počet bodov, než sa pýtalo.")
        return status
    times = series[0]["hourly"]["time"]
    idx = list(range(min(72, len(times)))) + list(range(74, len(times), 3))
    run = init if init is not None else int(times[0]) // 21600 * 21600
    kroky = [{"h": int(round((times[i] - run) / 3600)), "cas": int(times[i]), "okno": 1 if i < 72 else 3} for i in idx]

    def value(h: dict, var: str, i: int):
        arr = h.get(var) or []
        if var == "precipitation" and i >= 72:
            part = [arr[q] for q in range(i - 2, i + 1) if 0 <= q < len(arr) and arr[q] is not None]
            return sum(part) / 3 if part else None
        return arr[i] if i < len(arr) else None

    for var, name in ICON_PREMENNE.items():
        mul = 100 if name == "zrazky" else 10
        steps = []
        for i in idx:
            row = []
            for loc in series:
                v = value(loc["hourly"], var, i)
                row.append(None if v is None else round(v * mul))
            steps.append(row if any(v is not None for v in row) else None)
        dump(folder / f"predpoved-{name}.json", {"pole": name, "jednotka": srv.FORECAST_FIELDS[name][0], "nasobok": mul, "hodnoty": steps})
    dump(folder / "predpoved.json", {
        "model": "ICON-EU", "zdroj": "DWD cez Open-Meteo", "beh": run, "kroky": kroky,
        "polia": {k: {"jednotka": u, "nazov": n} for k, (u, n) in srv.FORECAST_FIELDS.items()},
        "mriezka": grid})
    state.update(beh=run, stiahnute=int(now), format=ICON_FORMAT, bodov=len(pts))
    log(f"ICON: {len(pts)} bodov, {len(kroky)} krokov, beh {datetime.fromtimestamp(run, timezone.utc):%d.%m. %H UTC}.")
    return status


# --------------------------------------------------------------------------
# Weather Underground
# --------------------------------------------------------------------------

def update_wu(folder: Path, state: dict, key: str, own: str, extra: list[str]) -> tuple[wu.Store, dict]:
    store = wu.Store()
    status = {"stav": "vypnute", "chyba": None, "interval": None, "poslednaKontrola": int(time.time())}
    settings = wu.Settings(folder / "nepouziva-sa.json")
    settings.data = {"kluc": key, "stanica": own, "dalsie": extra, "okolie": True}
    client = wu.Client(folder / "wu_pocitadlo.json", srv.make_ssl_context() if wu.WU_BASE.startswith("https") else None)
    coll = wu.Collector(settings, store, client, log=log)
    if state.get("vlastna") != own:
        state.clear()
        state["vlastna"] = own
    for sid, st in state.get("stanice", {}).items():
        s = store.ensure(sid, st.get("own", False), st.get("meta"))
        s["series"] = {int(r[0]): tuple(r[1:]) for r in st.get("series", [])}
        s["offline"] = st.get("offline", False)
        s["next"] = st.get("next", 0)
    if not key or not own:
        state.clear()
        store.reset()
        return store, status
    now = time.time()
    try:
        ok = coll.fetch_current(own, True)
        if not ok:
            raise RuntimeError(f"Stanica {own} za poslednú hodinu neposlala žiadne dáta. Skontrolujte jej ID a či je zapnutá.")
        if len(store.stations[own]["series"]) < 3:
            coll.fetch_history(own)
        meta = store.stations[own]["meta"]
        near = state.get("okolie") or {}
        if near.get("datum") != date.today().isoformat() and meta.get("lat") is not None:
            try:
                found = coll.find_near(meta["lat"], meta["lon"])
                near = {"datum": date.today().isoformat(), "stanice": [[s, m] for s, m in found if s.upper() != own]}
                state["okolie"] = near
            except (wu.AuthError, wu.LimitError):
                raise
            except Exception as err:
                status["chyba"] = f"Stanice v okolí sa nepodarilo nájsť: {err}"
        wanted, seen = [], {own}
        for sid, m in [(x, {}) for x in extra] + [tuple(x) for x in near.get("stanice", [])]:
            sid = sid.strip().upper()
            if sid and sid not in seen:
                seen.add(sid)
                wanted.append((sid, m))
        wanted = wanted[:wu.MAX_OTHERS]
        for sid in [s for s in store.stations if s not in seen]:
            del store.stations[sid]
        n = len(wanted)
        per_station = max(1, (WU_BUDGET - WU_RUNS_PER_DAY - 30) / max(n, 1))
        interval = max(1200, math.ceil(86400 / per_station / 600) * 600)
        status["interval"] = interval
        for sid, m in wanted:
            st = store.ensure(sid, False, m)
            if st.get("next", 0) > now:
                continue
            first = not st["series"]
            if coll.fetch_current(sid, False) and first:
                coll.fetch_history(sid)
            st["next"] = now + interval - 60
        status["stav"] = "ok"
    except wu.LimitError as err:
        status.update(stav="limit", chyba=str(err))
    except Exception as err:
        status.update(stav="chyba", chyba=str(err))
        log(f"Weather Underground: {err}")
    state["stanice"] = {
        sid: {"own": st["own"], "meta": st["meta"], "offline": st["offline"], "next": st.get("next", 0),
              "series": [[ts, *v] for ts, v in sorted(st["series"].items())]}
        for sid, st in store.stations.items()}
    status.update(volaniaDnes=client.calls_today(), limit=wu.DAILY_LIMIT)
    return store, status


# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Vytvorí statickú stránku s počasím (pre GitHub Pages).")
    ap.add_argument("--stav", default="stav", help="priečinok s uloženým stavom medzi spusteniami")
    ap.add_argument("--vystup", default="_site", help="kam zapísať stránku")
    ap.add_argument("--zdroj", default=os.environ.get("SHMU_BASE_URL", "https://opendata.shmu.sk"), help=argparse.SUPPRESS)
    ap.add_argument("--bez-predpovede", action="store_true")
    ap.add_argument("--bez-icon", action="store_true", help="nesťahovať mapu ICON z Open-Meteo")
    ap.add_argument("--bez-radaru", action="store_true", help="nesťahovať snímky radaru")
    ap.add_argument("--bez-zrazkomerov", action="store_true", help="nesťahovať údaje zrážkomerných staníc")
    args = ap.parse_args()

    stav_dir, out = Path(args.stav), Path(args.vystup)
    stav_dir.mkdir(parents=True, exist_ok=True)
    state = load(stav_dir / "stav.json.gz", {})
    shmu = srv.Shmu(args.zdroj)
    catalog = srv.Stations(srv.STATIONS_FILE).get()

    obs = Observations(state.get("merania", {}))
    files, obs_status = update_observations(shmu, obs)

    fc_state = state.get("predpoved", {})
    fc_status = {"stav": "vypnute"} if args.bez_predpovede else update_forecast(shmu, stav_dir, fc_state)
    fcf = ForecastFiles(stav_dir)
    icon_state = state.get("icon", {})
    icon_dir = stav_dir / "icon"
    icon_status = {"stav": "vypnute"}
    if not args.bez_predpovede and not args.bez_icon:
        icon_dir.mkdir(parents=True, exist_ok=True)
        icon_status = update_icon(icon_dir, icon_state)

    # zrážkomerné stanice a radar
    gauges = zr.Gauges(state.get("zrazkomery", {}))
    gauge_files, gauge_status = [], {"stav": "vypnute"}
    if not args.bez_zrazkomerov:
        gauge_files, gauge_status = zr.update(shmu, gauges, older=ZR_OLDER_PER_RUN, log=log)
    radar_dir = stav_dir / "radar"
    radar_meta = None if args.bez_radaru else radar.update(shmu, radar_dir, log=log)

    stations, newest = obs.current(catalog)
    fresh =[s for s in stations if s["lat"] is not None and "t" in s["hodnoty"]]

    def shmu_temp(lat, lon, ts):
        best = None
        for s in fresh:
            if abs(s["cas"] - ts) > 3600:
                continue
            dy = (s["lat"] - lat) * 111.2
            dx = (s["lon"] - lon) * 111.2 * math.cos(math.radians(lat))
            km = math.hypot(dx, dy)
            if km <= 30 and (best is None or km < best[1]):
                best = (s["hodnoty"]["t"], km, s["nazov"] or str(s["id"]))
        return best

    key = os.environ.get("WU_API_KEY", "").strip()
    own = os.environ.get("WU_STATION", "").strip().upper()
    extra = [x.upper() for x in os.environ.get("WU_DALSIE", "").replace(";", ",").replace(" ", ",").split(",") if x.strip()]
    wu_state = state.get("wu", {})
    wstore, wu_status = update_wu(stav_dir, wu_state, key, own, extra)
    amateurs = wu.build_list(wstore, fcf.temperature, shmu_temp)
    n_other = sum(1 for st in wstore.stations.values() if not st["own"])
    wu_status.update(
        rezim="github", ostatnych=n_other,
        aktivnych=sum(1 for st in wstore.stations.values() if not st["own"] and st["series"] and not st["offline"]),
        nastavenia={"nastavene": bool(key and own), "klucKonci": None, "stanica": own or None, "dalsie": extra, "okolie": True})
    wu_status.setdefault("volaniaDnes", 0)
    wu_status.setdefault("limit", wu.DAILY_LIMIT)

    # stránka
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(srv.STATIC_DIR, out)
    page = (out / "index.html").read_text(encoding="utf-8")
    page = page.replace('<html lang="sk">', '<html lang="sk" data-mode="static">', 1)
    (out / "index.html").write_text(page, encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")

    generated = int(time.time())
    data = out / "data"
    dump(data / "aktualne.json", {
        "generovane": generated, "stav": obs_status, "amaterske": amateurs, "wuStav": wu_status,
        "najnovsieMeranie": newest or None,
        "subor": obs.newest_file.rpartition("/")[2] if obs.newest_file else None,
        "stanice": stations})
    for s in stations:
        dump(data / "stanice" / f"{s['id']}.json", obs.series(s["id"], catalog))
    for s in amateurs:
        series = wu.build_series(wstore, s["wuId"])
        if series:
            dump(data / "stanice" / f"{s['id']}.json", series)
    if fcf.meta:
        meta = dict(fcf.meta)
        meta["stav"] = fc_status
        dump(data / "predpoved.json", meta)
        for name in srv.FORECAST_FIELDS:
            src = stav_dir / f"predpoved-{name}.json"
            if src.exists():
                shutil.copyfile(src, data / f"predpoved-{name}.json")
        points = {}
        for s in stations + amateurs:
            if s.get("lat") is not None:
                p = fcf.point(s["lat"], s["lon"])
                if p:
                    points[str(s["id"])] = p
        dump(data / "predpoved-body.json", points)
    else:
        dump(data / "predpoved.json", {"stav": fc_status, "kroky": [], "beh": None})
    icon_meta = load(icon_dir / "predpoved.json", None)
    if icon_meta and icon_meta.get("kroky"):
        icon_meta["stav"] = icon_status
        dump(data / "icon" / "predpoved.json", icon_meta)
        for name in srv.FORECAST_FIELDS:
            src = icon_dir / f"predpoved-{name}.json"
            if src.exists():
                shutil.copyfile(src, data / "icon" / f"predpoved-{name}.json")

    if not args.bez_zrazkomerov:
        glist, gnewest = gauges.build(zr.load_catalog())
        dump(data / "zrazkomery.json", {"generovane": generated, "stav": gauge_status, "najnovsieMeranie": gnewest or None,
                                        "stanice": glist})
    if radar_meta:
        (data / "radar").mkdir(parents=True, exist_ok=True)
        for f in radar_meta.get("snimky", []):
            shutil.copyfile(radar_dir / f["subor"], data / "radar" / f["subor"])
        dump(data / "radar.json", radar_meta)

    state = {"verzia": 1, "merania": obs.to_state(files), "predpoved": fc_state, "icon": icon_state, "wu": wu_state,
             "zrazkomery": gauges.to_state(gauge_files) if gauge_files else state.get("zrazkomery", {})}
    dump(stav_dir / "stav.json.gz", state, gz=True)
    log(f"Hotovo: {len(stations)} staníc SHMÚ, {len(amateurs)} amatérskych, "
        f"najnovšie meranie {datetime.fromtimestamp(newest, timezone.utc).strftime('%H:%M UTC') if newest else '—'}.")


if __name__ == "__main__":
    if sys.version_info < (3, 9):
        sys.exit("Potrebný je Python 3.9 alebo novší.")
    main()
