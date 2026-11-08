"""Query -> embedding -> top-k chunk, dengan ambang skor minimum."""
from __future__ import annotations

from dataclasses import dataclass, field

import config
from rag.embedder import Embedder
from rag.store import Hit, VectorStore


@dataclass
class SearchResult:
    query: str
    hits: list[Hit] = field(default_factory=list)   # sudah lolos ambang skor
    best_score: float | None = None                 # skor tertinggi sebelum ambang
    min_score: float = 0.0

    def format(self) -> str:
        """Teks untuk LLM (hasil tool search_knowledge)."""
        if not self.hits:
            best = f" (skor tertinggi {self.best_score:.2f})" if self.best_score is not None else ""
            return ("Tidak ditemukan bagian materi atau catatan yang relevan untuk pertanyaan ini"
                    f"{best}.")
        blocks = []
        for i, h in enumerate(self.hits, 1):
            where = h.source + (f" ({h.location})" if h.location else "")
            heading = h.metadata.get("heading")
            head = f" | {heading}" if heading else ""
            blocks.append(f"[{i}] skor {h.score:.2f} | {where}{head}\n{h.text}")
        return "\n\n".join(blocks)


class Retriever:
    def __init__(self, store: VectorStore, embedder: Embedder,
                 top_k: int | None = None, min_score: float | None = None):
        self.store = store
        self.embedder = embedder
        self.top_k = top_k or config.TOP_K
        self.min_score = config.MIN_SCORE if min_score is None else min_score

    def search(self, query: str, k: int | None = None, course: str | None = None,
               source_type: str | None = None, min_score: float | None = None) -> SearchResult:
        query = (query or "").strip()
        ms = self.min_score if min_score is None else min_score
        if not query or len(self.store) == 0:
            return SearchResult(query, [], None, ms)
        qvec = self.embedder.embed_query(query)
        hits = self.store.search(qvec, k=k or self.top_k, min_score=-1.0,
                                 course=course, source_type=source_type)
        best = hits[0].score if hits else None
        return SearchResult(query, [h for h in hits if h.score >= ms], best, ms)
