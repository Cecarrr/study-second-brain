"""Impor jadwal kuliah dari CSV atau ICS (tanpa LLM). Memakai validasi yang sama dengan add_class,
jadi bentrok, duplikat, dan jam salah dilaporkan per baris (tidak menggagalkan seluruh impor).

CSV: baris pertama = judul kolom (Indonesia/Inggris, urutan bebas, pemisah koma atau titik koma):
  matkul, kode, kelas, semester, sks, dosen, hari, mulai, selesai (atau jam_ke), ruang, jenis, tahun_ajaran, catatan
ICS: tiap VEVENT = satu mata kuliah (SUMMARY), LOCATION = ruang, RRULE WEEKLY;BYDAY=MO,WE = banyak hari.
"""
from __future__ import annotations

import csv
import io
import re

import config
import timetable as tt
from tools import jadwal
from tools.common import ToolContext, ToolError

TEMPLATE = ("matkul,kode,kelas,semester,sks,dosen,hari,mulai,selesai,ruang,jenis,tahun_ajaran,catatan\n"
            "Basis Data,IF2101,A,5,3,Dr. Budi,Senin,07:00,09:30,B204,teori,Ganjil 2026/2027,\n"
            "Praktikum Basis Data,IF2102,A1,5,1,Ir. Sari,Rabu,jam 3-4,,Lab 2,praktikum,Ganjil 2026/2027,\n")
# `mulai` boleh berisi "jam 3-4" (jam kelas) dan `selesai` dikosongkan; lihat _slots_from.

_HEAD = {"course": ("matkul", "mata kuliah", "mk", "course", "nama", "nama mata kuliah"),
         "code": ("kode", "code", "kode mk"), "class_name": ("kelas", "class"),
         "semester": ("semester", "smt"), "sks": ("sks", "kredit", "credits"),
         "lecturer": ("dosen", "pengampu", "lecturer"), "day": ("hari", "day"),
         "start": ("mulai", "jam mulai", "start"), "end": ("selesai", "jam selesai", "end"),
         "slots": ("jam ke", "jam_ke", "slot", "slots"), "room": ("ruang", "ruangan", "room"),
         "kind": ("jenis", "kind", "tipe"), "term": ("tahun ajaran", "tahun_ajaran", "ta", "term"),
         "notes": ("catatan", "notes", "keterangan")}


def _col(name: str) -> str | None:
    n = re.sub(r"[_\s]+", " ", name.strip().lower())
    for key, aliases in _HEAD.items():
        if n in aliases:
            return key
    return None


def _slots_from(row: dict) -> dict:
    """`mulai` berbentuk 'jam 3-4' (tanpa `selesai`) dianggap jam kelas."""
    s = row.get("start", "")
    if s and not row.get("end") and re.search(r"(?i)\bjam\s*(?:ke)?\s*-?\s*\d+(?:\s*[-–]\s*\d+)?\s*$", s) \
            and not re.search(r"[:.]\d{2}", s):
        row = {**row, "slots": re.sub(r"(?i)jam|ke", "", s).strip(), "start": ""}
    return row


def parse_csv(text: str) -> tuple[list[dict], list[str]]:
    text = text.lstrip("\ufeff")
    try:
        dialect = csv.Sniffer().sniff(text[:2000], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows, errors = [], []
    reader = csv.reader(io.StringIO(text), dialect)
    header = next(reader, None)
    if not header:
        return [], ["file kosong"]
    cols = [_col(h) for h in header]
    if "course" not in cols or "day" not in cols:
        return [], [f"judul kolom tidak lengkap: butuh minimal matkul dan hari (ditemukan: {', '.join(header)})"]
    for i, rec in enumerate(reader, start=2):
        if not any(c.strip() for c in rec):
            continue
        row = {k: v.strip() for k, v in zip(cols, rec) if k and v.strip()}
        rows.append((i, _slots_from(row)))
    return rows, errors


def _unfold(text: str) -> list[str]:
    out: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _ics_dt(value: str, utc_offset: int):
    from datetime import datetime, timedelta
    m = re.search(r"(\d{8})T(\d{6})(Z?)", value)
    if not m:
        return None
    dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    return dt + timedelta(hours=utc_offset) if m.group(3) else dt


def parse_ics(text: str) -> tuple[list[tuple[int, dict]], list[str]]:
    offset = int(getattr(config, "ICS_UTC_OFFSET_HOURS", 7))     # penanda 'Z' (UTC) -> WIB
    rows, errors, ev, n = [], [], None, 0
    for line in _unfold(text):
        if line.strip() == "BEGIN:VEVENT":
            ev, n = {}, n + 1
        elif line.strip() == "END:VEVENT" and ev is not None:
            start, end = _ics_dt(ev.get("DTSTART", ""), offset), _ics_dt(ev.get("DTEND", ""), offset)
            if not start or not end or not ev.get("SUMMARY"):
                errors.append(f"event #{n}: SUMMARY/DTSTART/DTEND tidak lengkap")
            else:
                byday = re.search(r"BYDAY=([A-Z,]+)", ev.get("RRULE", ""))
                days = [tt._ICS_DAY[d[-2:]] for d in byday.group(1).split(",") if d[-2:] in tt._ICS_DAY] \
                    if byday else [start.weekday()]
                desc = ev.get("DESCRIPTION", "").replace("\\n", "\n").replace("\\,", ",")
                lec = re.search(r"(?i)dosen\s*[:=]\s*([^\n;]+)", desc)
                for wd in days:
                    rows.append((n, {"course": ev["SUMMARY"].replace("\\,", ","), "day": tt.DAYS[wd],
                                     "start": start.strftime("%H:%M"), "end": end.strftime("%H:%M"),
                                     **({"room": ev["LOCATION"].replace("\\,", ",")} if ev.get("LOCATION") else {}),
                                     **({"lecturer": lec.group(1).strip()} if lec else {})}))
            ev = None
        elif ev is not None and ":" in line:
            key, _, val = line.partition(":")
            ev[key.split(";")[0].upper()] = val
    return rows, errors


def import_text(ctx: ToolContext, text: str, fmt: str) -> str:
    """Impor CSV ('csv') atau ICS ('ics'). Kembalikan laporan ringkas."""
    rows, errors = parse_csv(text) if fmt == "csv" else parse_ics(text)
    added = dup = 0
    problems = list(errors)
    for line_no, row in rows:
        args = dict(row)
        for k in ("semester", "sks"):
            if k in args:
                try:
                    args[k] = int(float(args[k]))
                except ValueError:
                    problems.append(f"baris {line_no}: {k} '{args[k]}' bukan angka")
                    args.pop(k)
        if args.get("kind") and args["kind"].lower() not in jadwal._KINDS:
            args["kind"] = "lainnya"
        elif args.get("kind"):
            args["kind"] = args["kind"].lower()
        try:
            out = jadwal.add_class(ctx, args)
        except ToolError as e:
            problems.append(f"baris {line_no} ({args.get('course', '?')}): {str(e).splitlines()[0]}")
            continue
        if out.startswith("Sesi yang sama sudah ada"):
            dup += 1
        else:
            added += 1
    lines = [f"Impor selesai: {added} sesi ditambahkan, {dup} sudah ada, {len(problems)} bermasalah."]
    lines += [f"  ! {p}" for p in problems[:15]]
    if len(problems) > 15:
        lines.append(f"  ... dan {len(problems) - 15} lainnya")
    return "\n".join(lines)
