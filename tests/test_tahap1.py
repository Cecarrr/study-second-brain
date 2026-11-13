"""Uji offline tahap 1, item 2-4 (update/delete task, list_notes, ipynb, konversi .ppt/.doc,
ringkasan harian) plus regresi dua bug lama. 0 request API.

  python -m unittest tests.test_tahap1 -v      (dari akar repo)
  python tests/test_tahap1.py
"""
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from agent import WRITE_TOOLS, Agent  # noqa: E402
from db.database import TaskDB  # noqa: E402
from providers.base import Message, ToolCall  # noqa: E402
from rag import convert  # noqa: E402
from rag.ingest import FileRef, discover_files, read_document, sha256_file  # noqa: E402
from tools import notes, tasks  # noqa: E402
from tools.common import ToolContext, ToolError  # noqa: E402
from tools.registry import build_registry  # noqa: E402
from tools.summary import build_daily_summary  # noqa: E402

TODAY = date(2026, 11, 9)      # Senin


class FakeStore:
    def __init__(self, courses=("basis-data", "jaringan")):
        self._courses, self.hashes = list(courses), {}

    def courses(self):
        return self._courses

    def doc_hash(self, doc_id):
        return self.hashes.get(doc_id)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = TaskDB(self.dir / "t.db", today_fn=lambda: TODAY)
        self.store = FakeStore()
        self.ctx = ToolContext(retriever=None, store=self.store, embedder=None, tasks=self.db,
                               notes_dir=self.dir / "notes", log_path=self.dir / "tools.jsonl")

    def tearDown(self):
        self.tmp.cleanup()


# ---------------------------------------------------------------- bug 1: pencocokan judul
class TitleMatchTests(Base):
    def test_kata_pendek_tidak_cocok_di_tengah_kata(self):
        self.db.add("Evaluasi laporan praktikum", "2026-12-01")
        self.db.add("UAS Basis Data", "2026-12-20")
        self.assertEqual([t["title"] for t in self.db.find_pending("uas")], ["UAS Basis Data"])

    def test_awalan_kata_tetap_cocok(self):
        self.db.add("Laporan praktikum jaringan", "2026-12-01")
        self.assertEqual(len(self.db.find_pending("lap jaringan")), 1)

    def test_judul_persis_didahulukan(self):
        self.db.add("Kuis 1", "2026-12-01")
        self.db.add("Kuis 1 ulang", "2026-12-02")
        self.assertEqual([t["title"] for t in self.db.find_pending("kuis 1")], ["Kuis 1"])

    def test_complete_task_tidak_menyelesaikan_tugas_yang_salah(self):
        self.db.add("Evaluasi laporan", "2026-12-01")
        with self.assertRaises(ToolError):
            tasks.complete_task(self.ctx, {"title": "UAS"})
        self.assertEqual(self.db.list("pending")[0]["status"], "pending")


