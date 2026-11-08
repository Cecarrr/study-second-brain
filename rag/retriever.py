"""Query -> embedding -> top-k chunk, dengan ambang skor minimum.

Bila embedding query gagal (kuota habis, pool diblokir, jaringan), pencarian turun ke
BM25 kata kunci (rag/bm25.py) supaya search_knowledge tetap menjawab. Setelah satu
kegagalan, embedding tidak dicoba lagi selama config.EMBED_FAIL_COOLDOWN_S detik agar
setiap pencarian tidak menunggu kegagalan yang sama.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable

import config
from providers.base import ProviderError
from rag.embedder import Embedder
from rag.store import Hit, VectorStore

log = logging.getLogger(__name__)

KEYWORD_NOTE = (
    "(Mode kata kunci: pencarian makna sedang tidak tersedia, jadi hanya kata yang sama persis "
    "yang cocok. Query bahasa lain atau sinonim bisa gagal; bila hasilnya kurang, coba istilah "
    "persis yang mungkin dipakai di materi, dalam bahasa Indonesia atau Inggris.)")


@dataclass
class SearchResult:
    query: str
    hits: list[Hit] = field(default_factory=list)   # sudah lolos ambang skor
    best_score: float | None = None                 # skor tertinggi sebelum ambang (mode vektor)
    min_score: float = 0.0
    mode: str = "vector"                            # "vector" | "keyword"
    fallback_reason: str | None = None              # kenapa turun ke kata kunci

    def format(self) -> str:
        """Teks untuk LLM (hasil tool search_knowledge)."""
        keyword = self.mode == "keyword"
        note = KEYWORD_NOTE + "\n" if keyword else ""
        if not self.hits:
            best = f" (skor tertinggi {self.best_score:.2f})" if self.best_score is not None else ""
            return note + ("Tidak ditemukan bagian materi atau catatan yang relevan untuk pertanyaan "
                           f"ini{best}.")
        blocks = []
        for i, h in enumerate(self.hits, 1):
            where = h.source + (f" ({h.location})" if h.location else "")
            heading = h.metadata.get("heading")
            head = f" | {heading}" if heading else ""
            # Skor BM25 tidak sebanding dengan cosine; di mode kata kunci cukup urutannya.
            tag = "kata kunci" if keyword else f"skor {h.score:.2f}"
            blocks.append(f"[{i}] {tag} | {where}{head}\n{h.text}")
        return note + "\n\n".join(blocks)


class Retriever:
    def __init__(self, store: VectorStore, embedder: Embedder,
                 top_k: int | None = None, min_score: float | None = None,
                 keyword_fallback: bool | None = None, cooldown_s: float | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.store = store
        self.embedder = embedder
        self.top_k = top_k or config.TOP_K
        self.min_score = config.MIN_SCORE if min_score is None else min_score
        self.keyword_fallback = config.KEYWORD_FALLBACK if keyword_fallback is None else keyword_fallback
        self.cooldown_s = config.EMBED_FAIL_COOLDOWN_S if cooldown_s is None else cooldown_s
        self.clock = clock
        self._embed_down_until = 0.0

    def search(self, query: str, k: int | None = None, course: str | None = None,
               source_type: str | None = None, min_score: float | None = None) -> SearchResult:
        query = (query or "").strip()
        ms = self.min_score if min_score is None else min_score
        if not query or len(self.store) == 0:
            return SearchResult(query, [], None, ms)
        k = k or self.top_k

        reason = None
        if self.keyword_fallback and self.clock() < self._embed_down_until:
            reason = "embedding dijeda sementara setelah kegagalan sebelumnya"
        else:
            try:
                qvec = self.embedder.embed_query(query)
            except ProviderError as e:
                if not self.keyword_fallback:
                    raise
                self._embed_down_until = self.clock() + self.cooldown_s
                reason = str(e).splitlines()[0][:160] if str(e) else type(e).__name__
                log.warning("Embedding query gagal, pakai pencarian kata kunci: %s", reason)
            else:
                self._embed_down_until = 0.0
                hits = self.store.search(qvec, k=k, min_score=-1.0,
                                         course=course, source_type=source_type)
                best = hits[0].score if hits else None
                return SearchResult(query, [h for h in hits if h.score >= ms], best, ms)

        hits = self.store.keyword_search(query, k=k, course=course, source_type=source_type)
        return SearchResult(query, hits, None, ms, mode="keyword", fallback_reason=reason)
