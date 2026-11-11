"""Tool: add_task, list_tasks, complete_task."""
from __future__ import annotations

from datetime import date, timedelta

from db.database import TRASH_DAYS, TaskConflict
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

UPDATE_SPEC = ToolSpec(
    name="update_task",
    description=(
        "Mengubah tugas yang sudah ada: judul, tenggat, mata kuliah, keterangan, atau membukanya "
        "kembali. GUNAKAN untuk 'ubah/ganti/geser/undur deadline ...', 'ganti nama tugas ...', "
        "'buka lagi tugas ...'. Tentukan tugas dengan 'title' (kata kunci judul yang LAMA) atau "
        "'task_id' dari list_tasks. Isi HANYA bagian yang berubah: judul baru di 'new_title', tenggat "
        "baru di 'due_date' (YYYY-MM-DD, hitung dari kalender di prompt sistem), 'clear_due_date'=true "
        "untuk menghapus tenggat, 'reopen'=true untuk membuka kembali tugas yang sudah selesai. "
        "JANGAN dipakai untuk menandai selesai (itu complete_task) atau menambah tugas baru "
        "(add_task). Bila hasilnya daftar kandidat, tanyakan ke pengguna yang mana; jangan menebak id."),
    parameters={"type": "object", "properties": {
        "title": {"type": "string", "description": "Kata kunci judul tugas yang akan diubah (judul lama)."},
        "task_id": {"type": "integer", "description": "Id tugas dari list_tasks, bila sudah diketahui."},
        "new_title": {"type": "string", "description": "Judul baru."},
        "due_date": {"type": "string", "description": "Tenggat baru, format YYYY-MM-DD."},
        "clear_due_date": {"type": "boolean", "description": "true = hapus tenggat."},
        "course": {"type": "string", "description": "Mata kuliah baru."},
        "notes": {"type": "string", "description": "Keterangan baru (menggantikan yang lama)."},
        "reopen": {"type": "boolean", "description": "true = tandai belum selesai lagi."}}})

