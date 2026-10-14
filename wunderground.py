"""Amatérske stanice z Weather Underground (PWS API).

Kľúč k API dostane zadarmo každý, kto má vlastnú stanicu, ktorá posiela dáta na
Weather Underground (https://www.wunderground.com/member/api-keys). Kľúč má limit
1 500 volaní denne a 30 za minútu, preto tento modul:

* sťahuje vlastnú stanicu každých 5 minút,
* ostatné stanice (v okolí a zadané ručne) tak často, aby sa za deň neprekročilo
  približne 1 300 volaní,
* drží odstup aspoň 2,2 sekundy medzi volaniami a počíta volania za deň.

Nastavenia (kľúč, ID stanice) sa ukladajú do súboru wunderground.json vedľa
servera. Kľúč sa nikdy neposiela späť do prehliadača.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

WU_BASE = os.environ.get("WU_BASE_URL", "https://api.weather.com")
DAILY_LIMIT = 1500
DAILY_BUDGET = 1300          # rezerva pre reštarty a iné aplikácie s rovnakým kľúčom
OWN_INTERVAL = 300           # vlastná stanica: každých 5 minút
MIN_OTHER_INTERVAL = 900     # ostatné stanice najčastejšie raz za 15 minút
MAX_OTHERS = 20
CALL_SPACING = float(os.environ.get("WU_CALL_SPACING", "2.2"))
KEEP_SECONDS = 25 * 3600

KEY_RE = re.compile(r"^[0-9a-fA-F]{32}$")
STATION_RE = re.compile(r"^[A-Za-z0-9]{3,24}$")

# hodnoty v rade: t, vlh, tlak, vietor (m/s), naraz (m/s), smer, zrazky dnes (mm), intenzita (mm/h), ziarenie, rosny bod
F_T, F_RH, F_P, F_WS, F_WG, F_WD, F_PTOT, F_PRATE, F_SOL, F_DEW = range(10)


def _num(v):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _kmh(v):
    v = _num(v)
    return None if v is None else round(v / 3.6, 2)


class Settings:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.data = self._read()
        self.version = 0

    def _read(self) -> dict:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    @property
    def configured(self) -> bool:
        return bool(self.data.get("kluc")) and bool(self.data.get("stanica"))

    def public(self) -> dict:
        d = self.data
        key = d.get("kluc") or ""
        return {
            "nastavene": self.configured,
            "klucKonci": key[-4:] if key else None,
            "stanica": d.get("stanica"),
            "dalsie": d.get("dalsie", []),
            "okolie": d.get("okolie", True),
        }

    def update(self, kluc, stanica, dalsie, okolie) -> None:
        with self.lock:
            new = dict(self.data)
            if kluc:
                new["kluc"] = kluc
            new["stanica"] = stanica
            new["dalsie"] = dalsie
            new["okolie"] = bool(okolie)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(new, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
            self.data = new
            self.version += 1

    def clear(self) -> None:
        with self.lock:
            try:
                self.path.unlink()
            except OSError:
                pass
            self.data = {}
            self.version += 1


class Client:
    """Volania API s odstupom medzi nimi a denným počítadlom."""

    def __init__(self, counter_path: Path, ssl_context=None):
        self.ctx = ssl_context
        self.counter_path = counter_path
        self.last_call = 0.0
        self.lock = threading.Lock()
        self.day, self.calls = self._load_counter()

    def _load_counter(self):
        try:
            d = json.loads(self.counter_path.read_text(encoding="utf-8"))
            if d.get("datum") == date.today().isoformat():
                return d["datum"], int(d.get("volania", 0))
        except (OSError, ValueError, KeyError):
            pass
        return date.today().isoformat(), 0

    def _save_counter(self) -> None:
        try:
            self.counter_path.write_text(json.dumps({"datum": self.day, "volania": self.calls}), encoding="utf-8")
        except OSError:
            pass

    def calls_today(self) -> int:
        if self.day != date.today().isoformat():
            self.day, self.calls = date.today().isoformat(), 0
        return self.calls

    def get(self, path: str, key: str):
        """Vráti (HTTP kód, JSON alebo None)."""
        with self.lock:
            if self.calls_today() >= DAILY_LIMIT - 20:
                raise LimitError("Dnešný limit volaní Weather Underground je takmer vyčerpaný.")
            wait = self.last_call + CALL_SPACING - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.last_call = time.monotonic()
            self.calls += 1
            self._save_counter()
        sep = "&" if "?" in path else "?"
        url = f"{WU_BASE}{path}{sep}format=json&apiKey={quote(key)}"
        req = Request(url, headers={"User-Agent": "pocasie-shmu/1.0", "Accept-Encoding": "identity"})
        try:
            with urlopen(req, timeout=30, context=self.ctx) as resp:
                body = resp.read()
                code = resp.status
        except HTTPError as err:
            return err.code, None
        except (URLError, TimeoutError, OSError) as err:
            raise ConnectionError(f"Weather Underground je nedostupný: {getattr(err, 'reason', err)}")
        if code == 204 or not body:
            return 204, None
        try:
            return code, json.loads(body)
        except ValueError:
            return code, None


class LimitError(RuntimeError):
    pass


class AuthError(RuntimeError):
    pass


class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.version = 0
        self.stations: dict[str, dict] = {}   # ID -> {meta, series, latest, own, next, offline}
        self.status = {"stav": "vypnute", "chyba": None, "interval": None, "poslednaKontrola": None}

    def reset(self) -> None:
        with self.lock:
            self.stations = {}
            self.version += 1

    def ensure(self, sid: str, own: bool = False, meta: dict | None = None) -> dict:
        with self.lock:
            st = self.stations.get(sid)
            if st is None:
                st = {"meta": {"id": sid}, "series": {}, "latest": None, "own": own, "next": 0.0, "offline": False}
                self.stations[sid] = st
            if meta:
                st["meta"].update({k: v for k, v in meta.items() if v is not None})
            st["own"] = st["own"] or own
            return st

    def add_points(self, sid: str, points: list[tuple[int, tuple]], meta: dict | None = None, latest: dict | None = None) -> None:
        with self.lock:
            st = self.stations.get(sid)
            if st is None:
                return
            for ts, vals in points:
                st["series"][ts] = vals
            if meta:
                st["meta"].update({k: v for k, v in meta.items() if v is not None})
            if latest is not None:
                st["latest"] = latest
            st["offline"] = False
            if st["series"]:
                cutoff = max(st["series"]) - KEEP_SECONDS
                for ts in [t for t in st["series"] if t < cutoff]:
                    del st["series"][ts]
            self.version += 1


def parse_current(doc: dict):
    obs = (doc or {}).get("observations") or []
    if not obs:
        return None
    o = obs[0]
    m = o.get("metric") or {}
    ts = int(_num(o.get("epoch")) or 0)
    if not ts:
        return None
    vals = (
        _num(m.get("temp")), _num(o.get("humidity")), _num(m.get("pressure")),
        _kmh(m.get("windSpeed")), _kmh(m.get("windGust")), _num(o.get("winddir")),
        _num(m.get("precipTotal")), _num(m.get("precipRate")), _num(o.get("solarRadiation")),
        _num(m.get("dewpt")),
    )
    meta = {
        "nazov": (o.get("neighborhood") or "").strip() or None,
        "lat": _num(o.get("lat")), "lon": _num(o.get("lon")),
        "vyska": _num(m.get("elev")), "qc": o.get("qcStatus"),
        "softver": o.get("softwareType"),
    }
    return ts, vals, meta


def parse_history(doc: dict):
    points = []
    for o in (doc or {}).get("observations") or []:
        m = o.get("metric") or {}
        ts = int(_num(o.get("epoch")) or 0)
        if not ts:
            continue
        pmax, pmin = _num(m.get("pressureMax")), _num(m.get("pressureMin"))
        pres = (pmax + pmin) / 2 if pmax is not None and pmin is not None else (pmax if pmax is not None else pmin)
        t = _num(m.get("tempAvg"))
        if t is None:
            t = _num(m.get("temp"))
        points.append((ts, (
            t, _num(o.get("humidityAvg")), pres,
            _kmh(m.get("windspeedAvg")), _kmh(m.get("windgustHigh")), _num(o.get("winddirAvg")),
            _num(m.get("precipTotal")), _num(m.get("precipRate")), _num(o.get("solarRadiationHigh")),
            _num(m.get("dewptAvg")),
        )))
    return points


class Collector(threading.Thread):
    def __init__(self, settings: Settings, store: Store, client: Client, log=print):
        super().__init__(daemon=True)
        self.settings, self.store, self.client, self.log = settings, store, client, log
        self.wake = threading.Event()
        self.loaded_version = -1

    def key(self) -> str:
        return self.settings.data.get("kluc", "")

    def call(self, path: str):
        code, doc = self.client.get(path, self.key())
        if code == 401:
            raise AuthError("Kľúč k API nie je platný (HTTP 401). Skontrolujte ho na wunderground.com/member/api-keys.")
        return code, doc

    def fetch_current(self, sid: str, own: bool) -> bool:
        code, doc = self.call(f"/v2/pws/observations/current?stationId={quote(sid)}&units=m&numericPrecision=decimal")
        st = self.store.ensure(sid, own)
        if code == 204:
            st["offline"] = True
            return False
        if code != 200:
            if code == 404 or code == 400:
                st["offline"] = True
                st["meta"]["chyba"] = f"Stanica {sid} neexistuje alebo nie je verejná."
            return False
        parsed = parse_current(doc)
        if parsed is None:
            st["offline"] = True
            return False
        ts, vals, meta = parsed
        self.store.add_points(sid, [(ts, vals)], meta=meta, latest={"ts": ts})
        return True

    def fetch_history(self, sid: str) -> None:
        code, doc = self.call(f"/v2/pws/observations/all/1day?stationId={quote(sid)}&units=m&numericPrecision=decimal")
        if code == 200:
            self.store.add_points(sid, parse_history(doc))

    def find_near(self, lat: float, lon: float) -> list[tuple[str, dict]]:
        code, doc = self.call(f"/v3/location/near?geocode={lat:.4f},{lon:.4f}&product=pws")
        loc = (doc or {}).get("location") or {}
        ids = loc.get("stationId") or []
        out = []
        for k, sid in enumerate(ids):
            if not sid:
                continue

            def pick(name):
                arr = loc.get(name) or []
                return arr[k] if k < len(arr) else None

            out.append((sid, {"nazov": pick("stationName"), "lat": _num(pick("latitude")), "lon": _num(pick("longitude")),
                              "vzdialenost": _num(pick("distanceKm")), "qc": pick("qcStatus")}))
        return out

    def setup(self) -> None:
        """Po zmene nastavení: overí vlastnú stanicu a zostaví zoznam ostatných."""
        self.store.reset()
        own = self.settings.data["stanica"].strip().upper()
        self.store.status.update(stav="pripajam", chyba=None)
        if not self.fetch_current(own, True):
            st = self.store.stations.get(own, {})
            msg = st.get("meta", {}).get("chyba") or (
                f"Stanica {own} za poslednú hodinu neposlala žiadne dáta. Skontrolujte jej ID a či je zapnutá.")
            raise RuntimeError(msg)
        self.fetch_history(own)
        own_meta = self.store.stations[own]["meta"]
        others: list[tuple[str, dict]] = []
        for sid in self.settings.data.get("dalsie", []):
            sid = sid.strip().upper()
            if sid and sid != own:
                others.append((sid, {}))
        if self.settings.data.get("okolie", True) and own_meta.get("lat") is not None:
            try:
                others += [(s, m) for s, m in self.find_near(own_meta["lat"], own_meta["lon"]) if s.upper() != own]
            except (AuthError, LimitError):
                raise
            except Exception as err:  # vyhľadanie okolia nie je nevyhnutné
                self.store.status["chyba"] = f"Stanice v okolí sa nepodarilo nájsť: {err}"
        seen, chosen = set(), []
        for sid, meta in others:
            if sid.upper() in seen:
                continue
            seen.add(sid.upper())
            chosen.append((sid, meta))
        chosen = chosen[:MAX_OTHERS]
        n = len(chosen)
        spare = DAILY_BUDGET - 86400 // OWN_INTERVAL - 2 * n - 10
        interval = MIN_OTHER_INTERVAL if n == 0 else max(MIN_OTHER_INTERVAL, math.ceil(n * 86400 / max(spare, 1) / 60) * 60)
        self.store.status["interval"] = interval
        now = time.time()
        own_st = self.store.stations[own]
        own_st["next"] = now + OWN_INTERVAL
        for k, (sid, meta) in enumerate(chosen):
            st = self.store.ensure(sid, False, meta)
            st["next"] = now + k * 5  # najprv prvé načítanie všetkých
        self.store.status.update(stav="ok")

    def tick(self) -> None:
        now = time.time()
        due = sorted((st["next"], sid) for sid, st in list(self.store.stations.items()) if st["next"] <= now)
        interval = self.store.status.get("interval") or MIN_OTHER_INTERVAL
        for _, sid in due:
            st = self.store.stations.get(sid)
            if st is None:
                continue
            first = not st["series"] and not st["offline"]
            try:
                ok = self.fetch_current(sid, st["own"])
                if ok and first and not st["own"]:
                    self.fetch_history(sid)
                st["next"] = time.time() + (OWN_INTERVAL if st["own"] else interval)
                if st["own"]:
                    self.store.status["chyba"] = None
            except (AuthError, LimitError):
                raise
            except Exception as err:  # výpadok siete: skúsiť o 2 minúty, nič nezahadzovať
                st["next"] = time.time() + 120
                self.store.status["chyba"] = str(err)

    def run(self) -> None:
        while True:
            try:
                if self.settings.version != self.loaded_version:
                    self.loaded_version = self.settings.version
                    if self.settings.configured:
                        self.setup()
                        self.log(f"Weather Underground: stanica {self.settings.data['stanica']} pripojená, "
                                 f"{sum(1 for s in self.store.stations.values() if not s['own'])} staníc v okolí.")
                    else:
                        self.store.reset()
                        self.store.status.update(stav="vypnute", chyba=None, interval=None)
                if self.settings.configured and self.store.status["stav"] == "ok":
                    self.tick()
            except AuthError as err:
                self.store.status.update(stav="chyba", chyba=str(err))
                self.log(f"Weather Underground: {err}")
            except LimitError as err:
                self.store.status.update(stav="limit", chyba=str(err))
                self.log(f"Weather Underground: {err}")
                self.loaded_version = -1 if self.settings.configured else self.loaded_version
                self.wake.wait(1800)
                self.wake.clear()
                continue
            except Exception as err:
                self.store.status.update(stav="chyba", chyba=str(err))
                self.log(f"Weather Underground: {err}")
                if self.settings.configured:
                    self.loaded_version = -1  # skúsiť pripojenie znova o 2 minúty
                    self.wake.wait(120)
                    self.wake.clear()
                    continue
            self.store.status["poslednaKontrola"] = int(time.time())
            self.wake.wait(15)
            self.wake.clear()


# --------------------------------------------------------------------------
# Výstupy pre stránku
# --------------------------------------------------------------------------

def _c(x: float) -> str:
    """Číslo s jedným desatinným miestom a čiarkou."""
    return f"{x:.1f}".replace(".", ",")


def _rain_increments(points):
    """Prírastky denného úhrnu medzi po sebe idúcimi meraniami (s nulovaním o polnoci)."""
    out = []
    prev = None
    for ts, v in points:
        tot = v[F_PTOT]
        if tot is None:
            continue
        if prev is not None:
            inc = tot - prev if tot >= prev else tot
            out.append((ts, max(0.0, inc)))
        prev = tot
    return out


def _window_rain(points, start, end):
    incs = [(ts, inc) for ts, inc in _rain_increments(points) if start < ts <= end]
    base = [ts for ts, _ in points if ts <= start and points and points[-1][0] - ts < 7200]
    if not incs:
        return None
    covered_from = max(base) if base else incs[0][0]
    cover = min(1.0, (end - covered_from) / (end - start)) if end > start else 1.0
    return {"mm": round(sum(i for _, i in incs), 2), "pokrytie": round(cover, 2)}


def build_list(store: Store, model_temp, shmu_temp=lambda lat, lon, ts: None) -> list[dict]:
    """model_temp(lat, lon, ts) -> teplota z ALADIN-u alebo None;
    shmu_temp(lat, lon, ts) -> (teplota, km, názov) najbližšej stanice SHMÚ do 30 km alebo None."""
    out = []
    with store.lock:
        items = [(sid, dict(st["meta"]), sorted(st["series"].items()), st["own"], st["offline"])
                 for sid, st in store.stations.items()]
    for sid, meta, points, own, offline in items:
        if not points or meta.get("lat") is None:
            continue
        last_ts, v = points[-1]
        hodnoty = {}
        for name, i in (("t", F_T), ("vlh_rel", F_RH), ("tlak", F_P), ("vie_pr_rych", F_WS), ("vie_max_rych", F_WG),
                        ("vie_pr_smer", F_WD), ("intenzita", F_PRATE), ("zglo", F_SOL), ("rosny", F_DEW)):
            if v[i] is not None:
                hodnoty[name] = v[i]
        temps = [p[F_T] for ts, p in points if p[F_T] is not None and last_ts - ts <= 86400]
        t_hour = None
        for ts, p in points:
            if ts >= last_ts - 3600 and p[F_T] is not None:
                if ts <= last_ts - 2700:
                    t_hour = p[F_T]
                break
        flags = []
        if meta.get("qc") == 0:
            flags.append("Weather Underground označil meranie ako možno chybné.")
        t = hodnoty.get("t")
        model = model_temp(meta["lat"], meta["lon"], last_ts) if t is not None else None
        dev = None if model is None else round(t - model, 1)
        near = shmu_temp(meta["lat"], meta["lon"], last_ts) if t is not None else None
        dev_s = None if near is None else round(t - near[0], 1)
        # Podozrivé je až to, čo nesedí s modelom ani s najbližšou stanicou SHMÚ; pri inverziách
        # sa model mýli často, ale stanica SHMÚ v rovnakom údolí vtedy amatérsku stanicu potvrdí.
        if dev_s is not None:
            if (dev is not None and abs(dev_s) > 5 and abs(dev) > 5) or (dev is None and abs(dev_s) > 7):
                flags.append(f"Teplota sa líši o {_c(abs(dev_s))} °C od stanice SHMÚ {near[2]} ({_c(near[1])} km)"
                             + (f" a o {_c(abs(dev))} °C od modelu ALADIN." if dev is not None else "."))
        elif dev is not None and abs(dev) > 7:
            flags.append(f"Teplota sa od modelu ALADIN líši o {_c(abs(dev))} °C a v okolí 30 km nie je stanica SHMÚ na porovnanie.")
        if t is not None and not (-40 <= t <= 48):
            flags.append("Teplota je mimo reálneho rozsahu.")
        out.append({
            "id": "wu-" + sid,
            "wuId": sid,
            "zdroj": "wu",
            "vlastna": own,
            "nazov": meta.get("nazov") or sid,
            "lat": meta.get("lat"), "lon": meta.get("lon"),
            "vyska": None if meta.get("vyska") is None else round(meta["vyska"]),
            "cas": last_ts,
            "hodnoty": hodnoty,
            "zmenaTeploty1h": None if t_hour is None or t is None else round(t - t_hour, 1),
            "tMin24": min(temps) if temps else None,
            "tMax24": max(temps) if temps else None,
            "zrazky1h": _window_rain(points, last_ts - 3600, last_ts),
            "zrazkyDnes": v[F_PTOT],
            "kvalita": {"wu": meta.get("qc"), "odchylkaOdModelu": dev, "odchylkaOdShmu": dev_s,
                        "shmuStanica": None if near is None else near[2],
                        "shmuVzdialenost": None if near is None else round(near[1], 1), "upozornenia": flags},
            "offline": offline,
            "historiaOd": points[0][0],
        })
    out.sort(key=lambda s: (not s["vlastna"], s["nazov"].lower()))
    return out


def build_series(store: Store, sid: str) -> dict | None:
    with store.lock:
        st = store.stations.get(sid)
        if not st or not st["series"]:
            return None
        meta = dict(st["meta"])
        points = sorted(st["series"].items())
    incs = dict(_rain_increments(points))
    bins: dict[int, list] = {}
    for ts, v in points:
        bins.setdefault(ts // 600 * 600, []).append((ts, v))

    def mean(rows, i):
        vals = [r[i] for _, r in rows if r[i] is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    def peak(rows, i):
        vals = [r[i] for _, r in rows if r[i] is not None]
        return max(vals) if vals else None

    def circ(rows):
        a = [r[F_WD] for _, r in rows if r[F_WD] is not None]
        if not a:
            return None
        s = sum(math.sin(math.radians(x)) for x in a)
        c = sum(math.cos(math.radians(x)) for x in a)
        return None if abs(s) < 1e-9 and abs(c) < 1e-9 else round(math.degrees(math.atan2(s, c)) % 360)

    cols = {k: [] for k in ("cas", "t", "vlh", "tlak", "vietor", "naraz", "smer", "zrazky", "zrazkyMinut", "ziarenie")}
    for k in sorted(bins):
        rows = bins[k]
        rain = [incs[ts] for ts, _ in rows if ts in incs]
        cols["cas"].append(k + 300)
        cols["t"].append(mean(rows, F_T))
        cols["vlh"].append(mean(rows, F_RH))
        cols["tlak"].append(mean(rows, F_P))
        cols["vietor"].append(mean(rows, F_WS))
        cols["naraz"].append(peak(rows, F_WG))
        cols["smer"].append(circ(rows))
        cols["zrazky"].append(round(sum(rain), 2) if rain else None)
        cols["zrazkyMinut"].append(len(rows))
        cols["ziarenie"].append(mean(rows, F_SOL))
    return {"id": "wu-" + sid, "nazov": meta.get("nazov") or sid, "vyska": meta.get("vyska"),
            "lat": meta.get("lat"), "lon": meta.get("lon"), "rady": cols, "zdroj": "wu"}


def build_status(settings: Settings, store: Store, client: Client) -> dict:
    with store.lock:
        others = [st for st in store.stations.values() if not st["own"]]
        n_other = len(others)
        n_active = sum(1 for st in others if st["series"] and not st["offline"])
    return {**store.status, "nastavenia": settings.public(), "volaniaDnes": client.calls_today(),
            "limit": DAILY_LIMIT, "ostatnych": n_other, "aktivnych": n_active}
