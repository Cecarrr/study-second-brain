"""Uji offline: hybrid search, /source, mode sederhana, penyamaran, dasbor, pemeriksaan sumber, /undo.
0 request API.   python -m unittest tests.test_fragmen_a -v"""
import json
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import grounding  # noqa: E402
import privacy  # noqa: E402
from agent import Agent  # noqa: E402
from dashboard import build_dashboard  # noqa: E402
from db.database import TaskDB  # noqa: E402
from prompts import build_system_prompt  # noqa: E402
from providers.base import ChatResponse, ToolCall  # noqa: E402
from providers.mock import MockProvider  # noqa: E402
from rag.embedder import Embedder  # noqa: E402
from rag.indexer import sync_folder  # noqa: E402
from rag.retriever import Retriever  # noqa: E402
from rag.store import VectorStore  # noqa: E402
from tools import sources  # noqa: E402
from tools.common import ToolContext  # noqa: E402
from tools.registry import build_registry  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        m = self.tmp / "materi" / "basis-data"
        m.mkdir(parents=True)
        (m / "norm.md").write_text("# Normalisasi\n\nSyarat 3NF: tidak ada ketergantungan transitif.\n"
                                   "\n# Subnet\n\nTabel subnet: /27 punya 30 host usable.\n", encoding="utf-8")
        self.emb = Embedder(MockProvider(embed_dim=256))
        self.store = VectorStore(self.tmp / "vs.db", self.emb.embedding_id)
        sync_folder(self.tmp / "materi", "materi", self.store, self.emb)
        self.ret = Retriever(self.store, self.emb, min_score=0.05)
        self.ctx = ToolContext(retriever=self.ret, store=self.store, embedder=self.emb,
                               tasks=TaskDB(self.tmp / "a.db", lambda: date(2026, 11, 9)),
                               notes_dir=self.tmp / "notes", log_path=self.tmp / "t.jsonl")
        self.reg = build_registry(self.ctx)

    def call(self, name, **a):
        m = self.reg.execute(ToolCall(name=name, args=a))
        return m.content, m.is_error


class HybridSourceTests(Base):
    def test_hybrid_marks_via_and_keyword_rescues_below_threshold(self):
        r = self.ret.search("ketergantungan transitif", min_score=0.0)
        self.assertEqual(r.mode, "hybrid")
        self.assertIn("vec+kw", {h.metadata["_via"] for h in r.hits})
        r2 = self.ret.search("host usable subnet", min_score=0.99)       # vektor tak lolos, BM25 tetap menemukan
        self.assertTrue(r2.hits and r2.hits[0].metadata["_via"] == "kw")
        self.assertIn("kata kunci", r2.format())

    def test_hybrid_off_restores_old_behavior(self):
        config.HYBRID_SEARCH = False
        try:
            self.assertEqual(self.ret.search("host usable subnet", min_score=0.99).mode, "vector")
        finally:
            del config.HYBRID_SEARCH

    def test_source_command(self):
        self.assertIn("Belum ada hasil", sources.show_source(self.ctx))
        self.call("search_knowledge", query="syarat 3NF transitif")
        self.assertIn("[1]", sources.show_source(self.ctx))
        self.assertIn("Syarat 3NF", sources.show_source(self.ctx, "1"))
        self.assertIn("Pilih nomor", sources.show_source(self.ctx, "99"))


