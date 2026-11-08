"""Membaca file materi/catatan menjadi teks bersegmen + metadata.

Alur: discover_files() -> read_document(ref) -> rag.chunker.chunk_document().
File yang gagal dibaca tidak menghentikan proses: Document.error / .warnings
berisi alasannya.

Konvensi catatan (untuk tools/knowledge.py:add_note): file .md dengan
frontmatter sederhana, yang dibaca di sini dan menimpa nilai dari folder:

    ---
    title: Normalisasi database
    course: Basis Data
    tags: uts, bab-3
    created: 2026-11-07T10:00:00
    ---
    isi catatan...
"""
from __future__ import annotations

import codecs
import csv
import hashlib
import io
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

import config

log = logging.getLogger(__name__)

DEFAULT_COURSE = "umum"

# nama modul yang gagal di-import -> nama paket pip
_PIP_NAMES = {"docx": "python-docx", "pptx": "python-pptx", "pypdf": "pypdf"}


# ---------------------------------------------------------------- Tipe data
@dataclass
class Segment:
    """Satu unit teks bermakna: halaman PDF, slide, bagian dokumen, kelompok baris CSV."""
    text: str
    page: int | None = None        # nomor halaman / slide
    heading: str | None = None     # judul bagian (jalur: "Bab 1 > Pengantar")


@dataclass
class FileRef:
    path: Path
    rel_path: str                  # relatif terhadap folder akar, gaya posix
    course: str
    source_type: str               # "materi" | "catatan"
    ext: str
    size: int
    sha256: str

    @property
    def filename(self) -> str:
        return self.path.name

    @property
    def doc_id(self) -> str:
        return hashlib.sha1(f"{self.source_type}:{self.rel_path}".encode("utf-8")).hexdigest()[:10]


@dataclass
class Document:
    ref: FileRef
    course: str
    segments: list[Segment] = field(default_factory=list)
    page_label: str | None = None  # "hlm" (PDF) | "slide" (PPTX) | None
    title: str | None = None
    tags: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    added_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    @property
    def doc_id(self) -> str:
        return self.ref.doc_id

    @property
    def filename(self) -> str:
        return self.ref.filename

    @property
    def rel_path(self) -> str:
        return self.ref.rel_path

    @property
    def source_type(self) -> str:
        return self.ref.source_type

    @property
    def ok(self) -> bool:
        return bool(self.segments) and self.error is None

    @property
    def char_count(self) -> int:
        return sum(len(s.text) for s in self.segments)


# ---------------------------------------------------------------- Penemuan file
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def discover_files(root: str | Path, source_type: str = "materi",
                   ) -> tuple[list[FileRef], list[tuple[str, str]]]:
    """Telusuri folder. Kembalikan (file yang akan dibaca, [(path, alasan dilewati)]).

    Mata kuliah = nama subfolder pertama di bawah root; file langsung di root
    masuk "umum". File tersembunyi dan temporer Office (~$...) diabaikan.
    """
    root = Path(root)
    refs: list[FileRef] = []
    skipped: list[tuple[str, str]] = []
    if not root.exists():
        return refs, skipped

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part.startswith(".") for part in rel.parts) or path.name.startswith("~$"):
            continue
        ext = path.suffix.lower()
        if ext not in config.SUPPORTED_EXTENSIONS:
            skipped.append((rel.as_posix(), f"format {ext or '(tanpa ekstensi)'} belum didukung"))
            continue
        size = path.stat().st_size
        if size == 0:
            skipped.append((rel.as_posix(), "file kosong"))
            continue
        course = rel.parts[0] if len(rel.parts) > 1 else DEFAULT_COURSE
        refs.append(FileRef(path=path, rel_path=rel.as_posix(), course=course,
                            source_type=source_type, ext=ext, size=size,
                            sha256=sha256_file(path)))
    return refs, skipped


# ---------------------------------------------------------------- Pembersihan teks
_INVISIBLE = re.compile("[\u00ad\u200b\u200c\u200d\ufeff\x00]")


def _clean(text: str, nfkc: bool = False) -> str:
    """Normalisasi ringan. NFKC (ligatur fi/fl dst.) hanya untuk PDF/slide."""
    text = unicodedata.normalize("NFKC" if nfkc else "NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x0b", "\n")
    text = text.replace("\u00a0", " ")
    text = _INVISIBLE.sub("", text)
    text = re.sub(r"(?<=\S)[ \t]{2,}", " ", text)   # rapatkan spasi di dalam baris
    text = re.sub(r"[ \t]+\n", "\n", text)          # buang spasi di ujung baris
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _dehyphenate(text: str) -> str:
    """Sambung kata yang terpotong tanda hubung di akhir baris (hasil PDF)."""
    return re.sub(r"(?<=[a-z])-\n(?=[a-z])", "", text)


def _decode(raw: bytes, doc: Document) -> str:
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        text = raw.decode("cp1252")
        doc.warnings.append("bukan UTF-8; dibaca sebagai cp1252")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
        doc.warnings.append("encoding tidak dikenal; dibaca sebagai latin-1")
    return text


