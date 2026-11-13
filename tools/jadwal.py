"""Tool jadwal kuliah: add_class, list_schedule, update_class, delete_class."""
from __future__ import annotations

from datetime import timedelta

import timetable as tt
from db.database import TRASH_DAYS, norm
from providers.base import ToolSpec
from tools.common import ToolContext, ToolError

_KINDS = ["teori", "praktikum", "daring", "lainnya"]
_P = {  # parameter bersama
    "start": {"type": "string", "description": "Jam mulai HH:MM (mis. 07:00)."},
    "end": {"type": "string", "description": "Jam selesai HH:MM (mis. 08:40)."},
    "slots": {"type": "string", "description": "Alternatif start/end: jam kelas kampus, mis. '3' atau '3-5' (jam ke-3 s.d. ke-5)."},
    "room": {"type": "string", "description": "Ruang, mis. B204 atau Lab Komputer 2."},
    "lecturer": {"type": "string", "description": "Nama dosen pengampu."},
    "semester": {"type": "integer", "minimum": 1, "maximum": 14, "description": "Semester mahasiswa (1-14)."},
    "sks": {"type": "integer", "minimum": 1, "maximum": 8, "description": "Jumlah SKS."},
    "code": {"type": "string", "description": "Kode mata kuliah, mis. IF2101."},
    "term": {"type": "string", "description": "Tahun ajaran, mis. 'Ganjil 2026/2027'."},
    "kind": {"type": "string", "enum": _KINDS, "description": "Jenis sesi."},
    "notes": {"type": "string", "description": "Keterangan singkat."},
    "force": {"type": "boolean", "description": "true = tetap simpan walau bentrok (hanya atas izin pengguna)."},
}

ADD_SPEC = ToolSpec(
    name="add_class",
    description=(
        "Menambah SATU sesi kuliah mingguan ke jadwal pengguna (mata kuliah, hari, jam, ruang, dosen, kelas, "
        "semester, SKS). GUNAKAN untuk 'tambah jadwal ...', 'kuliah X tiap Senin jam 7-9 di B204', 'aku ambil "
        "kelas A'. Mata kuliah yang bertemu dua kali seminggu = dua panggilan. Jam: isi start dan end, ATAU "
        "slots untuk sistem jam kelas kampus. Bentrok dengan sesi lain ditolak: tanyakan ke pengguna, dan "
        "pakai force=true hanya bila ia memintanya. JANGAN dipakai untuk tugas atau tenggat ujian (add_task)."),
    parameters={"type": "object", "properties": {
        "course": {"type": "string", "description": "Nama mata kuliah."},
        "day": {"type": "string", "description": "Hari: Senin..Minggu."},
        "class_name": {"type": "string", "description": "Kelas, mis. A atau TI-3B."}, **_P},
        "required": ["course", "day"]})

LIST_SPEC = ToolSpec(
    name="list_schedule",
    description=(
        "Menampilkan jadwal kuliah (id, jam, ruang, dosen, kelas, semester, SKS). GUNAKAN untuk 'hari ini ada "
        "kelas apa', 'jadwal besok/Senin/minggu ini', 'kelas berikutnya jam berapa', 'siapa dosen X', 'ruang X "
        "di mana', 'jadwal semester 5'. day: 'hari ini', 'besok', 'lusa', nama hari, 'minggu ini' (semua, "
        "bawaan), atau 'berikutnya' (kelas terdekat setelah sekarang). JANGAN dipakai untuk tenggat tugas "
        "(list_tasks)."),
    parameters={"type": "object", "properties": {
        "day": {"type": "string", "description": "hari ini | besok | lusa | nama hari | minggu ini | berikutnya"},
        "course": {"type": "string", "description": "Saring berdasarkan mata kuliah atau kode."},
        "semester": {"type": "integer", "minimum": 1, "maximum": 14, "description": "Saring semester."},
        "lecturer": {"type": "string", "description": "Saring berdasarkan dosen."},
        "term": {"type": "string", "description": "Saring tahun ajaran."},
        "archived": {"type": "boolean", "description": "true = tampilkan sesi yang diarsipkan."}}})

UPDATE_SPEC = ToolSpec(
    name="update_class",
    description=(
        "Mengubah satu sesi kuliah (hari, jam, ruang, dosen, kelas, semester, SKS, jenis, catatan) atau "
        "mengarsipkannya (active=false, mis. semester selesai). Tentukan sesinya dengan class_id (dari "
        "list_schedule) atau course (+ day bila mata kuliah punya beberapa sesi). Bila hasilnya daftar "
        "kandidat, tanyakan ke pengguna yang mana. Perubahan jam yang bentrok ditolak."),
    parameters={"type": "object", "properties": {
        "class_id": {"type": "integer", "description": "Id sesi dari list_schedule."},
        "course": {"type": "string", "description": "Kata kunci mata kuliah yang akan diubah."},
        "day": {"type": "string", "description": "Hari sesi yang dimaksud (penyaring)."},
        "new_course": {"type": "string", "description": "Nama mata kuliah baru."},
        "new_day": {"type": "string", "description": "Hari baru."},
        "new_class_name": {"type": "string", "description": "Kelas baru."},
        "active": {"type": "boolean", "description": "false = arsipkan, true = aktifkan lagi."}, **_P}})