# ---------------------------------------------------------------- item 2: update/delete
class UpdateTaskTests(Base):
    def setUp(self):
        super().setUp()
        self.t, _ = self.db.add("Laporan jaringan", "2026-11-13", "jaringan", "bab 1-3")

    def test_ubah_tenggat(self):
        out = tasks.update_task(self.ctx, {"title": "laporan jaringan", "due_date": "2026-11-16"})
        self.assertIn("tenggat: 2026-11-13 -> 2026-11-16", out)
        self.assertEqual(self.db.get(self.t["id"])["due_date"], "2026-11-16")
        self.assertEqual(self.db.get(self.t["id"])["notes"], "bab 1-3")      # field lain utuh

    def test_ubah_judul_dan_course_kanonik(self):
        tasks.update_task(self.ctx, {"task_id": self.t["id"], "new_title": "Laporan praktikum 3",
                                     "course": "Basis Data"})
        row = self.db.get(self.t["id"])
        self.assertEqual((row["title"], row["course"]), ("Laporan praktikum 3", "basis-data"))

    def test_hapus_tenggat(self):
        out = tasks.update_task(self.ctx, {"title": "laporan", "clear_due_date": True})
        self.assertIn("(kosong)", out)
        self.assertIsNone(self.db.get(self.t["id"])["due_date"])

    def test_due_date_dan_clear_bersamaan_ditolak(self):
        with self.assertRaises(ToolError):
            tasks.update_task(self.ctx, {"title": "laporan", "due_date": "2026-11-20", "clear_due_date": True})

    def test_tanggal_lampau_dan_format_salah_ditolak(self):
        for bad in ("2024-11-01", "besok", "2031-01-01"):
            with self.assertRaises(ToolError, msg=bad):
                tasks.update_task(self.ctx, {"title": "laporan", "due_date": bad})
        self.assertEqual(self.db.get(self.t["id"])["due_date"], "2026-11-13")

    def test_tanpa_perubahan_ditolak(self):
        with self.assertRaises(ToolError):
            tasks.update_task(self.ctx, {"title": "laporan"})

    def test_nilai_sama_tidak_dianggap_perubahan(self):
        out = tasks.update_task(self.ctx, {"title": "laporan", "due_date": "2026-11-13"})
        self.assertIn("Tidak ada yang berubah", out)

    def test_konflik_dengan_tugas_lain_ditolak(self):
        self.db.add("Laporan jaringan", "2026-11-20")
        with self.assertRaises(ToolError) as cm:
            tasks.update_task(self.ctx, {"task_id": self.t["id"], "due_date": "2026-11-20"})
        self.assertIn("Sudah ada tugas lain", str(cm.exception))
        self.assertEqual(self.db.get(self.t["id"])["due_date"], "2026-11-13")

    def test_kandidat_ganda_tidak_mengubah_apa_pun(self):
        self.db.add("Laporan basis data", "2026-11-14")
        out = tasks.update_task(self.ctx, {"title": "laporan", "due_date": "2026-11-18"})
        self.assertIn("beberapa tugas", out)
        self.assertEqual(self.db.get(self.t["id"])["due_date"], "2026-11-13")

    def test_buka_lagi_tugas_selesai(self):
        self.db.complete(self.t["id"])
        out = tasks.update_task(self.ctx, {"title": "laporan jaringan", "reopen": True})
        self.assertIn("selesai -> belum selesai", out)
        row = self.db.get(self.t["id"])
        self.assertEqual((row["status"], row["done_at"]), ("pending", None))

    def test_buka_lagi_bentrok_dengan_pending_kembar(self):
        self.db.complete(self.t["id"])
        self.db.add("Laporan jaringan", "2026-11-13")             # pengganti yang masih pending
        with self.assertRaises(ToolError):
            tasks.update_task(self.ctx, {"task_id": self.t["id"], "reopen": True})
        self.assertEqual(self.db.get(self.t["id"])["status"], "done")

    def test_id_tidak_ada(self):
        with self.assertRaises(ToolError):
            tasks.update_task(self.ctx, {"task_id": 999, "notes": "x"})


class DeleteTaskTests(Base):
    def test_hapus_by_title(self):
        t, _ = self.db.add("Kuis 2", "2026-11-12")
        out = tasks.delete_task(self.ctx, {"title": "kuis 2"})
        self.assertIn("tempat sampah", out)
        self.assertIsNone(self.db.get(t["id"]))

    def test_kandidat_ganda_tidak_menghapus(self):
        self.db.add("Kuis 1", "2026-11-12")
        self.db.add("Kuis 2", "2026-11-19")
        out = tasks.delete_task(self.ctx, {"title": "kuis"})
        self.assertIn("beberapa tugas", out)
        self.assertEqual(len(self.db.list("all")), 2)

    def test_tidak_ada_dan_tanpa_argumen(self):
        for args in ({"title": "tidak ada"}, {"task_id": 42}, {}):
            with self.assertRaises(ToolError, msg=str(args)):
                tasks.delete_task(self.ctx, args)

    def test_hapus_tugas_selesai_bila_tidak_ada_yang_pending(self):
        t, _ = self.db.add("Tugas lama", None)
        self.db.complete(t["id"])
        tasks.delete_task(self.ctx, {"title": "tugas lama"})
        self.assertIsNone(self.db.get(t["id"]))