def _heading_path(stack: list[tuple[int, str]]) -> str | None:
    return " > ".join(t for _, t in stack) or None


# ---------------------------------------------------------------- Pembaca per format
def _read_text(path: Path, doc: Document) -> None:
    text = _clean(_decode(path.read_bytes(), doc))
    doc.segments = [Segment(text)]


_FM_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.S)
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t", "#")):
            k, v = line.split(":", 1)
            meta[k.strip().lower()] = v.strip().strip("\"'")
    return meta, text[m.end():]


def _read_markdown(path: Path, doc: Document) -> None:
    text = _clean(_decode(path.read_bytes(), doc))
    meta, body = _parse_frontmatter(text)
    if meta.get("title"):
        doc.title = meta["title"]
    if meta.get("course"):
        doc.course = meta["course"]
    if meta.get("tags"):
        doc.tags = [t.strip() for t in meta["tags"].strip("[]").split(",") if t.strip()]

    segments: list[Segment] = []
    stack: list[tuple[int, str]] = []
    buf: list[str] = []
    in_fence = False

    def flush() -> None:
        content = "\n".join(buf).strip()
        if content:
            segments.append(Segment(content, heading=_heading_path(stack)))
        buf.clear()

    for line in body.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            buf.append(line)
            continue
        m = None if in_fence else _HEADING_RE.match(line)
        if m:
            flush()
            level, title = len(m.group(1)), m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
        else:
            buf.append(line)
    flush()
    doc.segments = segments


def _read_pdf(path: Path, doc: Document) -> None:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if reader.is_encrypted:
        try:
            unlocked = reader.decrypt("")
        except Exception:  # noqa: BLE001
            unlocked = 0
        if not unlocked:
            raise ValueError("PDF terenkripsi (butuh password)")

    doc.page_label = "hlm"
    n_pages = len(reader.pages)
    segments: list[Segment] = []
    total = 0
    for i, page in enumerate(reader.pages, start=1):
        try:
            raw = page.extract_text() or ""
        except Exception as e:  # noqa: BLE001
            doc.warnings.append(f"hlm {i}: gagal ekstrak teks ({type(e).__name__})")
            continue
        text = _clean(_dehyphenate(raw), nfkc=True)
        total += len(text)
        if text:
            segments.append(Segment(text, page=i))

    if n_pages and total / n_pages < config.PDF_MIN_CHARS_PER_PAGE:
        doc.warnings.append(
            f"hampir tanpa teks ({total} karakter di {n_pages} halaman): kemungkinan PDF scan "
            "atau gambar, dilewati (OCR belum didukung)")
        segments = []
    elif len(segments) < n_pages:
        empty = n_pages - len(segments)
        doc.warnings.append(f"{empty} halaman tanpa teks (kemungkinan gambar)")
    doc.segments = segments


_DOCX_HEADING = re.compile(r"^(?:heading|judul)\s*(\d+)$", re.I)


def _docx_heading_level(style_name: str) -> int | None:
    if style_name.strip().lower() == "title":
        return 0
    m = _DOCX_HEADING.match(style_name.strip())
    return int(m.group(1)) if m else None


def _table_lines(rows: list[list[str]]) -> list[str]:
    """Baris tabel -> "sel | sel | sel"; sel gabungan yang berulang dibuang."""
    lines = []
    for row in rows:
        cells: list[str] = []
        for cell in row:
            t = " ".join(cell.split())
            if t and (not cells or cells[-1] != t):
                cells.append(t)
        if cells:
            lines.append(" | ".join(cells))
    return lines


def _read_docx(path: Path, doc: Document) -> None:
    from docx import Document as DocxDocument
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = DocxDocument(str(path))
    segments: list[Segment] = []
    stack: list[tuple[int, str]] = []
    buf: list[str] = []

    def flush() -> None:
        content = _clean("\n".join(buf))
        if content:
            segments.append(Segment(content, heading=_heading_path(stack)))
        buf.clear()

    for child in d.element.body.iterchildren():
        if child.tag == qn("w:p"):
            p = Paragraph(child, d)
            text = p.text.strip()
            if not text:
                continue
            style = (p.style.name if p.style is not None else "") or ""
            level = _docx_heading_level(style)
            if level is not None:
                flush()
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, " ".join(text.split())))
            elif style.lower().startswith("list"):
                buf.append("- " + text)
            else:
                buf.append(text)
        elif child.tag == qn("w:tbl"):
            table = Table(child, d)
            lines = _table_lines([[c.text for c in row.cells] for row in table.rows])
            if lines:
                buf.append("\n".join(lines))
    flush()
    doc.segments = segments


def _is_group(shape) -> bool:
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    try:
        return shape.shape_type == MSO_SHAPE_TYPE.GROUP
    except NotImplementedError:
        return False


