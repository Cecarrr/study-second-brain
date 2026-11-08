"""Vector store buatan sendiri.

Penyimpanan: SQLite (data/vector_store.db). Pencarian: matriks numpy di memori
dan cosine similarity (vektor sudah dinormalisasi, jadi cukup perkalian matriks).

Tabel:
  meta         : id model embedding yang dipakai indeks (cegah campur model)
  documents    : satu baris per file (doc_id, sha256 untuk deteksi perubahan)
  chunks       : teks + metadata + vektor
  embed_cache  : vektor per isi chunk (content_hash), supaya ingest ulang / rebuild
                 tidak memakai request lagi untuk teks yang sama
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import numpy as np

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY, source_type TEXT, rel_path TEXT, course TEXT,
    title TEXT, sha256 TEXT, n_chunks INTEGER, indexed_at TEXT);
CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY, doc_id TEXT, chunk_index INTEGER, text TEXT, header TEXT,
    metadata TEXT, content_hash TEXT, vector BLOB);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
CREATE TABLE IF NOT EXISTS embed_cache (
    content_hash TEXT, embedding_id TEXT, vector BLOB,
    PRIMARY KEY (content_hash, embedding_id));
"""


class EmbeddingMismatchError(RuntimeError):
    """Model/dimensi embedding indeks berbeda dengan konfigurasi sekarang."""


def norm_name(s: str | None) -> str:
    """'Basis Data', 'basis-data', 'basis_data' dianggap sama."""
    return re.sub(r"[\s_\-]+", "", (s or "").lower())


@dataclass
class Hit:
    id: str
    score: float
    text: str
    header: str
    metadata: dict = field(default_factory=dict)

    @property
    def source(self) -> str:
        return self.metadata.get("rel_path") or self.metadata.get("source_file") or "?"

    @property
    def location(self) -> str:
        m = self.metadata
        a, b, label = m.get("page_start"), m.get("page_end"), m.get("page_label")
        if a is None or not label:
            return ""
        return f"{label} {a}" if a == b else f"{label} {a}-{b}"


