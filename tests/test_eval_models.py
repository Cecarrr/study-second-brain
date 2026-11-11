"""Uji offline eval_models.py dan eval_scenarios.py. 0 request API, tanpa jaringan.

  python -m unittest tests.test_eval_models -v      (dari akar repo)
"""
import csv
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import eval_models as em  # noqa: E402
import eval_scenarios as es  # noqa: E402
from providers.base import BaseProvider, RateLimitError, ServerError  # noqa: E402


class FailingProvider(BaseProvider):
    name = "fail"

    def __init__(self, exc):
        super().__init__("fail-1")
        self.exc, self.n = exc, 0

    def chat(self, messages, tools=None):
        self.n += 1
        raise self.exc


def failing_factory(exc):
    def f(model, sc=None):
        return FailingProvider(exc), None
    return f


def run_mock(models=("mock-ideal",), scenarios=None, limit=None):
    budget = em.Budget(limit)
    reps = [em.eval_model(m, scenarios or es.SCENARIOS, em.make_mock_provider, lambda m: em.NullPacer(), budget)
            for m in models]
    return reps, budget


class EnvCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.env = em.Env(Path(self.tmp.name))

    def tearDown(self):
        self.env.close()
        self.tmp.cleanup()


# ---------------------------------------------------------------- daftar skenario
class ScenarioListTests(unittest.TestCase):
    def test_jumlah_dan_id_unik(self):
        ids = [s.id for s in es.SCENARIOS]
        self.assertTrue(15 <= len(ids) <= 25, len(ids))
        self.assertEqual(len(ids), len(set(ids)))

    def test_semua_punya_script_untuk_mode_mock(self):
        for s in es.SCENARIOS:
            self.assertTrue(s.script, s.id)

    def test_cakupan_yang_diminta_handoff(self):
        ids = {s.id for s in es.SCENARIOS}
        for need in ("catat+tenggat", "tugas-jumat", "tanpa-duplikat", "selesai-judul", "ubah-tenggat",
                     "hapus-tugas", "daftar-catatan", "materi-id", "materi-en", "kuis-tanpa-kunci",
                     "luar-materi-id"):
            self.assertIn(need, ids)


# ---------------------------------------------------------------- lingkungan sementara
class EnvTests(EnvCase):
    def test_materi_terindeks_dan_hari_ini_tetap(self):
        self.assertEqual(set(self.env.store.courses()), {"basis-data", "jaringan"})
        self.assertEqual(self.env.ctx.today(), es.TODAY)
        res = self.env.ctx.retriever.search("bobot nilai UAS")
        self.assertTrue(any("silabus-penilaian.md" in h.source for h in res.hits))

    def test_changed_mendeteksi_perubahan_tugas_dan_catatan(self):
        es.seed_tasks(self.env)
        es.seed_notes(self.env)
        self.env.freeze()
        self.assertFalse(self.env.changed())
        self.env.tasks.add("Baru", None)
        self.assertTrue(self.env.changed())
        self.env.freeze()
        self.env.add_note("Isi catatan baru yang cukup panjang.", "Baru")
        self.assertTrue(self.env.changed())

    def test_tidak_menyentuh_data_asli(self):
        watched = [config.USAGE_PATH, config.DB_PATH, config.VECTOR_STORE_PATH]
        before = {p: (p.stat().st_mtime_ns if p.exists() else None) for p in watched}
        n_notes = len(list(config.NOTES_DIR.glob("*"))) if config.NOTES_DIR.exists() else 0
        run_mock(scenarios=[es.BY_ID["catat+tenggat"], es.BY_ID["kuis-tanpa-kunci"]])
        after = {p: (p.stat().st_mtime_ns if p.exists() else None) for p in watched}
        self.assertEqual(before, after)
        self.assertEqual(n_notes, len(list(config.NOTES_DIR.glob("*"))) if config.NOTES_DIR.exists() else 0)


