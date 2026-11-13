"""Jadwal kuliah: waktu, sistem jam kelas, bentrok, dan format. Fungsi murni (tanpa DB/LLM).

Sistem jam kelas ("jam ke-N") dapat diatur di config.py (semua opsional):
  CLASS_FIRST_START = "07:00"   awal jam ke-1
  CLASS_PERIOD_MIN  = 50        lama satu jam kelas (menit)
  CLASS_PERIOD_COUNT = 12       jumlah jam kelas per hari
  CLASS_PERIODS = {1: ("07:00", "07:50"), 2: ("07:50", "08:40"), ...}   tabel eksplisit (menimpa tiga di atas)
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

import config

DAYS = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
_ALIAS = {"senin": 0, "mon": 0, "monday": 0, "selasa": 1, "tue": 1, "tues": 1, "tuesday": 1, "rabu": 2,
          "wed": 2, "wednesday": 2, "kamis": 3, "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
          "jumat": 4, "fri": 4, "friday": 4, "sabtu": 5, "sat": 5, "saturday": 5, "minggu": 6,
          "ahad": 6, "sun": 6, "sunday": 6}
_ICS_DAY = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


# ---------------------------------------------------------------- hari
def parse_weekday(text) -> int | None:
    t = re.sub(r"[^a-z]", "", str(text).lower().replace("hari", "", 1) if str(text).lower().startswith("hari")
               else str(text).lower())
    return _ALIAS.get(t)


def parse_day_ref(text: str, today: date):
    """-> ("date", d) | ("weekday", n, d) | ("week",) | ("next",) | None.
    d = tanggal terdekat (hari ini atau sesudahnya) untuk hari tersebut."""
    t = " ".join(str(text).lower().split())
    if t in ("hari ini", "today", "sekarang hari ini"):
        return ("date", today)
    if t in ("besok", "tomorrow"):
        return ("date", today + timedelta(days=1))
    if t == "lusa":
        return ("date", today + timedelta(days=2))
    if t in ("berikutnya", "selanjutnya", "next", "next class", "kelas berikutnya", "sekarang"):
        return ("next",)
    if t in ("minggu ini", "pekan ini", "semua", "all", "seminggu", "this week", "week", "mingguan"):
        return ("week",)
    wd = parse_weekday(t)
    if wd is not None:
        return ("weekday", wd, today + timedelta(days=(wd - today.weekday()) % 7))
    return None


# ---------------------------------------------------------------- waktu dan jam kelas
def parse_time(s) -> str:
    t = str(s).strip().lower()
    for w in ("pukul", "jam", "wib", "wita", "wit"):
        t = t.replace(w, "")
    m = re.fullmatch(r"\s*(\d{1,2})(?:[:.](\d{2}))?\s*", t)
    if not m:
        raise ValueError(f"jam '{s}' tidak dikenali (pakai format HH:MM, mis. 07:30)")
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        raise ValueError(f"jam '{s}' di luar rentang 00:00-23:59")
    return f"{h:02d}:{mi:02d}"


def periods() -> dict[int, tuple[str, str]]:
    custom = getattr(config, "CLASS_PERIODS", None)
    if custom:
        return {int(k): (parse_time(v[0]), parse_time(v[1])) for k, v in custom.items()}
    h, m = map(int, parse_time(getattr(config, "CLASS_FIRST_START", "07:00")).split(":"))
    length, n = int(getattr(config, "CLASS_PERIOD_MIN", 50)), int(getattr(config, "CLASS_PERIOD_COUNT", 12))
    out = {}
    for i in range(n):
        a = h * 60 + m + i * length
        b = a + length
        out[i + 1] = (f"{a // 60:02d}:{a % 60:02d}", f"{b // 60:02d}:{b % 60:02d}")
    return out


def slots_to_range(spec) -> tuple[str, str]:
    nums = [int(x) for x in re.findall(r"\d+", str(spec))]
    if not nums or len(nums) > 2:
        raise ValueError(f"slots '{spec}' tidak dikenali (contoh: '3' atau '3-5')")
    a, b, p = nums[0], nums[-1], periods()
    if a not in p or b not in p or b < a:
        raise ValueError(f"jam ke-{a}..{b} tidak valid (jam kelas tersedia: 1-{max(p)})")
    return p[a][0], p[b][1]


def resolve_times(start=None, end=None, slots=None) -> tuple[str, str]:
    if start and end:
        s, e = parse_time(start), parse_time(end)
    elif slots:
        s, e = slots_to_range(slots)
    else:
        raise ValueError("isi jam: start dan end (mis. 07:00 dan 08:40), atau slots (mis. '1-2')")
    if s >= e:
        raise ValueError(f"jam selesai ({e}) harus setelah jam mulai ({s})")
    return s, e


def overlaps(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    return a_start < b_end and b_start < a_end


def conflicts(rows: list[dict], weekday: int, start: str, end: str, exclude_id: int | None = None) -> list[dict]:
    return [r for r in rows if r["weekday"] == weekday and r["id"] != exclude_id
            and overlaps(start, end, r["start_time"], r["end_time"])]


# ---------------------------------------------------------------- format
def fmt_session(r: dict) -> str:
    code = f" ({r['code']})" if r.get("code") else ""
    cls = f" kelas {r['class_name']}" if r.get("class_name") else ""
    bits = [f"#{r['id']} {DAYS[r['weekday']]} {r['start_time']}-{r['end_time']}", f"{r['course']}{code}{cls}"]
    if r.get("room"):
        bits.append(f"ruang {r['room']}")
    if r.get("lecturer"):
        bits.append(r["lecturer"])
    meta = []
    if r.get("semester"):
        meta.append(f"semester {r['semester']}")
    if r.get("sks"):
        meta.append(f"{r['sks']} SKS")
    if r.get("kind"):
        meta.append(r["kind"])
    if r.get("term"):
        meta.append(r["term"])
    if meta:
        bits.append(", ".join(meta))
    if r.get("notes"):
        bits.append(r["notes"])
    if not r.get("active", 1):
        bits.append("(diarsipkan)")
    return " | ".join(bits)


def fmt_brief(r: dict) -> str:
    cls = f" kelas {r['class_name']}" if r.get("class_name") else ""
    tail = " | ".join(x for x in (f"ruang {r['room']}" if r.get("room") else "", r.get("lecturer") or "") if x)
    return f"{r['start_time']}-{r['end_time']} {r['course']}{cls}" + (f" | {tail}" if tail else "")


def status_at(r: dict, now: datetime) -> str:
    hm = now.strftime("%H:%M")
    if r["start_time"] <= hm < r["end_time"]:
        return " [sedang berlangsung]"
    return " [sudah selesai]" if r["end_time"] <= hm else ""


def total_sks(rows: list[dict]) -> int:
    seen, total = set(), 0
    for r in rows:
        key = (re.sub(r"\W+", "", r["course"].lower()), (r.get("class_name") or "").lower())
        if key not in seen and r.get("sks"):
            seen.add(key)
            total += int(r["sks"])
    return total


def week_table(rows: list[dict]) -> str:
    out = []
    for wd in range(7):
        day = sorted((r for r in rows if r["weekday"] == wd), key=lambda r: r["start_time"])
        if day:
            out.append(DAYS[wd])
            out += [f"  {fmt_brief(r)}" for r in day]
    return "\n".join(out)


def next_session(rows: list[dict], now: datetime):
    """Sesi terdekat yang belum mulai (7 hari ke depan). -> (sesi, tanggal) atau None."""
    for off in range(8):
        d = now.date() + timedelta(days=off)
        hm = now.strftime("%H:%M")
        for r in sorted((r for r in rows if r["weekday"] == d.weekday()), key=lambda r: r["start_time"]):
            if off > 0 or r["start_time"] > hm:
                return r, d
    return None
