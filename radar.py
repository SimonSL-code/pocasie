"""Radarový kompozit SHMÚ (CAPPI 2 km) z formátu ODIM HDF5.

Nie je to všeobecná knižnica HDF5. Číta presne to, čo SHMÚ zverejňuje
v priečinku meteorology/weather/radar/composite/skcomp/cappi2km:

* HDF5 so superblokom verzie 0, hlavičkami objektov verzie 1 a skupinami
  cez tabuľky symbolov (B-strom verzie 1 a lokálna halda),
* atribúty s textom, celými číslami a desatinnými číslami,
* dataset /dataset1/data1/data s 8-bitovými hodnotami, uložený súvisle alebo
  po kúskoch (chunks) a skomprimovaný filtrom deflate,
* projekcia Merkator na guli (+proj=merc ... +ellps=sphere).

Ak by SHMÚ formát zmenil, kód radšej zlyhá s chybou, než aby vrátil zlé čísla.
"""

from __future__ import annotations

import math
import re
import struct
import time
import zlib
from datetime import datetime, timezone

SPHERE_R = 6370997.0   # polomer gule pre "+ellps=sphere" v knižnici PROJ


class FormatError(ValueError):
    pass


# --------------------------------------------------------------------------
# Minimálne HDF5
# --------------------------------------------------------------------------