# ---------------------------------------------------------------- pemeriksa
class CheckerTests(EnvCase):
    def run_of(self, calls=(), answer="ok"):
        return es.Run(calls=[es.Call(*c) for c in calls], answer=answer)

    def judge(self, sid, run=None):
        sc = es.BY_ID[sid]
        if sc.seed:
            sc.seed(self.env)
        self.env.freeze()
        return sc, run or self.run_of()

    def test_note_task_lulus_dan_gagal(self):
        sc, run = self.judge("catat+tenggat")
        self.assertIn("harusnya 1 catatan", es.c_note_task(self.env, run))
        self.env.add_note("UTS Basis Data bab 1-4, 2026-12-12.", "UTS")
        self.assertIn("harusnya 1 tugas", es.c_note_task(self.env, run))
        self.env.tasks.add("UTS Basis Data", "2026-12-13")
        self.assertIn("due_date='2026-12-13'", es.c_note_task(self.env, run))
        self.env.tasks.update(1, due_date="2026-12-12")
        self.assertIsNone(es.c_note_task(self.env, run))

    def test_tugas_tanpa_tanggal_menolak_tanggal_karangan(self):
        self.env.tasks.add("Baca bab 5", "2026-11-13")
        self.assertIn("due_date", es.c_task_nodate(self.env, None))

    def test_tanggal_relatif_jumat_dan_lusa(self):
        self.env.tasks.add("Laporan jaringan", "2026-11-13")
        self.assertIsNone(es.c_task_friday(self.env, None))
        self.assertIn("judul", es.c_task_lusa(self.env, None))
        self.env.tasks.update(1, title="Praktikum jaringan")
        self.assertIn("due_date='2026-11-13'", es.c_task_lusa(self.env, None))
        self.env.tasks.update(1, due_date="2026-11-11")
        self.assertIsNone(es.c_task_lusa(self.env, None))

    def test_duplikat_terdeteksi(self):
        es.seed_tasks(self.env)
        self.assertIsNone(es.c_dup(self.env, None))
        self.env.tasks.add("Laporan basis data", "2026-11-11")
        self.assertIn("harusnya 1 tugas pending", es.c_dup(self.env, None))

    def test_selesai_judul(self):
        es.seed_tasks(self.env)
        self.assertIn("belum selesai", es.c_complete_title(self.env, None))
        self.env.tasks.complete(self.env.tasks.find_pending("kuis jaringan")[0]["id"])
        self.assertIsNone(es.c_complete_title(self.env, None))
        self.env.tasks.complete(self.env.tasks.find_pending("esai")[0]["id"])
        self.assertIn("tugas lain ikut berubah", es.c_complete_title(self.env, None))

    def test_ambigu_tidak_boleh_mengubah(self):
        es.seed_two_quizzes(self.env)
        self.assertIsNone(es.c_complete_ambig(self.env, None))
        self.env.tasks.complete(self.env.tasks.find_pending("kuis 2")[0]["id"])
        self.assertIn("ambigu", es.c_complete_ambig(self.env, None))

    def test_dikumpulkan_bukan_dihapus(self):
        es.seed_tasks(self.env)
        t = self.env.tasks.find_pending("laporan basis data")[0]
        self.env.tasks.delete(t["id"])
        self.assertIn("tugas hilang", es.c_submitted(self.env, None))

    def test_hapus_tugas_harus_masuk_sampah(self):
        es.seed_tasks(self.env)
        self.assertIn("masih ada", es.c_delete_task(self.env, None))
        self.env.tasks.delete(self.env.tasks.find_pending("esai")[0]["id"])
        self.assertIsNone(es.c_delete_task(self.env, None))

    def test_catatan_tambah_bukan_ganti(self):
        es.seed_notes(self.env)
        f = next(p for p in self.env.ctx.notes_dir.glob("*.md") if "uts" in p.name)
        f.write_text("---\ntitle: UTS Basis Data\n---\nHanya bab 5.\n", encoding="utf-8")
        self.assertIn("isi lama hilang", es.c_update_note(self.env, None))
        f.write_text("---\ntitle: UTS Basis Data\n---\nbab 1 sampai 4.\n\nBab 5 ikut.\n", encoding="utf-8")
        self.assertIsNone(es.c_update_note(self.env, None))

    def test_pencarian_memeriksa_query_dan_sumber(self):
        ok = es.Call("search_knowledge", {"query": "bobot UAS"}, True, "[1] ... basis-data/silabus-penilaian.md")
        self.assertIsNone(es._search_ok(es.Run(calls=[ok]), "silabus-penilaian.md", ("uas",)))
        miss = es.Call("search_knowledge", {"query": "bobot UAS"}, True, "Tidak ditemukan bagian materi")
        self.assertIn("tidak memuat", es._search_ok(es.Run(calls=[miss]), "silabus-penilaian.md", ("uas",)))
        self.assertIn("query tidak memuat", es._search_ok(es.Run(calls=[ok]), "silabus-penilaian.md", ("osi",)))
        self.assertIn("tidak memanggil", es._search_ok(es.Run(), "x.md", ("a",)))

    def test_kunci_kuis_tidak_boleh_bocor(self):
        out = ("Kuis: x (2 soal).\n\n1. Soal satu?\n   A. a\n\n2. Soal dua?\n   A. a\n\n"
               "=== KUNCI JAWABAN (jangan tampilkan) ===\n"
               "1. B - Karena ketergantungan transitif dihapus (sumber: n.md)\n2. C - Atomik artinya tak terbagi lagi")
        call = es.Call("make_quiz", {"topic": "normalisasi", "num_questions": 2}, True, out)
        good = es.Run(calls=[call], answer="Berikut soalnya:\n1. Soal satu?\n2. Soal dua?")
        self.assertIsNone(es.leaked_key(good))
        for leak in ("1. Soal satu?\n2. Soal dua?\nKUNCI JAWABAN: 1 B",
                     "1. Soal satu?\n2. Soal dua?\n\n1. B - hore",
                     "1. Soal satu?\n2. Soal dua?\nKarena ketergantungan transitif dihapus"):
            self.assertIsNotNone(es.leaked_key(es.Run(calls=[call], answer=leak)), leak)
        self.assertIn("tidak ditampilkan", es.leaked_key(es.Run(calls=[call], answer="Siap, kuis dibuat.")))

    def test_judge_tool_terlarang_hilang_dan_data_berubah(self):
        sc, run = self.judge("tugas-jumat", self.run_of([("add_note", {}, True, "")]))
        self.assertIn("tidak memanggil add_task", em.judge(sc, self.env, run))
        run = self.run_of([("add_task", {}, True, ""), ("add_note", {}, True, "")])
        self.assertIn("terlarang", em.judge(sc, self.env, run))
        sc, run = self.judge("daftar-tugas", self.run_of([("list_tasks", {}, True, "")]))
        self.assertIsNone(em.judge(sc, self.env, run))
        self.env.tasks.add("Diam-diam", None)
        self.assertIn("data berubah", em.judge(sc, self.env, run))
        wrote = self.run_of([("list_tasks", {}, True, ""), ("delete_task", {}, True, "")])
        self.assertIn("tool tulis", em.judge(sc, self.env, wrote))

    def test_judge_tanpa_tool(self):
        sc, run = self.judge("luar-materi-id", self.run_of([("search_knowledge", {}, True, "")]))
        self.assertIn("tanpa perlu", em.judge(sc, self.env, run))
        self.assertIsNone(em.judge(sc, self.env, self.run_of()))

    def test_judge_expect_any_dan_placeholder_agent(self):
        sc, run = self.judge("selesai-ambigu")
        self.assertIn("tidak memanggil satu pun", em.judge(sc, self.env, run))
        sc, _ = self.judge("daftar-catatan")
        run = self.run_of([("list_notes", {}, True, "")], answer="Aku mencapai batas langkah untuk satu pesan.")
        self.assertIn("batas langkah", em.judge(sc, self.env, run))


