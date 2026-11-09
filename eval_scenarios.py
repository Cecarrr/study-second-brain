"""Skenario evaluasi model (dipakai eval_models.py). Murni data + pemeriksa, tanpa I/O jaringan.

Tiap Scenario berisi:
  message   pesan pengguna ke agent
  expect    tool yang HARUS dipanggil (urutan longgar, tool tambahan boleh)
  expect_any  minimal satu dari tool ini harus dipanggil (untuk kasus yang boleh diselesaikan dua cara)
  forbid    tool yang TIDAK boleh dipanggil
  readonly  True = data (tugas + catatan) tidak boleh berubah; tool tulis dilarang
  no_tools  True = tidak boleh ada tool call sama sekali (pertanyaan di luar materi)
  seed      seed(env): isi DB/folder catatan sebelum pesan dikirim
  check     check(env, run) -> None bila lulus, selain itu alasan gagal. Memeriksa DB/file dan
            argumen/hasil tool, BUKAN teks jawaban model (satu pengecualian: kuis, yang memang
            menguji bahwa kunci jawaban tidak bocor ke teks).
  script    jawaban "model ideal" untuk mode --mock (format providers.mock.MockProvider)

Tanggal tetap: hari ini = Senin 2026-11-09 (Jumat terdekat 2026-11-13, lusa 2026-11-11).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from providers.mock import call

TODAY = date(2026, 11, 9)   # Senin


# ---------------------------------------------------------------- tipe
@dataclass
class Call:
    name: str
    args: dict
    ok: bool
    out: str


@dataclass
class Run:
    calls: list[Call] = field(default_factory=list)
    answer: str = ""
    requests: int = 0


@dataclass
class Scenario:
    id: str
    message: str
    expect: tuple = ()
    expect_any: tuple = ()      # minimal satu dari tool ini harus dipanggil
    forbid: tuple = ()
    readonly: bool = False
    no_tools: bool = False
    seed: Callable | None = None
    check: Callable | None = None
    script: list[Any] = field(default_factory=list)


# ---------------------------------------------------------------- materi untuk RAG (diindeks palsu)
MATERI = {
    "basis-data/normalisasi.md": """---
title: Normalisasi Basis Data
---
# Normalisasi Basis Data
Normalisasi adalah proses merapikan struktur tabel untuk mengurangi redundansi dan anomali data.

## Bentuk Normal Pertama (1NF)
Setiap kolom hanya berisi nilai atomik dan tidak ada kelompok kolom yang berulang.

## Bentuk Normal Kedua (2NF)
Sudah 1NF dan setiap atribut non-kunci bergantung penuh pada seluruh primary key, bukan sebagian.

## Bentuk Normal Ketiga (3NF)
Sudah 2NF dan tidak ada ketergantungan transitif antara atribut non-kunci.
""",
    "basis-data/silabus-penilaian.md": """---
title: Silabus dan Penilaian Basis Data
---
# Penilaian Mata Kuliah Basis Data
Bobot nilai akhir: kuis 10 persen, praktikum 20 persen, UTS 30 persen, dan UAS 40 persen.
Kehadiran minimal 75 persen untuk boleh mengikuti UAS.
""",
    "jaringan/osi-model.md": """---
