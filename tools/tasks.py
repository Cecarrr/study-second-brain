"""Tool: add_task, list_tasks, complete_task."""
from __future__ import annotations

from datetime import date, timedelta

from providers.base import ToolSpec
from tools.common import ToolContext, ToolError

ADD_SPEC = ToolSpec(
    name="add_task",
    description=(
        "Menambah satu tugas atau tenggat ke daftar tugas pengguna. GUNAKAN untuk 'tambah tugas', "
        "'ada deadline ...', 'ingatkan aku mengumpulkan ...', dan untuk setiap tenggat yang muncul "
        "di informasi kelas (ujian, kuis, pengumpulan). Satu tugas = satu panggilan; tugas yang sama "
        "tidak akan dibuat dua kali. Judul singkat dan jelas, mis. 'UTS Basis Data' atau 'Laporan "
        "praktikum jaringan'. Hitung due_date dari kalender di prompt sistem."),
    parameters={"type": "object", "properties": {
        "title": {"type": "string", "description": "Judul tugas/tenggat."},
        "due_date": {"type": "string",
                     "description": "Format YYYY-MM-DD. Kosongkan bila pengguna tidak menyebut tanggal; "
                                    "jangan mengarang."},
        "course": {"type": "string", "description": "Mata kuliah terkait, bila ada."},
        "notes": {"type": "string", "description": "Keterangan tambahan singkat, bila ada."}},
        "required": ["title"]})

LIST_SPEC = ToolSpec(
    name="list_tasks",
    description=(
        "Menampilkan daftar tugas beserta id-nya, urut tenggat terdekat; yang lewat tenggat ditandai "
        "TERLAMBAT. GUNAKAN untuk 'tugas apa saja', 'apa yang harus kukerjakan hari ini/minggu ini', "
        "'deadline terdekat'. Bawaan hanya menampilkan tugas yang belum selesai (status pending). "
        "'hari ini' -> due_within_days=0 (termasuk yang terlambat); 'minggu ini' -> due_within_days=7; "
        "'semua tugas' -> status=all."),
    parameters={"type": "object", "properties": {
        "status": {"type": "string", "enum": ["pending", "done", "all"],
                   "description": "pending (bawaan), done, atau all."},
        "due_within_days": {"type": "integer", "minimum": 0, "maximum": 365,
                            "description": "Hanya tugas dengan tenggat dalam N hari ke depan (termasuk terlambat)."},
        "course": {"type": "string", "description": "Saring berdasarkan mata kuliah."}}})

COMPLETE_SPEC = ToolSpec(
    name="complete_task",
    description=(
        "Menandai tugas selesai. GUNAKAN saat pengguna bilang tugas sudah selesai atau dikumpulkan. "
        "Cukup isi 'title' dengan kata kunci judul tugas (tidak perlu list_tasks dulu); isi 'task_id' "
        "hanya bila id sudah diketahui dari list_tasks. Bila hasilnya berupa daftar kandidat, tanyakan "
        "ke pengguna yang mana; jangan menebak id."),
    parameters={"type": "object", "properties": {
        "title": {"type": "string", "description": "Kata kunci judul tugas."},
        "task_id": {"type": "integer", "description": "Id tugas dari list_tasks."}}})

_DAYS = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]


def due_text(due: str | None, today: date) -> str:
    if not due:
        return "tanpa tenggat"
    d = (date.fromisoformat(due) - today).days
    if d < 0:
        rel = f"TERLAMBAT {-d} hari"
    elif d == 0:
        rel = "HARI INI"
    elif d == 1:
        rel = "besok"
    else:
        rel = f"{d} hari lagi"
    return f"tenggat {_DAYS[date.fromisoformat(due).weekday()]} {due} ({rel})"


def fmt_task(t: dict, today: date) -> str:
    course = f" | {t['course']}" if t.get("course") else ""
    mark = "selesai" if t["status"] == "done" else "belum"
    notes = f" | {t['notes']}" if t.get("notes") else ""
    return f"#{t['id']} [{mark}] {t['title']} | {due_text(t['due_date'], today)}{course}{notes}"


def add_task(ctx: ToolContext, a: dict) -> str:
    title = a["title"].strip()
    today = ctx.today()
    due = a.get("due_date")
    if due:
        try:
            d = date.fromisoformat(due)
        except ValueError:
            raise ToolError(f"due_date '{due}' bukan format YYYY-MM-DD (contoh: {today + timedelta(days=5)}).")
        if d < today:
            raise ToolError(f"Tanggal {due} sudah lewat (hari ini {today.isoformat()}). Periksa lagi "
                            "tanggalnya dari kalender di prompt sistem; bila pengguna memang bermaksud "
                            "tanggal lampau, tanyakan dulu.")
        if d > today + timedelta(days=730):
            raise ToolError(f"Tanggal {due} terlalu jauh (hari ini {today.isoformat()}); kemungkinan salah tahun.")
    course, _ = ctx.resolve_course(a.get("course"))
    t, created = ctx.tasks.add(title, due, course, a.get("notes"))
    if not created:
        return f"Tugas yang sama sudah ada, tidak dibuat ulang: {fmt_task(t, today)}"
    return f"Tugas ditambahkan: {fmt_task(t, today)}"


def list_tasks(ctx: ToolContext, a: dict) -> str:
    today = ctx.today()
    course, _ = ctx.resolve_course(a.get("course"))
    status = a.get("status", "pending")
    rows = ctx.tasks.list(status, a.get("due_within_days"), course)
    head = f"Hari ini {_DAYS[today.weekday()]} {today.isoformat()}."
    if not rows:
        extra = " dalam rentang itu" if a.get("due_within_days") is not None else ""
        return f"{head} Tidak ada tugas ({status}){extra}."
    return f"{head} {len(rows)} tugas ({status}):\n" + "\n".join(fmt_task(r, today) for r in rows)


def complete_task(ctx: ToolContext, a: dict) -> str:
    today = ctx.today()
    tid, title = a.get("task_id"), a.get("title")
    if tid is None and not title:
        raise ToolError("Isi title (kata kunci judul) atau task_id.")
    if tid is not None:
        res = ctx.tasks.complete(int(tid))
        if res is None:
            raise ToolError(f"Tugas #{tid} tidak ada. Panggil list_tasks untuk melihat id yang benar.")
        t, was_done = res
        return (f"Tugas #{t['id']} '{t['title']}' sudah selesai sebelumnya." if was_done
                else f"Tugas #{t['id']} '{t['title']}' ditandai selesai.")
    cands = ctx.tasks.find_pending(title)
    if not cands:
        raise ToolError(f"Tidak ada tugas belum-selesai yang cocok dengan '{title}'. "
                        "Panggil list_tasks untuk melihat daftar, atau beri tahu pengguna.")
    if len(cands) > 1:
        return ("Ada beberapa tugas yang cocok; tanyakan ke pengguna yang mana, lalu panggil "
                "complete_task dengan task_id:\n" + "\n".join(fmt_task(c, today) for c in cands))
    t, _ = ctx.tasks.complete(cands[0]["id"])
    return f"Tugas #{t['id']} '{t['title']}' ditandai selesai."