class RegistryTests(Base):
    def test_sembilan_tool_terdaftar_dan_skema_valid(self):
        reg = build_registry(self.ctx)
        self.assertEqual(len(reg.names), 15)
        for need in ("update_task", "delete_task", "list_notes"):
            self.assertIn(need, reg.names)
        for spec in reg.specs():
            props = spec.parameters.get("properties", {})
            self.assertTrue(spec.description.strip())
            for r in spec.parameters.get("required", []):
                self.assertIn(r, props, f"{spec.name}: required '{r}' tidak ada di properties")

    def test_lewat_registry_bool_string_dan_log(self):
        reg = build_registry(self.ctx)
        t, _ = self.db.add("Laporan jaringan", "2026-11-13")
        res = reg.execute(ToolCall(name="update_task", args={"title": "laporan", "clear_due_date": "true"}))
        self.assertFalse(res.is_error, res.content)
        self.assertIsNone(self.db.get(t["id"])["due_date"])
        res = reg.execute(ToolCall(name="delete_task", args={"task_id": "1"}))     # id sebagai string
        self.assertFalse(res.is_error, res.content)
        self.assertEqual(len((self.dir / "tools.jsonl").read_text().splitlines()), 2)

    def test_tool_tulis_baru_terdaftar_di_write_tools(self):
        self.assertTrue({"update_task", "delete_task"} <= WRITE_TOOLS)


# ---------------------------------------------------------------- item 2: list_notes
class ListNotesTests(Base):
    def write(self, name, title, course, created, body, tags=""):
        self.ctx.notes_dir.mkdir(parents=True, exist_ok=True)
        head = [f"title: {title}", f"course: {course}", f"created: {created}"]
        if tags:
            head.append(f"tags: {tags}")
        p = self.ctx.notes_dir / name
        p.write_text("---\n" + "\n".join(head) + "\n---\n" + body + "\n", encoding="utf-8")
        return p

    def test_kosong(self):
        self.assertEqual(notes.list_notes(self.ctx, {}), "Belum ada catatan.")

    def test_urut_terbaru_filter_course_dan_limit(self):
        self.write("a.md", "UTS Basis Data", "basis-data", "2026-11-01", "UTS bab 1-4 tanggal 2026-12-12", "uts")
        self.write("b.md", "Rute statis", "jaringan", "2026-11-05", "Gunakan ip route.")
        self.write("c.md", "Join", "basis-data", "2026-11-07", "INNER JOIN vs LEFT JOIN\n\nbeda.")
        out = notes.list_notes(self.ctx, {})
        self.assertTrue(out.startswith("3 catatan:"))
        self.assertLess(out.index("Join"), out.index("Rute statis"))
        self.assertLess(out.index("Rute statis"), out.index("UTS Basis Data"))
        self.assertIn("tag: uts", out)
        db_only = notes.list_notes(self.ctx, {"course": "Basis Data"})
        self.assertIn("2 catatan", db_only)
        self.assertNotIn("Rute statis", db_only)
        self.assertIn("1 terbaru ditampilkan", notes.list_notes(self.ctx, {"limit": 1}))
        self.assertIn("Tidak ada catatan untuk mata kuliah 'kimia'",
                      notes.list_notes(self.ctx, {"course": "kimia"}))

    def test_penanda_belum_terindeks_dan_sudah(self):
        p = self.write("a.md", "Catatan A", "basis-data", "2026-11-01", "isi a")
        self.assertIn("BELUM ter-index", notes.list_notes(self.ctx, {}))
        ref = FileRef(path=p, rel_path=p.name, course="umum", source_type="catatan", ext=".md",
                      size=1, sha256=sha256_file(p))
        self.store.hashes[ref.doc_id] = ref.sha256
        self.assertNotIn("BELUM", notes.list_notes(self.ctx, {}))
        p.write_text(p.read_text() + "diedit\n", encoding="utf-8")      # file berubah setelah di-index
        self.assertIn("BELUM ter-index", notes.list_notes(self.ctx, {}))

    def test_catatan_tanpa_frontmatter(self):
        self.ctx.notes_dir.mkdir(parents=True)
        (self.ctx.notes_dir / "manual.md").write_text("Catatan tulisan tangan tanpa header.", encoding="utf-8")
        out = notes.list_notes(self.ctx, {})
        self.assertIn("manual", out)
        self.assertIn("umum", out)