DELETE_SPEC = ToolSpec(
    name="delete_task",
    description=(
        "Menghapus satu tugas dari daftar (dipindah ke tempat sampah, bisa dipulihkan pengguna lewat /restore). GUNAKAN hanya bila pengguna jelas meminta "
        "menghapus, membatalkan, atau membuang tugas ('hapus tugas ...', 'batalkan deadline ...', "
        "'tugas itu salah catat'). JANGAN dipakai untuk tugas yang sudah dikerjakan ('sudah selesai', "
        "'sudah dikumpulkan' -> complete_task) dan jangan menghapus lebih dari yang diminta. Tentukan "
        "dengan 'title' (kata kunci judul) atau 'task_id' dari list_tasks. Bila hasilnya daftar "
        "kandidat, tanyakan ke pengguna yang mana; jangan menebak id."),
    parameters={"type": "object", "properties": {
        "title": {"type": "string", "description": "Kata kunci judul tugas yang akan dihapus."},
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


def _rolled(orig: str | None, final: str | None) -> str:
    """Kalimat pemberitahuan bila _check_due memajukan tahun; kosong bila tidak berubah."""
    if orig and final and orig != final:
        return (f" (CATATAN: tanggal {orig} sudah lewat, jadi tenggat dipakai tahun depan: {final}. "
                "Sebutkan ini ke pengguna supaya bisa dikoreksi.)")
    return ""


def _check_due(due: str, today: date, allow_rollover: bool = True) -> str:
    """Validasi tenggat dari LLM; kembalikan string ISO yang sudah dinormalkan.
    Jika tanggal di masa lalu dan allow_rollover aktif, coba majukan 1 tahun."""
    try:
        d = date.fromisoformat(due)
    except ValueError:
        raise ToolError(f"due_date '{due}' bukan format YYYY-MM-DD (contoh: {today + timedelta(days=5)}).")
    
    if d < today:
        if allow_rollover:
            # Otomatis majukan 1 tahun bila jatuh di masa lalu (misal 1 November saat hari ini 8 November)
            try:
                d_next = date(d.year + 1, d.month, d.day)
                if d_next >= today:
                    return d_next.isoformat()
            except ValueError:
                pass
        raise ToolError(
            f"Tanggal {due} sudah lewat (hari ini {today.isoformat()}). "
            "Periksa tanggalnya; bila memang yang dimaksud, tanyakan ke pengguna tahun yang benar."
        )
    if d > today + timedelta(days=730):
        raise ToolError(f"Tanggal {due} terlalu jauh (hari ini {today.isoformat()}); kemungkinan salah tahun.")
    return d.isoformat()


def add_task(ctx: ToolContext, a: dict) -> str:
    title = a["title"].strip()
    today = ctx.today()
    due = _check_due(a["due_date"], today) if a.get("due_date") else None
    course, _ = ctx.resolve_course(a.get("course"))
    note = _rolled(a.get("due_date"), due)
    t, created = ctx.tasks.add(title, due, course, a.get("notes"))
    if not created:
        return f"Tugas yang sama sudah ada, tidak dibuat ulang: {fmt_task(t, today)}" + note
    return f"Tugas ditambahkan: {fmt_task(t, today)}" + note


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


# ---------------------------------------------------------------- ubah / hapus
def _pick(ctx: ToolContext, a: dict, tool: str) -> tuple[dict | None, str | None]:
    """Tentukan satu tugas dari task_id atau title. Kembalikan (tugas, None) atau (None, teks
    daftar kandidat untuk ditanyakan ke pengguna). Raise ToolError bila tidak ada yang cocok."""
    tid, title = a.get("task_id"), a.get("title")
    if tid is None and not title:
        raise ToolError("Isi title (kata kunci judul tugas) atau task_id.")
    if tid is not None:
        t = ctx.tasks.get(int(tid))
        if t is None:
            raise ToolError(f"Tugas #{tid} tidak ada. Panggil list_tasks untuk melihat id yang benar.")
        return t, None
    cands = ctx.tasks.find_any(title)
    if not cands:
        raise ToolError(f"Tidak ada tugas yang cocok dengan '{title}'. Panggil list_tasks "
                        "(status=all) untuk melihat daftar, atau beri tahu pengguna.")
    if len(cands) > 1:
        today = ctx.today()
        return None, (f"Ada beberapa tugas yang cocok; tanyakan ke pengguna yang mana, lalu panggil "
                      f"{tool} dengan task_id:\n" + "\n".join(fmt_task(c, today) for c in cands))
    return cands[0], None


_FIELD_LABEL = {"title": "judul", "due_date": "tenggat", "course": "mata kuliah",
                "notes": "keterangan", "status": "status"}


def _show(key: str, val) -> str:
    if key == "status":
        return "selesai" if val == "done" else "belum selesai"
    return str(val) if val else "(kosong)"


def update_task(ctx: ToolContext, a: dict) -> str:
    today = ctx.today()
    if a.get("due_date") and a.get("clear_due_date"):
        raise ToolError("Pilih salah satu: due_date (tenggat baru) atau clear_due_date (hapus tenggat).")
    changes: dict = {}
    note = ""
    if a.get("new_title"):
        changes["title"] = a["new_title"].strip()
    if a.get("due_date"):
        changes["due_date"] = _check_due(a["due_date"], today)
        note = _rolled(a["due_date"], changes["due_date"])
    elif a.get("clear_due_date"):
        changes["due_date"] = None
    if a.get("course"):
        changes["course"], _ = ctx.resolve_course(a["course"])
    if a.get("notes"):
        changes["notes"] = a["notes"].strip()
    if a.get("reopen"):
        changes["status"] = "pending"
    if not changes:
        raise ToolError("Tidak ada perubahan yang diminta. Isi minimal satu: new_title, due_date, "
                        "clear_due_date, course, notes, atau reopen.")

    t, cands = _pick(ctx, a, "update_task")
    if cands:
        return cands
    try:
        old, new = ctx.tasks.update(t["id"], **changes)
    except TaskConflict as e:
        raise ToolError(f"Sudah ada tugas lain dengan judul dan tenggat yang sama: "
                        f"{fmt_task(e.other, today)}. Gunakan tugas itu, atau ubah judul/tenggatnya.")
    diff = [f"{_FIELD_LABEL[k]}: {_show(k, old[k])} -> {_show(k, new[k])}"
            for k in _FIELD_LABEL if old[k] != new[k]]
    if not diff:
        return f"Tidak ada yang berubah (nilainya sudah sama): {fmt_task(new, today)}" + note
    return f"Tugas #{new['id']} diperbarui ({'; '.join(diff)}): {fmt_task(new, today)}" + note


def delete_task(ctx: ToolContext, a: dict) -> str:
    t, cands = _pick(ctx, a, "delete_task")
    if cands:
        return cands
    gone = ctx.tasks.delete(t["id"])
    if gone is None:   # terhapus di antara pencarian dan penghapusan
        raise ToolError(f"Tugas #{t['id']} sudah tidak ada.")
    return (f"Tugas dihapus: {fmt_task(gone, ctx.today())}. Tersimpan di tempat sampah selama "
            f"{TRASH_DAYS} hari; pengguna bisa memulihkannya dengan /trash lalu /restore NOMOR.")