class PrivacyGroundingTests(Base):
    def test_redact_kinds_and_clean_text(self):
        t = ("Email budi@kampus.ac.id, telp 0812-3456-7890, NIM: 2210123456, NIK 3201234567890123, "
             "kartu 4539 1488 0343 6467, password: rahasia123, key AIza" + "x" * 35)
        out, c = privacy.redact(t)
        self.assertEqual(set(c), {"EMAIL", "TELEPON", "NIM", "NIK", "KARTU", "KREDENSIAL", "KUNCI-API"})
        for leak in ("budi@", "3456-7890", "2210123456", "3201234567890123", "4539", "rahasia123"):
            self.assertNotIn(leak, out)
        clean = "UTS tanggal 12 Des, bab 1-4, 30 host."
        self.assertEqual(privacy.redact(clean), (clean, {}))

    def test_log_redacted_note_kept_and_warned(self):
        out, err = self.call("add_note", content="Kontak dosen: dosen@kampus.ac.id untuk konsultasi tugas.")
        self.assertFalse(err)
        self.assertIn("PERINGATAN PRIVASI", out)
        log = (self.tmp / "t.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("dosen@kampus", log)
        self.assertIn("[EMAIL]", log)
        self.assertIn("dosen@kampus.ac.id", next((self.tmp / "notes").glob("*.md")).read_text(encoding="utf-8"))

    def test_grounding(self):
        src = ["[1] Bobot UAS Jaringan 35%. Protokol TCP andal."]
        self.assertEqual(grounding.unsupported("UAS Jaringan bernilai 35 % dan memakai TCP.", src), [])
        bad = grounding.unsupported("Bobotnya 40% dan juga ada QUIC.", src)
        self.assertEqual(set(bad), {"40", "QUIC"})
        self.assertEqual(grounding.unsupported("1. Langkah satu\n2. Langkah dua", src), [])

    def test_agent_appends_grounding_note_but_history_stays_clean(self):
        script = [ChatResponse(tool_calls=[ToolCall("search_knowledge", {"query": "subnet 27"})]),
                  ChatResponse(text="Subnet /27 punya 62 host.")]
        a = Agent(self.reg, lambda m, s: script.pop(0), today_fn=self.ctx.today)
        ans = a.ask("berapa host subnet /27?")
        self.assertIn("(Periksa: 62", ans)
        self.assertNotIn("Periksa", a.history[-1].content)


class ModeDashboardTests(Base):
    def test_simple_mode_prompt(self):
        self.assertIn("SEDERHANA", build_system_prompt(date(2026, 11, 9), [], style="simple"))
        self.assertNotIn("SEDERHANA", build_system_prompt(date(2026, 11, 9), []))

    def test_dashboard(self):
        self.call("add_task", title="Laporan", due_date="2026-11-13")
        self.call("add_task", title="X", due_date="bukan-tanggal")
        out = build_dashboard(self.ctx, self.tmp / "t.jsonl", usage_report=lambda: "KUOTA-OK")
        self.assertIn("add_task", out)
        self.assertIn("1 (50%)", out)
        self.assertIn("Tugas: 1 belum selesai", out)
        self.assertIn("KUOTA-OK", out)
        self.assertIn("belum ada log", build_dashboard(None, self.tmp / "kosong.jsonl", usage_report=lambda: ""))


class UndoTests(Base):
    def test_undo_task_add_complete_delete(self):
        self.call("add_task", title="Laporan jaringan", due_date="2026-11-13")
        self.call("complete_task", title="laporan")
        self.assertEqual(self.ctx.tasks.list("pending"), [])
        self.assertIn("Dibatalkan: complete_task", self.reg.journal.undo(self.ctx))
        self.assertEqual(len(self.ctx.tasks.list("pending")), 1)
        self.call("delete_task", title="laporan")
        self.reg.journal.undo(self.ctx)
        self.assertEqual(len(self.ctx.tasks.list("all")), 1)
        self.assertEqual(self.ctx.tasks.trash_list(), [])             # sampah ikut dikembalikan
        self.reg.journal.undo(self.ctx)                               # membatalkan add_task
        self.assertEqual(self.ctx.tasks.list("all"), [])
        self.assertIn("Tidak ada aksi", self.reg.journal.undo(self.ctx))

    def test_undo_note_add_update_delete_with_index(self):
        self.call("add_note", content="Isi awal catatan tentang jaringan.", title="Jaringan")
        n0 = len(self.store)
        self.call("update_note", title="jaringan", new_content="Isi baru yang sama sekali berbeda zebra.")
        self.reg.journal.undo(self.ctx)
        text = next((self.tmp / "notes").glob("*.md")).read_text(encoding="utf-8")
        self.assertIn("Isi awal", text)
        self.assertFalse(any("zebra" in h.text for h in self.ret.search("zebra", min_score=0.0).hits))
        self.call("delete_note", title="jaringan")
        self.assertEqual(len(self.store), n0 - 1)
        self.reg.journal.undo(self.ctx)
        self.assertEqual(len(self.store), n0)
        self.reg.journal.undo(self.ctx)                               # membatalkan add_note
        self.assertEqual(list((self.tmp / "notes").glob("*.md")), [])
        self.assertEqual(len(self.store), n0 - 1)

    def test_failed_or_noop_actions_not_journaled_and_depth_limited(self):
        self.call("add_task", title="X", due_date="bukan-tanggal")      # error
        self.call("add_task", title="A", due_date="2026-11-13")
        self.call("add_task", title="A", due_date="2026-11-13")         # duplikat: tidak ada perubahan
        self.assertEqual(len(self.reg.journal.stack), 1)
        self.reg.journal.depth = 2
        for i in range(5):
            self.call("add_task", title=f"T{i}", due_date="2026-11-20")
        self.assertEqual(len(self.reg.journal.stack), 2)


if __name__ == "__main__":
    unittest.main()