class VectorStore:
    def __init__(self, path: str | Path | None = None, embedding_id: str | None = None,
                 rebuild: bool = False):
        self.path = Path(path or config.VECTOR_STORE_PATH)
        self.embedding_id = embedding_id or config.embedding_id()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.executescript(SCHEMA)
        self._matrix = np.zeros((0, 0), dtype=np.float32)
        self._rows: list[dict] = []
        self._dirty = True
        self._init_meta(rebuild)

    # ------------------------------------------------------------ meta
    def _get_meta(self, key: str) -> str | None:
        r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def _set_meta(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))

    def _init_meta(self, rebuild: bool) -> None:
        stored = self._get_meta("embedding_id")
        if rebuild:
            with self.db:
                self.db.execute("DELETE FROM chunks")
                self.db.execute("DELETE FROM documents")
            self._set_meta("embedding_id", self.embedding_id)
        elif stored is None:
            self._set_meta("embedding_id", self.embedding_id)
        elif stored != self.embedding_id:
            n = self.db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            if n:
                raise EmbeddingMismatchError(
                    f"Indeks dibuat dengan embedding '{stored}', tetapi konfigurasi sekarang "
                    f"'{self.embedding_id}'. Vektor dari model berbeda tidak kompatibel. "
                    "Kembalikan EMBED_MODEL/EMBED_DIM di .env, atau jalankan: "
                    "python index_materi.py --rebuild")
            self._set_meta("embedding_id", self.embedding_id)

    # ------------------------------------------------------------ memuat ke memori
    def _ensure_loaded(self) -> None:
        if not self._dirty:
            return
        rows = self.db.execute(
            "SELECT id, doc_id, text, header, metadata, vector FROM chunks "
            "ORDER BY doc_id, chunk_index").fetchall()
        self._rows, vecs = [], []
        for id_, doc_id, text, header, meta, blob in rows:
            m = json.loads(meta) if meta else {}
            self._rows.append({"id": id_, "doc_id": doc_id, "text": text, "header": header,
                               "metadata": m, "course": norm_name(m.get("course")),
                               "type": m.get("source_type")})
            vecs.append(np.frombuffer(blob, dtype=np.float32))
        self._matrix = np.vstack(vecs) if vecs else np.zeros((0, 0), dtype=np.float32)
        self._dirty = False

    @property
    def dim(self) -> int | None:
        self._ensure_loaded()
        return int(self._matrix.shape[1]) if self._matrix.size else None

    def __len__(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    # ------------------------------------------------------------ dokumen
    def doc_hash(self, doc_id: str) -> str | None:
        r = self.db.execute("SELECT sha256 FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
        return r[0] if r else None

    def list_documents(self, source_type: str | None = None) -> list[dict]:
        q = "SELECT doc_id, source_type, rel_path, course, title, sha256, n_chunks FROM documents"
        args: tuple = ()
        if source_type:
            q += " WHERE source_type=?"
            args = (source_type,)
        cols = ["doc_id", "source_type", "rel_path", "course", "title", "sha256", "n_chunks"]
        return [dict(zip(cols, r)) for r in self.db.execute(q + " ORDER BY rel_path", args)]

    def replace_document(self, doc, chunks: list, vectors) -> None:
        """Simpan dokumen beserta chunk dan vektornya (menggantikan versi lama)."""
        vectors = np.asarray(vectors, dtype=np.float32)
        if len(chunks) != len(vectors):
            raise ValueError("Jumlah chunk dan vektor tidak sama")
        existing = self.dim
        if len(chunks) and existing and vectors.shape[1] != existing:
            raise EmbeddingMismatchError(
                f"Dimensi vektor baru ({vectors.shape[1]}) berbeda dari indeks ({existing}). "
                "Jalankan: python index_materi.py --rebuild")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rows = [(c.id, doc.doc_id, i, c.text, c.header,
                 json.dumps(c.metadata, ensure_ascii=False), c.content_hash,
                 vectors[i].tobytes()) for i, c in enumerate(chunks)]
        with self.db:
            self.db.execute("DELETE FROM chunks WHERE doc_id=?", (doc.doc_id,))
            self.db.execute(
                "INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?,?,?,?)",
                (doc.doc_id, doc.source_type, doc.rel_path, doc.course,
                 doc.title or doc.filename, doc.ref.sha256, len(chunks), now))
            self.db.executemany("INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?)", rows)
        self._dirty = True

    def delete_document(self, doc_id: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
            self.db.execute("DELETE FROM documents WHERE doc_id=?", (doc_id,))
        self._dirty = True

    # ------------------------------------------------------------ cache embedding
    def cache_get_many(self, hashes: Iterable[str]) -> dict[str, np.ndarray]:
        hashes = list(set(hashes))
        out: dict[str, np.ndarray] = {}
        for i in range(0, len(hashes), 500):
            part = hashes[i:i + 500]
            q = ",".join("?" * len(part))
            for h, blob in self.db.execute(
                    f"SELECT content_hash, vector FROM embed_cache "
                    f"WHERE embedding_id=? AND content_hash IN ({q})",
                    [self.embedding_id, *part]):
                out[h] = np.frombuffer(blob, dtype=np.float32)
        return out

    def cache_put_many(self, items: dict[str, np.ndarray]) -> None:
        with self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO embed_cache VALUES (?,?,?)",
                [(h, self.embedding_id, np.asarray(v, dtype=np.float32).tobytes())
                 for h, v in items.items()])

    # ------------------------------------------------------------ pencarian
    def search(self, qvec, k: int = 5, min_score: float = 0.0, course: str | None = None,
               source_type: str | None = None) -> list[Hit]:
        """Top-k chunk berdasarkan cosine similarity. qvec harus sudah dinormalisasi."""
        self._ensure_loaded()
        if not self._rows:
            return []
        q = np.asarray(qvec, dtype=np.float32).ravel()
        if q.shape[0] != self._matrix.shape[1]:
            raise EmbeddingMismatchError(
                f"Dimensi query ({q.shape[0]}) berbeda dari indeks ({self._matrix.shape[1]})")
        want = norm_name(course) if course else None
        idx = [i for i, r in enumerate(self._rows)
               if (want is None or r["course"] == want)
               and (source_type is None or r["type"] == source_type)]
        if not idx:
            return []
        scores = self._matrix[idx] @ q
        order = np.argsort(-scores)[:k]
        hits = []
        for o in order:
            s = float(scores[o])
            if s < min_score:
                break
            r = self._rows[idx[o]]
            hits.append(Hit(r["id"], s, r["text"], r["header"], r["metadata"]))
        return hits

    # ------------------------------------------------------------ info
    def courses(self) -> list[str]:
        return [r[0] for r in self.db.execute(
            "SELECT DISTINCT course FROM documents ORDER BY course")]

    def stats(self) -> dict:
        by_type = dict(self.db.execute(
            "SELECT source_type, COUNT(*) FROM documents GROUP BY source_type"))
        by_course = dict(self.db.execute(
            "SELECT course, SUM(n_chunks) FROM documents GROUP BY course"))
        return {"embedding_id": self.embedding_id, "dim": self.dim,
                "documents": by_type, "chunks": len(self), "chunks_per_course": by_course}

    def close(self) -> None:
        self.db.close()
