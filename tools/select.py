"""Subset tool dinamis: kirim ke model hanya tool yang relevan dengan pesan, untuk memangkas token.

Skema 11 tool menghabiskan sekitar 3.000 token di setiap request. Pesan seperti "kuis aku 3 soal
normalisasi" hanya butuh dua tool. Pemilihan memakai kata kunci (Indonesia + Inggris), tanpa
memanggil model.

ATURAN KONSERVATIF (salah memangkas lebih mahal daripada token berlebih):
  - Tidak ada kecocokan yang jelas -> SEMUA tool dikirim ("ya yang pertama", "Ibu kota Prancis apa?").
  - search_knowledge selalu ikut. Tool yang tidak masuk kelompok mana pun (tool baru yang lupa
    didaftarkan di GROUPS) selalu ikut.
  - Pesan panjang (> LONG_TEXT karakter, mis. pengumuman kelas yang ditempel) -> semua tool.
  - Asisten baru bertanya klarifikasi -> kelompok giliran sebelumnya dipertahankan (`last_groups`).
  - Bila model tetap memanggil tool yang tidak dikirim, Agent.ask mengulang request SEKALI dengan
    semua tool (lihat agent.py).
Kelompok yang tidak punya tool terdaftar (mis. jadwal sebelum fitur 3 dipasang) tidak dihitung.

Coba dari terminal:  python -m tools.select "kuis aku 3 soal normalisasi"
"""
from __future__ import annotations

import json
import re

GROUPS: dict[str, tuple[str, ...]] = {
    "tugas": ("add_task", "list_tasks", "complete_task", "update_task", "delete_task"),
    "catatan": ("add_note", "list_notes", "update_note", "delete_note"),
    "pengetahuan": ("search_knowledge",),
    "kuis": ("make_quiz",),
    "jadwal": ("add_class", "list_schedule", "delete_class"),
}
ALWAYS = ("pengetahuan",)           # kelompok yang selalu ikut
LONG_TEXT = 400                     # pesan lebih panjang dari ini -> semua tool
SHORT_REPLY = 60                    # balasan sependek ini tanpa kata kunci melanjutkan kelompok sebelumnya


def _rx(*alts: str) -> re.Pattern:
    return re.compile(r"\b(?:" + "|".join(alts) + r")\b", re.I)


TUGAS = _rx(r"tugas\w*", r"deadline\w*", r"tenggat\w*", r"\w*kumpul\w*", r"pr", r"to-?do", r"task\w*",
            r"assignment\w*", r"homework", r"due", r"submit\w*", r"submission\w*", r"jatuh tempo",
            r"\w*kerjakan\w*", r"ingatkan", r"remind\w*", r"batalkan", r"selesai\w*", r"beres",
            r"finish\w*", r"done", r"complet\w*", r"undur\w*", r"geser\w*", r"tunda\w*",
            r"postpone\w*", r"reschedule\w*", r"buka lagi", r"reopen")
CATATAN = _rx(r"\w*catat\w*", r"\w*simpan\w*", r"ingat bahwa", r"note\w*", r"save\w*", r"remember that",
              r"memo", r"dosen (?:bilang|bilangnya|menyampaikan|mengumumkan|mengatakan|menginfokan)",
              r"kata dosen", r"pengumuman", r"announce\w*",
              r"(?:my )?(?:lecturer|professor|teacher) (?:said|says|told|announced)")
DATE = _rx(r"besok", r"lusa", r"minggu (?:ini|depan)", r"pekan (?:ini|depan)", r"bulan depan", r"senin",
           r"selasa", r"rabu", r"kamis", r"jum'?at", r"sabtu", r"minggu", r"tanggal", r"tgl", r"tomorrow",
           r"next (?:week|month)", r"(?:mon|tues|wednes|thurs|fri|satur|sun)day", r"\d{4}-\d{2}-\d{2}",
           r"\d{1,2}/\d{1,2}(?:/\d{2,4})?", r"\d{1,2}-\d{1,2}-\d{2,4}",
           r"januari|februari|maret|april|juni|juli|agustus|september|oktober|november|desember",
           r"\d{1,2} ?(?:jan|feb|mar|apr|mei|jun|jul|agu|sep|okt|nov|des|dec|aug|oct|may)\w*")
EXAM = _rx(r"uts", r"uas", r"ujian", r"exam\w*", r"midterm", r"final")
JADWAL = _rx(r"jadwal\w*", r"ada (?:kelas|kuliah)", r"kelas (?:apa|hari|besok|lusa|jam|pukul|mulai|dimulai|berapa)",
             r"(?<!mata )kuliah", r"jam berapa", r"pukul berapa", r"ruang(?:an)?", r"schedule\w*", r"timetable",
             r"classes", r"class (?:today|tomorrow|schedule|at|on)", r"what class", r"which room", r"room")
