"""Impor sumber baru -> file di data/materi/<matkul>/imports/, lalu diindeks lewat jalur ingest biasa.

Sumber: halaman web (HTML -> markdown), URL dokumen (PDF/DOCX/PPTX/CSV disimpan apa adanya), transkrip
YouTube, gambar (OCR), audio/video (Whisper), file/folder lokal. Paket berat bersifat OPSIONAL dan diimpor
lambat; bila belum terpasang, pesan berisi cara memasangnya (/import --check).

Opsi config.py (semua opsional): IMPORT_MAX_BYTES=2_000_000, IMPORT_TIMEOUT_S=20, IMPORT_ALLOW_PRIVATE=False,
WHISPER_MODEL="small", OCR_LANGS="ind+eng".
"""
from __future__ import annotations

import hashlib
import importlib
import ipaddress
import re
import shlex
import shutil
import socket
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import config
import privacy

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".mp4", ".mkv", ".webm", ".aac"}
DOC_EXT = {".pdf", ".docx", ".pptx", ".csv", ".md", ".txt", ".ppt", ".doc"}
USAGE = ("Pemakaian: /import SUMBER [--course NAMA] [--title JUDUL] [--no-index]\n"
         "  SUMBER = URL halaman, URL YouTube, atau path file/folder (gambar, audio, pdf, docx, pptx, csv, md, txt); kutip path yang berspasi\n"
         "  /import --check   lihat paket opsional yang sudah/belum terpasang")


class ImportFailed(Exception):
    """Kesalahan yang bisa dijelaskan ke pengguna."""


@dataclass
class Result:
    kind: str                      # web | youtube | ocr | audio | file
    source: str
    dest: str = ""                 # path relatif terhadap data/materi
    chars: int = 0
    warnings: list = field(default_factory=list)

    def line(self) -> str:
        w = "".join(f"\n    ! {x}" for x in self.warnings)
        size = f", {self.chars} karakter" if self.chars else ""
        return f"  [{self.kind}] {self.source} -> {self.dest}{size}{w}"


@dataclass
class Args:
    source: str = ""
    course: str | None = None
    title: str | None = None
    no_index: bool = False
    check: bool = False


def parse_args(line: str) -> Args:
    toks = [t.strip('"\'') for t in shlex.split(line, posix=False)] if line.strip() else []
    a, i = Args(), 0
    while i < len(toks):
        t = toks[i]
        if t == "--no-index":
            a.no_index = True
        elif t == "--check":
            a.check = True
        elif t in ("--course", "--title"):
            j = i + 1
            while j < len(toks) and not toks[j].startswith("--"):      # nilai boleh beberapa kata tanpa tanda kutip
                j += 1
            setattr(a, t[2:], " ".join(toks[i + 1:j]) or None)
            i = j - 1
        elif not a.source:
            a.source = t
        i += 1
    return a


# ---------------------------------------------------------------- utilitas
def _optional(name: str):
    try:
        return importlib.import_module(name)
    except Exception:  # noqa: BLE001 - paket hilang atau rusak
        return None


def _slug(text: str, n: int = 60) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:n] or "sumber"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def course_folder(course: str | None) -> str:
    """Folder mata kuliah: pakai subfolder materi yang sudah ada bila namanya setara, selain itu slug."""
    if not course:
        return "umum"
    root = Path(config.MATERI_DIR)
    if root.is_dir():
        for d in root.iterdir():
            if d.is_dir() and _norm(d.name) == _norm(course):
                return d.name
    return _slug(course, 40)


def _imports_dir(course: str | None) -> Path:
    d = Path(config.MATERI_DIR) / course_folder(course) / "imports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _rel(path: Path) -> str:
    return path.relative_to(Path(config.MATERI_DIR)).as_posix()


def _clean_source(url: str) -> str:
    p = urlparse(url)
    if "youtube" in p.netloc or "youtu.be" in p.netloc:
        return url
    return p._replace(query="", fragment="").geturl()              # token di query string tidak disimpan