def _iter_shapes(shapes) -> Iterator:
    """Shape berurutan atas -> bawah, kiri -> kanan; grup dibuka rekursif."""
    for sh in sorted(shapes, key=lambda s: (s.top or 0, s.left or 0)):
        if _is_group(sh):
            yield from _iter_shapes(sh.shapes)
        else:
            yield sh


def _read_pptx(path: Path, doc: Document) -> None:
    from pptx import Presentation

    prs = Presentation(str(path))
    doc.page_label = "slide"
    segments: list[Segment] = []
    textless = 0

    for idx, slide in enumerate(prs.slides, start=1):
        title, title_id = None, None
        try:
            t_shape = slide.shapes.title
            if t_shape is not None:
                title = " ".join(t_shape.text_frame.text.split()) or None
                title_id = t_shape.shape_id
        except Exception:  # noqa: BLE001
            pass

        lines: list[str] = []
        for sh in _iter_shapes(slide.shapes):
            if title_id is not None and sh.shape_id == title_id:
                continue
            if getattr(sh, "has_text_frame", False) and sh.has_text_frame:
                paras = [(p.level, p.text.strip()) for p in sh.text_frame.paragraphs
                         if p.text.strip()]
                for level, text in paras:
                    lines.append(("  " * level + "- " + text) if len(paras) > 1 else text)
            elif getattr(sh, "has_table", False) and sh.has_table:
                lines += _table_lines([[c.text for c in row.cells] for row in sh.table.rows])

        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                lines.append("Catatan pembicara: " + notes)

        body = _clean("\n".join(lines), nfkc=True)
        if not body and not title:
            textless += 1
            continue
        segments.append(Segment(body or title or "", page=idx, heading=title))

    if textless:
        doc.warnings.append(f"{textless} slide tanpa teks (kemungkinan hanya gambar)")
    doc.segments = segments


def _read_csv(path: Path, doc: Document) -> None:
    """Tiap baris -> "kolom: nilai; kolom: nilai"; baris dikelompokkan ~CHUNK_SIZE."""
    text = _decode(path.read_bytes(), doc)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    header = next(reader, None)
    if not header:
        return
    header = [h.strip() or f"kolom{i + 1}" for i, h in enumerate(header)]

    segments: list[Segment] = []
    lines: list[str] = []
    size, first = 0, 1

    def flush(last: int) -> None:
        nonlocal size
        if lines:
            segments.append(Segment("\n".join(lines), heading=f"baris {first}-{last}"))
        lines.clear()
        size = 0

    n = 0
    for n, row in enumerate(reader, start=1):
        if n > config.MAX_CSV_ROWS:
            doc.warnings.append(f"hanya {config.MAX_CSV_ROWS} baris pertama dibaca")
            n = config.MAX_CSV_ROWS
            break
        pairs = [f"{h}: {v.strip()}" for h, v in zip(header, row) if v and v.strip()]
        if not pairs:
            continue
        line = "; ".join(pairs)
        if lines and size + len(line) > config.CHUNK_SIZE:
            flush(n - 1)
            first = n
        elif not lines:
            first = n
        lines.append(line)
        size += len(line) + 1
    flush(n)
    doc.segments = segments


_READERS: dict[str, Callable[[Path, Document], None]] = {
    ".md": _read_markdown,
    ".txt": _read_text,
    ".pdf": _read_pdf,
    ".docx": _read_docx,
    ".pptx": _read_pptx,
    ".csv": _read_csv,
}


# ---------------------------------------------------------------- API publik
def read_document(ref: FileRef) -> Document:
    """Baca satu file. Tidak pernah raise: kegagalan masuk Document.error."""
    doc = Document(ref=ref, course=ref.course)
    reader = _READERS.get(ref.ext)
    if reader is None and ref.ext in config.TEXT_LIKE_EXTENSIONS:
        reader = _read_text
    if reader is None:
        doc.error = f"format {ref.ext} tidak didukung"
        return doc

    try:
        reader(ref.path, doc)
    except ImportError as e:
        pkg = _PIP_NAMES.get(e.name or "", e.name or "?")
        doc.error = f"pustaka '{pkg}' belum terpasang (pip install {pkg})"
    except Exception as e:  # noqa: BLE001
        doc.error = f"gagal membaca: {type(e).__name__}: {e}"
        log.warning("Gagal membaca %s: %s", ref.rel_path, doc.error)

    doc.segments = [s for s in doc.segments if s.text.strip()]
    if not doc.segments and not doc.error:
        doc.warnings.append("tidak ada teks yang bisa diambil")
    return doc


def read_folder(root: str | Path, source_type: str = "materi",
                should_skip: Callable[[FileRef], bool] | None = None) -> Iterator[Document]:
    """Baca semua file di folder. `should_skip(ref)` -> True untuk file yang
    tidak berubah (mis. sha256 sama dengan yang tersimpan di store)."""
    refs, skipped = discover_files(root, source_type)
    for rel, reason in skipped:
        log.info("Dilewati %s: %s", rel, reason)
    for ref in refs:
        if should_skip and should_skip(ref):
            continue
        yield read_document(ref)
