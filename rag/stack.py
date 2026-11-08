"""Perakit komponen RAG: store + embedder + retriever (dipakai CLI, tools, agent)."""
from __future__ import annotations

import config
from rag.embedder import Embedder
from rag.retriever import Retriever
from rag.store import VectorStore


def open_stack(mock: bool = False, rebuild: bool = False):
    """Kembalikan (store, embedder, retriever).

    mock=True: embedding palsu offline dengan file indeks terpisah
    (vector_store_mock.db), tidak memakai request dan tidak menyentuh indeks asli.
    """
    if mock:
        from providers.mock import MockProvider
        embedder = Embedder(MockProvider(embed_dim=256))
        path = config.VECTOR_STORE_PATH.with_name("vector_store_mock.db")
    else:
        embedder = Embedder()
        path = config.VECTOR_STORE_PATH
    store = VectorStore(path, embedder.embedding_id, rebuild=rebuild)
    return store, embedder, Retriever(store, embedder)