def _write_md(kind: str, title: str, source: str, body: str, course: str | None) -> Result:
    body = body.strip()
    if len(body) < 80:
        raise ImportFailed(f"teks terlalu sedikit ({len(body)} karakter); halaman kosong, butuh login, atau dirender JavaScript")
    name = f"{_slug(title)}-{hashlib.sha1(source.encode()).hexdigest()[:6]}.md"     # sumber sama -> file sama (impor ulang menimpa)
    path = _imports_dir(course) / name
    head = "\n".join([f"title: {' '.join(title.split())}", f"source: {source}", f"type: {kind}",
                      f"imported: {date.today().isoformat()}"])
    path.write_text(f"---\n{head}\n---\n{body}\n", encoding="utf-8")
    res = Result(kind, source, _rel(path), len(body))
    _, found = privacy.redact(body)
    if found and privacy.mode() != "off":
        res.warnings.append("teks memuat " + ", ".join(f"{k.lower()} ({n})" for k, n in found.items()) +
                            "; isinya dikirim ke API embedding saat diindeks (tier gratis dapat dipakai Google untuk pengembangan produk)")
    return res


# ---------------------------------------------------------------- web
class _Page(HTMLParser):
    SKIP = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg", "iframe", "button", "template"}
    HEAD = {"h1": 1, "h2": 2, "h3": 3, "h4": 4}

    def __init__(self, only_main: bool):
        super().__init__(convert_charrefs=True)
        self.only_main, self.main_depth, self.skip, self.lines = only_main, 0, 0, []
        self.cur: list[str] = []
        self.title, self._in_title, self.prefix, self.pre, self.row = "", False, "", False, None

    def _flush(self):
        text = " ".join("".join(self.cur).split()) if not self.pre else "".join(self.cur).strip("\n")
        self.cur = []
        if text and (not self.only_main or self.main_depth):
            if not self.lines or self.lines[-1] != self.prefix + text:
                self.lines.append(self.prefix + text)
        self.prefix = ""

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in self.SKIP:
            self._flush()
            self.skip += 1
        if self.skip:
            return
        if tag in ("main", "article"):
            self.main_depth += 1
        if tag in self.HEAD:
            self._flush()
            self.prefix = "#" * self.HEAD[tag] + " "
        elif tag in ("p", "div", "section", "br", "ul", "ol", "blockquote", "table"):
            self._flush()
        elif tag == "li":
            self._flush()
            self.prefix = "- "
        elif tag == "tr":
            self._flush()
            self.row = []
        elif tag == "pre":
            self._flush()
            self.pre = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in ("main", "article"):
            self.main_depth = max(0, self.main_depth - 1)
        if tag in ("td", "th") and self.row is not None:
            self.row.append(" ".join("".join(self.cur).split()))
            self.cur = []
        elif tag == "tr" and self.row is not None:
            row, self.row = [c for c in self.row if c], None
            if row and (not self.only_main or self.main_depth):
                self.lines.append(" | ".join(row))
        elif tag == "pre":
            self._flush()
            self.pre = False
        elif tag in self.HEAD or tag in ("p", "div", "li", "section", "blockquote"):
            self._flush()

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip:
            self.cur.append(data)


def html_to_markdown(html: str) -> tuple[str, str]:
    """-> (judul, markdown). Memakai isi <main>/<article> bila ada; membuang nav, header, footer, script."""
    p = _Page(only_main=bool(re.search(r"<(main|article)\b", html, re.I)))
    p.feed(html)
    p._flush()
    title = " ".join(p.title.split())
    for ln in p.lines:
        if ln.startswith("# "):
            title = title or ln[2:]
            break
    return title, "\n\n".join(p.lines)


def _public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        raise ImportFailed(f"host '{host}' tidak ditemukan")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def _http_get(url: str, **kw):
    import requests
    return requests.get(url, **kw)


HTTP_GET = _http_get          # diganti di tes


