"""Uji offline subset tool dinamis (tools/select.py + integrasi Agent.ask). 0 request API.

  python -m unittest tests.test_tool_select -v      (dari akar repo)
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import eval_models as em  # noqa: E402
import eval_scenarios as es  # noqa: E402
from agent import Agent  # noqa: E402
from providers.base import ToolSpec  # noqa: E402
from providers.mock import MockProvider, call  # noqa: E402
from tools import select as sel  # noqa: E402
from tools.common import ToolContext  # noqa: E402
from tools.registry import build_registry  # noqa: E402

REG = build_registry(ToolContext(retriever=None, store=None, embedder=None, tasks=None))
ALL = {s.name for s in REG.specs()}
AVAIL = sel.available_groups(ALL)


def groups(msg, carry=()):
    g = sel.select_groups(msg, AVAIL, carry)
    return None if g is None else set(g)


def names(msg, last=None, reg=REG):
    return {s.name for s in sel.select_specs(msg, reg, last)}


# ---------------------------------------------------------------- tabel pesan -> kelompok
class GroupTableTests(unittest.TestCase):
    TABLE = [
        # catatan / tugas
        ("Catat: dosen basis data bilang UTS bab 1-4, tanggal 12 Des", {"catatan", "tugas"}),
        ("Catat: dosen jaringan bilang kehadiran praktikum minimal 80%", {"catatan"}),
        ("Hapus catatan pengumuman lab jaringan", {"catatan"}),
        ("Tambahkan ke catatan UTS basis data: bab 5 ikut diuji", {"catatan"}),
        ("Catatan apa saja yang sudah kusimpan?", {"catatan"}),
        ("Tambah tugas: laporan jaringan, deadline Jumat", {"tugas"}),
        ("Apa yang harus kukerjakan hari ini?", {"tugas"}),
        ("Tugas kuis jaringan sudah selesai", {"tugas"}),          # 'kuis' di sini nama tugas
        ("Kuis sudah selesai", {"tugas"}),
        ("Geser deadline laporan basis data jadi Jumat", {"tugas"}),
        ("Tugas laporan basis data sudah dikumpulkan", {"tugas"}),
        # pengetahuan / kuis
        ("Berapa bobot nilai UAS Basis Data menurut materi kita?", {"pengetahuan"}),
        ("According to my networking material, how many layers does the OSI model have?", {"pengetahuan"}),
        ("Jelaskan 3NF dari materi", {"pengetahuan"}),
        ("What is the capital of Japan?", {"pengetahuan"}),
        ("Kuis aku 3 soal normalisasi", {"kuis"}),
        ("Buatkan kuis tentang normalisasi", {"kuis"}),
        # campuran
        ("Tolong ringkas bab 3 lalu simpan sebagai catatan", {"pengetahuan", "catatan"}),
        ("Tambah tugas baca bab 5 lalu kuis aku soal bab 5", {"tugas", "kuis"}),
        # Inggris
        ("Add a task: finish the report, due Friday", {"tugas"}),
        ("Delete my note about the lab", {"catatan"}),
        ("Quiz me on normalization", {"kuis"}),
        ("Save a note: lecturer said the midterm is on 12 Dec", {"catatan", "tugas"}),
    ]

    def test_tabel(self):
        for msg, want in self.TABLE:
            self.assertEqual(groups(msg), want, msg)

    def test_pesan_ambigu_atau_tanpa_kecocokan_mengirim_semua(self):
        for msg in ("ya yang pertama", "oke lanjut", "Ibu kota Prancis apa?", "hapus yang itu",
                    "kapan UTS basis data?", "yang kedua saja", ""):
            self.assertIsNone(groups(msg), msg)
            self.assertEqual(names(msg), ALL, msg)

    def test_pesan_panjang_mengirim_semua(self):
        self.assertIsNone(groups("Tambah tugas laporan. " + "info kelas " * 60))

    def test_search_knowledge_selalu_ikut(self):
        for msg, _ in self.TABLE:
            self.assertIn("search_knowledge", names(msg), msg)

    def test_isi_subset(self):
        self.assertEqual(names("Kuis aku 3 soal normalisasi"), {"search_knowledge", "make_quiz"})
        self.assertEqual(names("Jelaskan 3NF dari materi"), {"search_knowledge"})
        self.assertEqual(names("Tambah tugas: laporan jaringan, deadline Jumat"),
                         {"search_knowledge", "add_task", "list_tasks", "complete_task", "update_task",
                          "delete_task"})

    def test_urutan_mengikuti_registry(self):
        order = [s.name for s in REG.specs()]
        got = [s.name for s in sel.select_specs("Catat: dosen bilang UTS tanggal 12 Des", REG)]
        self.assertEqual(got, [n for n in order if n in got])

    def test_penghematan_token(self):
        full = sel.estimate_tokens(REG.specs())
        kuis = sel.estimate_tokens(sel.select_specs("Kuis aku 3 soal normalisasi", REG))
        self.assertLess(kuis, full * 0.3)
        per = [sel.estimate_tokens(sel.select_specs(s.message, REG)) for s in es.SCENARIOS]
        self.assertLess(sum(per) / len(per), full * 0.65)

    def test_semua_skenario_eval_tetap_mendapat_tool_yang_dibutuhkan(self):
        for s in es.SCENARIOS:
            need = {c["name"] for it in s.script if isinstance(it, dict) for c in it.get("tool_calls", [])}
            self.assertLessEqual(need, names(s.message), s.id)
            self.assertLessEqual(set(s.expect), names(s.message), s.id)


# ---------------------------------------------------------------- registry dengan tool tambahan
class _Reg:
    def __init__(self, extra=()):
        self._specs = list(REG.specs()) + [ToolSpec(n, "d", {"type": "object", "properties": {}}) for n in extra]

    def specs(self):
        return list(self._specs)


class RegistryShapeTests(unittest.TestCase):
    def test_kelompok_tanpa_tool_terdaftar_tidak_dihitung(self):
        # jadwal belum terpasang (fitur 3): pesan jadwal tidak boleh berakhir hanya dengan search_knowledge
        self.assertNotIn("jadwal", AVAIL)
        self.assertEqual(names("besok ada kelas apa?"), ALL)

    def test_kelompok_jadwal_aktif_bila_tool_ada(self):
        reg = _Reg(("add_class", "list_schedule", "delete_class"))
        got = names("besok ada kelas apa?", reg=reg)
        self.assertEqual(got, {"search_knowledge", "add_class", "list_schedule", "delete_class"})
        self.assertEqual(names("kuis aku 3 soal normalisasi", reg=reg), {"search_knowledge", "make_quiz"})

    def test_tool_tanpa_kelompok_selalu_ikut(self):
        reg = _Reg(("tool_baru",))
        self.assertIn("tool_baru", names("Kuis aku 3 soal normalisasi", reg=reg))
        self.assertIn("tool_baru", names("Jelaskan 3NF dari materi", reg=reg))


# ---------------------------------------------------------------- konteks giliran sebelumnya
class CarryOverTests(unittest.TestCase):
    def test_klarifikasi_terdeteksi(self):
        for a in ("Ada dua tugas cocok: Kuis 1 dan Kuis 2. Yang mana?", "Maksudmu yang mana",
                  "Which one do you mean", "Tugas apa yang kamu maksud?"):
            self.assertTrue(sel.asked_clarification(a), a)
        for a in ("Tugas ditambahkan.", "", "Sudah selesai. Semangat belajarnya!"):
            self.assertFalse(sel.asked_clarification(a), a)

    def test_kelompok_lama_dipertahankan(self):
        self.assertIsNone(groups("ya yang pertama"))
        self.assertEqual(groups("ya yang pertama", carry={"tugas"}), {"tugas"})
        self.assertEqual(names("ya yang pertama", last=frozenset({"tugas"})),
                         {"search_knowledge", "add_task", "list_tasks", "complete_task", "update_task",
                          "delete_task"})

    def test_kelompok_lama_digabung_dengan_kelompok_baru(self):
        self.assertEqual(groups("Kuis 2 saja, tolong catat", carry={"tugas"}), {"tugas", "catatan"})

    def test_groups_of(self):
        self.assertEqual(sel.groups_of(["add_task", "make_quiz"]), {"tugas", "kuis"})
        self.assertEqual(sel.groups_of([]), frozenset())


# ---------------------------------------------------------------- integrasi Agent.ask
class AgentCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.env = em.Env(Path(self.tmp.name))
        self._old = getattr(config, "TOOL_SUBSET", True)
        config.TOOL_SUBSET = True

    def tearDown(self):
        config.TOOL_SUBSET = self._old
        self.env.close()
        self.tmp.cleanup()

    def agent(self, script):
        self.mock = MockProvider(script=script)
        return Agent(self.env.reg, self.mock.chat, courses_fn=self.env.store.courses,
                     today_fn=self.env.ctx.today, notify=lambda s: None)

    def sent(self):
        return [set(c["tools"]) for c in self.mock.chat_calls]


class AgentIntegrationTests(AgentCase):
    def test_hanya_subset_yang_dikirim_ke_model(self):
        ag = self.agent([call("make_quiz", topic="normalisasi", num_questions=3), "Soal sudah dibuat."])
        self.env.ctx.llm_chat = em._QuizLLM().chat
        ag.ask("Kuis aku 3 soal normalisasi")
        self.assertEqual(self.sent(), [{"search_knowledge", "make_quiz"}] * 2)

    def test_tool_subset_false_mengirim_semua(self):
        config.TOOL_SUBSET = False
        ag = self.agent(["Paris."])
        ag.ask("Kuis aku 3 soal normalisasi")
        self.assertEqual(self.sent(), [ALL])

    def test_pesan_ambigu_mengirim_semua(self):
        ag = self.agent(["Maksudnya apa?"])
        ag.ask("ya yang pertama")
        self.assertEqual(self.sent(), [ALL])

    def test_pemulihan_saat_model_memanggil_tool_yang_tidak_dikirim(self):
        # "Jelaskan ..." hanya mengirim search_knowledge; model malah memanggil add_task.
        ag = self.agent([call("add_task", title="Salah"), call("add_task", title="Benar"), "Selesai."])
        ans = ag.ask("Jelaskan normalisasi dari materi")
        self.assertEqual(ans, "Selesai.")
        s = self.sent()
        self.assertEqual(len(s), 3)
        self.assertEqual(s[0], {"search_knowledge"})
        self.assertEqual(s[1], ALL)               # diulang SEKALI dengan semua tool
        self.assertEqual(s[2], ALL)               # dan tetap semua untuk sisa giliran
        self.assertEqual([t["title"] for t in self.env.tasks.list("all")], ["Benar"])   # respons yang dibuang tidak dijalankan
        self.assertEqual([m.role for m in ag.history], ["user", "assistant"])

    def test_tool_yang_tidak_ada_di_registry_tidak_memicu_pelebaran(self):
        ag = self.agent([call("tool_ngawur"), "Maaf, tidak bisa."])
        ag.ask("Kuis aku 3 soal normalisasi")
        self.assertEqual(self.sent(), [{"search_knowledge", "make_quiz"}] * 2)

    def test_klarifikasi_mempertahankan_kelompok_di_giliran_berikutnya(self):
        es.seed_two_quizzes(self.env)
        ag = self.agent([call("complete_task", title="kuis"),
                         "Ada dua tugas cocok: Kuis 1 dan Kuis 2. Yang mana?",
                         call("complete_task", task_id=1), "Kuis 1 selesai."])
        ag.ask("Kuis sudah selesai")
        ag.ask("ya yang pertama")
        tugas = {"search_knowledge", "add_task", "list_tasks", "complete_task", "update_task", "delete_task"}
        self.assertEqual(self.sent(), [tugas] * 4)
        self.assertEqual(self.env.tasks.get(1)["status"], "done")
        # setelah jawaban biasa (bukan pertanyaan), kelompok lama dilepas lagi
        ag2 = self.agent(["Selesai.", "Entahlah."])
        ag2.ask("Kuis sudah selesai")
        ag2.ask("ya yang pertama")
        self.assertEqual(self.sent(), [tugas, ALL])

    def test_reset_membersihkan_kelompok_tersimpan(self):
        ag = self.agent(["Yang mana?"])
        ag.ask("Tugas sudah selesai")
        self.assertTrue(ag._last_groups)
        ag.reset()
        self.assertEqual(ag._last_groups, frozenset())


class EvalFlagTests(unittest.TestCase):
    def test_no_subset_mematikan_subset_lalu_memulihkannya(self):
        old = getattr(config, "TOOL_SUBSET", True)
        config.TOOL_SUBSET = True
        try:
            lines = []
            em.main(["--mock", "--models", "mock-ideal", "--only", "kuis-tanpa-kunci", "--no-subset"],
                    out=lines.append)
            self.assertIn("Subset tool: mati", "\n".join(lines))
            self.assertIn("lulus 1/1", "\n".join(lines))
            self.assertTrue(config.TOOL_SUBSET)
            lines.clear()
            em.main(["--mock", "--models", "mock-ideal", "--only", "kuis-tanpa-kunci"], out=lines.append)
            self.assertIn("Subset tool: aktif", "\n".join(lines))
        finally:
            config.TOOL_SUBSET = old

    def test_model_ideal_tetap_lulus_semua_dengan_dan_tanpa_subset(self):
        for flag in ([], ["--no-subset"]):
            lines = []
            em.main(["--mock", "--models", "mock-ideal", *flag], out=lines.append)
            self.assertIn("lulus 22/22", "\n".join(lines), flag)


if __name__ == "__main__":
    unittest.main()
