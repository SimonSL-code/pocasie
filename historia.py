"""Súhrny meraní pre grafy na niekoľko dní a denné maximá, minimá a úhrny.

Spoločné pre server.py (lokálna stránka) aj generuj.py (GitHub Pages).
Súhrn (zoznam) opisuje jeden časový interval, 10-minútový alebo hodinový:

    t, vlh, tlak, vietor, náraz max, smer, zrážky súčet, zrážky minút,
    žiarenie, t min, t max, minút spolu

Dni sú v miestnom čase (SEČ/SELČ, Európa/Bratislava), od polnoci do polnoci.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

# Čas merania ("minuta") je v SEČ, teda pevne UTC+1 aj v lete.
SEC = timezone(timedelta(hours=1))


def parse_minuta(value) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SEC)
    return int(dt.timestamp())


# Polia, ktoré sa ukladajú z minútových meraní SHMÚ (poradie v n-tici hodnôt).
SERIES_FIELDS = (
    "t",             # teplota vzduchu 2 m [°C]
    "vlh_rel",       # relatívna vlhkosť [%]
    "tlak",          # tlak vzduchu v mieste stanice [hPa]
    "vie_pr_rych",   # priemerná rýchlosť vetra [m/s]
    "vie_max_rych",  # maximálna rýchlosť vetra (náraz) [m/s]
    "vie_pr_smer",   # priemerný smer vetra [°]
    "zra_uhrn",      # úhrn zrážok za minútu [mm]
    "dohl",          # dohľadnosť [m]
    "sneh_pokr",     # výška snehovej pokrývky [cm]
    "stav_poc",      # stav počasia, kód WMO 4680
    "zglo",          # globálne žiarenie [W/m²]
    "tprz",          # prízemná teplota [°C]
)
IDX = {name: i for i, name in enumerate(SERIES_FIELDS)}
I_T, I_RH, I_P, I_WS, I_WG, I_WD, I_RAIN, I_SOL = (IDX[k] for k in (
    "t", "vlh_rel", "tlak", "vie_pr_rych", "vie_max_rych", "vie_pr_smer", "zra_uhrn", "zglo"))
B_T, B_RH, B_P, B_WS, B_WG, B_WD, B_RAIN, B_RAIN_N, B_SOL, B_TMIN, B_TMAX, B_N = range(12)
SERIES_KEYS = ("cas", "t", "vlh", "tlak", "vietor", "naraz", "smer", "zrazky", "zrazkyMinut", "ziarenie", "tMin", "tMax")


# --------------------------------------------------------------------------
# Miestny čas bez knižnice tzdata (na Windows nemusí byť nainštalovaná)
# --------------------------------------------------------------------------

def _last_sunday(year: int, month: int) -> int:
    """Posledná nedeľa v mesiaci o 01:00 UTC (začiatok/koniec letného času v EÚ)."""
    nxt = datetime(year + (month == 12), month % 12 + 1, 1, 1, tzinfo=timezone.utc)
    ts = int(nxt.timestamp()) - 86400
    wd = datetime.fromtimestamp(ts, timezone.utc).weekday()  # pondelok = 0
    return ts - ((wd - 6) % 7) * 86400


def local_offset(ts: float) -> int:
    year = datetime.fromtimestamp(ts, timezone.utc).year
    return 7200 if _last_sunday(year, 3) <= ts < _last_sunday(year, 10) else 3600


def day_start(ts: float) -> int:
    """Začiatok miestneho dňa (polnoc), do ktorého patrí čas ts."""
    naive = int(ts + local_offset(ts)) // 86400 * 86400
    for off in (local_offset(naive - 7200), 3600, 7200):
        if local_offset(naive - off) == off:
            return naive - off
    return naive - 3600


def day_label(start: int) -> str:
    return datetime.fromtimestamp(start + local_offset(start) + 3600, timezone.utc).date().isoformat()


# --------------------------------------------------------------------------
# Súhrny
# --------------------------------------------------------------------------

def circular_mean(angles: list[float], weights: list[float] | None = None):
    if not angles:
        return None
    w = weights or [1.0] * len(angles)
    s = sum(math.sin(math.radians(a)) * k for a, k in zip(angles, w))
    c = sum(math.cos(math.radians(a)) * k for a, k in zip(angles, w))
    if abs(s) < 1e-9 and abs(c) < 1e-9:
        return None
    return round(math.degrees(math.atan2(s, c)) % 360)


def summarize(rows: list[tuple]) -> list:
    """Súhrn minútových meraní (n-tice v poradí SERIES_FIELDS)."""
    def vals(i):
        return [r[i] for r in rows if r[i] is not None]

    def mean(i):
        v = vals(i)
        return round(sum(v) / len(v), 2) if v else None

    t, g, rain = vals(I_T), vals(I_WG), vals(I_RAIN)
    return [mean(I_T), mean(I_RH), mean(I_P), mean(I_WS), max(g) if g else None,
            circular_mean(vals(I_WD)), round(sum(rain), 2) if rain else None, len(rain),
            mean(I_SOL), min(t) if t else None, max(t) if t else None, len(rows)]


def combine(parts: list[list]) -> list:
    """Spojí súhrny kratších intervalov do jedného (priemery vážené počtom minút)."""
    parts = [p for p in parts if p]
    if len(parts) == 1:
        return list(parts[0])

    def wmean(i):
        num = den = 0.0
        for p in parts:
            if p[i] is not None:
                num += p[i] * p[B_N]
                den += p[B_N]
        return round(num / den, 2) if den else None

    def pick(i, fn):
        v = [p[i] for p in parts if p[i] is not None]
        return fn(v) if v else None

    rain = [p[B_RAIN] for p in parts if p[B_RAIN] is not None]
    dirs = [(p[B_WD], p[B_N]) for p in parts if p[B_WD] is not None]
    return [wmean(B_T), wmean(B_RH), wmean(B_P), wmean(B_WS), pick(B_WG, max),
            circular_mean([d for d, _ in dirs], [n for _, n in dirs]),
            round(sum(rain), 2) if rain else None, sum(p[B_RAIN_N] for p in parts),
            wmean(B_SOL), pick(B_TMIN, min), pick(B_TMAX, max), sum(p[B_N] for p in parts)]


def roll_into_hours(bins: dict[int, list], hours: dict[int, list], before: int) -> None:
    """Presunie súhrny staršie než `before` do hodinových súhrnov (a zo `bins` ich zmaže)."""
    groups: dict[int, list] = {}
    for ts in [t for t in bins if t < before]:
        groups.setdefault(ts // 3600 * 3600, []).append(bins.pop(ts))
    for h, parts in groups.items():
        if h in hours:
            parts.append(hours[h])
        hours[h] = combine(parts)


def series_columns(items: list[tuple[int, list]], step: int) -> dict:
    """Stĺpce pre graf zo súhrnov [(začiatok intervalu, súhrn)]; čas = stred intervalu."""
    cols = {k: [] for k in SERIES_KEYS}
    for ts, b in items:
        cols["cas"].append(ts + step // 2)
        cols["t"].append(b[B_T])
        cols["vlh"].append(b[B_RH])
        cols["tlak"].append(b[B_P])
        cols["vietor"].append(b[B_WS])
        cols["naraz"].append(b[B_WG])
        cols["smer"].append(b[B_WD])
        cols["zrazky"].append(b[B_RAIN] if b[B_RAIN_N] else None)
        cols["zrazkyMinut"].append(b[B_RAIN_N])
        cols["ziarenie"].append(b[B_SOL])
        cols["tMin"].append(b[B_TMIN])
        cols["tMax"].append(b[B_TMAX])
    return cols


def daily(items: list[tuple[int, list, int]], until: int | None = None) -> list[dict]:
    """Denné súhrny z intervalov [(začiatok, súhrn, dĺžka v s)], od najstaršieho dňa.

    pokrytie = podiel minút dňa (pri dnešku len doteraz uplynulých, do `until`),
    z ktorých máme merania; ovplyvňuje spoľahlivosť maxima, minima a úhrnu zrážok.
    """
    days: dict[int, dict] = {}
    for ts, b, _step in items:
        d = days.setdefault(day_start(ts), {"tMin": None, "tMax": None, "zrazky": 0.0, "zrazkyMinut": 0, "minut": 0,
                                             "naraz": None, "tMinCas": None, "tMaxCas": None})
        if b[B_TMIN] is not None and (d["tMin"] is None or b[B_TMIN] < d["tMin"]):
            d["tMin"], d["tMinCas"] = b[B_TMIN], ts
        if b[B_TMAX] is not None and (d["tMax"] is None or b[B_TMAX] > d["tMax"]):
            d["tMax"], d["tMaxCas"] = b[B_TMAX], ts
        if b[B_WG] is not None and (d["naraz"] is None or b[B_WG] > d["naraz"]):
            d["naraz"] = b[B_WG]
        d["zrazky"] += b[B_RAIN] or 0
        d["zrazkyMinut"] += b[B_RAIN_N]
        d["minut"] += b[B_N]
    out = []
    for start in sorted(days):
        d = days[start]
        length = day_start(start + 30 * 3600) - start  # 23, 24 alebo 25 hodín
        if until is not None:
            length = max(600, min(length, until - start))
        out.append({
            "den": day_label(start), "zaciatok": start,
            "tMin": d["tMin"], "tMax": d["tMax"], "tMinCas": d["tMinCas"], "tMaxCas": d["tMaxCas"],
            "naraz": d["naraz"],
            "zrazky": round(d["zrazky"], 1) if d["zrazkyMinut"] else None,
            "pokrytie": round(min(1, d["minut"] / (length / 60)), 2),
            "pokrytieZrazok": round(min(1, d["zrazkyMinut"] / (length / 60)), 2),
        })
    return out
