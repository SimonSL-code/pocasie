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
import shutil
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import aladin
import server as srv
import wunderground as wu

HERE = Path(__file__).resolve().parent
KEEP_MINUTES = 80 * 60        # minútové merania: posledných 80 minút
KEEP_BINS = 25 * 3600         # 10-minútové súhrny: posledných 25 hodín
MAX_FILES_PER_RUN = 72        # strop sťahovania, keby GitHub dlho nespúšťal
WU_RUNS_PER_DAY = 144
FORECAST_FORMAT = 2           # zvýšiť pri zmene tvaru uložených polí predpovede (vynúti nové spracovanie)
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

I_T, I_RH, I_P, I_WS, I_WG, I_WD, I_RAIN = (srv.IDX[k] for k in
                                             ("t", "vlh_rel", "tlak", "vie_pr_rych", "vie_max_rych", "vie_pr_smer", "zra_uhrn"))
I_SOL = srv.IDX["zglo"]
# súhrn košíka: t, vlh, tlak, vietor, náraz max, smer, zrážky súčet, zrážky minút, žiarenie, t min, t max, minút spolu
B_T, B_RH, B_P, B_WS, B_WG, B_WD, B_RAIN, B_RAIN_N, B_SOL, B_TMIN, B_TMAX, B_N = range(12)


def summarize(rows: list[tuple]) -> list:
    def vals(i):
        return [r[i] for r in rows if r[i] is not None]

    def mean(i):
        v = vals(i)
        return round(sum(v) / len(v), 2) if v else None

    t, g, rain = vals(I_T), vals(I_WG), vals(I_RAIN)
    return [mean(I_T), mean(I_RH), mean(I_P), mean(I_WS), max(g) if g else None,
            srv.circular_mean(vals(I_WD)), round(sum(rain), 2) if rain else None, len(rain),
            mean(I_SOL), min(t) if t else None, max(t) if t else None, len(rows)]


class Observations:
    def __init__(self, state: dict):
        self.minutes: dict[int, dict[int, tuple]] = {
            int(sid): {int(r[0]): tuple(r[1:]) for r in rows} for sid, rows in state.get("minuty", {}).items()}
        self.bins: dict[int, dict[int, list]] = {
            int(sid): {int(ts): v for ts, v in b.items()} for sid, b in state.get("kosiky", {}).items()}
        self.latest_raw: dict[int, tuple[int, dict]] = {
            int(sid): (int(v[0]), v[1]) for sid, v in state.get("posledny", {}).items()}
        self.processed: set[str] = set(state.get("spracovane", []))
        self.newest_file: str | None = state.get("najnovsiSubor")

    def ingest(self, path: str, records: list[dict]) -> None:
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
        self.processed.add(path)
        name = path.rpartition("/")[2]
        if self.newest_file is None or name > self.newest_file.rpartition("/")[2]:
            self.newest_file = path

    def finish(self) -> None:
        """Prepočíta 10-minútové súhrny z minút a odstráni staré dáta."""
        newest = max((max(m) for m in self.minutes.values() if m), default=0)
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
            for ts in [t for t in b if t < newest - KEEP_BINS]:
                del b[ts]
            if not b:
                del self.bins[sid]

    def to_state(self, recent_files: list[str]) -> dict:
        keep = set(recent_files)
        return {
            "minuty": {str(s): [[ts, *v] for ts, v in sorted(m.items())] for s, m in self.minutes.items()},
            "kosiky": {str(s): {str(ts): v for ts, v in sorted(b.items())} for s, b in self.bins.items()},
            "posledny": {str(s): [ts, rec] for s, (ts, rec) in self.latest_raw.items()},
            "spracovane": sorted(p for p in self.processed if p in keep),
            "najnovsiSubor": self.newest_file,
        }

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
            raw = self.latest_raw.get(sid, (None, {}))[1]
            soil = {d: srv.num(raw.get(f"t_pod{d}")) for d in (5, 10, 20, 50, 100)}
            meta = catalog.get(sid, {})
            out.append({
                "id": sid, "nazov": meta.get("nazov"), "lat": meta.get("lat"), "lon": meta.get("lon"),
                "vyska": meta.get("vyska"), "cas": last_ts, "hodnoty": latest,
                "zmenaTeploty1h": None if t_hour is None or "t" not in latest else round(latest["t"] - t_hour, 1),
                "tMin24": min(tmins) if tmins else None, "tMax24": max(tmaxs) if tmaxs else None,
                "zrazky1h": {"mm": round(sum(r1), 2), "pokrytie": round(min(len(r1) / 60, 1), 2)} if r1 else None,
                "zrazky24h": {"mm": round(r24, 2), "pokrytie": round(min(r24n / 1440, 1), 2)} if r24n else None,
                "poda": {k: v for k, v in soil.items() if v is not None},
                "historiaOd": bins[0][0] if bins else (mins[0][0] if mins else None),
            })
        return out, newest_all

    def series(self, sid: int, catalog: dict[int, dict]) -> dict:
        bins = sorted(self.bins.get(sid, {}).items())
        cols = {k: [] for k in ("cas", "t", "vlh", "tlak", "vietor", "naraz", "smer", "zrazky", "zrazkyMinut", "ziarenie")}
        for ts, b in bins:
            cols["cas"].append(ts + 300)
            cols["t"].append(b[B_T])
            cols["vlh"].append(b[B_RH])
            cols["tlak"].append(b[B_P])
            cols["vietor"].append(b[B_WS])
            cols["naraz"].append(b[B_WG])
            cols["smer"].append(b[B_WD])
            cols["zrazky"].append(b[B_RAIN] if b[B_RAIN_N] else None)
            cols["zrazkyMinut"].append(b[B_RAIN_N])
            cols["ziarenie"].append(b[B_SOL])
        meta = catalog.get(sid, {})
        return {"id": sid, "nazov": meta.get("nazov"), "vyska": meta.get("vyska"),
                "lat": meta.get("lat"), "lon": meta.get("lon"), "rady": cols}


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
    obs.finish()
    return files, status


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

    stations, newest = obs.current(catalog)
    fresh = [s for s in stations if s["lat"] is not None and "t" in s["hodnoty"]]

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

    state = {"verzia": 1, "merania": obs.to_state(files), "predpoved": fc_state, "wu": wu_state}
    dump(stav_dir / "stav.json.gz", state, gz=True)
    log(f"Hotovo: {len(stations)} staníc SHMÚ, {len(amateurs)} amatérskych, "
        f"najnovšie meranie {datetime.fromtimestamp(newest, timezone.utc).strftime('%H:%M UTC') if newest else '—'}.")


if __name__ == "__main__":
    if sys.version_info < (3, 9):
        sys.exit("Potrebný je Python 3.9 alebo novší.")
    main()
