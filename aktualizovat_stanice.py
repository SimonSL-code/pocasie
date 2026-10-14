#!/usr/bin/env python3
"""Doplní názvy a polohy staníc do stanice.json z uloženej stránky SHMÚ.

Postup:
  1. V prehliadači otvorte https://www.shmu.sk/sk/?page=838&uhrny=24
  2. Uložte ju (Ctrl+S / Cmd+S) ako „Webová stránka, iba HTML“.
  3. Spustite:  python aktualizovat_stanice.py "cesta/k/ulozenej-strane.html"

Stránka obsahuje mapu so zoznamom automatických meteorologických staníc
(názov, index a súradnice). Skript pridá stanice, ktoré v stanice.json
chýbajú, a doplní chýbajúce súradnice. Názvy a výšky, ktoré už v súbore sú,
nemení (ak ich chcete prepísať, pridajte voľbu --prepisat-nazvy).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATIONS_FILE = HERE / "stanice.json"

OBJECT_RE = re.compile(r"\{([^{}]*uid\s*:\s*'AWS2\d+'[^{}]*)\}")
STRING_FIELD_RE = re.compile(r"(\w+)\s*:\s*'((?:[^'\\]|\\.)*)'")
NUMBER_FIELD_RE = re.compile(r"(\w+)\s*:\s*(-?\d+(?:\.\d+)?)\b")


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8", "cp1250"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def unescape_js(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)


def parse_stations(html: str) -> dict[int, dict]:
    """Vráti {index: {nazov, lat, lon}} pre automatické stanice (AWS2…)."""
    found: dict[int, dict] = {}
    for body in OBJECT_RE.findall(html):
        strings = {k: unescape_js(v) for k, v in STRING_FIELD_RE.findall(body)}
        numbers = {k: float(v) for k, v in NUMBER_FIELD_RE.findall(body)}
        uid = strings.get("uid", "")
        try:
            ind = int(uid[4:])
        except ValueError:
            continue
        label = strings.get("nazov", "").strip()
        # názov má tvar "11803 - Trenčín"; oddeľuje sa len prvé " - "
        prefix = f"{ind} - "
        name = label[len(prefix):].strip() if label.startswith(prefix) else label
        lat, lon = numbers.get("lat"), numbers.get("lon")
        if not name or lat is None or lon is None:
            continue
        if not (47.0 < lat < 50.5 and 16.0 < lon < 23.5):
            continue  # mimo Slovenska, zjavne chybný záznam
        found[ind] = {"nazov": name, "lat": round(lat, 5), "lon": round(lon, 5)}
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description="Doplní stanice do stanice.json z uloženej stránky SHMÚ.")
    parser.add_argument("stranka", type=Path, help="uložená stránka shmu.sk (HTML)")
    parser.add_argument("--prepisat-nazvy", action="store_true", help="prepísať aj názvy staníc, ktoré už v súbore sú")
    args = parser.parse_args()

    if not args.stranka.is_file():
        sys.exit(f"Súbor {args.stranka} neexistuje.")
    found = parse_stations(read_text(args.stranka))
    if not found:
        sys.exit("V súbore sa nenašiel zoznam automatických staníc. Uložili ste stránku "
                 "https://www.shmu.sk/sk/?page=838&uhrny=24 ako „Webová stránka, iba HTML“?")

    current = json.loads(STATIONS_FILE.read_text(encoding="utf-8")) if STATIONS_FILE.exists() else {}
    added = completed = renamed = 0
    for ind, info in sorted(found.items()):
        key = str(ind)
        entry = current.get(key)
        if entry is None:
            current[key] = dict(info)
            added += 1
            continue
        if entry.get("lat") is None or entry.get("lon") is None:
            entry["lat"], entry["lon"] = info["lat"], info["lon"]
            completed += 1
        if args.prepisat_nazvy and entry.get("nazov") != info["nazov"]:
            entry["nazov"] = info["nazov"]
            renamed += 1

    note = {k: v for k, v in current.items() if not k.isdigit()}
    stations = {k: current[k] for k in sorted((k for k in current if k.isdigit()), key=int)}
    STATIONS_FILE.write_text(json.dumps({**note, **stations}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Na stránke: {len(found)} staníc. Pridané: {added}, doplnené súradnice: {completed}"
          + (f", premenované: {renamed}" if args.prepisat_nazvy else "") + f". Spolu v stanice.json: {len(stations)}.")


if __name__ == "__main__":
    main()
