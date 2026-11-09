"""Uji offline: update_note, delete_note, tempat sampah (catatan + tugas), pembersihan cache konversi.
0 request API.   python -m unittest tests.test_notes_trash -v"""
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from db.database import TaskDB  # noqa: E402
from providers.base import ToolCall  # noqa: E402
from providers.mock import MockProvider  # noqa: E402
from rag import convert  # noqa: E402
from rag.embedder import Embedder  # noqa: E402
from rag.indexer import sync_folder  # noqa: E402
from rag.retriever import Retriever  # noqa: E402
from rag.store import VectorStore  # noqa: E402
from tools import trash  # noqa: E402
from tools.common import ToolContext  # noqa: E402
from tools.registry import build_registry  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        emb = Embedder(MockProvider(embed_dim=256))
        self.store = VectorStore(self.tmp / "vs.db", emb.embedding_id)
        self.ctx = ToolContext(retriever=Retriever(self.store, emb, min_score=0.1), store=self.store,
                               embedder=emb, tasks=TaskDB(self.tmp / "a.db", lambda: date(2026, 11, 9)),
                               notes_dir=self.tmp / "notes", log_path=self.tmp / "t.jsonl")
        self.reg = build_registry(self.ctx)

    def call(self, name, **args):
        m = self.reg.execute(ToolCall(name=name, args=args))
        return m.content, m.is_error

    def add(self, content, title):
        out, err = self.call("add_note", content=content, title=title, course="Basis Data")
        self.assertFalse(err, out)


class NoteTests(Base):
    def test_update_append_keeps_created_and_reindexes(self):
        self.add("UTS mencakup bab 1 sampai 4.", "UTS Basis Data")
        out, err = self.call("update_note", title="uts basis", append_text="Bab 5 ikut diuji dengan topik zebra.")
        self.assertFalse(err, out)
        self.assertIn("diperbarui", out)
        f = next(self.ctx.notes_dir.glob("*.md"))
        text = f.read_text(encoding="utf-8")
        self.assertIn("bab 1 sampai 4", text.lower())
        self.assertIn("zebra", text)
        self.assertIn("created: 2026-11-09", text)
        self.assertIn("updated: 2026-11-09", text)
        res = self.ctx.retriever.search("zebra bab 5", min_score=0.0)
        self.assertTrue(any("zebra" in h.text for h in res.hits))
        out, _ = self.call("list_notes")
        self.assertNotIn("BELUM ter-index", out)

    def test_update_replace_title_course_tags(self):
        self.add("Isi lama catatan satu.", "Catatan Satu")
        out, err = self.call("update_note", file=next(self.ctx.notes_dir.glob("*.md")).name,
                             new_content="Isi baru yang benar sekali.", new_title="Judul Baru", tags="uts, penting")
        self.assertFalse(err, out)
        text = next(self.ctx.notes_dir.glob("*.md")).read_text(encoding="utf-8")
        self.assertIn("title: Judul Baru", text)
        self.assertIn("tags: uts, penting", text)
        self.assertNotIn("Isi lama", text)
        self.assertEqual(len(list(self.ctx.notes_dir.glob("*.md"))), 1)        # tidak membuat file baru

    def test_update_errors(self):
        self.add("Catatan alfa tentang jaringan.", "Alfa Jaringan")
        self.add("Catatan alfa tentang basis data.", "Alfa Basis Data")
        out, err = self.call("update_note", title="alfa", append_text="x tambahan panjang")
        self.assertFalse(err)
        self.assertIn("beberapa catatan", out)                                  # ambigu -> kandidat, tidak menebak
        out, err = self.call("update_note", title="alfa jaringan", new_content="abcdef", append_text="ghijkl")
        self.assertTrue(err and "Pilih salah satu" in out)
        out, err = self.call("update_note", title="alfa jaringan")
        self.assertTrue(err and "Tidak ada perubahan" in out)
        out, err = self.call("update_note", title="tidak ada", append_text="isi tambahan")
        self.assertTrue(err and "list_notes" in out)

    def test_delete_restore_note(self):
        self.add("Pengumuman lab hari Jumat jam sepuluh.", "Pengumuman Lab")
        out, err = self.call("delete_note", title="pengumuman lab")
        self.assertFalse(err, out)
        self.assertIn("/restore", out)
        self.assertEqual(list(self.ctx.notes_dir.glob("*.md")), [])
        self.assertEqual(len(self.store), 0)                                    # hilang dari pencarian
        self.assertIn("Pengumuman Lab", trash.list_trash(self.ctx))
        msg = trash.restore(self.ctx, 1)
        self.assertIn("dipulihkan", msg)
        self.assertEqual(len(list(self.ctx.notes_dir.glob("*.md"))), 1)
        self.assertGreater(len(self.store), 0)                                  # masuk pencarian lagi
        self.assertEqual(trash.list_trash(self.ctx), "Tempat sampah kosong.")

    def test_restore_when_filename_taken(self):
        self.add("Catatan pertama yang cukup panjang.", "Sama")
        self.call("delete_note", title="sama")
        self.add("Catatan pengganti yang cukup panjang.", "Sama")               # nama file yang sama dipakai lagi
        self.assertIn("-pulih", trash.restore(self.ctx, 1))
        self.assertEqual(len(list(self.ctx.notes_dir.glob("*.md"))), 2)