# ---------------------------------------------------------------- item 4: ringkasan harian
class SummaryTests(Base):
    def test_tanpa_tugas(self):
        self.assertIn("Tidak ada tugas yang belum selesai", build_daily_summary(self.db))

    def test_bagian_dan_hitungan(self):
        self.db.add("Telat", "2026-11-05")
        self.db.add("Hari ini", "2026-11-09")
        self.db.add("Lusa", "2026-11-11")
        self.db.add("Batas 3 hari", "2026-11-12")
        self.db.add("Jauh", "2026-12-20")
        self.db.add("Tanpa tenggat", None)
        done, _ = self.db.add("Sudah selesai", "2026-11-09")
        self.db.complete(done["id"])
        out = build_daily_summary(self.db)
        self.assertTrue(out.startswith("Ringkasan Senin, 9 November 2026"))
        self.assertIn("TERLAMBAT (1):", out)
        self.assertIn("Dalam 3 hari ke depan (3):", out)
        self.assertNotIn("Sudah selesai", out)
        self.assertNotIn("Jauh |", out)
        self.assertIn("1 bertenggat lebih jauh, 1 tanpa tenggat", out)
        self.assertLess(out.index("Telat"), out.index("Hari ini"))

    def test_tidak_ada_yang_mendesak(self):
        self.db.add("Jauh", "2026-12-20")
        out = build_daily_summary(self.db)
        self.assertIn("Tidak ada tugas mendesak", out)
        self.assertNotIn("TERLAMBAT", out)

    def test_data_tenggat_rusak_tidak_menjatuhkan_ringkasan(self):
        import sqlite3
        from contextlib import closing
        self.db.add("Normal", "2026-11-10")
        with closing(sqlite3.connect(self.db.path)) as c, c:      # `with conn` saja tidak menutup koneksi
            c.execute("INSERT INTO tasks(title, due_date, created_at) VALUES ('Rusak', 'bukan-tanggal', 'x')")
        self.assertIn("1 tanpa tenggat", build_daily_summary(self.db))


# ---------------------------------------------------------------- bug 2: dedupe agent
class Resp:
    def __init__(self, text="", calls=None):
        self.text, self.tool_calls = text, calls or []

    def to_message(self):
        return Message.assistant(self.text, self.tool_calls)


class AgentDedupeTests(unittest.TestCase):
    def run_agent(self, script_calls):
        class Reg:
            def __init__(s):
                s.state, s.log = ["tugas A"], []

            def specs(s):
                return []

            def execute(s, call):
                s.log.append(call.name)
                if call.name == "list_tasks":
                    return Message.tool(call, "daftar: " + ", ".join(s.state))
                s.state.clear()
                return Message.tool(call, "ok")
        reg, seen = Reg(), []
        script = iter([*(Resp(calls=[c]) for c in script_calls), Resp("selesai")])

        def chat(msgs, specs):
            seen.append(msgs[-1].content if msgs[-1].role == "tool" else "")
            return next(script)
        Agent(reg, chat).ask("x")
        return reg, seen

    def test_baca_setelah_tulis_dijalankan_ulang(self):
        reg, seen = self.run_agent([ToolCall("list_tasks", {}), ToolCall("update_task", {"title": "A"}),
                                    ToolCall("list_tasks", {})])
        self.assertEqual(reg.log, ["list_tasks", "update_task", "list_tasks"])
        self.assertIn("daftar: ", seen[-1])
        self.assertNotIn("tugas A", seen[-1])                 # hasil segar, bukan salinan basi

    def test_baca_identik_tanpa_tulis_tetap_diblokir(self):
        reg, seen = self.run_agent([ToolCall("list_tasks", {}), ToolCall("list_tasks", {})])
        self.assertEqual(reg.log, ["list_tasks"])
        self.assertIn("Panggilan identik", seen[-1])

    def test_tulis_identik_tidak_pernah_dijalankan_dua_kali(self):
        reg, _ = self.run_agent([ToolCall("delete_task", {"title": "A"}), ToolCall("list_tasks", {}),
                                 ToolCall("delete_task", {"title": "A"})])
        self.assertEqual(reg.log.count("delete_task"), 1)


