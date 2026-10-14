"""Čítanie predpovede modelu ALADIN (SHMÚ, rozlíšenie 4,5 km) z formátu GRIB2.

Nie je to všeobecná knižnica GRIB2. Číta presne to, čo SHMÚ zverejňuje
v priečinku meteorology/weather/nwp/aladin/sk/4.5km:

* GRIB edícia 2, hodnoty zbalené jednoduchým balením (šablóna 5.0) alebo ako
  IEEE čísla (šablóna 5.4, len orografia v hodine 000),
* bitová maska (sekcia 6), ktorá z obdĺžnikovej mriežky 94 × 48 bodov
  vyberá približne 2 479 bodov nad Slovenskom,
* Lambertova kužeľová projekcia so stálou geometriou (nižšie).

Ak by SHMÚ formát zmenil, kód radšej zlyhá s chybou, než aby vrátil zlé čísla.

Geometria mriežky a čísla parametrov vychádzajú z projektu vaind/ha-shmu
(licencia MIT), kde boli overené na živých dátach; tu sú overené aj nezávisle
knižnicou ecCodes.
"""

from __future__ import annotations

import math
import struct
from array import array

# --- Pevná mriežka ALADIN/SHMÚ 4,5 km ----------------------------------------
EARTH_R = 6371229.0
PHI0 = math.radians(46.2447)   # štandardná rovnobežka (dotykový kužeľ)
LON0 = math.radians(17.0)      # stredný poludník
LA1, LO1 = 47.74175, 16.849607  # prvý bod mriežky (juhozápadný roh)
DX = DY = 4500.0
NX, NY = 94, 48

# Kľúč parametra: (disciplína, kategória, číslo, typ hladiny, hladina)
T2M = (0, 0, 0, 103, 2.0)          # teplota 2 m [K]
U10 = (0, 2, 2, 103, 10.0)         # vietor 10 m, zložka u [m/s]
V10 = (0, 2, 3, 103, 10.0)         # vietor 10 m, zložka v [m/s]
GUST_U = (0, 2, 23, 103, 10.0)     # náraz vetra, zložka u [m/s]
GUST_V = (0, 2, 24, 103, 10.0)     # náraz vetra, zložka v [m/s]
TP = (0, 1, 193, 1, 0.0)           # úhrn zrážok za interval [mm]
TCC = (192, 128, 164, 1, 0.0)      # celková oblačnosť [0–1]
PRMSL = (0, 3, 1, 101, 0.0)        # tlak prepočítaný na hladinu mora [Pa]


class GribError(ValueError):
    pass


def _u(b: bytes, off: int, n: int) -> int:
    return int.from_bytes(b[off:off + n], "big")


def _signed(b: bytes, off: int, n: int) -> int:
    """Celé číslo GRIB so znamienkom v najvyššom bite (nie dvojkový doplnok)."""
    raw = _u(b, off, n)
    sign = 1 << (8 * n - 1)
    return -(raw & (sign - 1)) if raw & sign else raw


def _level(sec4: bytes):
    if sec4[23] == 0xFF or _u(sec4, 24, 4) == 0xFFFFFFFF:
        return None
    return _u(sec4, 24, 4) / 10.0 ** _signed(sec4, 23, 1)


_HOURS_PER_UNIT = {0: 1 / 60, 1: 1.0, 2: 24.0, 10: 3.0, 11: 6.0, 12: 12.0, 13: 1 / 3600}


def _unpack_simple(sec5: bytes, data: bytes, count: int) -> list:
    ref = struct.unpack(">f", sec5[11:15])[0]
    e = _signed(sec5, 15, 2)
    d = _signed(sec5, 17, 2)
    nbits = sec5[19]
    scale_e, scale_d = 2.0 ** e, 10.0 ** -d
    if nbits == 0:
        return [ref * scale_d] * count
    needed = (count * nbits + 7) // 8
    if len(data) < needed:
        raise GribError("Sekcia 7 je kratšia, ako udáva počet hodnôt.")
    if nbits == 8:
        raw = data[:count]
    elif nbits == 16:
        raw = struct.unpack(f">{count}H", data[:2 * count])
    else:
        bits = bin(int.from_bytes(data[:needed], "big"))[2:].zfill(needed * 8)
        raw = [int(bits[k * nbits:(k + 1) * nbits], 2) for k in range(count)]
    return [(ref + x * scale_e) * scale_d for x in raw]


def _unpack_ieee(sec5: bytes, data: bytes, count: int) -> list:
    precision = sec5[11]
    fmt, size = {1: ("f", 4), 2: ("d", 8)}.get(precision, (None, 0))
    if fmt is None:
        raise GribError(f"Nepodporovaná presnosť IEEE: {precision}")
    return list(struct.unpack(f">{count}{fmt}", data[:count * size]))