# ---------------------------------------------------------------- penjalanan mock
class MockRunTests(unittest.TestCase):
    def test_model_ideal_lulus_semua_dan_malas_hanya_tanpa_tool(self):
        (ideal, lazy), budget = run_mock(("mock-ideal", "mock-lazy"))
        self.assertEqual([r.scenario for r in ideal.results if r.status != em.PASS], [])
        self.assertEqual(ideal.pass_rate, 1.0)
        passed = {r.scenario for r in lazy.results if r.status == em.PASS}
        self.assertEqual(passed, {"luar-materi-id", "luar-materi-en"})
        self.assertEqual(budget.used, ideal.total_requests + lazy.total_requests)

    def test_error_tool_dihitung(self):
        (ideal,), _ = run_mock(scenarios=[es.BY_ID["tanggal-lampau"]])
        r = ideal.results[0]
        # tanggal lampau kini dimajukan otomatis (dengan catatan), jadi panggilan pertama tidak error
        self.assertEqual((r.status, r.tool_calls, r.tool_errors), (em.PASS, 2, 0))
        self.assertEqual(ideal.tool_error_rate, 0.0)

    def test_anggaran_menghentikan_run(self):
        (ideal,), budget = run_mock(limit=5)
        self.assertEqual(budget.used, 5)
        statuses = [r.status for r in ideal.results]
        self.assertEqual(statuses.count(em.PASS), 2)             # 2 skenario x 2 request, ke-5 terpotong
        self.assertTrue(all(s == em.SKIP for s in statuses[2:]))
        self.assertIn("anggaran", ideal.results[2].reason)
        self.assertEqual(ideal.count(em.FAIL), 0)
        self.assertEqual(len(ideal.ran), 2)

    def test_anggaran_lintas_model(self):
        reps, budget = run_mock(("mock-ideal", "mock-lazy"), limit=44)
        self.assertEqual(reps[0].count(em.PASS), 22)
        self.assertEqual(reps[1].count(em.SKIP), 22)