DELETE_SPEC = ToolSpec(
    name="delete_class",
    description=(
        "Menghapus satu sesi kuliah (dipindah ke tempat sampah, pengguna bisa memulihkannya lewat /restore). "
        "GUNAKAN hanya bila pengguna jelas meminta hapus atau batal mengambil kelas. Tentukan dengan class_id "
        "atau course (+ day). Bila hasilnya daftar kandidat, tanyakan ke pengguna yang mana."),
    parameters={"type": "object", "properties": {
        "class_id": {"type": "integer", "description": "Id sesi dari list_schedule."},
        "course": {"type": "string", "description": "Kata kunci mata kuliah."},
        "day": {"type": "string", "description": "Hari sesi yang dimaksud (penyaring)."}}})


def _weekday(text: str) -> int:
    wd = tt.parse_weekday(text)
    if wd is None:
        raise ToolError(f"Hari '{text}' tidak dikenali. Pakai Senin, Selasa, Rabu, Kamis, Jumat, Sabtu, atau Minggu.")
    return wd


def _times(a: dict) -> dict:
    if not any(a.get(k) for k in ("start", "end", "slots")):
        return {}
    try:
        s, e = tt.resolve_times(a.get("start"), a.get("end"), a.get("slots"))
    except ValueError as ex:
        raise ToolError(str(ex))
    return {"start_time": s, "end_time": e}


def _common(a: dict) -> dict:
    out = {k: a[k] for k in ("room", "lecturer", "semester", "sks", "code", "term", "kind", "notes") if a.get(k) is not None}
    if out.get("code"):
        out["code"] = out["code"].upper()
    return out


def _clash_text(rows: list[dict]) -> str:
    return "\n".join(f"  - {tt.fmt_session(r)}" for r in rows)


def add_class(ctx: ToolContext, a: dict) -> str:
    f = {"course": a["course"].strip(), "weekday": _weekday(a["day"]), **_times(a), **_common(a)}
    if a.get("class_name"):
        f["class_name"] = a["class_name"].strip()
    if "start_time" not in f:
        raise ToolError("Isi jam: start dan end (mis. 07:00 dan 08:40), atau slots (mis. '1-2').")
    rows = ctx.tasks.sched_list(weekday=f["weekday"])
    for r in rows:
        if (norm(r["course"]) == norm(f["course"]) and r["start_time"] == f["start_time"]
                and norm(r.get("class_name")) == norm(f.get("class_name"))):
            return f"Sesi yang sama sudah ada, tidak dibuat ulang: {tt.fmt_session(r)}"
    clash = tt.conflicts(rows, f["weekday"], f["start_time"], f["end_time"])
    if clash and not a.get("force"):
        raise ToolError("Bentrok dengan sesi lain:\n" + _clash_text(clash) + "\nTanyakan ke pengguna; bila ia tetap "
                        "ingin menambahkan, panggil lagi dengan force=true.")
    row = ctx.tasks.sched_add(f)
    return "Jadwal ditambahkan: " + tt.fmt_session(row) + (
        "\n(CATATAN: bentrok dengan:\n" + _clash_text(clash) + ")" if clash else "")


def _due_footer(ctx: ToolContext, d) -> str:
    due = [t for t in ctx.tasks.list("pending") if t.get("due_date") == d.isoformat()]
    return "\nTenggat tugas hari itu: " + "; ".join(f"#{t['id']} {t['title']}" for t in due) if due else ""


