"""Uji offline impor sumber (jaringan, OCR, Whisper, YouTube dipalsukan). python -m unittest tests.test_importers -v"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import main  # noqa: E402
from providers.mock import MockProvider  # noqa: E402
from rag import importers as im  # noqa: E402
from rag.embedder import Embedder  # noqa: E402
from rag.indexer import sync_folder  # noqa: E402
from rag.retriever import Retriever  # noqa: E402
from rag.store import VectorStore  # noqa: E402

HTML = """<html><head><title>Normalisasi Basis Data - Blog</title></head><body>
<nav>Beranda | Kontak | Login</nav><script>var x = 'bocor';</script>
<main><h1>Normalisasi Database</h1><p>Normalisasi adalah proses menyusun tabel supaya data tidak redundan dan anomali dapat dihindari.</p>
<h2>Bentuk Normal Ketiga</h2><ul><li>Sudah 2NF</li><li>Tidak ada ketergantungan transitif antar atribut non-kunci</li></ul>
<table><tr><th>Bentuk</th><th>Syarat</th></tr><tr><td>1NF</td><td>nilai atomik</td></tr></table></main>
<footer>Hak cipta 2026</footer></body></html>"""


class FakeResp:
    def __init__(self, body: bytes, ctype="text/html; charset=utf-8", url="https://blog.example.com/norm?token=RAHASIA#x", status=200):
        self.content, self.headers, self.url, self.status = body, {"Content-Type": ctype}, url, status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    def iter_content(self, n):
        for i in range(0, len(self.content), n):
            yield self.content[i:i + n]

    def json(self):
        return {"title": "Kuliah Normalisasi"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._cfg = config.MATERI_DIR
        config.MATERI_DIR = self.tmp / "materi"
        config.MATERI_DIR.mkdir()
        self._get, self._host = im.HTTP_GET, im._public_host
        im._public_host = lambda host: True              # tanpa DNS sungguhan
        self.resp = FakeResp(HTML.encode())
        im.HTTP_GET = lambda url, **kw: self.resp

    def tearDown(self):
        config.MATERI_DIR, im.HTTP_GET, im._public_host = self._cfg, self._get, self._host


class WebTests(Base):
    def test_html_to_markdown_keeps_structure_drops_chrome(self):
        title, md = im.html_to_markdown(HTML)
        self.assertEqual(title, "Normalisasi Basis Data - Blog")
        for keep in ("# Normalisasi Database", "## Bentuk Normal Ketiga", "- Sudah 2NF", "1NF | nilai atomik"):
            self.assertIn(keep, md)
        for drop in ("Beranda", "bocor", "Hak cipta"):
            self.assertNotIn(drop, md)

    def test_import_web_writes_file_with_clean_source_and_is_idempotent(self):
        r = im.import_web("https://blog.example.com/norm?token=RAHASIA", course="Basis Data")
        text = (config.MATERI_DIR / r.dest).read_text(encoding="utf-8")
        self.assertIn("source: https://blog.example.com/norm", text)
        self.assertNotIn("RAHASIA", text)                              # token di query tidak disimpan
        self.assertTrue(r.dest.startswith("basis-data/imports/"))
        r2 = im.import_web("https://blog.example.com/norm?token=RAHASIA", course="Basis Data")
        self.assertEqual(r.dest, r2.dest)
        self.assertEqual(len(list((config.MATERI_DIR / "basis-data" / "imports").glob("*.md"))), 1)

    def test_existing_course_folder_is_reused(self):
        (config.MATERI_DIR / "basis-data").mkdir()
        self.assertTrue(im.import_web("https://blog.example.com/a", course="Basis  Data").dest.startswith("basis-data/imports/"))

    def test_rejections(self):
        for url, frag in (("ftp://x.id/a", "http/https"), ("file:///etc/passwd", "http/https")):
            with self.assertRaises(im.ImportFailed) as cm:
                im.import_web(url)
            self.assertIn(frag, str(cm.exception))
        im._public_host = self._host
        with self.assertRaises(im.ImportFailed) as cm:
            im.import_web("http://127.0.0.1/admin")
        self.assertIn("lokal", str(cm.exception))
        im._public_host = lambda h: True
        self.resp = FakeResp(b"x" * 3_000_000)
        with self.assertRaises(im.ImportFailed) as cm:
            im.import_web("https://x.id/besar")
        self.assertIn("lebih besar", str(cm.exception))
        self.resp = FakeResp(b"<html><body><p>kosong</p></body></html>")
        with self.assertRaises(im.ImportFailed) as cm:
            im.import_web("https://x.id/kosong")
        self.assertIn("terlalu sedikit", str(cm.exception))
        self.resp = FakeResp(b"", status=404)
        with self.assertRaises(im.ImportFailed):
            im.import_web("https://x.id/hilang")

    def test_pdf_url_saved_as_is(self):
        self.resp = FakeResp(b"%PDF-1.4 isi", ctype="application/pdf", url="https://x.id/slide.pdf")
        r = im.import_web("https://x.id/slide.pdf", course="Jaringan")
        self.assertTrue(r.dest.endswith(".pdf"))
        self.assertEqual((config.MATERI_DIR / r.dest).read_bytes(), b"%PDF-1.4 isi")

    def test_privacy_warning_on_sensitive_text(self):
        self.resp = FakeResp(HTML.replace("Normalisasi adalah", "Hubungi dosen@kampus.ac.id. Normalisasi adalah").encode())
        r = im.import_web("https://x.id/kontak")
        self.assertTrue(any("email" in w for w in r.warnings))


class MediaTests(Base):
    def test_youtube_ids(self):
        for url in ("https://www.youtube.com/watch?v=abc123XYZ_-&t=5", "https://youtu.be/abc123XYZ_-",
                    "https://www.youtube.com/shorts/abc123XYZ_-", "https://www.youtube.com/embed/abc123XYZ_-"):
            self.assertEqual(im.youtube_id(url), "abc123XYZ_-", url)
        self.assertIsNone(im.youtube_id("https://example.com/watch?v=abc"))

    def test_youtube_transcript_markdown_and_missing_dependency(self):
        seg = [(0.0, "Halo semua, hari ini kita bahas normalisasi."), (310.0, "Sekarang masuk ke bentuk normal ketiga yang penting sekali."),
               (320.0, "Ingat syaratnya adalah tidak ada ketergantungan transitif.")]
        orig = im.fetch_youtube_transcript
        im.fetch_youtube_transcript = lambda vid: seg
        try:
            r = im.run_import(im.parse_args("https://youtu.be/abc123XYZ_- --course Basis Data"))[0]
        finally:
            im.fetch_youtube_transcript = orig
        text = (config.MATERI_DIR / r.dest).read_text(encoding="utf-8")
        self.assertIn("title: Kuliah Normalisasi", text)
        self.assertIn("## 00:00", text)
        self.assertIn("## 05:00", text)
        self._opt, im._optional = im._optional, lambda name: None
        try:
            with self.assertRaises(im.ImportFailed) as cm:
                im.fetch_youtube_transcript("abc")
            self.assertIn("pip install youtube-transcript-api", str(cm.exception))
        finally:
            im._optional = self._opt

    def test_ocr_and_audio_and_missing_engines(self):
        img = self.tmp / "papan tulis.png"
        img.write_bytes(b"png")
        aud = self.tmp / "kuliah.mp3"
        aud.write_bytes(b"mp3")
        o1, o2 = im.ocr_image, im.transcribe_audio
        im.ocr_image = lambda p: "Rumus kompleksitas waktu merge sort adalah O(n log n) untuk semua kasus yang diuji."
        im.transcribe_audio = lambda p: [(0.0, "Selamat pagi, kita mulai kuliah jaringan komputer hari ini dengan model OSI.")]
        try:
            self.assertIn("O(n log n)", (config.MATERI_DIR / im.import_file(img, "Algoritma").dest).read_text(encoding="utf-8"))
            self.assertIn("model OSI", (config.MATERI_DIR / im.import_file(aud, "Jaringan").dest).read_text(encoding="utf-8"))
        finally:
            im.ocr_image, im.transcribe_audio = o1, o2
        self._opt, im._optional = im._optional, lambda name: None
        try:
            for fn, frag in ((im.ocr_image, "OCR belum tersedia"), (im.transcribe_audio, "faster-whisper")):
                with self.assertRaises(im.ImportFailed) as cm:
                    fn(img)
                self.assertIn(frag, str(cm.exception))
        finally:
            im._optional = self._opt


class LocalTests(Base):
    def test_files_folder_collision_and_unsupported(self):
        src = self.tmp / "sumber"
        src.mkdir()
        (src / "a.docx").write_bytes(b"docx-1")
        (src / "catatan.md").write_text("# T\n\nisi yang cukup panjang untuk catatan ini saja ya.", encoding="utf-8")
        (src / "aneh.xyz").write_bytes(b"?")
        res = im.run_import(im.parse_args(f'"{src}" --course Jaringan'))
        self.assertEqual(sorted(r.dest for r in res), ["jaringan/imports/a.docx", "jaringan/imports/catatan.md"])
        other = self.tmp / "lain"
        other.mkdir()
        (other / "a.docx").write_bytes(b"docx-2")
        r = im.import_file(other / "a.docx", "Jaringan")                  # nama sama, isi beda -> tidak menimpa
        self.assertNotEqual(r.dest, "jaringan/imports/a.docx")
        self.assertEqual((config.MATERI_DIR / "jaringan/imports/a.docx").read_bytes(), b"docx-1")
        with self.assertRaises(im.ImportFailed):
            im.import_file(src / "aneh.xyz")
        with self.assertRaises(im.ImportFailed):
            im.run_import(im.parse_args("tidak-ada.pdf"))

    def test_scanned_pdf_warning(self):
        pdf = self.tmp / "scan.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        orig = im._scanned_pdf
        im._scanned_pdf = lambda p: True
        try:
            self.assertTrue(any("scan" in w for w in im.import_file(pdf).warnings))
        finally:
            im._scanned_pdf = orig


class CliAndIndexTests(Base):
    def test_args_check_and_usage(self):
        a = im.parse_args('"C:\\Users\\M S I\\kuliah.pdf" --title "Judul Baru" --no-index')
        self.assertEqual((a.source, a.title, a.no_index), ("C:\\Users\\M S I\\kuliah.pdf", "Judul Baru", True))
        self.assertEqual(im.parse_args("https://x.id --course Basis Data --no-index").course, "Basis Data")
        self.assertIn("pip install", im.check_deps_text())
        self.assertIn("Pemakaian", main.do_import(None, None, None, ""))
        self.assertIn("Impor gagal", main.do_import(None, None, None, "bukan-sumber --no-index"))

    def test_imported_page_is_indexed_and_searchable_end_to_end(self):
        emb = Embedder(MockProvider(embed_dim=256))
        store = VectorStore(self.tmp / "vs.db", emb.embedding_id)
        out = main.do_import(None, store, emb, "https://blog.example.com/norm --course Basis Data --no-index")
        self.assertIn("Indeks belum diperbarui", out)
        self.assertEqual(len(store), 0)
        rep = sync_folder(config.MATERI_DIR, "materi", store, emb)
        self.assertEqual(rep.indexed, 1)
        res = Retriever(store, emb, min_score=0.05).search("ketergantungan transitif atribut non-kunci", min_score=0.0)
        self.assertTrue(res.hits and res.hits[0].source.startswith("basis-data/imports/"))
        self.assertEqual(res.hits[0].metadata["course"], "basis-data")


if __name__ == "__main__":
    unittest.main()
