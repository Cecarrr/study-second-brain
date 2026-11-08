"""BM25 sederhana (tanpa dependensi) untuk pencarian kata kunci.

Dipakai sebagai cadangan saat embedding query tidak tersedia (kuota habis, pool diblokir,
jaringan putus), sehingga search_knowledge tetap menjawab. Tidak memahami makna: query
bahasa Indonesia tidak akan menemukan materi bahasa Inggris, dan sebaliknya. Itu sebabnya
hasilnya diberi catatan "mode kata kunci" saat dikirim ke LLM (lihat rag/retriever.py).

Rumus (Robertson/Lucene): idf = ln(1 + (N - n + 0.5) / (n + 0.5)),
skor = sum idf * f * (k1 + 1) / (f + k1 * (1 - b + b * len / avglen)).
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter, defaultdict
from typing import Iterable

import config

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)

# Kata fungsi yang hanya menambah noise. Sengaja pendek; istilah teknis tidak disentuh.
STOPWORDS = frozenset("""
yang dan di ke dari untuk pada dengan ini itu adalah atau apa apakah bagaimana mengapa kenapa
kapan siapa mana jelaskan tentang dalam akan juga saya aku kamu tolong sebagai oleh karena agar
sudah belum bisa ada tidak lagi saja dong sih
the a an of to in on for and or is are was were be what how why when which who explain with by
from that this it as at can do does
""".split())

_CLITICS = ("nya", "lah", "kah")   # normalisasi -> normalisasinya, normalisasilah
_MIN_STEM = 4                      # sisa kata minimal setelah clitic dibuang


def _stem(tok: str) -> str:
    for suf in _CLITICS:
        if tok.endswith(suf) and len(tok) - len(suf) >= _MIN_STEM:
            return tok[: -len(suf)]
    return tok


def tokenize(text: str) -> list[str]:
    """Huruf kecil, pecah di non-alfanumerik, buang stopword dan token 1 huruf (angka 1 digit tetap)."""
    text = unicodedata.normalize("NFKC", text or "").lower()
    out = []
    for t in _TOKEN_RE.findall(text):
        if t in STOPWORDS or (len(t) < 2 and not t.isdigit()):
            continue
        out.append(_stem(t))
    return out


class BM25Index:
    """Indeks BM25 dalam memori di atas daftar dokumen yang sudah ditokenisasi."""

    def __init__(self, docs_tokens: Iterable[list[str]], k1: float | None = None,
                 b: float | None = None):
        self.k1 = config.BM25_K1 if k1 is None else k1
        self.b = config.BM25_B if b is None else b
        docs = [list(d) for d in docs_tokens]
        self.n = len(docs)
        self.doc_len = [len(d) for d in docs]
        self.avgdl = (sum(self.doc_len) / self.n) if self.n else 0.0
        self.tf = [Counter(d) for d in docs]
        self.postings: dict[str, list[int]] = defaultdict(list)
        for i, c in enumerate(self.tf):
            for term in c:
                self.postings[term].append(i)
        self.idf = {t: math.log(1 + (self.n - len(p) + 0.5) / (len(p) + 0.5))
                    for t, p in self.postings.items()}

    def search(self, terms: list[str], allowed: set[int] | None = None, k: int = 5,
               min_matched: int = 1) -> list[tuple[int, float, int]]:
        """Kembalikan [(indeks_dokumen, skor, jumlah_kata_cocok)] terurut skor menurun.

        `allowed` membatasi kandidat (filter mata kuliah/jenis). `min_matched` = jumlah kata
        query berbeda yang minimal harus ada di dokumen (dipakai untuk membuang kecocokan
        kebetulan pada satu kata umum)."""
        scores: dict[int, float] = defaultdict(float)
        matched: dict[int, int] = defaultdict(int)
        avgdl = self.avgdl or 1.0
        for term in dict.fromkeys(terms):            # unik, urutan terjaga
            idf = self.idf.get(term)
            if idf is None:
                continue
            for i in self.postings[term]:
                if allowed is not None and i not in allowed:
                    continue
                f = self.tf[i][term]
                norm = f + self.k1 * (1 - self.b + self.b * self.doc_len[i] / avgdl)
                scores[i] += idf * f * (self.k1 + 1) / norm
                matched[i] += 1
        ranked = [i for i in scores if matched[i] >= min_matched]
        ranked.sort(key=lambda i: (-scores[i], i))
        return [(i, scores[i], matched[i]) for i in ranked[:k]]
