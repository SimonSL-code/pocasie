#!/usr/bin/env python3
"""Automatické zrážkomerné stanice SHMÚ (meteorology/precipitation/now).

SHMÚ každých 5 minút zverejní súbor s minútovými úhrnmi zrážok (a teplotou)
asi zo 180 zrážkomerných staníc. Stanice sú označené len číslom (ind_zra);
názvy a súradnice sú v súbore zrazkomery.json, ktorý vytvorí tento skript
zo služby WFS SHMÚ (sieť zrážkomerných staníc, INSPIRE):

    python zrazkomery.py          # obnoví zrazkomery.json

Úhrny sa počítajú presne po minútach: každý 10-minútový interval si pamätá,
ktoré minúty už má, takže sa nič nezapočíta dvakrát, ani keď sa súbory
prekrývajú alebo prídu neskôr. Staršie intervaly sa spájajú do hodín.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

import historia as hist

HERE = Path(__file__).resolve().parent
CATALOG_FILE = HERE / "zrazkomery.json"
PRECIP_PATH = "/meteorology/precipitation/now/data"
FILE_RE = re.compile(r"^aps1min - \d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2}\.json$")
DAY_RE = re.compile(r"^\d{8}/$")
WFS_URL = ("https://geo.shmu.sk/geoserver/ef-swn-precs/wfs?SERVICE=WFS&REQUEST=GetFeature&VERSION=2.0.0"
           "&TYPENAMES=ef-swn-precs:EnvironmentalMonitoringFacility&outputFormat=application/json")

KEEP_BINS = 26 * 3600      # 10-minútové intervaly s minútami
KEEP_DAYS = 8              # hodinové úhrny
FULL_MASK = (1 << 10) - 1


def load_catalog(path: Path = CATALOG_FILE) -> dict[int, dict]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {int(k): v for k, v in raw.items() if k.isdigit() and isinstance(v, dict)}


class Gauges:
    """Úhrny zrážok zrážkomerných staníc (stav sa dá uložiť do JSON)."""

    def __init__(self, state: dict | None = None):
        state = state or {}
        # stanica -> začiatok 10-min intervalu -> [úhrn mm, bitová maska minút s meraním]
        self.bins: dict[int, dict[int, list]] = {
            int(s): {int(t): v for t, v in b.items()} for s, b in state.get("kosiky", {}).items()}
        # stanica -> začiatok hodiny -> [úhrn mm, počet minút s meraním]
        self.hours: dict[int, dict[int, list]] = {
            int(s): {int(t): v for t, v in b.items()} for s, b in state.get("hodiny", {}).items()}
        # stanica -> [čas posledného merania, teplota]
        self.last: dict[int, list] = {int(s): v for s, v in state.get("posledne", {}).items()}
        self.processed: set[str] = set(state.get("spracovane", []))
        self.older: set[str] = set(state.get("starsie", []))
        self.older_days: set[str] = set(state.get("starsieDni", []))

    def ingest(self, records: list[dict]) -> None:
        for rec in records:
            try:
                sid = int(rec.get("ind_zra"))
            except (TypeError, ValueError):
                continue
            ts = hist_parse(rec.get("minuta"))
            if ts is None:
                continue
            rain = _num(rec.get("zra_uhrn"))
            if rain is not None:
                start = ts // 600 * 600
                bit = 1 << ((ts - start) // 60)
                b = self.bins.setdefault(sid, {}).setdefault(start, [0.0, 0])
                if not b[1] & bit:
                    b[0] = round(b[0] + max(0.0, rain), 2)
                    b[1] |= bit
            prev = self.last.get(sid)
            if prev is None or ts >= prev[0]:
                t = _num(rec.get("t"))
                self.last[sid] = [ts, t if t is not None else (prev[1] if prev and ts - prev[0] < 3600 else None)]

    def finish(self) -> None:
        newest = max((v[0] for v in self.last.values()), default=0)
        if not newest:
            return
        for sid in list(self.bins):
            b, h = self.bins[sid], self.hours.setdefault(sid, {})
            for ts in [t for t in b if t < newest - KEEP_BINS]:
                mm, mask = b.pop(ts)
                hb = h.setdefault(ts // 3600 * 3600, [0.0, 0])
                hb[0] = round(hb[0] + mm, 2)
                hb[1] += bin(mask).count("1")
            if not b:
                del self.bins[sid]
        for sid in list(self.hours):
            h = self.hours[sid]
            for ts in [t for t in h if t < newest - KEEP_DAYS * 86400]:
                del h[ts]
            if not h:
                del self.hours[sid]
        for sid in [s for s, v in self.last.items() if v[0] < newest - KEEP_DAYS * 86400]:
            del self.last[sid]

    def to_state(self, recent_files: list[str]) -> dict:
        keep = set(recent_files)
        oldest = hist.day_label(time.time() - (KEEP_DAYS + 1) * 86400).replace("-", "")
        return {
            "kosiky": {str(s): {str(t): v for t, v in sorted(b.items())} for s, b in self.bins.items()},
            "hodiny": {str(s): {str(t): v for t, v in sorted(b.items())} for s, b in self.hours.items()},
            "posledne": {str(s): v for s, v in self.last.items()},
            "spracovane": sorted(p for p in self.processed if p in keep),
            "starsie": sorted(p for p in self.older if p.rsplit("/", 2)[-2] >= oldest),
            "starsieDni": sorted(d for d in self.older_days if d >= oldest),
        }

    def _window(self, sid: int, start: int, end: int) -> tuple[float, int]:
        """Úhrn a počet minút s meraním v intervale [start, end)."""
        mm, n = 0.0, 0
        for ts, (v, mask) in self.bins.get(sid, {}).items():
            if start <= ts < end:
                mm += v
                n += bin(mask).count("1")
        for ts, (v, cnt) in self.hours.get(sid, {}).items():
            if start <= ts < end:
                mm += v
                n += cnt
        return mm, n

    def build(self, catalog: dict[int, dict]) -> tuple[list[dict], int]:
        newest = max((v[0] for v in self.last.values()), default=0)
        out = []
        if not newest:
            return out, 0
        today0 = hist.day_start(newest)
        day_starts = []
        d = today0
        for _ in range(KEEP_DAYS):
            day_starts.append(d)
            d = hist.day_start(d - 3600)
        for sid in sorted(self.last):
            last_ts, t = self.last[sid]
            end = last_ts // 600 * 600 + 600

            def total(span, minutes):
                mm, n = self._window(sid, end - span, end)
                return {"mm": round(mm, 1), "pokrytie": round(min(1, n / minutes), 2)} if n else None

            days = []
            for start in reversed(day_starts):
                stop = hist.day_start(start + 30 * 3600) if start != today0 else end
                mm, n = self._window(sid, start, stop)
                if n:
                    days.append({"den": hist.day_label(start), "mm": round(mm, 1),
                                 "pokrytie": round(min(1, n / max(1, (stop - start) / 60)), 2)})
            last_bin = self.bins.get(sid, {}).get(end - 600)
            meta = catalog.get(sid, {})
            out.append({
                "id": sid, "nazov": meta.get("nazov"), "lat": meta.get("lat"), "lon": meta.get("lon"),
                "cas": last_ts, "t": t,
                # intenzita z posledných 10 minút v mm/h
                "intenzita": round(last_bin[0] * 60 / max(1, bin(last_bin[1]).count("1")), 1) if last_bin else None,
                "zrazky1h": total(3600, 60), "zrazky3h": total(3 * 3600, 180), "zrazky24h": total(86400, 1440),
                "dni": days,
            })
        return out, newest


hist_parse = hist.parse_minuta   # čas merania je v SEČ, rovnako ako pri klimatologických staniciach


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def recent_files(shmu) -> list[str]:
    """Cesty k súborom z dvoch najnovších denných priečinkov, od najstaršieho."""
    days = sorted((e for e in shmu.listing(PRECIP_PATH) if DAY_RE.match(e)), reverse=True)
    files = []
    for day in days[:2]:
        folder = f"{PRECIP_PATH}/{day.rstrip('/')}"
        files.extend((n, f"{folder}/{n}") for n in shmu.listing(folder) if FILE_RE.match(n))
    files.sort()
    return [p for _, p in files]


def snapshot(shmu, path: str) -> list[dict]:
    folder, _, name = path.rpartition("/")
    doc = json.loads(shmu.get(f"{folder}/{quote(name)}"))
    records = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(records, list):
        raise ValueError(f"Neočakávaný formát súboru {name}")
    return [r for r in records if isinstance(r, dict)]


def update(shmu, gauges: Gauges, max_files: int = 72, older: int = 0, log=print) -> tuple[list[str], dict]:
    """Stiahne nové súbory (najnovší ako prvý); vynechané staršie z posledných 24 h dopĺňa postupne."""
    status = {"stav": "ok", "chyba": None, "poslednaKontrola": int(time.time())}
    try:
        files = recent_files(shmu)
    except Exception as err:
        status.update(stav="chyba", chyba=str(err))
        log(f"Zrážkomery: {err}")
        return sorted(gauges.processed), status
    plan = [p for p in files[-288:] if p not in gauges.processed][::-1][:max_files]
    if plan:
        log(f"Zrážkomery: {len(plan)} nových súborov.")
    for path in plan:
        try:
            gauges.ingest(snapshot(shmu, path))
        except Exception as err:
            log(f"Zrážkomery: preskakujem {path.rpartition('/')[2]}: {err}")
        gauges.processed.add(path)
    if older and len(plan) < max_files // 2:
        try:
            _backfill(shmu, gauges, files, older, log)
        except Exception as err:
            log(f"Zrážkomery: história dní: {err}")
    gauges.finish()
    missing = sum(1 for p in files[-288:] if p not in gauges.processed)
    status["historia"] = {"chyba": missing, "spolu": len(files[-288:])}
    return files, status


def _backfill(shmu, gauges: Gauges, files: list[str], limit: int, log) -> None:
    """Doplní staršie dni (všetky súbory, úhrny musia byť úplné), najviac `limit` súborov za beh."""
    oldest = hist.day_label(time.time() - (KEEP_DAYS + 1) * 86400).replace("-", "")
    plan = [p for p in files[:-288] if p not in gauges.older][::-1]
    if len(plan) < limit:
        recent_days = {p.rsplit("/", 2)[-2] for p in files}
        days = sorted((e.rstrip("/") for e in shmu.listing(PRECIP_PATH) if DAY_RE.match(e)), reverse=True)
        for day in days:
            if day < oldest or len(plan) >= limit:
                break
            if day in recent_days or day in gauges.older_days:
                continue
            folder = f"{PRECIP_PATH}/{day}"
            todo = sorted((f"{folder}/{n}" for n in shmu.listing(folder) if FILE_RE.match(n)), reverse=True)
            todo = [p for p in todo if p not in gauges.older]
            if not todo:
                gauges.older_days.add(day)
            plan.extend(todo)
    plan = plan[:limit]
    if plan:
        log(f"Zrážkomery: dopĺňam históriu dní, {len(plan)} starších súborov.")
    for path in plan:
        try:
            gauges.ingest(snapshot(shmu, path))
        except Exception as err:
            log(f"Zrážkomery: preskakujem {path.rpartition('/')[2]}: {err}")
        gauges.older.add(path)


# --------------------------------------------------------------------------
# Zoznam staníc zo služby WFS SHMÚ
# --------------------------------------------------------------------------

def fetch_catalog() -> dict[str, dict]:
    from urllib.request import Request, urlopen

    import server as srv
    with urlopen(Request(WFS_URL, headers={"User-Agent": srv.USER_AGENT}), timeout=120,
                 context=srv.make_ssl_context()) as resp:
        doc = json.loads(resp.read())
    out = {}
    for f in doc.get("features", []):
        props, geom = f.get("properties") or {}, f.get("geometry") or {}
        lid = ((props.get("inspireId") or {}).get("localId") or "").rpartition("/")[2]
        m = re.match(r"SK(\d+)(?:\.([A-Z]+))?", lid)
        coords = geom.get("coordinates") or []
        if not m or len(coords) < 2:
            continue
        out[str(int(m.group(1)))] = {"nazov": (props.get("name") or "").strip() or None, "typ": m.group(2) or None,
                                     "lat": round(float(coords[1]), 5), "lon": round(float(coords[0]), 5)}
    return dict(sorted(out.items(), key=lambda kv: int(kv[0])))


def main() -> None:
    cat = fetch_catalog()
    if not cat:
        sys.exit("Služba WFS nevrátila žiadne stanice, súbor nemením.")
    text = "{\n" + ",\n".join(f'  "{k}": {json.dumps(v, ensure_ascii=False)}' for k, v in cat.items()) + "\n}\n"
    CATALOG_FILE.write_text(text, encoding="utf-8")
    print(f"Uložených {len(cat)} staníc do {CATALOG_FILE.name}.")


if __name__ == "__main__":
    main()
