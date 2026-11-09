"""Tool: search_knowledge dan add_note."""
from __future__ import annotations

import re
from pathlib import Path

import config
import privacy
from providers.base import ProviderError, ToolSpec
from rag.indexer import index_document
from rag.ingest import FileRef, read_document, sha256_file
from tools.common import ToolContext, ToolError

SEARCH_SPEC = ToolSpec(
    name="search_knowledge",
    description=(
        "Mencari di materi kuliah dan catatan pribadi pengguna (termasuk catatan yang dibuat dengan "
        "add_note). GUNAKAN untuk setiap pertanyaan tentang isi materi kuliah, konsep yang diajarkan "
        "dosen, angka atau aturan di dokumen kuliah (mis. bobot nilai), dan 'apa yang tadi kucatat "
        "tentang X'. JANGAN dipakai untuk pengetahuan umum di luar materi, atau untuk melihat tugas "
        "(itu list_tasks). Materi bercampur bahasa Indonesia dan Inggris: bila hasil kosong, coba SEKALI "
        "lagi dengan kata kunci bahasa lain. Mengembalikan potongan teks beserta sumber dan skor; "
        "jawab hanya berdasarkan potongan itu dan sebut sumbernya."),
    parameters={"type": "object", "properties": {
        "query": {"type": "string",
                  "description": "Kata kunci atau kalimat pencarian yang spesifik, tanpa sapaan."},
        "course": {"type": "string",
                   "description": "Nama mata kuliah untuk mempersempit, hanya bila pengguna menyebutnya."},
        "top_k": {"type": "integer", "minimum": 1, "maximum": 10,
                  "description": "Jumlah potongan teks (bawaan 5)."}},
        "required": ["query"]})

NOTE_SPEC = ToolSpec(
    name="add_note",
    description=(
        "Menyimpan catatan milik pengguna dan langsung memasukkannya ke pencarian (search_knowledge). "
        "GUNAKAN setiap kali pengguna meminta mencatat/menyimpan/mengingat sesuatu ('catat', 'simpan', "
        "'ingat bahwa') atau menyampaikan informasi kelas (pengumuman, cakupan ujian, aturan dosen, "
        "'dosen bilang ...'). Bila informasi itu memuat tenggat atau tanggal (ujian, kuis, pengumpulan), "
        "panggil add_note untuk isinya DAN add_task untuk tenggatnya. Tulis 'content' lengkap dan "
        "mandiri (tanggal dalam format YYYY-MM-DD, nama mata kuliah disebut), bukan ringkasan dua kata. "
        "JANGAN dipakai untuk perintah 'tambah tugas' semata (itu add_task), dan jangan memanggilnya "
        "dua kali untuk catatan yang sama."),
    parameters={"type": "object", "properties": {
        "content": {"type": "string", "description": "Isi catatan, lengkap dan mandiri."},
        "title": {"type": "string", "description": "Judul singkat (maks 8 kata); boleh kosong."},
        "course": {"type": "string", "description": "Mata kuliah terkait, bila disebut atau jelas."},
        "tags": {"type": "array", "items": {"type": "string"},
                 "description": "Label singkat, mis. uts, tugas, pengumuman."}},
        "required": ["content"]})


def search_knowledge(ctx: ToolContext, a: dict) -> str:
    course, known = ctx.resolve_course(a.get("course"))
    note = ""
    if course and not known:
        note = (f"(Mata kuliah '{course}' tidak ada di indeks; mencari di semua. "
                f"Yang ada: {', '.join(ctx.known_courses()) or '-'}.)\n")
        course = None
    res = ctx.retriever.search(a["query"], k=a.get("top_k") or config.TOP_K, course=course)
    ctx.last_sources = list(res.hits)
    return note + res.format()


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:50] or "catatan"


def _auto_title(content: str) -> str:
    first = re.split(r"[\n.!?]", content.strip(), maxsplit=1)[0].strip()
    return (first[:60] + "...") if len(first) > 60 else first or "Catatan"


def _one_line(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def add_note(ctx: ToolContext, a: dict) -> str:
    content = a["content"].strip()
    if len(content) < 5:
        raise ToolError("Isi catatan terlalu pendek; tulis informasi lengkap.")
    title = _one_line(a.get("title") or _auto_title(privacy.redact(content)[0] if privacy.mode() != "off" else content))
    if privacy.mode() != "off":
        title = privacy.redact(title)[0]           # judul menjadi nama file: jangan bocorkan data sensitif
    course, _ = ctx.resolve_course(a.get("course"))
    tags = [t for t in (_one_line(t) for t in (a.get("tags") or [])) if t]

    ctx.notes_dir.mkdir(parents=True, exist_ok=True)
    base = f"{ctx.today().isoformat()}-{_slug(title)}"
    path, n = ctx.notes_dir / f"{base}.md", 2
    while path.exists():
        path = ctx.notes_dir / f"{base}-{n}.md"
        n += 1
    head = [f"title: {title}"]
    if course:
        head.append(f"course: {_one_line(course)}")
    if tags:
        head.append("tags: " + ", ".join(tags))
    head.append(f"created: {ctx.today().isoformat()}")
    path.write_text("---\n" + "\n".join(head) + "\n---\n" + content + "\n", encoding="utf-8")

    ref = FileRef(path=path, rel_path=path.name, course="umum", source_type="catatan", ext=".md",
                  size=path.stat().st_size, sha256=sha256_file(path))
    doc = read_document(ref)
    if not doc.ok:
        raise ToolError(f"Catatan tersimpan di {path.name} tetapi gagal dibaca: {doc.error}")
    try:
        index_document(doc, ctx.store, ctx.embedder)
        status = "dan sudah masuk pencarian"
    except ProviderError as e:
        first = str(e).splitlines()[0][:140]
        status = ("tetapi BELUM masuk pencarian karena embedding sedang tidak tersedia "
                  f"({first}); akan di-index saat 'python index_materi.py' dijalankan")
    where = f" (mata kuliah: {course})" if course else ""
    return f"Catatan '{title}'{where} tersimpan di {path.name} {status}." + privacy.note_warning(content)