def iter_messages(data: bytes):
    """Vráti správy súboru ako slovníky s kľúčom parametra a hodnotami.

    "hodnoty" obsahujú len body povolené bitovou maskou, v poradí mriežky
    (najprv riadok na juhu, zo západu na východ); "maska" je bitová maska
    celej mriežky ako bytes (1 = bod má hodnotu).
    """
    pos = 0
    previous_mask = None
    while True:
        start = data.find(b"GRIB", pos)
        if start < 0:
            return
        if data[start + 7] != 2:
            raise GribError("Podporovaná je len edícia GRIB 2.")
        discipline = data[start + 6]
        total = _u(data, start + 8, 8)
        end = start + total
        if data[end - 4:end] != b"7777":
            raise GribError("Správa GRIB nekončí značkou 7777.")
        off = start + 16
        sec = {}
        while off < end - 4:
            length = _u(data, off, 4)
            num = data[off + 4]
            sec[num] = data[off:off + length]
            off += length
        s1, s3, s4, s5, s6, s7 = (sec.get(i) for i in (1, 3, 4, 5, 6, 7))
        if None in (s1, s3, s4, s5, s6, s7):
            raise GribError("Správe GRIB chýba niektorá sekcia.")
        n_grid = _u(s3, 6, 4)
        if _u(s3, 12, 2) == 30 and (_u(s3, 30, 4), _u(s3, 34, 4)) != (NX, NY):
            raise GribError("Mriežka ALADIN-u má iný rozmer, než sa čakalo.")
        indicator = s6[5]
        if indicator == 0:
            mask = bytes((s6[6 + k // 8] >> (7 - k % 8)) & 1 for k in range(n_grid))
            previous_mask = mask
        elif indicator == 254 and previous_mask is not None:
            mask = previous_mask
        elif indicator == 255:
            mask = bytes([1]) * n_grid
        else:
            raise GribError(f"Nepodporovaná bitová maska ({indicator}).")
        count = _u(s5, 5, 4)
        template = _u(s5, 9, 2)
        if template == 0:
            values = _unpack_simple(s5, s7[5:], count)
        elif template == 4:
            values = _unpack_ieee(s5, s7[5:], count)
        else:
            raise GribError(f"Nepodporovaný spôsob balenia dát (šablóna 5.{template}).")
        if count != sum(mask):
            raise GribError("Počet hodnôt nesedí s bitovou maskou.")

        pdt = _u(s4, 7, 2)
        unit = _HOURS_PER_UNIT.get(s4[17], 1.0)
        start_h = _u(s4, 18, 4) * unit
        window_h = 0.0
        if pdt == 8 and len(s4) >= 53:
            window_h = _u(s4, 49, 4) * _HOURS_PER_UNIT.get(s4[48], 1.0)
        yield {
            "kluc": (discipline, s4[9], s4[10], s4[22], _level(s4)),
            "beh": (_u(s1, 12, 2), s1[14], s1[15], s1[16], s1[17]),
            "od": start_h,
            "okno": window_h,
            "maska": mask,
            "hodnoty": values,
        }
        pos = end


# --- Geometria mriežky --------------------------------------------------------
_N = math.sin(PHI0)
_F = math.cos(PHI0) * math.tan(math.pi / 4 + PHI0 / 2) ** _N / _N
_RHO0 = EARTH_R * _F / math.tan(math.pi / 4 + PHI0 / 2) ** _N


def _forward(lat: float, lon: float):
    rho = EARTH_R * _F / math.tan(math.pi / 4 + math.radians(lat) / 2) ** _N
    theta = _N * (math.radians(lon) - LON0)
    return rho * math.sin(theta), _RHO0 - rho * math.cos(theta)


def _inverse(x: float, y: float):
    rho = math.copysign(math.hypot(x, _RHO0 - y), _N)
    theta = math.atan2(x, _RHO0 - y)
    lat = 2 * math.atan((EARTH_R * _F / rho) ** (1 / _N)) - math.pi / 2
    return math.degrees(lat), math.degrees(LON0 + theta / _N)


_X1, _Y1 = _forward(LA1, LO1)


def grid_latlon(i: float, j: float):
    """Zemepisná šírka a dĺžka bodu mriežky (i na východ, j na sever; môžu byť aj polovičné)."""
    return _inverse(_X1 + i * DX, _Y1 + j * DY)


def nearest_index(lat: float, lon: float):
    x, y = _forward(lat, lon)
    return round((x - _X1) / DX), round((y - _Y1) / DY)


def to_array(values) -> array:
    return array("f", values)