class TrashTests(Base):
    def test_task_soft_delete_and_restore(self):
        self.call("add_task", title="Laporan jaringan", due_date="2026-11-13")
        out, err = self.call("delete_task", title="laporan jaringan")
        self.assertFalse(err)
        self.assertIn("tempat sampah", out)
        self.assertEqual(self.ctx.tasks.list("all"), [])
        t = self.ctx.tasks.restore_task(1)
        self.assertEqual((t["id"], t["title"], t["due_date"]), (1, "Laporan jaringan", "2026-11-13"))
        self.assertEqual(len(self.ctx.tasks.list("all")), 1)

    def test_expired_items_purged_on_start(self):
        self.ctx.tasks.trash_put("task", "lama", {"id": 9, "title": "lama"})
        con = sqlite3.connect(self.tmp / "a.db")
        con.execute("UPDATE trash SET expires_at=?", ((datetime.now() - timedelta(days=1)).isoformat(),))
        con.commit()
        con.close()
        self.assertEqual(TaskDB(self.tmp / "a.db").trash_list(), [])

    def test_purge_and_unknown_id(self):
        self.ctx.tasks.trash_put("task", "x", {"id": 1, "title": "x"})
        self.assertIn("1 item dibuang", trash.purge(self.ctx))
        self.assertIn("Tidak ada item #5", trash.restore(self.ctx, 5))


class CachePruneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._old = config.CONVERT_CACHE_DIR
        config.CONVERT_CACHE_DIR = self.tmp / "converted"
        config.CONVERT_CACHE_DIR.mkdir()

    def tearDown(self):
        config.CONVERT_CACHE_DIR = self._old

    def test_prune_only_orphans_with_our_naming(self):
        c = config.CONVERT_CACHE_DIR
        (c / "aaaaaaaaaaaaaaaa.pptx").write_bytes(b"x" * 100)      # sumber masih ada -> tetap
        (c / "bbbbbbbbbbbbbbbb.docx").write_bytes(b"x" * 50)       # yatim -> dihapus
        (c / "catatan-saya.txt").write_text("bukan milik cache")   # nama asing -> tidak disentuh
        fresh, old = c / "cccccccccccccccc.pptx.part", c / "dddddddddddddddd.pptx.part"
        fresh.write_bytes(b"x")
        old.write_bytes(b"x")
        t = time.time() - 7200
        import os
        os.utime(old, (t, t))
        n, size = convert.prune_cache({"aaaaaaaaaaaaaaaa"})
        self.assertEqual((n, size), (2, 51))
        self.assertEqual(sorted(p.name for p in c.iterdir()),
                         ["aaaaaaaaaaaaaaaa.pptx", "catatan-saya.txt", "cccccccccccccccc.pptx.part"])

    def test_sync_prunes_and_reports_but_not_for_missing_root(self):
        materi = self.tmp / "materi"
        materi.mkdir()
        (materi / "a.md").write_text("# A\n\nIsi materi yang cukup panjang untuk jadi chunk.", encoding="utf-8")
        stale = config.CONVERT_CACHE_DIR / "eeeeeeeeeeeeeeee.pptx"
        stale.write_bytes(b"x" * 10)
        emb = Embedder(MockProvider(embed_dim=64))
        store = VectorStore(self.tmp / "v.db", emb.embedding_id)
        sync_folder(self.tmp / "tidak-ada", "materi", store, emb)
        self.assertTrue(stale.exists())                                          # path salah -> aman
        rep = sync_folder(materi, "materi", store, emb)
        self.assertFalse(stale.exists())
        self.assertEqual(rep.pruned, 1)
        self.assertTrue(any("Cache konversi dibersihkan" in l for l in rep.lines()))

    def test_current_keys_hash_from_disk(self):
        materi = self.tmp / "m"
        materi.mkdir()
        (materi / "x.ppt").write_bytes(b"isi ppt")
        (materi / "y.md").write_text("bukan legacy")
        import hashlib
        self.assertEqual(convert.current_keys(materi), {hashlib.sha256(b"isi ppt").hexdigest()[:16]})


if __name__ == "__main__":
    unittest.main()
