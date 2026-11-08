"""Sinkronisasi folder materi/catatan ke vector store.

Hanya file baru/berubah (sha256 beda) yang diproses; file yang hilang dihapus dari
indeks. Embedding semua chunk baru dikirim bersama (batch 100), bukan per file,
supaya jumlah request sekecil mungkin.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import config
from rag.chunker import chunk_document
from rag.embedder import Embedder
from rag.ingest import Document, discover_files, read_document
from rag.store import VectorStore

LEGACY_HINT = {".ppt": ".pptx", ".doc": ".docx", ".xls": ".csv"}   # .xlsx belum didukung, .csv sudah


@dataclass
class SyncReport:
    indexed: int = 0
    unchanged: int = 0
    removed: int = 0
    failed: int = 0
    chunks: int = 0
    new_embeddings: int = 0
    skipped: list = field(default_factory=list)    # (path, alasan)
    notes: list = field(default_factory=list)      # peringatan per file

    def lines(self) -> list[str]:
        out = []
        for path, why in self.skipped:
            ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
            hint = f" -> simpan ulang sebagai {LEGACY_HINT[ext]}" if ext in LEGACY_HINT else ""
            out.append(f"  [dilewati] {path}: {why}{hint}")
        out += [f"  [peringatan] {n}" for n in self.notes]
        out.append(f"Selesai: {self.indexed} diindeks ({self.chunks} chunk, "
                   f"{self.new_embeddings} teks baru di-embed), {self.unchanged} tidak berubah, "
                   f"{self.removed} dihapus, {self.failed} gagal, {len(self.skipped)} dilewati")
        return out


def sync_folder(root, source_type: str, store: VectorStore, embedder: Embedder,
                report: SyncReport | None = None, progress=None) -> SyncReport:
    report = report or SyncReport()
    refs, skipped = discover_files(root, source_type)
    report.skipped += [(f"{source_type}/{p}", w) for p, w in skipped]

    present = {r.doc_id for r in refs}
    for d in store.list_documents(source_type):
        if d["doc_id"] not in present:
            store.delete_document(d["doc_id"])
            report.removed += 1

    pending = []   # (doc, chunks)
    for ref in refs:
        if store.doc_hash(ref.doc_id) == ref.sha256:
            report.unchanged += 1
            continue
        doc = read_document(ref)
        for w in doc.warnings:
            report.notes.append(f"{ref.rel_path}: {w}")
        if doc.error:
            report.failed += 1
            report.notes.append(f"{ref.rel_path}: GAGAL - {doc.error}")
            continue
        chunks = chunk_document(doc)
        if not chunks:
            report.failed += 1
            report.notes.append(f"{ref.rel_path}: tidak menghasilkan chunk, dilewati")
            continue
        pending.append((doc, chunks))

    if pending:
        all_chunks = [c for _, chs in pending for c in chs]
        vecs = embedder.embed_chunks(all_chunks, cache=store, progress=progress)
        report.new_embeddings += getattr(embedder, "last_new_embeddings", 0)
        pos = 0
        for doc, chs in pending:
            store.replace_document(doc, chs, vecs[pos:pos + len(chs)])
            pos += len(chs)
            report.indexed += 1
            report.chunks += len(chs)
    return report


def index_document(doc: Document, store: VectorStore, embedder: Embedder) -> int:
    """Indeks satu Document (dipakai add_note). Kembalikan jumlah chunk."""
    chunks = chunk_document(doc)
    if not chunks:
        return 0
    vecs = embedder.embed_chunks(chunks, cache=store)
    store.replace_document(doc, chunks, vecs)
    return len(chunks)
