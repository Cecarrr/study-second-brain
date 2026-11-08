"""Embedder proyek + PooledProvider: tanpa mengubah embedder.py. Jalankan dari root proyek:
python -m unittest keypool.tests.test_embedder_bridge -v"""
import os
import sys
import unittest
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from keypool.pooled import PooledProvider
from keypool.router import PoolExhausted
from keypool.quota import next_reset
from keypool.tests.test_keypool import T0, acct, make

from rag import embedder as emb_mod
from providers.base import RateLimitError


@dataclass
class Chunk:
    content_hash: str
    embed_text: str


class DictCache:
    def __init__(self):
        self.d = {}
    def cache_get_many(self, hashes):
        return {h: self.d[h] for h in hashes if h in self.d}
    def cache_put_many(self, items):
        self.d.update(items)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.router, self.log, self.clock, _ = make(
            [acct("A", "kA", embed=("e1",))], limits={"e1": {"rpd": 4}},
            embedding={"provider": "mock", "model": "e1", "batch_size": 2, "count": "item"})
        self.embedder = emb_mod.Embedder(provider=PooledProvider(self.router))
        self.chunks = [Chunk(f"h{i}", f"teks nomor {i}") for i in range(5)]

    def test_limiter_lama_tidak_aktif_dan_batch_mengikuti_pool(self):
        self.assertIsNone(self.embedder.eff_rpm)
        self.assertEqual(self.embedder.item_cap, 2)

    def test_habis_di_tengah_jalan_tersimpan_di_cache_lalu_lanjut_setelah_reset(self):
        cache = DictCache()
        with self.assertRaises(RateLimitError) as cm:
            self.embedder.embed_chunks(self.chunks, cache=cache)
        self.assertIsInstance(cm.exception, PoolExhausted)
        self.assertFalse(cm.exception.retryable)
        self.assertEqual(len(cache.d), 4)                       # 2 batch tersimpan
        self.clock.t = next_reset(T0, "UTC") + 5
        before = len(self.log)
        vecs = self.embedder.embed_chunks(self.chunks, cache=cache)
        self.assertEqual(vecs.shape[0], 5)
        self.assertEqual(len(self.log) - before, 1)             # hanya 1 teks sisa yang di-embed


if __name__ == "__main__":
    unittest.main()