def list_schedule(ctx: ToolContext, a: dict) -> str:
    today, now = ctx.today(), ctx.now()
    ref = tt.parse_day_ref(a.get("day") or "minggu ini", today)
    if ref is None:
        raise ToolError(f"day '{a.get('day')}' tidak dikenali. Pakai: hari ini, besok, lusa, nama hari, minggu ini, atau berikutnya.")
    kw = dict(course=a.get("course"), semester=a.get("semester"), term=a.get("term"), lecturer=a.get("lecturer"),
              active=False if a.get("archived") else True)
    head = f"Sekarang {tt.DAYS[today.weekday()]} {today.isoformat()} pukul {now.strftime('%H:%M')}."
    if ref[0] == "next":
        hit = tt.next_session(ctx.tasks.sched_list(**kw), now)
        if not hit:
            return f"{head} Tidak ada kelas terjadwal dalam 7 hari ke depan."
        r, d = hit
        when = "hari ini" if d == today else "besok" if d == today + timedelta(days=1) else f"{tt.DAYS[d.weekday()]} {d.isoformat()}"
        eta = ""
        if d == today:
            mins = int((now.replace(hour=int(r["start_time"][:2]), minute=int(r["start_time"][3:]), second=0,
                                    microsecond=0) - now).total_seconds() // 60)
            eta = f" (sekitar {mins // 60} jam {mins % 60} menit lagi)" if mins >= 60 else f" ({mins} menit lagi)"
        return f"{head} Kelas berikutnya, {when}{eta}:\n{tt.fmt_session(r)}"
    if ref[0] in ("date", "weekday"):
        d = ref[1] if ref[0] == "date" else ref[2]
        rows = ctx.tasks.sched_list(weekday=d.weekday(), **kw)
        label = f"{tt.DAYS[d.weekday()]} {d.isoformat()}" + (" (hari ini)" if d == today else " (besok)" if d == today + timedelta(days=1) else "")
        if not rows:
            return f"{head} Tidak ada kelas pada {label}." + _due_footer(ctx, d)
        lines = [tt.fmt_session(r) + (tt.status_at(r, now) if d == today else "") for r in rows]
        return f"{head} {len(rows)} sesi pada {label}:\n" + "\n".join(lines) + _due_footer(ctx, d)
    rows = ctx.tasks.sched_list(**kw)
    if not rows:
        return f"{head} Jadwal kosong" + (" untuk filter itu." if any(a.get(k) for k in ("course", "semester", "lecturer", "term")) else ". Tambahkan dengan add_class atau /import-jadwal.")
    body = []
    for wd in range(7):
        day = [r for r in rows if r["weekday"] == wd]
        if day:
            body.append(f"{tt.DAYS[wd]}:")
            body += [f"  {tt.fmt_session(r)}" for r in day]
    sks = tt.total_sks(rows)
    return f"{head} {len(rows)} sesi" + (f", total {sks} SKS" if sks else "") + ":\n" + "\n".join(body)


def _pick(ctx: ToolContext, a: dict, tool: str):
    if a.get("class_id") is not None:
        r = ctx.tasks.sched_get(int(a["class_id"]))
        if r is None:
            raise ToolError(f"Sesi #{a['class_id']} tidak ada. Panggil list_schedule untuk melihat id yang benar.")
        return r, None
    if not a.get("course"):
        raise ToolError("Isi class_id (dari list_schedule) atau course.")
    rows = ctx.tasks.sched_list(course=a["course"], active=None)
    if a.get("day"):
        wd = _weekday(a["day"])
        rows = [r for r in rows if r["weekday"] == wd]
    if not rows:
        raise ToolError(f"Tidak ada sesi untuk '{a['course']}'" + (f" pada {a['day']}" if a.get("day") else "") +
                        ". Panggil list_schedule untuk melihat daftar.")
    if len(rows) > 1:
        return None, (f"Ada beberapa sesi yang cocok; tanyakan ke pengguna yang mana, lalu panggil {tool} dengan "
                      "class_id:\n" + "\n".join(tt.fmt_session(r) for r in rows))
    return rows[0], None


def update_class(ctx: ToolContext, a: dict) -> str:
    row, cands = _pick(ctx, a, "update_class")
    if cands:
        return cands
    ch = {**_times(a), **_common(a)}
    if a.get("new_course"):
        ch["course"] = a["new_course"].strip()
    if a.get("new_day"):
        ch["weekday"] = _weekday(a["new_day"])
    if a.get("new_class_name"):
        ch["class_name"] = a["new_class_name"].strip()
    if a.get("active") is not None:
        ch["active"] = 1 if a["active"] else 0
    if not ch:
        raise ToolError("Tidak ada perubahan yang diminta (isi mis. new_day, start/end, room, lecturer, active).")
    new = {**row, **ch}
    if new["active"] and not a.get("force"):
        clash = tt.conflicts(ctx.tasks.sched_list(weekday=new["weekday"]), new["weekday"], new["start_time"],
                             new["end_time"], exclude_id=row["id"])
        if clash:
            raise ToolError("Perubahan bentrok dengan sesi lain:\n" + _clash_text(clash) +
                            "\nTanyakan ke pengguna; force=true hanya bila ia memintanya.")
    changed = [k for k, v in ch.items() if row.get(k) != v]
    if not changed:
        return f"Tidak ada yang berubah (nilainya sudah sama): {tt.fmt_session(row)}"
    upd = ctx.tasks.sched_update(row["id"], ch)
    return f"Sesi #{upd['id']} diperbarui ({', '.join(changed)}): {tt.fmt_session(upd)}"


def delete_class(ctx: ToolContext, a: dict) -> str:
    row, cands = _pick(ctx, a, "delete_class")
    if cands:
        return cands
    ctx.tasks.sched_delete(row["id"])
    tid = max(t["id"] for t in ctx.tasks.trash_list())
    return (f"Sesi dihapus: {tt.fmt_session(row)}. Tersimpan di tempat sampah #{tid} selama {TRASH_DAYS} hari; "
            f"pengguna bisa memulihkannya dengan /restore {tid}.")