def fetch(url: str):
    """Unduh URL dengan batas ukuran dan waktu. -> (isi bytes, content_type, url akhir)."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise ImportFailed("hanya URL http/https yang didukung")
    if not getattr(config, "IMPORT_ALLOW_PRIVATE", False) and not _public_host(p.hostname or ""):
        raise ImportFailed("alamat lokal/privat ditolak (atur IMPORT_ALLOW_PRIVATE=True di config.py bila memang perlu)")
    limit = int(getattr(config, "IMPORT_MAX_BYTES", 2_000_000))
    try:
        r = HTTP_GET(url, timeout=int(getattr(config, "IMPORT_TIMEOUT_S", 20)), stream=True,
                     headers={"User-Agent": "StudySecondBrain/1.0"})
        r.raise_for_status()
        if not getattr(config, "IMPORT_ALLOW_PRIVATE", False) and urlparse(r.url).hostname != p.hostname \
                and not _public_host(urlparse(r.url).hostname or ""):
            raise ImportFailed("pengalihan ke alamat lokal/privat ditolak")
        chunks, size = [], 0
        for part in r.iter_content(65536):
            size += len(part)
            if size > limit:
                raise ImportFailed(f"file lebih besar dari {limit // 1000} KB (atur IMPORT_MAX_BYTES)")
            chunks.append(part)
    except ImportFailed:
        raise
    except Exception as e:  # noqa: BLE001
        raise ImportFailed(f"gagal mengunduh: {type(e).__name__}: {str(e)[:120]}")
    return b"".join(chunks), r.headers.get("Content-Type", ""), r.url


def import_web(url: str, course=None, title=None) -> Result:
    data, ctype, final = fetch(url)
    ext = Path(unquote(urlparse(final).path)).suffix.lower()
    if "pdf" in ctype.lower() or ext in (".pdf", ".docx", ".pptx", ".csv"):
        ext = ".pdf" if "pdf" in ctype.lower() else ext
        name = f"{_slug(title or Path(unquote(urlparse(final).path)).stem or 'dokumen')}-{hashlib.sha1(url.encode()).hexdigest()[:6]}{ext}"
        path = _imports_dir(course) / name
        path.write_bytes(data)
        return Result("file", _clean_source(url), _rel(path), warnings=["disimpan apa adanya; teks diekstrak saat diindeks"])
    m = re.search(r"charset=([\w-]+)", ctype, re.I)
    html = data.decode(m.group(1) if m else "utf-8", errors="replace")
    page_title, md = html_to_markdown(html)
    return _write_md("web", title or page_title or urlparse(final).netloc, _clean_source(url), md, course)


# ---------------------------------------------------------------- YouTube
def youtube_id(url: str) -> str | None:
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if host.endswith("youtu.be"):
        return p.path.strip("/").split("/")[0] or None
    if "youtube.com" in host:
        if p.path == "/watch":
            return (parse_qs(p.query).get("v") or [None])[0]
        m = re.match(r"^/(?:shorts|embed|live|v)/([\w-]{6,})", p.path)
        return m.group(1) if m else None
    return None


def fetch_youtube_transcript(video_id: str) -> list[tuple[float, str]]:
    mod = _optional("youtube_transcript_api")
    if mod is None:
        raise ImportFailed("paket youtube-transcript-api belum terpasang: python -m pip install youtube-transcript-api")
    langs = ["id", "en"]
    try:
        api = mod.YouTubeTranscriptApi
        if hasattr(api, "get_transcript"):                           # versi lama
            items = api.get_transcript(video_id, languages=langs)
            return [(float(i["start"]), i["text"]) for i in items]
        return [(float(s.start), s.text) for s in api().fetch(video_id, languages=langs)]
    except Exception as e:  # noqa: BLE001
        raise ImportFailed(f"transkrip tidak tersedia ({type(e).__name__}); video mungkin tanpa subtitle")


def _timed_markdown(segments: list[tuple[float, str]], block_s: int = 300) -> str:
    out, cur_block, buf = [], -1, []
    for start, text in segments:
        b = int(start // block_s)
        if b != cur_block:
            if buf:
                out.append(" ".join(buf))
            sec = int(b * block_s)
            h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
            out.append(f"## {h:02d}:{m:02d}:{s:02d}" if h else f"## {m:02d}:{s:02d}")      # MM:SS, atau HH:MM:SS bila > 1 jam
            cur_block, buf = b, []
        buf.append(" ".join(text.split()))
    if buf:
        out.append(" ".join(buf))
    return "\n\n".join(out)


def _youtube_title(url: str) -> str:
    try:
        r = HTTP_GET("https://www.youtube.com/oembed", params={"url": url, "format": "json"}, timeout=10)
        return r.json().get("title", "")
    except Exception:  # noqa: BLE001
        return ""


def import_youtube(url: str, course=None, title=None) -> Result:
    vid = youtube_id(url)
    if not vid:
        raise ImportFailed("ID video YouTube tidak ditemukan di URL")
    segs = fetch_youtube_transcript(vid)
    return _write_md("youtube", title or _youtube_title(url) or f"YouTube {vid}", f"https://www.youtube.com/watch?v={vid}",
                     _timed_markdown(segs), course)


# ---------------------------------------------------------------- OCR dan audio (opsional)
def ocr_image(path: Path) -> str:
    rapid = _optional("rapidocr_onnxruntime")
    if rapid is not None:
        result, _ = rapid.RapidOCR()(str(path))
        return "\n".join(r[1] for r in (result or []))
    tess, pil = _optional("pytesseract"), _optional("PIL.Image")
    if tess is not None and pil is not None and shutil.which("tesseract"):
        langs = getattr(config, "OCR_LANGS", "ind+eng")
        try:
            return tess.image_to_string(pil.open(path), lang=langs)
        except Exception:  # noqa: BLE001 - paket bahasa belum ada
            return tess.image_to_string(pil.open(path), lang="eng")
    raise ImportFailed("OCR belum tersedia. Pasang salah satu: python -m pip install rapidocr-onnxruntime  "
                       "(atau pytesseract + Pillow + program Tesseract)")


def transcribe_audio(path: Path) -> list[tuple[float, str]]:
    fw = _optional("faster_whisper")
    if fw is None:
        raise ImportFailed("transkripsi audio belum tersedia (berat): python -m pip install faster-whisper")
    model = fw.WhisperModel(getattr(config, "WHISPER_MODEL", "small"), device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(path))
    return [(float(s.start), s.text) for s in segments]


def import_image(path: Path, course=None, title=None) -> Result:
    return _write_md("ocr", title or path.stem, path.name, ocr_image(path), course)


def import_audio(path: Path, course=None, title=None) -> Result:
    return _write_md("audio", title or path.stem, path.name, _timed_markdown(transcribe_audio(path)), course)


# ---------------------------------------------------------------- file lokal
def _scanned_pdf(path: Path) -> bool:
    pypdf = _optional("pypdf")
    if pypdf is None:
        return False
    try:
        pages = pypdf.PdfReader(str(path)).pages[:3]
        return bool(pages) and sum(len(p.extract_text() or "") for p in pages) / len(pages) < int(getattr(config, "PDF_MIN_CHARS_PER_PAGE", 30))
    except Exception:  # noqa: BLE001
        return False


def import_file(path: Path, course=None, title=None) -> Result:
    ext = path.suffix.lower()
    if ext in IMAGE_EXT:
        return import_image(path, course, title)
    if ext in AUDIO_EXT:
        return import_audio(path, course, title)
    if ext not in DOC_EXT:
        raise ImportFailed(f"format '{ext or '(tanpa ekstensi)'}' tidak didukung")
    res = Result("file", path.name)
    if ext == ".pdf" and _scanned_pdf(path):
        res.warnings.append("PDF ini tampak hasil scan (hampir tanpa teks); teksnya tidak akan terbaca tanpa OCR. "
                            "Ekspor halamannya sebagai gambar lalu /import gambar itu")
    dest = _imports_dir(course) / path.name
    if dest.exists() and dest.read_bytes() != path.read_bytes():
        dest = dest.with_name(f"{dest.stem}-{hashlib.sha1(str(path.resolve()).encode()).hexdigest()[:6]}{dest.suffix}")
    shutil.copyfile(path, dest)
    res.dest = _rel(dest)
    return res


def run_import(args: Args) -> list[Result]:
    src = args.source
    if re.match(r"^https?://", src, re.I):
        return [import_youtube(src, args.course, args.title) if youtube_id(src) else import_web(src, args.course, args.title)]
    p = Path(src)
    if p.is_dir():
        files = sorted(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in IMAGE_EXT | AUDIO_EXT | DOC_EXT)[:200]
        if not files:
            raise ImportFailed("tidak ada file yang didukung di folder itu")
        out = []
        for f in files:
            try:
                out.append(import_file(f, args.course))
            except ImportFailed as e:
                out.append(Result("file", f.name, warnings=[f"dilewati: {e}"]))
        return out
    if not p.is_file():
        raise ImportFailed(f"'{src}' bukan URL dan bukan file/folder yang ada")
    return [import_file(p, args.course, args.title)]


def check_deps_text() -> str:
    def row(label, ok, hint=""):
        return f"  [{'ok' if ok else '--'}] {label}" + ("" if ok else f"  -> {hint}")
    ocr = _optional("rapidocr_onnxruntime") is not None or (_optional("pytesseract") and shutil.which("tesseract"))
    return "\n".join([
        "Paket impor (opsional kecuali requests):",
        row("web / dokumen URL (requests)", _optional("requests") is not None, "python -m pip install requests"),
        row("transkrip YouTube", _optional("youtube_transcript_api") is not None, "python -m pip install youtube-transcript-api"),
        row("OCR gambar", bool(ocr), "python -m pip install rapidocr-onnxruntime  (butuh Python versi yang punya wheel onnxruntime)"),
        row("audio -> teks (Whisper, berat)", _optional("faster_whisper") is not None, "python -m pip install faster-whisper"),
        row("deteksi PDF scan (pypdf)", _optional("pypdf") is not None, "python -m pip install pypdf"),
    ])
