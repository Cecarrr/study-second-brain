"""Uji 9 tool tanpa API (embedding dan LLM palsu, DB dan catatan sementara).

  python check_tools.py          -> semua uji offline (0 request)
  python check_tools.py --live   -> tambahan: search_knowledge dan make_quiz dengan indeks dan model asli
                                    (sekitar 1 request embedding + 1 request chat)
"""
import json
import sys
import tempfile
from datetime import date
from pathlib import Path

import config
from db.database import TaskDB
from prompts import build_system_prompt
from providers.base import ChatResponse, ProviderError, ToolCall
from providers.mock import MockProvider
from rag.embedder import Embedder
from rag.indexer import sync_folder
from rag.retriever import Retriever
from rag.store import VectorStore
from tools.common import ToolContext
from tools.registry import build_registry
from tools.summary import build_daily_summary

FAILS = []


def check(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def call(reg, name, **args):
    m = reg.execute(ToolCall(name=name, args=args))
    return m.content, m.is_error


QUIZ_JSON = json.dumps({"questions": [
    {"question": "Apa syarat 3NF?", "options": ["A. Tidak ada ketergantungan transitif", "B. Nilai atomik",
     "C. Tidak ada composite key", "D. Semua kolom unik"], "answer": "A",
     "explanation": "3NF melarang ketergantungan transitif.", "source": "01_normalisasi.md"},
    {"question": "Apa itu 1NF?", "options": ["Nilai atomik", "Tanpa foreign key", "Tanpa NULL", "Satu tabel"],
     "answer": "A", "explanation": "Kolom berisi satu nilai.", "source": "01_normalisasi.md"}]})


def main():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "materi" / "basis-data").mkdir(parents=True)
    (tmp / "materi" / "basis-data" / "01_normalisasi.md").write_text(
        "# Normalisasi\n\n## 3NF\nSyarat 3NF: tidak ada ketergantungan transitif antar atribut non-kunci.\n"
        "\n## 1NF\nSyarat 1NF: setiap kolom berisi nilai atomik.\n", encoding="utf-8")
    emb = Embedder(MockProvider(embed_dim=256))
    store = VectorStore(tmp / "vs.db", emb.embedding_id)
    sync_folder(tmp / "materi", "materi", store, emb)
    llm_replies = [ChatResponse(text=QUIZ_JSON)]
    ctx = ToolContext(retriever=Retriever(store, emb, min_score=0.1), store=store, embedder=emb,
                      tasks=TaskDB(tmp / "app.db", today_fn=lambda: date(2026, 11, 8)),
                      notes_dir=tmp / "notes", log_path=tmp / "tools.jsonl",
                      llm_chat=lambda msgs, tools=None: llm_replies.pop(0))
    reg = build_registry(ctx)

    print("[1] add_note + search_knowledge")
    out, err = call(reg, "add_note", content="Dosen Basis Data: UTS mencakup bab 1-4 tanggal 2026-12-12.",
                    course="Basis Data", tags="uts, pengumuman")
    check(not err and "sudah masuk pencarian" in out, "catatan tersimpan dan ter-index: " + out[:60])
    files = list((tmp / "notes").glob("*.md"))
    check(len(files) == 1 and "course: basis-data" in files[0].read_text(encoding="utf-8"),
          "file .md + frontmatter, mata kuliah dikanonikkan ke 'basis-data'")
    out, _ = call(reg, "search_knowledge", query="UTS bab 1-4 tanggal")
    check("catatan" in out.lower() or ".md" in out, "catatan baru bisa dicari")
    out, _ = call(reg, "search_knowledge", query="ketergantungan transitif 3NF", course="Basis Data")
    check("01_normalisasi.md" in out, "pencarian dengan filter mata kuliah 'Basis Data'")
    out, _ = call(reg, "search_knowledge", query="x", course="Mata Kuliah Ngawur")
    check("tidak ada di indeks" in out, "mata kuliah tak dikenal: diberi tahu, cari di semua")
    out, err = call(reg, "add_note", content="ab")
    check(err and "terlalu pendek" in out, "catatan terlalu pendek ditolak")

    print("[2] add_task")
    out, err = call(reg, "add_task", title="UTS Basis Data", due_date="2026-12-12", course="Basis Data")
    check(not err and "#1" in out and "Sabtu 2026-12-12" in out, "tugas dibuat + hari benar: " + out[:70])
    out, _ = call(reg, "add_task", title="  uts basis data ", due_date="2026-12-12")
    check("sudah ada" in out, "duplikat tidak dibuat ulang")
    out, err = call(reg, "add_task", title="X", due_date="12 Desember")
    check(err and "YYYY-MM-DD" in out, "format tanggal salah ditolak")
    out, err = call(reg, "add_task", title="X", due_date="2026-11-01")
    check(err and "sudah lewat" in out, "tanggal lampau ditolak")
    out, err = call(reg, "add_task", title="X", due_date="2030-01-01")
    check(err and "terlalu jauh" in out, "tahun salah ditolak")
    call(reg, "add_task", title="Laporan praktikum jaringan", due_date="2026-11-13", course="Jaringan Komputer")
    call(reg, "add_task", title="Baca bab 5 algoritma")
    call(reg, "add_task", title="Kuis algoritma", due_date="2026-11-09")

    print("[3] list_tasks")
    out, _ = call(reg, "list_tasks")
    lines = [l for l in out.splitlines() if l.startswith("#")]
    check(len(lines) == 4 and lines[0].startswith("#4") and lines[-1].startswith("#3"),
          "urut tenggat, tanpa tenggat di akhir")
    check("besok" in out and "5 hari lagi" in out, "teks sisa hari benar")
    out, _ = call(reg, "list_tasks", due_within_days=7)
    check(out.count("\n#") == 2, "due_within_days=7 -> 2 tugas")
    out, _ = call(reg, "list_tasks", due_within_days=0)
    check("Tidak ada tugas" in out, "hari ini kosong dilaporkan")
    out, _ = call(reg, "list_tasks", course="jaringan")
    check(out.count("\n#") == 1, "filter mata kuliah (cocok sebagian)")
    out, err = call(reg, "list_tasks", status="selesai")
    check(err and "pending, done, all" in out, "enum salah ditolak jelas")

    print("[4] complete_task")
    out, _ = call(reg, "complete_task", title="laporan jaringan")
    check("ditandai selesai" in out, "selesai lewat kata kunci judul")
    out, _ = call(reg, "add_task", title="Kuis basis data", due_date="2026-11-10")
    call(reg, "add_task", title="Kuis jaringan", due_date="2026-11-11")
    out, err = call(reg, "complete_task", title="kuis")
    check(not err and "tanyakan ke pengguna" in out and "#" in out, "ambigu -> daftar kandidat, tidak menebak")
    out, err = call(reg, "complete_task", title="skripsi")
    check(err and "list_tasks" in out, "tidak ada yang cocok -> error jelas")
    out, err = call(reg, "complete_task", task_id=999)
    check(err and "tidak ada" in out, "id tidak ada -> error")
    out, _ = call(reg, "complete_task", task_id="2")
    check("sudah selesai sebelumnya" in out or "ditandai selesai" in out, "id sebagai string dikoersi")
    out, _ = call(reg, "complete_task", task_id=2)
    check("sudah selesai sebelumnya" in out, "selesai dua kali tidak error")
    out, err = call(reg, "complete_task")
    check(err and "title" in out, "tanpa argumen -> error")

    print("[5] make_quiz")
    out, err = call(reg, "make_quiz", topic="normalisasi 3NF", num_questions="2")
    check(not err and "KUNCI JAWABAN" in out and out.index("1. Apa syarat") < out.index("=== KUNCI JAWABAN"), "soal sebelum kunci")
    check("A. Tidak ada ketergantungan transitif" in out and "A. A." not in out, "awalan huruf ganda dibersihkan")
    llm_replies.append(ChatResponse(text="maaf aku tidak bisa"))
    out, err = call(reg, "make_quiz", topic="normalisasi")
    check(err and "format yang benar" in out, "JSON rusak -> error jelas, tanpa loop")
    llm_replies.append(ChatResponse(text="```json\n" + QUIZ_JSON + "\n```"))
    out, err = call(reg, "make_quiz", topic="normalisasi")
    check(not err and "KUNCI" in out, "JSON dalam code fence tetap terbaca")
    out, err = call(reg, "make_quiz", topic="zzzz qqqq")
    check(not err and "tidak dibuat" in out, "tanpa materi relevan -> tidak mengarang kuis")

    print("[6] registry")
    out, err = call(reg, "tool_ngawur")
    check(err and "tidak ada" in out, "tool tak dikenal")
    out, err = call(reg, "search_knowledge", top_k=3)
    check(err and "query" in out, "parameter wajib hilang")
    out, _ = call(reg, "search_knowledge", query="3NF", top_k="99", ngawur="x")
    check("Error" not in out, "argumen asing dibuang, top_k dibatasi")
    check(len(reg.names) == 9 and {"update_task", "delete_task", "list_notes"} <= set(reg.names),
          "9 tool terdaftar (termasuk update_task, delete_task, list_notes)")

    def boom(msgs, tools=None):
        raise ProviderError("Semua slot chat habis\nlaporan panjang")
    ctx.llm_chat = boom
    out, err = call(reg, "make_quiz", topic="normalisasi")
    check(err and "tidak tersedia" in out and "laporan" not in out, "ProviderError jadi pesan ringkas, tidak crash")
    check((tmp / "tools.jsonl").read_text(encoding="utf-8").count("\n") > 20, "log tool call tertulis")

    print("[7] update_task")
    out, err = call(reg, "update_task", title="baca bab 5", due_date="2026-11-14")
    check(not err and "tenggat: (kosong) -> 2026-11-14" in out and "Sabtu 2026-11-14" in out,
          "tenggat tugas tanpa tenggat diisi: " + out[:60])
    out, err = call(reg, "update_task", title="baca bab 5", clear_due_date=True)
    check(not err and "2026-11-14 -> (kosong)" in out, "clear_due_date menghapus tenggat")
    out, err = call(reg, "update_task", title="baca bab 5")
    check(err and "Tidak ada perubahan" in out, "tanpa perubahan -> error jelas")
    out, err = call(reg, "update_task", title="kuis", due_date="2026-11-20")
    check(not err and "tanyakan ke pengguna" in out and out.count("\n#") == 3,
          "kandidat ganda -> daftar, tidak mengubah apa pun")
    out, _ = call(reg, "list_tasks", course=None)
    check("2026-11-20" not in out, "kandidat ganda memang tidak mengubah data")
    out, err = call(reg, "update_task", title="uts", due_date="2026-11-01")
    check(err and "sudah lewat" in out, "tenggat baru di masa lalu ditolak")
    out, err = call(reg, "update_task", task_id=6, new_title="Kuis basis data", due_date="2026-11-10")
    check(err and "Sudah ada tugas lain" in out, "perubahan yang menduplikasi tugas lain ditolak")
    out, err = call(reg, "update_task", title="laporan praktikum", reopen="true")
    check(not err and "selesai -> belum selesai" in out, "tugas selesai dibuka lagi (bool sebagai string)")
    out, err = call(reg, "update_task", title="skripsi", notes="x")
    check(err and "Tidak ada tugas yang cocok" in out, "judul tak dikenal -> error jelas")
    call(reg, "add_task", title="Evaluasi laporan akhir", due_date="2026-11-30")
    out, err = call(reg, "complete_task", title="uas")
    check(err and "Tidak ada tugas belum-selesai" in out,
          "regresi: 'uas' tidak lagi cocok dengan 'Evaluasi' (pencocokan per kata)")

    print("[8] delete_task")
    n_before = call(reg, "list_tasks", status="all")[0].count("\n#")
    out, err = call(reg, "delete_task", title="kuis")
    check(not err and "tanyakan ke pengguna" in out and
          call(reg, "list_tasks", status="all")[0].count("\n#") == n_before,
          "kandidat ganda -> tidak ada yang dihapus")
    out, err = call(reg, "delete_task", title="evaluasi laporan akhir")
    check(not err and "dihapus permanen" in out, "hapus lewat kata kunci judul: " + out[:50])
    check("Evaluasi laporan akhir" not in call(reg, "list_tasks", status="all")[0], "tugas benar-benar hilang")
    out, err = call(reg, "delete_task", task_id=999)
    check(err and "tidak ada" in out, "id tidak ada -> error")
    out, err = call(reg, "delete_task")
    check(err and "title" in out, "tanpa argumen -> error")

    print("[9] list_notes dan ringkasan harian")
    out, err = call(reg, "list_notes")
    check(not err and out.startswith("1 catatan") and "BELUM" not in out and "UTS mencakup" in out,
          "catatan hasil add_note tercantum dan sudah ter-index")
    (tmp / "notes" / "manual.md").write_text("Catatan tulisan tangan.", encoding="utf-8")
    out, _ = call(reg, "list_notes")
    check(out.startswith("2 catatan") and "BELUM ter-index" in out, "file baru ditandai belum ter-index")
    out, _ = call(reg, "list_notes", course="Basis Data")
    check(out.startswith("1 catatan"), "filter mata kuliah")
    summ = build_daily_summary(ctx.tasks)
    check(summ.startswith("Ringkasan Minggu, 8 November 2026") and "Dalam 3 hari ke depan" in summ
          and "TERLAMBAT" not in summ, "ringkasan harian: tanggal, bagian mendesak, tanpa terlambat")

    print("[10] prompt dan schema")
    p = build_system_prompt(date(2026, 11, 8), ["basis-data", "jaringan"])
    check("Bila pengguna mencatat informasi kelas yang memuat tenggat, panggil add_note untuk isinya "
          "dan add_task untuk tenggatnya." in p, "kalimat aturan catatan+tenggat ada")
    check("Senin 2026-11-09 (besok)" in p and 'due_date="2026-11-13"' in p and "2026-12-12" in p,
          "kalender dan contoh tanggal dihitung benar")
    approx = (len(p) + sum(len(s.description) + len(json.dumps(s.parameters)) for s in reg.specs())) // 3.5
    print(f"  info: prompt + 9 tool ~ {int(approx)} token per request")
    try:
        from google.genai import types
        for s in reg.specs():
            types.FunctionDeclaration(name=s.name, description=s.description, parameters_json_schema=s.parameters)
        check(True, "schema 9 tool diterima SDK Gemini")
    except Exception as e:  # noqa: BLE001
        check(False, f"schema ditolak SDK: {e}")

    if "--live" in sys.argv:
        print("[LIVE] indeks dan model asli")
        from rag.stack import open_stack
        store2, emb2, ret2 = open_stack()
        live = build_registry(ToolContext(retriever=ret2, store=store2, embedder=emb2,
                                          tasks=TaskDB(tmp / "live.db"), notes_dir=tmp / "notes_live"))
        print("  courses:", store2.courses(), "| chunk:", len(store2))
        out, err = call(live, "search_knowledge", query="apa itu normalisasi database")
        print("  search_knowledge ->", out[:300].replace("\n", " | "))
        out, err = call(live, "make_quiz", topic="normalisasi database", num_questions=2)
        print("  make_quiz ->", out[:500])

    print("\nSEMUA LULUS" if not FAILS else f"\n{len(FAILS)} GAGAL: {FAILS}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