class _H5:
    def __init__(self, buf: bytes):
        if buf[:8] != b"\x89HDF\r\n\x1a\n":
            raise FormatError("Súbor nie je HDF5.")
        self.b = buf
        sb_ver = buf[8]
        if sb_ver not in (0, 1):
            raise FormatError(f"Nepodporovaná verzia superbloku HDF5 {sb_ver}.")
        self.so, self.sl = buf[13], buf[14]
        pos = 24 + (4 if sb_ver == 1 else 0)
        self.base = self.off(pos)
        pos += 4 * self.so
        # vstup koreňovej skupiny v tabuľke symbolov: názov, adresa hlavičky
        self.root = self.off(pos + self.so)

    def off(self, p: int) -> int:
        return int.from_bytes(self.b[p:p + self.so], "little")

    def length(self, p: int) -> int:
        return int.from_bytes(self.b[p:p + self.sl], "little")

    # hlavička objektu verzie 1: zoznam správ (typ, dáta)
    def messages(self, addr: int) -> list[tuple[int, bytes]]:
        b = self.b
        if b[addr] != 1:
            raise FormatError(f"Nepodporovaná verzia hlavičky objektu {b[addr]}.")
        count = struct.unpack_from("<H", b, addr + 2)[0]
        size = struct.unpack_from("<I", b, addr + 8)[0]
        blocks = [(addr + 16, size)]
        out = []
        while blocks and len(out) < count:
            start, size = blocks.pop(0)
            p, end = start, start + size
            while p + 8 <= end and len(out) < count:
                mtype, msize = struct.unpack_from("<HH", b, p)
                data = b[p + 8:p + 8 + msize]
                out.append((mtype, data))
                if mtype == 0x10:  # pokračovanie hlavičky
                    blocks.append((self.off_in(data, 0), self.len_in(data, self.so)))
                p += 8 + msize
        return out

    def off_in(self, data: bytes, p: int) -> int:
        return int.from_bytes(data[p:p + self.so], "little")

    def len_in(self, data: bytes, p: int) -> int:
        return int.from_bytes(data[p:p + self.sl], "little")

    # skupina: názov → adresa hlavičky
    def children(self, addr: int) -> dict[str, int]:
        for mtype, data in self.messages(addr):
            if mtype == 0x11:
                btree, heap = self.off_in(data, 0), self.off_in(data, self.so)
                return self._group_btree(btree, self._heap_data(heap))
        return {}

    def _heap_data(self, addr: int) -> int:
        if self.b[addr:addr + 4] != b"HEAP":
            raise FormatError("Chýba lokálna halda skupiny.")
        return self.off(addr + 8 + 2 * self.sl)

    def _name(self, heap: int, offset: int) -> str:
        p = heap + offset
        return self.b[p:self.b.index(b"\0", p)].decode("utf-8")

    def _group_btree(self, addr: int, heap: int) -> dict[str, int]:
        b = self.b
        if b[addr:addr + 4] != b"TREE":
            raise FormatError("Chýba B-strom skupiny.")
        level = b[addr + 5]
        used = struct.unpack_from("<H", b, addr + 6)[0]
        p = addr + 8 + 2 * self.so + self.sl  # za susedmi a prvým kľúčom
        out = {}
        for _ in range(used):
            child = self.off(p)
            p += self.so + self.sl
            if level > 0:
                out.update(self._group_btree(child, heap))
            else:
                out.update(self._snod(child, heap))
        return out

    def _snod(self, addr: int, heap: int) -> dict[str, int]:
        b = self.b
        if b[addr:addr + 4] != b"SNOD":
            raise FormatError("Chýba uzol tabuľky symbolov.")
        n = struct.unpack_from("<H", b, addr + 6)[0]
        out, p, entry = {}, addr + 8, 2 * self.so + 24
        for _ in range(n):
            out[self._name(heap, self.off(p))] = self.off(p + self.so)
            p += entry
        return out

    # typ a tvar hodnôt
    @staticmethod
    def _dtype(data: bytes):
        cls, ver = data[0] & 0x0F, data[0] >> 4
        bits = data[1]
        size = struct.unpack_from("<I", data, 4)[0]
        if cls == 0:
            signed = bool(bits & 0x08)
            big = bool(bits & 0x01)
            return ("int", size, signed, big)
        if cls == 1:
            return ("float", size, True, bool(bits & 0x01))
        if cls == 3:
            return ("str", size, False, False)
        raise FormatError(f"Nepodporovaný dátový typ HDF5 (trieda {cls}, verzia {ver}).")

    def _dspace(self, data: bytes) -> tuple:
        ver, ndim, flags = data[0], data[1], data[2]
        if ver == 1:
            p = 8
        elif ver == 2:
            if data[3] == 0:          # skalár
                return ()
            p = 4
        else:
            raise FormatError(f"Nepodporovaná verzia dataspace {ver}.")
        return tuple(self.len_in(data, p + k * self.sl) for k in range(ndim))

    @staticmethod
    def _decode(raw: bytes, dtype, count: int):
        kind, size, signed, big = dtype
        if kind == "str":
            vals = [raw[k * size:(k + 1) * size].split(b"\0", 1)[0].decode("utf-8", "replace") for k in range(count)]
            return vals
        code = {("int", 1): "b", ("int", 2): "h", ("int", 4): "i", ("int", 8): "q", ("float", 4): "f", ("float", 8): "d"}.get((kind, size))
        if code is None:
            raise FormatError(f"Nepodporovaná veľkosť čísla {size} B.")
        if kind == "int" and not signed:
            code = code.upper()
        return list(struct.unpack((">" if big else "<") + code * count, raw[:size * count]))

    def attrs(self, addr: int) -> dict:
        out = {}
        for mtype, data in self.messages(addr):
            if mtype != 0x0C:
                continue
            ver = data[0]
            nlen, tlen, slen = struct.unpack_from("<HHH", data, 2)
            p = 8 if ver == 1 else 9 if ver == 3 else 8
            pad = (lambda n: (n + 7) // 8 * 8) if ver == 1 else (lambda n: n)
            name = data[p:p + nlen].split(b"\0", 1)[0].decode("utf-8")
            p += pad(nlen)
            dtype = self._dtype(data[p:p + tlen])
            p += pad(tlen)
            shape = self._dspace(data[p:p + slen])
            p += pad(slen)
            count = 1
            for d in shape:
                count *= d
            vals = self._decode(data[p:], dtype, count)
            out[name] = vals[0] if not shape or count == 1 else vals
        return out

    def dataset(self, addr: int) -> tuple[tuple, tuple, bytes]:
        """(tvar, typ, surové bajty v poradí riadkov)"""
        shape = dtype = layout = None
        filters = []
        for mtype, data in self.messages(addr):
            if mtype == 0x01:
                shape = self._dspace(data)
            elif mtype == 0x03:
                dtype = self._dtype(data)
            elif mtype == 0x08:
                layout = data
            elif mtype == 0x0B:
                filters = self._filters(data)
        if shape is None or dtype is None or layout is None:
            raise FormatError("Neúplný dataset HDF5.")
        esize = dtype[1]
        total = esize
        for d in shape:
            total *= d
        if layout[0] != 3:
            raise FormatError(f"Nepodporovaná verzia rozloženia dát {layout[0]}.")
        lclass = layout[1]
        if lclass == 0:       # kompaktné
            n = struct.unpack_from("<H", layout, 2)[0]
            return shape, dtype, bytes(layout[4:4 + n])
        if lclass == 1:       # súvislé
            a = self.off_in(layout, 2)
            raw = self.b[a:a + total]
            return shape, dtype, self._unfilter(raw, filters)
        if lclass == 2:       # po kúskoch
            ndim = layout[2] - 1
            btree = self.off_in(layout, 3)
            cdims = struct.unpack_from("<" + "I" * ndim, layout, 3 + self.so)
            out = bytearray(total)
            for offs, chunk in self._chunks(btree, ndim):
                raw = self._unfilter(chunk, filters)
                self._place(out, raw, shape, cdims, offs, esize)
            return shape, dtype, bytes(out)
        raise FormatError(f"Nepodporované rozloženie dát {lclass}.")

    @staticmethod
    def _filters(data: bytes) -> list[int]:
        ver, n = data[0], data[1]
        p = 8 if ver == 1 else 2
        out = []
        for _ in range(n):
            fid = struct.unpack_from("<H", data, p)[0]
            if ver == 1 or fid >= 256:
                nlen = struct.unpack_from("<H", data, p + 2)[0]
                p += 4
            else:
                nlen = 0
                p += 2
            ncv = struct.unpack_from("<H", data, p + 2)[0]
            p += 4
            p += ((nlen + 7) // 8 * 8) if ver == 1 else nlen
            p += 4 * ncv
            if ver == 1 and ncv % 2:
                p += 4
            out.append(fid)
        return out

    @staticmethod
    def _unfilter(raw: bytes, filters: list[int]) -> bytes:
        for fid in reversed(filters):
            if fid == 1:
                raw = zlib.decompress(raw)
            else:
                raise FormatError(f"Nepodporovaný filter HDF5 č. {fid}.")
        return raw

    def _chunks(self, addr: int, ndim: int):
        b = self.b
        if b[addr:addr + 4] != b"TREE" or b[addr + 4] != 1:
            raise FormatError("Chýba B-strom kúskov dát.")
        level = b[addr + 5]
        used = struct.unpack_from("<H", b, addr + 6)[0]
        p = addr + 8 + 2 * self.so
        key = 8 + 8 * (ndim + 1)
        for _ in range(used):
            csize = struct.unpack_from("<I", b, p)[0]
            offs = struct.unpack_from("<" + "Q" * ndim, b, p + 8)
            child = self.off(p + key)
            p += key + self.so
            if level > 0:
                yield from self._chunks(child, ndim)
            else:
                yield offs, b[child:child + csize]

    @staticmethod
    def _place(out: bytearray, raw: bytes, shape, cdims, offs, esize: int) -> None:
        if len(shape) != 2:
            raise FormatError("Očakával sa dvojrozmerný obraz.")
        ny, nx = shape
        ch, cw = cdims
        y0, x0 = offs
        w = min(cw, nx - x0) * esize
        for r in range(min(ch, ny - y0)):
            src = r * cw * esize
            dst = ((y0 + r) * nx + x0) * esize
            out[dst:dst + w] = raw[src:src + w]

    def path(self, *names: str) -> int:
        addr = self.root
        for n in names:
            kids = self.children(addr)
            if n not in kids:
                raise FormatError(f"V súbore chýba skupina {'/'.join(names)}.")
            addr = kids[n]
        return addr


# --------------------------------------------------------------------------
# ODIM kompozit
# --------------------------------------------------------------------------

def _text(v) -> str:
    return v if isinstance(v, str) else str(v)


def read_composite(payload: bytes) -> dict:
    """Prečíta kompozit: čas, geometriu mriežky a 8-bitové hodnoty odrazivosti."""
    h = _H5(payload)
    where = h.attrs(h.path("where"))
    what_ds = h.attrs(h.path("dataset1", "what"))
    what_root = h.attrs(h.path("what"))
    data_addr = h.path("dataset1", "data1")
    # gain, offset a veličina bývajú v dataset1/data1/what, v kompozite SHMÚ sú o úroveň vyššie
    what_data = dict(what_ds)
    if "what" in h.children(data_addr):
        what_data.update(h.attrs(h.path("dataset1", "data1", "what")))
    quantity = _text(what_data.get("quantity", "DBZH"))
    if quantity not in ("DBZH", "TH", "DBZ"):
        raise FormatError(f"Kompozit neobsahuje odrazivosť (veličina {quantity}).")
    shape, dtype, raw = h.dataset(h.path("dataset1", "data1", "data"))
    if dtype[0] != "int" or dtype[1] != 1:
        raise FormatError("Hodnoty kompozitu nie sú 8-bitové.")
    ny, nx = shape
    if (nx, ny) != (int(where["xsize"]), int(where["ysize"])):
        raise FormatError("Rozmery obrazu nesúhlasia s atribútmi where.")
    proj = _text(where.get("projdef", ""))
    params = dict(re.findall(r"\+(\w+)=([^\s]+)", proj))
    if params.get("proj") != "merc":
        raise FormatError(f"Nepodporovaná projekcia kompozitu: {proj}")
    if params.get("ellps", params.get("datum", "")) not in ("sphere",) and "R" not in params:
        raise FormatError(f"Nepodporovaný elipsoid kompozitu: {proj}")
    date = _text(what_ds.get("enddate") or what_ds.get("startdate") or what_root.get("date"))
    tim = _text(what_ds.get("endtime") or what_ds.get("starttime") or what_root.get("time"))
    ts = int(datetime.strptime(date + tim, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp())
    return {
        "cas": ts,
        "nx": nx, "ny": ny,
        "data": raw,
        "gain": float(what_data.get("gain", 0.5)),
        "offset": float(what_data.get("offset", -32.0)),
        "nodata": float(what_data.get("nodata", 255)),
        "undetect": float(what_data.get("undetect", 0)),
        "lon0": float(params.get("lon_0", 0)),
        "lat_ts": float(params.get("lat_ts", 0)),
        "R": float(params.get("R", SPHERE_R)),
        "xscale": float(where["xscale"]), "yscale": float(where["yscale"]),
        "UL": (float(where["UL_lon"]), float(where["UL_lat"])),
        "LR": (float(where["LR_lon"]), float(where["LR_lat"])),
    }


class Merc:
    """Merkator na guli so štandardnou rovnobežkou lat_ts (ako PROJ)."""

    def __init__(self, lon0: float, lat_ts: float, R: float):
        self.lon0 = math.radians(lon0)
        self.k = R * math.cos(math.radians(lat_ts))

    def fwd(self, lon: float, lat: float) -> tuple[float, float]:
        return (self.k * (math.radians(lon) - self.lon0),
                self.k * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)))


def crop(comp: dict, lon0: float, lon1: float, lat0: float, lat1: float, factor: int = 1) -> dict:
    """Výrez kompozitu, ktorý pokrýva zadaný obdĺžnik, s geometriou pre prehliadač.

    Vráti mriežku: ľavý horný roh (x0, y0) v metroch Merkatora, krok dx, dy
    a hodnoty v poradí riadkov zhora nadol. factor > 1 mriežku zmenší.
    """
    m = Merc(comp["lon0"], comp["lat_ts"], comp["R"])
    ulx, uly = m.fwd(*comp["UL"])
    lrx, lry = m.fwd(*comp["LR"])
    nx, ny = comp["nx"], comp["ny"]
    # Krok mriežky z rohov obrazu. Atribút yscale v súboroch SHMÚ nesedí (je to šírka
    # obrazu delená počtom riadkov), rohy a xscale však súhlasia a pixely sú štvorcové.
    dx, dy = (lrx - ulx) / nx, (uly - lry) / ny
    if abs(dx - comp["xscale"]) > 0.02 * comp["xscale"] or abs(dy - dx) > 0.03 * dx:
        raise FormatError("Geometria kompozitu nesúhlasí s rohmi obrazu.")
    xs = [m.fwd(lo, la)[0] for lo in (lon0, lon1) for la in (lat0, lat1)]
    ys = [m.fwd(lo, la)[1] for lo in (lon0, lon1) for la in (lat0, lat1)]
    i0 = max(0, int((min(xs) - ulx) // dx) - 1)
    i1 = min(nx, int((max(xs) - ulx) // dx) + 2)
    j0 = max(0, int((uly - max(ys)) // dy) - 1)
    j1 = min(ny, int((uly - min(ys)) // dy) + 2)
    if i1 <= i0 or j1 <= j0:
        raise FormatError("Kompozit nepokrýva Slovensko.")
    raw = comp["data"]
    if factor > 1:
        # zmenšenie: z každého štvorca factor × factor bodov najsilnejší odraz (255 = mimo dosahu sa neráta)
        j1 = j0 + (j1 - j0) // factor * factor
        i1 = i0 + (i1 - i0) // factor * factor
        out = bytearray()
        for j in range(j0, j1, factor):
            block = [raw[(j + q) * nx + i0:(j + q) * nx + i1] for q in range(factor)]
            for i in range(0, i1 - i0, factor):
                vals = [v for row in block for v in row[i:i + factor] if v != 255]
                out.append(max(vals) if vals else 255)
        rows = bytes(out)
    else:
        rows = b"".join(raw[j * nx + i0:j * nx + i1] for j in range(j0, j1))
    return {
        "nx": (i1 - i0) // factor, "ny": (j1 - j0) // factor, "x0": round(ulx + i0 * dx, 2), "y0": round(uly - j0 * dy, 2),
        "dx": round(dx * factor, 4), "dy": round(dy * factor, 4),
        "lon0": comp["lon0"], "latTs": comp["lat_ts"], "R": comp["R"],
        "gain": comp["gain"], "offset": comp["offset"], "nodata": comp["nodata"], "undetect": comp["undetect"],
        "data": rows,
    }


# --------------------------------------------------------------------------
# Snímky pre stránku: posledné 2 hodiny, výrez nad Slovenskom
# --------------------------------------------------------------------------

RADAR_PATH = "/meteorology/weather/radar/composite/skcomp/cappi2km"
FILE_RE = re.compile(r"^T_PANV22_C_LZIB_(\d{14})\.hdf$")
DAY_RE = re.compile(r"^\d{8}/$")
BBOX = (16.75, 22.65, 47.66, 49.66)   # rovnaký výrez ako mapa na stránke (lon0, lon1, lat0, lat1)
KEEP_SECONDS = 2 * 3600
FACTOR = 2                            # mriežka asi 660 m: na mape stačí a snímky sú 4× menšie
GEOM_KEYS = ("nx", "ny", "x0", "y0", "dx", "dy", "lon0", "latTs", "R", "gain", "offset", "nodata", "undetect")


def _load_meta(folder) -> dict:
    import json
    try:
        return json.loads((folder / "radar.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def update(shmu, folder, log=print) -> dict:
    """Stiahne chýbajúce snímky radaru do priečinka (súbory .bin = zlib, 1 bajt na bod) a zapíše radar.json.

    Vráti obsah radar.json so stavom. Každá snímka má asi 2 až 60 kB podľa toho, koľko prší.
    """
    import json
    folder.mkdir(parents=True, exist_ok=True)
    meta = _load_meta(folder)
    status = {"stav": "ok", "chyba": None, "poslednaKontrola": int(time.time())}
    same = meta.get("faktor") == FACTOR and meta.get("vyrez") == list(BBOX)
    frames = {f["cas"]: f for f in meta.get("snimky", []) if same and (folder / f["subor"]).exists()}
    geom = meta.get("mriezka") if same else None
    try:
        days = sorted((e for e in shmu.listing(RADAR_PATH) if DAY_RE.match(e)), reverse=True)[:2]
        avail = {}
        for day in days:
            day_path = f"{RADAR_PATH}/{day.rstrip('/')}"
            for name in shmu.listing(day_path):
                m = FILE_RE.match(name)
                if m:
                    ts = int(datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp())
                    avail[ts] = f"{day_path}/{name}"
        if not avail:
            raise ConnectionError("Na serveri SHMÚ sa nenašli snímky radaru.")
        newest = max(avail)
        wanted = sorted(ts for ts in avail if ts >= newest - KEEP_SECONDS)
        for ts in reversed(wanted):
            if ts in frames:
                continue
            try:
                comp = read_composite(shmu.get(avail[ts]))
                part = crop(comp, *BBOX, factor=FACTOR)
            except Exception as err:
                log(f"Radar: preskakujem {avail[ts].rpartition('/')[2]}: {err}")
                continue
            g = {k: part[k] for k in GEOM_KEYS}
            if geom != g:
                if geom:
                    log("Radar: zmenila sa geometria kompozitu, staršie snímky zahadzujem.")
                geom, frames = g, {}
            name = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y%m%d%H%M") + ".bin"
            (folder / name).write_bytes(zlib.compress(part["data"], 9))
            frames[ts] = {"cas": ts, "subor": name}
        keep = {ts: f for ts, f in frames.items() if ts >= newest - KEEP_SECONDS}
        frames = keep
    except Exception as err:
        status.update(stav="chyba", chyba=str(err))
        log(f"Radar: {err}")
    used = {f["subor"] for f in frames.values()}
    for p in folder.glob("*.bin"):
        if p.name not in used:
            try:
                p.unlink()
            except OSError:
                pass
    meta = {"produkt": "CAPPI 2 km, kompozit SHMÚ", "faktor": FACTOR, "vyrez": list(BBOX), "mriezka": geom,
            "snimky": [frames[ts] for ts in sorted(frames)], "stav": status}
    (folder / "radar.json").write_text(json.dumps(meta, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return meta