KUIS_STRONG = _rx(r"\d+ ?soal", r"(?:buat|beri)(?:kan)? (?:\d+ )?soal", r"soal (?:latihan|pilihan ganda)",
                  r"latihan(?: soal)?", r"pilihan ganda", r"quiz\w*", r"uji (?:pemahaman|aku|saya)\w*",
                  r"test me", r"tes (?:aku|saya|pemahaman)", r"flashcard\w*", r"kuis(?:in)? (?:aku|saya)",
                  r"practice (?:question|problem)s?")
KUIS_WEAK = _rx(r"kuis\w*")         # "kuis" juga nama tugas ("tugas kuis 2"), jadi hanya bila bukan konteks tugas
PENGETAHUAN = _rx(r"jelas\w*", r"apa itu", r"apa yang dimaksud", r"apa (?:bedanya|perbedaan)",
                  r"beda(?:nya)? antara", r"perbedaan", r"definisi", r"pengertian", r"artinya", r"materi",
                  r"slide", r"modul", r"menurut", r"bobot", r"silabus", r"rubrik", r"penilaian", r"rangkum\w*",
                  r"ringkas\w*", r"bandingkan", r"cari\w*", r"explain\w*", r"what (?:is|are)", r"define",
                  r"definition", r"difference between", r"according to", r"syllabus", r"lecture notes?",
                  r"how (?:does|do)", r"why (?:does|do|is)", r"summar\w*", r"compare", r"search")

_CLARIFY = re.compile(r"(?:yang mana|mana yang|maksud(?:mu| anda| kamu)|which one|do you mean)", re.I)


def match_groups(text: str, carry=()) -> set[str]:
    """Kelompok yang disebut jelas oleh pesan (belum dibatasi tool yang terdaftar).
    `carry`: kelompok yang masih berlaku dari giliran sebelumnya (memengaruhi arti kata 'kuis')."""
    g: set[str] = set()
    has_date = bool(DATE.search(text))
    notes = bool(CATATAN.search(text))
    if TUGAS.search(text) or (has_date and (notes or EXAM.search(text))):
        g.add("tugas")                  # catatan bertenggat = add_note + add_task
    if notes:
        g.add("catatan")
    if JADWAL.search(text):
        g.add("jadwal")
    if KUIS_STRONG.search(text) or (KUIS_WEAK.search(text) and "tugas" not in g and "tugas" not in carry):
        g.add("kuis")
    if PENGETAHUAN.search(text):
        g.add("pengetahuan")
    return g


def available_groups(names) -> set[str]:
    names = set(names)
    return {g for g, tools in GROUPS.items() if names & set(tools)}


def select_groups(text: str, available: set[str], carry=()) -> frozenset[str] | None:
    """Kelompok yang dikirim, atau None = semua tool. `carry` = kelompok yang dipertahankan dari
    giliran sebelumnya (asisten bertanya klarifikasi)."""
    if len(text) > LONG_TEXT:
        return None
    matched = match_groups(text, carry) & available
    kept = set(carry) & available
    if matched:
        return frozenset(matched | kept)
    # Tanpa kata kunci: balasan pendek ("ya yang pertama") melanjutkan giliran sebelumnya; pesan lain yang
    # tak dikenali -> SEMUA tool, supaya model tidak terkurung di kelompok lama (mis. kuis tak tersedia).
    return frozenset(kept) if kept and len(text) <= SHORT_REPLY else None


def select_specs(text: str, reg, last_groups=None) -> list:
    """Daftar ToolSpec untuk pesan ini, urutan sesuai registry."""
    specs = reg.specs()
    names = {s.name for s in specs}
    groups = select_groups(text, available_groups(names), last_groups or ())
    if groups is None:
        return specs
    keep = {t for g in groups | set(ALWAYS) for t in GROUPS[g]}
    grouped = {t for ts in GROUPS.values() for t in ts}
    return [s for s in specs if s.name in keep or s.name not in grouped]


def groups_of(tool_names) -> frozenset[str]:
    return frozenset(g for g, ts in GROUPS.items() if set(ts) & set(tool_names))


def asked_clarification(answer: str) -> bool:
    """Jawaban asisten berupa pertanyaan balik (kandidat ganda, 'yang mana?')."""
    tail = (answer or "").strip()[-200:]
    return "?" in tail or bool(_CLARIFY.search(tail))


def estimate_tokens(specs) -> int:
    """Perkiraan kasar token skema tool (sama dengan rumus check_tools.py)."""
    return int(sum(len(s.description) + len(json.dumps(s.parameters)) for s in specs) / 3.5)


if __name__ == "__main__":
    import sys

    from tools.common import ToolContext
    from tools.registry import build_registry

    reg = build_registry(ToolContext(retriever=None, store=None, embedder=None, tasks=None))
    full = estimate_tokens(reg.specs())
    for msg in sys.argv[1:] or ["kuis aku 3 soal normalisasi"]:
        sel = select_specs(msg, reg)
        print(f"{msg!r}\n  tool ({len(sel)}/{len(reg.specs())}): {', '.join(s.name for s in sel)}"
              f"\n  ~{estimate_tokens(sel)} dari ~{full} token skema")
