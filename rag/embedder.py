"""Embedding dengan cache, batching, rate limit (teks/menit + token/menit), dan retry.

Batas free tier embedding (config.EMBEDDING): RPM, TPM, RPD. Satu request berisi
banyak teks bisa menghabiskan batas per menit sekaligus, jadi di sini kita menjaga:
  - jumlah TEKS per menit  (anggap tiap teks dihitung sebagai 1 request, perkiraan aman)
  - jumlah TOKEN per menit (perkiraan: karakter / CHARS_PER_TOKEN)
Bila tetap kena 429, ukuran batch diperkecil otomatis lalu menunggu dan mencoba lagi.
Hasil tiap batch langsung masuk cache, jadi proses yang terputus bisa dilanjutkan.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Callable

import numpy as np

import config
from providers.base import BaseProvider, ProviderError, RateLimitError
from usage import backoff_delay, limiter, tracker

log = logging.getLogger(__name__)

CHARS_PER_TOKEN = 3.0     # konservatif: perkiraan token = karakter / 3
COUNT_PER_TEXT = True     # hitung kuota harian per teks (perkiraan aman)
MIN_ITEM_CAP = 5
MIN_TOKEN_CAP = 1500
MAX_429 = 6               # berapa kali 429 beruntun sebelum menyerah
SHRINK = 0.6              # batch dikecilkan 40% setiap kena 429


def est_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1


def normalize(arr) -> np.ndarray:
    a = np.asarray(arr, dtype=np.float32)
    if a.ndim == 1:
        a = a[None, :]
    n = np.linalg.norm(a, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return a / n


class Embedder:
    def __init__(self, provider: BaseProvider | None = None):
        if provider is None:
            try:
                from keypool.pooled import PooledProvider
                provider = PooledProvider()
            except FileNotFoundError:               # pool.toml belum dibuat: perilaku lama
                from providers.gemini import GeminiProvider
                provider = GeminiProvider(model=config.EMBEDDING["model"])
        self.provider = provider
        self.model = getattr(provider, "embed_model", provider.model)
        self.last_new_embeddings = 0
        self._window: deque = deque()          # (waktu, token, jumlah_teks)

        cfg = config.get_limits(provider.name, self.model)
        ratio = config.RPM_SAFETY_RATIO
        self.eff_rpm = int(cfg["rpm"] * ratio) if cfg.get("rpm") else None
        self.eff_tpm = int(cfg["tpm"] * ratio) if cfg.get("tpm") else None
        maxb = max(1, provider.max_embed_batch)
        if self.eff_tpm and self.eff_rpm:
            self.token_cap = max(MIN_TOKEN_CAP, self.eff_tpm // 4)
            self.item_cap = max(MIN_ITEM_CAP, min(maxb, self.eff_rpm // 4))
        else:
            self.token_cap, self.item_cap = 10 ** 9, maxb

    @property
    def embedding_id(self) -> str:
        if self.provider.name == "mock":
            return f"mock:{self.provider.model}:{self.provider.embed_dim}"
        sig = getattr(getattr(self.provider, "router", None), "embedding_signature", None)
        return sig or config.embedding_id()

    # ------------------------------------------------------------ penjaga per menit
    def _wait_for_capacity(self, n_items: int, tokens: int) -> None:
        if not (self.eff_rpm and self.eff_tpm):
            return
        while True:
            now = time.monotonic()
            while self._window and now - self._window[0][0] >= 60:
                self._window.popleft()
            used_tok = sum(w[1] for w in self._window)
            used_items = sum(w[2] for w in self._window)
            if not self._window or (used_items + n_items <= self.eff_rpm
                                    and used_tok + tokens <= self.eff_tpm):
                return
            wait = self._window[0][0] + 60 - now + 0.1
            if wait > 2:
                print(f"  [batas] menunggu {wait:.0f} detik "
                      f"(batas per menit: {self.eff_rpm} teks / {self.eff_tpm} token)")
            time.sleep(max(wait, 0.1))

    def _record(self, n_items: int, tokens: int, ok: bool) -> None:
        p = self.provider
        if COUNT_PER_TEXT and n_items > 1:
            for i in range(n_items):
                tracker.record(p.name, self.model, ok=ok, input_tokens=tokens if i == 0 else 0)
        else:
            tracker.record(p.name, self.model, ok=ok, input_tokens=tokens)

    # ------------------------------------------------------------ satu request
    def _send(self, texts: list[str], task: str, tokens: int):
        p = self.provider
        for attempt in range(1, config.RETRY_MAX_ATTEMPTS + 1):
            if tracker.near_daily_limit(p.name, self.model):
                raise RateLimitError(
                    f"Batas harian embedding hampir habis "
                    f"({tracker.requests_today(p.name, self.model)} hitungan hari ini). "
                    "Coba lagi besok; hasil yang sudah selesai tersimpan di cache.",
                    provider=p.name, model=self.model, retryable=False)
            self._wait_for_capacity(len(texts), tokens)
            limiter.acquire(p.name, self.model)
            self._window.append((time.monotonic(), tokens, len(texts)))
            try:
                vecs = p.embed(texts, task)
                self._record(len(texts), tokens, True)
                return vecs
            except RateLimitError:
                self._record(len(texts), tokens, False)
                raise                      # ditangani _embed_items (perkecil batch + tunggu)
            except ProviderError as e:
                self._record(len(texts), tokens, False)
                if not e.retryable or attempt == config.RETRY_MAX_ATTEMPTS:
                    raise
                delay = backoff_delay(attempt, e.retry_after)
                log.warning("Embedding gagal (%s), coba lagi dalam %.0f detik", e, delay)
                print(f"  [retry] embedding gagal, menunggu {delay:.0f} detik...")
                time.sleep(delay)

    # ------------------------------------------------------------ banyak teks
    def _embed_items(self, items: list[tuple[str, str]], task: str,
                     sink: Callable[[dict], None] | None = None,
                     progress: Callable[[int, int], None] | None = None) -> dict[str, np.ndarray]:
        """items = [(kunci, teks)] -> {kunci: vektor ternormalisasi}."""
        out: dict[str, np.ndarray] = {}
        total = len(items)
        if self.eff_tpm and self.eff_rpm and total:
            tok = sum(est_tokens(t) for _, t in items)
            minutes = max(tok / self.eff_tpm, total / self.eff_rpm)
            if minutes > 1:
                print(f"  Perkiraan: {total} teks (~{tok} token), sekitar {minutes:.0f} menit "
                      "karena batas per menit.")
        pending = deque(items)
        fails = 0
        while pending:
            batch, tok = [], 0
            while pending and len(batch) < self.item_cap:
                t = est_tokens(pending[0][1])
                if batch and tok + t > self.token_cap:
                    break
                batch.append(pending.popleft())
                tok += t
            try:
                vecs = normalize(self._send([t for _, t in batch], task, tok))
            except RateLimitError as e:
                if not e.retryable:
                    raise
                fails += 1
                if fails > MAX_429:
                    raise
                self.token_cap = max(MIN_TOKEN_CAP, int(self.token_cap * SHRINK))
                self.item_cap = max(MIN_ITEM_CAP, int(self.item_cap * SHRINK))
                limiter.penalize(self.provider.name, self.model, e.retry_after)
                delay = max(backoff_delay(fails, e.retry_after), 30.0 * fails)
                print(f"  [429] {str(e)[:160]}\n"
                      f"  batch diperkecil (maks {self.item_cap} teks), menunggu {delay:.0f} detik...")
                time.sleep(delay)
                self._window.clear()
                pending.extendleft(reversed(batch))
                continue
            fails = 0
            new = {k: v for (k, _), v in zip(batch, vecs)}
            out.update(new)
            if sink:
                sink(new)
            if progress:
                progress(len(out), total)
        return out

    # ------------------------------------------------------------ publik
    def embed_query(self, text: str) -> np.ndarray:
        """Vektor 1D ternormalisasi untuk satu query."""
        return self._embed_items([("q", text)], "query")["q"]

    def embed_chunks(self, chunks: list, cache=None,
                     progress: Callable[[int, int], None] | None = None) -> np.ndarray:
        """Vektor (n, dim) sejajar dengan `chunks`. `cache` = VectorStore."""
        if not chunks:
            return np.zeros((0, 0), dtype=np.float32)
        hashes = [c.content_hash for c in chunks]
        found = dict(cache.cache_get_many(hashes)) if cache is not None else {}
        missing: dict[str, str] = {}
        for c in chunks:
            if c.content_hash not in found and c.content_hash not in missing:
                missing[c.content_hash] = c.embed_text
        self.last_new_embeddings = len(missing)
        if missing:
            found.update(self._embed_items(
                list(missing.items()), "document",
                sink=cache.cache_put_many if cache is not None else None,
                progress=progress))
        return np.vstack([found[h] for h in hashes])
