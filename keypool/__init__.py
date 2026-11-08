"""keypool: pool API otomatis (rotasi key/model, deteksi kuota habis, failover).

    import keypool
    res = keypool.chat(messages, tools)      # -> providers.base.ChatResponse
    vecs = keypool.embed(texts, "document")
    print(keypool.usage_report())

Bergantung pada providers/base.py dan providers/gemini.py proyek; tidak mengubah file proyek.
"""
from .errors import ErrorKind, classify
from .pool import Pool, PoolConfigError
from .router import (LLMRouter, PoolExhausted, chat, embed, embedding_signature, get_router,
                     usage_report)

__all__ = ["ErrorKind", "classify", "Pool", "PoolConfigError", "LLMRouter", "PoolExhausted",
           "chat", "embed", "embedding_signature", "get_router", "usage_report"]