class ErrorHandlingTests(unittest.TestCase):
    def go(self, exc, scenarios=None):
        return em.eval_model("fail-1", scenarios or es.SCENARIOS[:4], failing_factory(exc),
                             lambda m: em.NullPacer(), em.Budget())

    def test_kuota_harian_habis_melewati_sisa_tanpa_gagal(self):
        rep = self.go(RateLimitError("habis", provider="fail", retryable=False))
        self.assertEqual([r.status for r in rep.results], [em.SKIP] * 4)
        self.assertEqual(rep.results[0].reason, "kuota harian habis")
        self.assertEqual(rep.count(em.FAIL), 0)
        self.assertIsNone(rep.pass_rate)

    def test_error_sementara_ditandai_error_dan_lanjut(self):
        rep = self.go(ServerError("503 sibuk", provider="fail"))
        # 3 error beruntun -> model dihentikan, sisa skenario dilewati (hemat request)
        self.assertEqual([r.status for r in rep.results], [em.ERROR] * 3 + [em.SKIP])
        self.assertIn("ServerError", rep.results[0].reason)
        self.assertIn("beruntun", rep.results[3].reason)
        self.assertEqual(rep.count(em.FAIL), 0)

    def test_jeda_antar_request(self):
        t = [1000.0]
        sleeps = []
        p = em.Pacer("gemini-3.1-flash-lite", sleep=sleeps.append, clock=lambda: t[0])
        p.wait()                       # request pertama tidak menunggu
        self.assertEqual(sleeps, [])
        p.wait()                       # kedua, tanpa waktu berlalu: menunggu satu jeda penuh
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], 60 / 15 * 1.1, places=3)


# ---------------------------------------------------------------- keluaran dan CLI
class OutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reps, cls.budget = run_mock(("mock-ideal", "mock-lazy"))

    def test_markdown(self):
        md = em.format_md(self.reps, es.SCENARIOS, mock=True, budget=self.budget)
        self.assertIn("## Ringkasan", md)
        self.assertIn("| Skenario | mock-ideal | mock-lazy |", md)
        self.assertEqual(md.count("| catat+tenggat |"), 1)
        self.assertIn("MOCK", md)
        self.assertIn("## Detail yang tidak lulus", md)

    def test_csv(self):
        rows = list(csv.DictReader(io.StringIO(em.format_csv(self.reps, es.SCENARIOS))))
        self.assertEqual(len(rows), 2 * len(es.SCENARIOS))
        self.assertEqual({r["status"] for r in rows if r["model"] == "mock-ideal"}, {em.PASS})
        self.assertIn("tidak memanggil", next(r["reason"] for r in rows if r["model"] == "mock-lazy" and r["status"] == em.FAIL))

    def test_teks_dan_urutan_ringkasan(self):
        txt = em.format_text(self.reps, es.SCENARIOS)
        self.assertLess(txt.index("mock-ideal  "), txt.index("mock-lazy  ", txt.index("Ringkasan")))
        self.assertIn("100%", txt)

    def test_pipa_di_alasan_di_escape(self):
        rep = em.ModelReport("m", [em.Result("s", em.FAIL, "a|b")])
        sc = es.Scenario("s", "x")
        self.assertIn("a\\|b", em.format_md([rep], [sc]))


class CliTests(unittest.TestCase):
    def cli(self, *argv):
        lines = []
        code = em.main(list(argv), out=lines.append)
        return code, "\n".join(lines)

    def test_list(self):
        code, text = self.cli("--list")
        self.assertEqual(code, 0)
        self.assertIn("tugas-jumat", text)

    def test_mock_dengan_only_dan_out(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            f = Path(d) / "hasil.md"
            code, text = self.cli("--mock", "--only", "tugas-jumat,luar-materi-id", "--out", str(f))
            self.assertEqual(code, 0)
            self.assertIn("mock-ideal: lulus 2/2", text)
            self.assertIn("| tugas-jumat | lulus | GAGAL |", f.read_text(encoding="utf-8"))

    def test_max_requests_lewat_cli(self):
        code, text = self.cli("--mock", "--models", "mock-ideal", "--max-requests", "3")
        self.assertIn("lewat", text)
        self.assertIn("anggaran", text)

    def test_argumen_salah(self):
        for argv in (["--mock", "--only", "tidak-ada"], ["--mock", "--out", "x.txt"],
                     ["--mock", "gemini-x"], ["--mock", "--max-requests", "0"]):
            with self.assertRaises(SystemExit) as cm, _quiet():
                self.cli(*argv)
            self.assertEqual(cm.exception.code, 2, argv)


class _quiet:
    """Redam stderr argparse selama uji."""

    def __enter__(self):
        self._old, sys.stderr = sys.stderr, io.StringIO()

    def __exit__(self, *a):
        sys.stderr = self._old
        return False


if __name__ == "__main__":
    unittest.main()
