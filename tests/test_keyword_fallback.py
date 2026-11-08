"""Uji offline untuk pencarian kata kunci (BM25) dan fallback retriever. 0 request API.

  python -m unittest tests.test_keyword_fallback -v     (dari akar repo)
  python tests/test_keyword_fallback.py
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from providers.base import RateLimitError  # noqa: E402
from rag.bm25 import BM25Index, tokenize  # noqa: E402
from rag.chunker import Chunk  # noqa: E402
from rag.ingest import Document, FileRef  # noqa: E402
from rag.retriever import KEYWORD_NOTE, Retriever  # noqa: E402
from rag.store import VectorStore  # noqa: E402

DIM = 4


def vec(i: int) -> np.ndarray:
    v = np.zeros(DIM, dtype=np.float32)
    v[i % DIM] = 1.0
    return v


def make_doc(name: str, course: str, source_type: str = "materi") -> Document:
    ref = FileRef(path=Path(name), rel_path=f"{course}/{name}", course=course,
                  source_type=source_type, ext=".md", size=1, sha256=f"sha-{name}")
    return Document(ref=ref, course=course, title=name)


def add(store: VectorStore, name: str, course: str, texts: list[str], source_type="materi"):
    doc = make_doc(name, course, source_type)
    chunks = [Chunk(id=f"{doc.doc_id}-{i:04d}", text=t, header=f"{course} | {name}",
                    metadata={"doc_id": doc.doc_id, "course": course, "rel_path": doc.rel_path,
                              "source_type": source_type, "heading": None})
              for i, t in enumerate(texts)]
    store.replace_document(doc, chunks, np.vstack([vec(i) for i in range(len(chunks))]))


class FakeEmbedder:
    """embed_query: sukses (mengembalikan vektor) atau gagal dengan RateLimitError."""

    def __init__(self, fail: bool):
        self.fail, self.calls = fail, 0

    def embed_query(self, text):
        self.calls += 1
        if self.fail:
            raise RateLimitError("Semua slot embed habis atau diblokir.\nrincian",
                                 provider="keypool", retryable=False)
        return vec(0)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class TokenizeTests(unittest.TestCase):
    def test_stopword_dan_huruf_tunggal(self):
        self.assertEqual(tokenize("Apa itu normalisasi database?"), ["normalisasi", "database"])
        self.assertEqual(tokenize("bab 1 dan C"), ["bab", "1"])

    def test_clitic_dibuang_hanya_bila_sisa_cukup(self):
        self.assertEqual(tokenize("normalisasinya"), ["normalisasi"])
        self.assertEqual(tokenize("tanya punya"), ["tanya", "punya"])   # sisa < 4 huruf: tetap

    def test_nfkc_dan_huruf_besar(self):
        self.assertEqual(tokenize("ﬁle Server"), ["file", "server"])    # ligatur fi


class BM25Tests(unittest.TestCase):
    def test_kata_langka_menang(self):
        docs = [tokenize(t) for t in ["server server web", "server database", "database indeks"]]
        idx = BM25Index(docs)
        res = idx.search(["indeks"])
        self.assertEqual([r[0] for r in res], [2])

    def test_min_matched_membuang_kecocokan_kebetulan(self):
        idx = BM25Index([tokenize("normalisasi database"), tokenize("jaringan database")])
        self.assertEqual(len(idx.search(["normalisasi", "database"], min_matched=1)), 2)
        self.assertEqual([r[0] for r in idx.search(["normalisasi", "database"], min_matched=2)], [0])

    def test_kosong(self):
        self.assertEqual(BM25Index([]).search(["apa"]), [])


class StoreKeywordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = VectorStore(Path(self.tmp.name) / "vs.db", f"test:emb:{DIM}")
        add(self.store, "normalisasi.md", "basis-data", [
            "Normalisasi database menghilangkan redundansi data. Bentuk normal ketiga (3NF) "
            "mensyaratkan tidak ada ketergantungan transitif.",
            "Denormalisasi dipakai demi kecepatan baca."])
        add(self.store, "routing.md", "jaringan", [
            "Routing statis memakai tabel rute manual; subnet mask menentukan jaringan."])
        add(self.store, "norm-en.md", "basis-data", [
            "Database normalization removes redundancy. Third normal form (3NF) forbids transitive dependency."])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_menemukan_istilah_persis(self):
        hits = self.store.keyword_search("normalisasi database")
        self.assertTrue(hits)
        self.assertIn("redundansi", hits[0].text)

    def test_header_ikut_diindeks(self):
        hits = self.store.keyword_search("routing")   # hanya ada di nama file/judul + isi
        self.assertEqual(hits[0].metadata["course"], "jaringan")

    def test_filter_course_dan_norm_nama(self):
        hits = self.store.keyword_search("redundansi redundancy", course="Basis Data")
        self.assertTrue(hits)
        self.assertTrue(all(h.metadata["course"] == "basis-data" for h in hits))
        self.assertEqual(self.store.keyword_search("normalisasi", course="jaringan"), [])
        self.assertEqual(self.store.keyword_search("normalisasi", course="tidak-ada"), [])

    def test_lintas_bahasa_tidak_cocok(self):
        # Batasan yang diterima: tanpa embedding, 'transitive' tidak menemukan 'transitif'.
        hits = self.store.keyword_search("transitive")
        self.assertTrue(all("transitive" in h.text for h in hits))

    def test_coverage(self):
        # 3 kata berbeda -> minimal 2 harus cocok; 'routing' saja tidak cukup
        self.assertEqual(self.store.keyword_search("routing kuantum fotonik"), [])

    def test_stopword_saja_tanpa_hasil(self):
        self.assertEqual(self.store.keyword_search("apa itu yang"), [])

    def test_indeks_dibangun_ulang_setelah_data_berubah(self):
        self.assertEqual(self.store.keyword_search("firewall"), [])
        add(self.store, "keamanan.md", "jaringan", ["Firewall menyaring paket berdasarkan aturan."])
        self.assertTrue(self.store.keyword_search("firewall"))
        self.store.delete_document(make_doc("keamanan.md", "jaringan").doc_id)
        self.assertEqual(self.store.keyword_search("firewall"), [])


class RetrieverFallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = VectorStore(Path(self.tmp.name) / "vs.db", f"test:emb:{DIM}")
        add(self.store, "normalisasi.md", "basis-data",
            ["Normalisasi database menghilangkan redundansi data."])
        self.clock = Clock()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def make(self, fail, **kw):
        emb = FakeEmbedder(fail)
        return emb, Retriever(self.store, emb, min_score=0.5, clock=self.clock, cooldown_s=120, **kw)

    def test_embedding_sehat_memakai_vektor(self):
        emb, r = self.make(fail=False)
        res = r.search("normalisasi")
        self.assertEqual(res.mode, "vector")
        self.assertTrue(res.hits)
        self.assertNotIn(KEYWORD_NOTE, res.format())

    def test_embedding_gagal_turun_ke_kata_kunci(self):
        emb, r = self.make(fail=True)
        res = r.search("normalisasi database")
        self.assertEqual(res.mode, "keyword")
        self.assertTrue(res.hits)
        self.assertIn("Semua slot embed habis", res.fallback_reason)
        text = res.format()
        self.assertTrue(text.startswith("(Mode kata kunci"))
        self.assertIn("[1] kata kunci | basis-data/normalisasi.md", text)
        self.assertNotIn("skor", text.split("\n", 1)[1])

    def test_hasil_kosong_tetap_memberi_catatan_mode(self):
        emb, r = self.make(fail=True)
        res = r.search("kuantum fotonik")
        self.assertEqual(res.hits, [])
        self.assertIn("Mode kata kunci", res.format())
        self.assertIn("Tidak ditemukan", res.format())

    def test_cooldown_tidak_memanggil_embedding_berulang(self):
        emb, r = self.make(fail=True)
        r.search("normalisasi")
        r.search("redundansi")
        r.search("database")
        self.assertEqual(emb.calls, 1)

    def test_pulih_setelah_cooldown(self):
        emb, r = self.make(fail=True)
        r.search("normalisasi")
        emb.fail = False
        self.clock.t += 121
        res = r.search("normalisasi")
        self.assertEqual(res.mode, "vector")
        self.assertEqual(emb.calls, 2)

    def test_fallback_dimatikan_error_diteruskan(self):
        emb, r = self.make(fail=True, keyword_fallback=False)
        with self.assertRaises(RateLimitError):
            r.search("normalisasi")

    def test_indeks_kosong_dan_query_kosong(self):
        emb, r = self.make(fail=True)
        self.assertEqual(r.search("   ").hits, [])
        self.assertEqual(emb.calls, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
