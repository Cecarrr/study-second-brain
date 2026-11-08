"""Memecah Document menjadi chunk siap embed.

Aturan (bagian 7.2 masterfile):
- Pecah di batas alami lebih dulu: paragraf -> baris -> kalimat -> klausa -> kata.
- Segmen pendek berurutan (slide, bagian pendek) digabung sampai ~CHUNK_MERGE_BELOW.
- Segmen panjang dipecah ~CHUNK_SIZE dengan overlap CHUNK_OVERLAP.
- Tiap chunk punya `header` (mata kuliah | file/judul | bagian) yang ikut di-embed.
- `content_hash` = sha256 dari teks yang di-embed; dipakai sebagai kunci cache.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Iterable

import config
from rag.ingest import Document, Segment

# Urutan pemisah dari yang paling "alami" ke yang paling halus.
_SEPARATORS = [
    re.compile(r"\n\s*\n"),          # paragraf
    re.compile(r"\n"),               # baris
    re.compile(r"(?<=[.!?])\s+"),    # kalimat
    re.compile(r"[;,]\s+"),          # klausa
    re.compile(r"\s+"),              # kata
]
_HEADER_MAX = 160


@dataclass
class Chunk:
    id: str
    text: str                        # isi chunk (tanpa header)
    header: str                      # konteks singkat di awal teks embed
    metadata: dict = field(default_factory=dict)
    content_hash: str = ""

    def __post_init__(self) -> None:
        if not self.content_hash:
            self.content_hash = hashlib.sha256(self.embed_text.encode("utf-8")).hexdigest()

    @property
    def embed_text(self) -> str:
        """Teks yang dikirim ke model embedding."""
        return f"{self.header}\n{self.text}" if self.header else self.text


# ---------------------------------------------------------------- Pemecahan
def _split_units(text: str, size: int, level: int = 0) -> list[tuple[str, str]]:
    """Pecah teks jadi unit <= size. Kembalikan [(unit, pemisah_setelahnya)]."""
    if len(text) <= size:
        return [(text, "")]
    if level >= len(_SEPARATORS):  # tanpa batas alami sama sekali: potong paksa
        return [(text[i:i + size], "") for i in range(0, len(text), size)]

    pieces: list[tuple[str, str]] = []
    pos = 0
    for m in _SEPARATORS[level].finditer(text):
        if m.end() == m.start():
            continue
        pieces.append((text[pos:m.start()], m.group()))
        pos = m.end()
    pieces.append((text[pos:], ""))

    if len(pieces) == 1:  # pemisah level ini tidak ada; turun satu level
        return _split_units(text, size, level + 1)

    out: list[tuple[str, str]] = []
    for piece, sep in pieces:
        if not piece.strip():
            continue
        sub = _split_units(piece, size, level + 1)
        sub[-1] = (sub[-1][0], sep)
        out.extend(sub)
    return out


def _pack(units: list[tuple[str, str]], size: int) -> list[str]:
    """Gabungkan unit berurutan sampai mendekati `size`."""
    chunks: list[str] = []
    cur, pending = "", ""
    for text, sep in units:
        if not cur:
            cur, pending = text, sep
        elif len(cur) + len(pending) + len(text) <= size:
            cur += pending + text
            pending = sep
        else:
            chunks.append(cur)
            cur, pending = text, sep
    if cur:
        chunks.append(cur)
    return chunks


def _tail(text: str, n: int) -> str:
    """Ekor `text` sepanjang <= n karakter, dipotong di awal kata."""
    if len(text) <= n:
        return text
    t = text[-n:]
    m = re.search(r"\s", t)
    return t[m.end():] if m else t


def split_text(text: str, size: int | None = None, overlap: int | None = None) -> list[str]:
    """Pecah satu teks panjang jadi potongan ~size dengan overlap."""
    size = size or config.CHUNK_SIZE
    overlap = config.CHUNK_OVERLAP if overlap is None else overlap
    text = text.strip()
    if not text:
        return []
    pieces = _pack(_split_units(text, size), size)
    if overlap <= 0 or len(pieces) < 2:
        return pieces
    out = [pieces[0]]
    for prev, cur in zip(pieces, pieces[1:]):
        out.append(f"{_tail(prev, overlap)} {cur}".strip())
    return out


# ---------------------------------------------------------------- Penggabungan segmen
def _render(seg: Segment, first: bool) -> str:
    """Segmen non-pertama dalam chunk gabungan membawa judulnya sendiri."""
    if seg.heading and not first and seg.text.strip() != seg.heading:
        return f"{seg.heading}\n{seg.text}"
    return seg.text


def _group_segments(segments: Iterable[Segment], size: int, merge_below: int) -> list[list[Segment]]:
    """Kelompokkan segmen berurutan; hanya segmen pendek yang digabung."""
    groups: list[list[Segment]] = []
    cur: list[Segment] = []
    cur_len = 0
    for seg in segments:
        block_len = len(seg.text) + (len(seg.heading) + 1 if seg.heading else 0) + 2
        if len(seg.text) > size:
            if cur:
                groups.append(cur)
                cur, cur_len = [], 0
            groups.append([seg])
        elif cur and cur_len < merge_below and cur_len + block_len <= size:
            cur.append(seg)
            cur_len += block_len
        else:
            if cur:
                groups.append(cur)
            cur, cur_len = [seg], len(seg.text)
    if cur:
        groups.append(cur)
    return groups


def _header(doc: Document, heading: str | None) -> str:
    parts = []
    if doc.course and doc.course != "umum":
        parts.append(doc.course)
    parts.append(doc.title or doc.filename)
    if heading:
        parts.append(heading)
    return " | ".join(parts)[:_HEADER_MAX]


# ---------------------------------------------------------------- API publik
def chunk_document(doc: Document, size: int | None = None, overlap: int | None = None,
                   merge_below: int | None = None) -> list[Chunk]:
    """Dokumen -> daftar Chunk berurutan. Parameter bawaan dari config.py."""
    size = size or config.CHUNK_SIZE
    overlap = config.CHUNK_OVERLAP if overlap is None else overlap
    merge_below = config.CHUNK_MERGE_BELOW if merge_below is None else merge_below

    chunks: list[Chunk] = []
    for group in _group_segments(doc.segments, size, merge_below):
        if len(group) == 1 and len(group[0].text) > size:
            bodies = split_text(group[0].text, size, overlap)
        else:
            bodies = ["\n\n".join(_render(s, i == 0) for i, s in enumerate(group))]

        pages = [s.page for s in group if s.page is not None]
        heading = group[0].heading
        header = _header(doc, heading)
        for body in bodies:
            body = body.strip()
            # Filter chunk "sampah" (mis. slide "Terima kasih") hanya untuk materi;
            # catatan pendek buatan sendiri ("UTS 12 Des") harus tetap masuk.
            tiny = len(re.sub(r"\s", "", body)) < config.CHUNK_DROP_BELOW
            if not body or (tiny and doc.source_type != "catatan"):
                continue
            idx = len(chunks)
            chunks.append(Chunk(
                id=f"{doc.doc_id}-{idx:04d}",
                text=body,
                header=header,
                metadata={
                    "doc_id": doc.doc_id,
                    "chunk_index": idx,
                    "source_file": doc.filename,
                    "rel_path": doc.rel_path,
                    "course": doc.course,
                    "source_type": doc.source_type,
                    "title": doc.title,
                    "heading": heading,
                    "page_start": min(pages) if pages else None,
                    "page_end": max(pages) if pages else None,
                    "page_label": doc.page_label,
                    "tags": doc.tags,
                    "added_at": doc.added_at,
                },
            ))
    return chunks


def chunk_documents(docs: Iterable[Document], **kwargs) -> list[Chunk]:
    out: list[Chunk] = []
    for doc in docs:
        out.extend(chunk_document(doc, **kwargs))
    return out