title: OSI Model
---
# OSI Model
The OSI model divides network communication into seven layers: Physical, Data Link, Network,
Transport, Session, Presentation, and Application. Each layer only talks to the layer directly
above and below it. Routers work at the Network layer, switches at the Data Link layer.
""",
}


# ---------------------------------------------------------------- seed
def seed_tasks(env) -> None:
    env.tasks.add("Laporan basis data", "2026-11-10", "basis-data")
    env.tasks.add("Kuis jaringan", "2026-11-12", "jaringan")
    env.tasks.add("Esai etika profesi", "2026-11-20")
    t, _ = env.tasks.add("Tugas algoritma 1", "2026-11-05")
    env.tasks.complete(t["id"])


def seed_notes(env) -> None:
    env.add_note("Pengumuman lab jaringan: praktikum dimulai pukul 08.00 di Lab 2.",
                 "Pengumuman lab jaringan", "jaringan")
    env.add_note("UTS Basis Data mencakup bab 1 sampai 4, tanggal 2026-12-12.",
                 "UTS Basis Data", "basis-data")


def seed_two_quizzes(env) -> None:
    env.tasks.add("Kuis 1", "2026-11-12")
    env.tasks.add("Kuis 2", "2026-11-14")


# ---------------------------------------------------------------- helper pemeriksa
def _tasks(env) -> list[dict]:
    return env.tasks.list("all")


def _titled(env, kw: str) -> list[dict]:
    return [t for t in _tasks(env) if kw.lower() in t["title"].lower()]


def _int_eq(v, n) -> bool:
    try:
        return float(v) == n
    except (TypeError, ValueError):
        return False


def _search_ok(run: Run, source: str, terms: tuple) -> str | None:
    s = [c for c in run.calls if c.name == "search_knowledge"]
    if not s:
        return f"tidak memanggil search_knowledge ({[c.name for c in run.calls] or 'tanpa tool'})"
    queries = [str(c.args.get("query", "")) for c in s]
    if not any(t in q.lower() for q in queries for t in terms):
        return f"query tidak memuat satu pun dari {terms}: {queries}"
    if not any(source in c.out for c in s):
        return f"hasil pencarian tidak memuat {source} (query {queries})"
    return None


def leaked_key(run: Run) -> str | None:
    """Kunci jawaban kuis tidak boleh muncul di teks jawaban; soal harus tampil."""
    ans = run.answer
    if "KUNCI JAWABAN" in ans.upper():
        return "teks jawaban memuat header KUNCI JAWABAN"
    if re.search(r"(?im)^\s*\d+\.\s*[A-D]\s*(?:-|:|$)", ans):
        return "teks jawaban memuat baris kunci (mis. '1. B - ...')"
    for c in run.calls:
        if c.name != "make_quiz" or not c.ok or "=== KUNCI JAWABAN" not in c.out:
            continue
        for line in c.out.split("=== KUNCI JAWABAN", 1)[1].splitlines()[1:]:
            m = re.match(r"\d+\.\s*.*?\s-\s(.+?)(?:\s\(sumber:.*\))?$", line.strip())
            if m and len(m.group(1)) >= 15 and m.group(1).lower() in ans.lower():
                return f"penjelasan kunci bocor ke jawaban: {m.group(1)[:40]!r}"
    shown = len(re.findall(r"(?m)^\s*\d+[.)]\s+\S", ans))
    if shown < 2:
        return "soal tidak ditampilkan ke pengguna"
    return None


# ---------------------------------------------------------------- pemeriksa per skenario
def c_note_task(env, run):
    notes = env.notes()
    if len(notes) != 1:
        return f"harusnya 1 catatan, ada {len(notes)}"
    if "uts" not in next(iter(notes.values())).lower():
        return "isi catatan tidak menyebut UTS"
    rows = _tasks(env)
    if len(rows) != 1:
        return f"harusnya 1 tugas, ada {len(rows)}"
    t = rows[0]
    if t["due_date"] != "2026-12-12":
        return f"due_date={t['due_date']!r}, harusnya '2026-12-12'"
    return None if "uts" in t["title"].lower() else f"judul tugas {t['title']!r} tidak menyebut UTS"


def c_note_only(env, run):
    notes = env.notes()
    if len(notes) != 1:
        return f"harusnya 1 catatan, ada {len(notes)}"
    if "80" not in next(iter(notes.values())):
        return "isi catatan tidak memuat angka 80"
    return None if not _tasks(env) else "membuat tugas padahal tidak ada tenggat"


def _single_task(env, kw: str, due: str | None):
    rows = _tasks(env)
    if len(rows) != 1:
        return f"harusnya 1 tugas, ada {len(rows)}"
    if kw not in rows[0]["title"].lower():
        return f"judul {rows[0]['title']!r} tidak memuat {kw!r}"
    if rows[0]["due_date"] != due:
        return f"due_date={rows[0]['due_date']!r}, harusnya {due!r}"
    return None if not env.notes() else "membuat catatan padahal hanya diminta tugas"


def c_task_friday(env, run):
    return _single_task(env, "laporan", "2026-11-13")


def c_task_lusa(env, run):
    return _single_task(env, "praktikum", "2026-11-11")


def c_task_nodate(env, run):
    return _single_task(env, "bab", None)


def c_past_date(env, run):
    return _single_task(env, "algoritma", "2027-11-01")


def c_dup(env, run):
    rows = [t for t in _titled(env, "laporan basis data") if t["status"] == "pending"]
    if len(rows) != 1:
        return f"harusnya 1 tugas pending 'laporan basis data', ada {len(rows)}"
    return None if rows[0]["due_date"] == "2026-11-10" else f"tenggat berubah jadi {rows[0]['due_date']!r}"


def c_complete_title(env, run):
    k = _titled(env, "kuis jaringan")
    if len(k) != 1 or k[0]["status"] != "done":
        return f"'Kuis jaringan' belum selesai: {[(t['title'], t['status']) for t in k]}"
    other = [t["title"] for t in _tasks(env) if t["status"] == "done" and t["id"] != k[0]["id"]]
    return None if other == ["Tugas algoritma 1"] else f"tugas lain ikut berubah: {other}"


def c_complete_ambig(env, run):
    bad = [t["title"] for t in _titled(env, "kuis") if t["status"] != "pending"]
    return None if not bad else f"menyelesaikan tugas padahal ambigu: {bad}"


def c_submitted(env, run):
    k = _titled(env, "laporan basis data")
    if len(k) != 1:
        return f"tugas hilang/ganda: {len(k)}"
    if k[0]["status"] != "done":
        return "belum ditandai selesai"
    return None if not env.trash() else "tugas dibuang ke sampah, harusnya hanya ditandai selesai"


def c_update_deadline(env, run):
    k = _titled(env, "laporan basis data")
    if len(k) != 1:
        return f"harusnya 1 tugas, ada {len(k)}"
    if k[0]["due_date"] != "2026-11-13":
        return f"due_date={k[0]['due_date']!r}, harusnya '2026-11-13'"
    return None if len(_tasks(env)) == 4 else f"jumlah tugas berubah ({len(_tasks(env))}, harusnya 4)"


def c_rename(env, run):
    if _titled(env, "etika profesi"):
        return "judul lama masih ada"
    k = [t for t in _tasks(env) if "etika" in t["title"].lower() and "hukum" in t["title"].lower()]
    if len(k) != 1:
        return f"judul baru tidak ditemukan: {[t['title'] for t in _tasks(env)]}"
    if k[0]["due_date"] != "2026-11-20":
        return f"tenggat ikut berubah: {k[0]['due_date']!r}"
    return None if len(_tasks(env)) == 4 else "jumlah tugas berubah"


def c_delete_task(env, run):
    if _titled(env, "esai"):
        return "tugas masih ada"
    if len(_tasks(env)) != 3:
        return f"harusnya sisa 3 tugas, ada {len(_tasks(env))}"
    t = [x for x in env.trash() if x["kind"] == "task"]
    return None if t and "esai" in t[0]["label"].lower() else "tidak masuk tempat sampah"


def c_search_id(env, run):
    return _search_ok(run, "silabus-penilaian.md", ("uas", "bobot", "nilai", "penilaian"))


def c_search_en(env, run):
    return _search_ok(run, "osi-model.md", ("osi", "layer", "lapisan"))


def c_update_note(env, run):
    notes = env.notes()
    if len(notes) != 2:
        return f"jumlah catatan berubah ({len(notes)}, harusnya 2)"
    uts = [t for t in notes.values() if "UTS Basis Data" in t]
    if len(uts) != 1:
        return "catatan UTS tidak ditemukan"
    low = uts[0].lower()
    if "bab 5" not in low:
        return "teks baru 'bab 5' tidak ada di catatan"
    return None if "bab 1 sampai 4" in low else "isi lama hilang (harusnya ditambah, bukan diganti)"


def c_delete_note(env, run):
    notes = env.notes()
    if len(notes) != 1 or "UTS Basis Data" not in next(iter(notes.values())):
        return f"catatan yang tersisa salah: {list(notes)}"
    t = [x for x in env.trash() if x["kind"] == "note"]
    return None if t else "tidak masuk tempat sampah"


def c_quiz(env, run):
    q = [c for c in run.calls if c.name == "make_quiz"]
    if not q:
        return None  # ditangkap oleh `expect`
    if not q[0].ok:
        return f"make_quiz error: {q[0].out[:100]}"
    if "normal" not in str(q[0].args.get("topic", "")).lower():
        return f"topic={q[0].args.get('topic')!r}"
    if not _int_eq(q[0].args.get("num_questions"), 3):
        return f"num_questions={q[0].args.get('num_questions')!r}, harusnya 3"
    return leaked_key(run)


# ---------------------------------------------------------------- skenario mock (model ideal)
def _quiz_reply(messages, tools=None) -> str:
    out = next((m.content for m in reversed(messages) if m.role == "tool"), "")
    head = out.split("=== KUNCI JAWABAN", 1)[0]
    return "Berikut soalnya:\n" + "\n".join(
        ln for ln in head.splitlines()[1:] if ln.strip() and not ln.startswith("[Untuk asisten"))


def _calls(*cs: dict) -> dict:
    return {"tool_calls": [c["tool_calls"][0] for c in cs]}


SCENARIOS: list[Scenario] = [
    Scenario("catat+tenggat", "Catat: dosen basis data bilang UTS bab 1-4, tanggal 12 Des",
             expect=("add_note", "add_task"), check=c_note_task, script=[
                 _calls(call("add_note", content="Dosen Basis Data: UTS mencakup bab 1-4, tanggal 2026-12-12.",
                             title="UTS Basis Data", course="Basis Data"),
                        call("add_task", title="UTS Basis Data (bab 1-4)", due_date="2026-12-12",
                             course="Basis Data")),
                 "Sudah dicatat dan tenggat UTS 2026-12-12 ditambahkan."]),
    Scenario("catat-tanpa-tenggat", "Catat: dosen jaringan bilang kehadiran praktikum minimal 80%",
             expect=("add_note",), forbid=("add_task",), check=c_note_only, script=[
                 call("add_note", content="Dosen Jaringan: kehadiran praktikum minimal 80%.",
                      title="Kehadiran praktikum jaringan", course="Jaringan"),
                 "Catatan disimpan."]),
    Scenario("tugas-jumat", "Tambah tugas: laporan jaringan, deadline Jumat",
             expect=("add_task",), forbid=("add_note",), check=c_task_friday, script=[
                 call("add_task", title="Laporan jaringan", due_date="2026-11-13"),
                 "Tugas ditambahkan, tenggat Jumat 2026-11-13."]),
    Scenario("tugas-lusa", "Tambah tugas: praktikum jaringan deadline lusa",
             expect=("add_task",), forbid=("add_note",), check=c_task_lusa, script=[
                 call("add_task", title="Praktikum jaringan", due_date="2026-11-11"),
                 "Ditambahkan, tenggat 2026-11-11."]),
    Scenario("tugas-tanpa-tanggal", "Tambah tugas: baca bab 5",
             expect=("add_task",), forbid=("add_note",), check=c_task_nodate, script=[
                 call("add_task", title="Baca bab 5"), "Ditambahkan tanpa tenggat."]),
    Scenario("tanggal-lampau", "Tambah tugas: kuis algoritma deadline 1 November",
             expect=("add_task",), check=c_past_date, script=[
                 call("add_task", title="Kuis algoritma", due_date="2026-11-01"),
                 call("add_task", title="Kuis algoritma", due_date="2027-11-01"),
                 "1 November tahun ini sudah lewat, jadi tenggatnya 2027-11-01."]),
    Scenario("tanpa-duplikat", "Tambah tugas laporan basis data deadline besok", seed=seed_tasks,
             expect_any=("add_task", "list_tasks"), check=c_dup, script=[
                 call("add_task", title="Laporan basis data", due_date="2026-11-10"),
                 "Tugas itu sudah ada, tidak dibuat ulang."]),
    Scenario("selesai-judul", "Tugas kuis jaringan sudah selesai", seed=seed_tasks,
             expect=("complete_task",), forbid=("delete_task",), check=c_complete_title, script=[
                 call("complete_task", title="kuis jaringan"), "Kuis jaringan ditandai selesai."]),
    Scenario("selesai-ambigu", "Kuis sudah selesai", seed=seed_two_quizzes,
             expect_any=("complete_task", "list_tasks"), check=c_complete_ambig, script=[
                 call("complete_task", title="kuis"),
                 "Ada dua tugas cocok: Kuis 1 dan Kuis 2. Yang mana?"]),
    Scenario("dikumpulkan-bukan-hapus", "Tugas laporan basis data sudah dikumpulkan", seed=seed_tasks,
             expect=("complete_task",), forbid=("delete_task",), check=c_submitted, script=[
                 call("complete_task", title="laporan basis data"), "Ditandai selesai."]),
    Scenario("ubah-tenggat", "Geser deadline laporan basis data jadi Jumat", seed=seed_tasks,
             expect=("update_task",), forbid=("add_task", "delete_task"), check=c_update_deadline, script=[
                 call("update_task", title="laporan basis data", due_date="2026-11-13"),
                 "Tenggat baru Jumat 2026-11-13."]),
    Scenario("ganti-judul", "Ganti nama tugas esai etika profesi jadi Esai etika dan hukum IT",
             seed=seed_tasks, expect=("update_task",), forbid=("add_task", "delete_task"),
             check=c_rename, script=[
                 call("update_task", title="esai etika profesi", new_title="Esai etika dan hukum IT"),
                 "Judul diganti."]),
    Scenario("hapus-tugas", "Hapus tugas esai etika profesi", seed=seed_tasks,
             expect=("delete_task",), forbid=("complete_task",), check=c_delete_task, script=[
                 call("delete_task", title="esai etika profesi"), "Tugas dihapus."]),
    Scenario("daftar-tugas", "Apa yang harus kukerjakan hari ini?", seed=seed_tasks,
             expect=("list_tasks",), readonly=True, script=[
                 call("list_tasks", due_within_days=0), "Ada tugas yang tenggatnya dekat."]),
    Scenario("daftar-catatan", "Catatan apa saja yang sudah kusimpan?", seed=seed_notes,
             expect=("list_notes",), readonly=True, script=[call("list_notes"), "Ada 2 catatan."]),
    Scenario("tambah-ke-catatan", "Tambahkan ke catatan UTS basis data: bab 5 ikut diuji", seed=seed_notes,
             expect=("update_note",), forbid=("add_note",), check=c_update_note, script=[
                 call("update_note", title="UTS Basis Data", append_text="Bab 5 ikut diuji."),
                 "Catatan diperbarui."]),
    Scenario("hapus-catatan", "Hapus catatan pengumuman lab jaringan", seed=seed_notes,
             expect=("delete_note",), check=c_delete_note, script=[
                 call("delete_note", title="pengumuman lab jaringan"), "Catatan dihapus."]),
    Scenario("materi-id", "Berapa bobot nilai UAS Basis Data menurut materi kita?",
             expect=("search_knowledge",), readonly=True, check=c_search_id, script=[
                 call("search_knowledge", query="bobot nilai UAS"),
                 "Bobot UAS 40 persen (silabus-penilaian.md)."]),
    Scenario("materi-en", "According to my networking material, how many layers does the OSI model have?",
             expect=("search_knowledge",), readonly=True, check=c_search_en, script=[
                 call("search_knowledge", query="OSI model layers"),
                 "Seven layers (osi-model.md)."]),
    Scenario("kuis-tanpa-kunci", "Kuis aku 3 soal normalisasi",
             expect=("make_quiz",), readonly=True, check=c_quiz, script=[
                 call("make_quiz", topic="normalisasi database", num_questions=3), _quiz_reply]),
    Scenario("luar-materi-id", "Ibu kota Prancis apa?", no_tools=True,
             script=["Ibu kota Prancis adalah Paris."]),
    Scenario("luar-materi-en", "What is the capital of Japan?", no_tools=True,
             script=["The capital of Japan is Tokyo."]),
]

BY_ID = {s.id: s for s in SCENARIOS}
