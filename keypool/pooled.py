"""PooledProvider: membungkus router sebagai BaseProvider.

Dipakai agar Embedder (dan kode lain yang menerima BaseProvider) memakai pool TANPA diubah.
Namanya "keypool" sengaja tidak ada di config.get_limits(), jadi penjaga RPM/TPM/RPD lama di
embedder.py tidak aktif; pembatasan dan failover dikerjakan router (per account, per model).
"""
from __future__ import annotations

from providers.base import BaseProvider

from .router import LLMRouter, get_router


class PooledProvider(BaseProvider):
    name = "keypool"
    supports_embedding = True

    def __init__(self, router: LLMRouter | None = None):
        self.router = router or get_router()
        emb = self.router.pool.embedding
        super().__init__(model=emb.model or "pool")
        self.embed_model = emb.model
        self.max_embed_batch = emb.batch_size

    def chat(self, messages, tools=None):
        return self.router.chat(messages, tools)

    def embed(self, texts, task="document"):
        return self.router.embed(texts, task)