# ---------------------------------------------------------------- item 3: ipynb
def notebook(cells, lang="python"):
    return json.dumps({"nbformat": 4, "metadata": {"language_info": {"name": lang}}, "cells": cells})


class IpynbTests(Base):
    def read(self, text, name="nb.ipynb"):
        p = self.dir / name
        p.write_text(text, encoding="utf-8")
        ref = FileRef(path=p, rel_path=name, course="umum", source_type="materi", ext=".ipynb",
                      size=p.stat().st_size, sha256=sha256_file(p))
        return read_document(ref)

    def test_struktur_judul_kode_dan_keluaran(self):
        doc = self.read(notebook([
            {"cell_type": "markdown", "source": ["# Praktikum\n", "Intro."]},
            {"cell_type": "code", "source": ["# komentar\n", "print('hi')"],
             "outputs": [{"output_type": "stream", "name": "stdout", "text": ["hi\n"]}]},
            {"cell_type": "markdown", "source": "## Bagian 2\nIsi."},
            {"cell_type": "raw", "source": "abaikan"},
        ]))
        self.assertTrue(doc.ok, doc.error)
        self.assertEqual(doc.page_label, "sel")
        self.assertEqual([(s.heading, s.page) for s in doc.segments],
                         [("Praktikum", 1), ("Praktikum > Bagian 2", 3)])
        first = doc.segments[0].text
        self.assertIn("```python\n# komentar\nprint('hi')\n```", first)
        self.assertIn("Keluaran:\n```\nhi\n```", first)
        self.assertNotIn("abaikan", " ".join(s.text for s in doc.segments))

    def test_gambar_repr_ansi_dan_pemangkasan(self):
        doc = self.read(notebook([
            {"cell_type": "code", "source": "plt.plot()", "outputs": [
                {"output_type": "display_data", "data": {"text/plain": "<Figure size 640x480 with 1 Axes>",
                                                         "image/png": "QUFB" * 9000}}]},
            {"cell_type": "code", "source": "1/0", "outputs": [
                {"output_type": "error", "ename": "ZeroDivisionError", "evalue": "division by zero",
                 "traceback": ["\u001b[31mboom\u001b[0m"]}]},
            {"cell_type": "code", "source": "print('x'*2000)", "outputs": [
                {"output_type": "stream", "name": "stdout", "text": "\u001b[1mx\u001b[0m" * 800}]},
        ]))
        text = "\n".join(s.text for s in doc.segments)
        self.assertNotIn("Figure", text)
        self.assertNotIn("QUFB", text)
        self.assertNotIn("\u001b", text)
        self.assertIn("ZeroDivisionError: division by zero", text)
        self.assertLess(len(text), 400 + 600)                  # keluaran 800 karakter dipangkas

    def test_tanpa_keluaran_bila_dimatikan(self):
        with mock.patch.object(config, "IPYNB_INCLUDE_OUTPUTS", False):
            doc = self.read(notebook([{"cell_type": "code", "source": "1+1", "outputs": [
                {"output_type": "execute_result", "data": {"text/plain": "2"}}]}]))
        self.assertNotIn("Keluaran", doc.segments[0].text)

    def test_nbformat_lama_dan_json_rusak_menjadi_error(self):
        old = self.read(json.dumps({"nbformat": 3, "worksheets": []}))
        self.assertFalse(old.ok)
        self.assertIn("nbformat", old.error)
        self.assertFalse(self.read("{bukan json").ok)

    def test_notebook_kosong_tidak_crash(self):
        doc = self.read(notebook([]))
        self.assertEqual(doc.segments, [])


# ---------------------------------------------------------------- item 3: konversi .ppt/.doc
class ConvertTests(Base):
    def setUp(self):
        super().setUp()
        self.mat = self.dir / "materi"
        (self.mat / "mk").mkdir(parents=True)
        self.cache_patch = mock.patch.object(config, "CONVERT_CACHE_DIR", self.dir / "cache")
        self.cache_patch.start()

    def tearDown(self):
        self.cache_patch.stop()
        super().tearDown()

    def test_tanpa_libreoffice_dilewati_dengan_petunjuk(self):
        (self.mat / "mk" / "slide.ppt").write_bytes(b"x")
        (self.mat / "mk" / "ok.md").write_text("# Judul\nIsi", encoding="utf-8")
        with mock.patch.object(convert, "find_soffice", return_value=None):
            refs, skipped = discover_files(self.mat, "materi")
        self.assertEqual([r.rel_path for r in refs], ["mk/ok.md"])
        self.assertEqual(len(skipped), 1)
        self.assertIn("LibreOffice", skipped[0][1])
        from rag.indexer import SyncReport
        rep = SyncReport()
        rep.skipped = [(f"materi/{p}", w) for p, w in skipped]
        self.assertIn("simpan ulang sebagai .pptx", "\n".join(rep.lines()))

    def test_konversi_gagal_menjadi_error_dokumen_bukan_exception(self):
        p = self.mat / "mk" / "rusak.doc"
        p.write_bytes(b"bukan dokumen")
        ref = FileRef(path=p, rel_path="mk/rusak.doc", course="mk", source_type="materi", ext=".doc",
                      size=13, sha256=sha256_file(p))
        with mock.patch.object(convert, "find_soffice", return_value="/nonexistent/soffice"):
            doc = read_document(ref)
        self.assertFalse(doc.ok)
        self.assertTrue(doc.error)

    def test_timeout_menjadi_convert_error(self):
        import subprocess
        p = self.mat / "mk" / "lambat.ppt"
        p.write_bytes(b"x")
        with mock.patch.object(convert, "find_soffice", return_value="soffice"), \
                mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("soffice", 1)):
            with self.assertRaises(convert.ConvertError) as cm:
                convert.convert_legacy(p, "ab" * 32, ".pptx")
        self.assertIn("batas", str(cm.exception))
        self.assertEqual(list((self.dir / "cache").glob("*.part")), [])     # tidak ada sisa separuh jadi

    @unittest.skipUnless(convert.find_soffice(), "LibreOffice tidak terpasang")
    def test_konversi_nyata_dan_cache(self):
        import subprocess
        try:
            from docx import Document as DocxDocument
        except ImportError:
            self.skipTest("python-docx tidak terpasang")
        src = self.dir / "baru.docx"
        d = DocxDocument()
        d.add_heading("Normalisasi", 1)
        d.add_paragraph("Bentuk normal ketiga menghilangkan ketergantungan transitif.")
        d.save(src)
        subprocess.run([convert.find_soffice(), "--headless", "--convert-to", "doc", "--outdir",
                        str(self.mat / "mk"), str(src)], capture_output=True, timeout=240, check=True)
        (self.mat / "mk" / "baru.doc").rename(self.mat / "mk" / "Catatan Lama.doc")   # nama dengan spasi
        refs, skipped = discover_files(self.mat, "materi")
        self.assertEqual(skipped, [])
        ref = refs[0]
        self.assertEqual(ref.ext, ".doc")
        doc = read_document(ref)
        self.assertTrue(doc.ok, doc.error)
        self.assertIn("transitif", " ".join(s.text for s in doc.segments))
        self.assertTrue(any("dikonversi" in w for w in doc.warnings))
        self.assertEqual(len(list((self.dir / "cache").glob("*.docx"))), 1)
        with mock.patch.object(convert, "find_soffice", return_value=None):      # cache dipakai, tanpa LO
            again = read_document(ref)
        self.assertTrue(again.ok, again.error)
        # hasil konversi tidak ikut ter-index sebagai dokumen kedua
        refs2, _ = discover_files(self.mat, "materi")
        self.assertEqual(len(refs2), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
