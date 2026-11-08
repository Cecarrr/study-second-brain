"""MockProvider: menguji agent loop, tools, dan retrieval tanpa satu request pun.

Contoh:

    from providers.mock import MockProvider, call
    p = MockProvider(script=[
        call("add_task", title="Laporan basis data", due_date="2026-11-12"),
        "Tugas sudah dicatat.",
    ])

Tiap panggilan chat() mengambil satu item dari script berurutan:
  - str                       -> respons teks
  - dict                      -> {"text": "...", "tool_calls": [{"name", "args"}]}
  - ChatResponse              -> dikembalikan apa adanya
  - Exception                 -> di-raise (uji retry/failover)
  - callable(messages, tools) -> hasilnya diproses sama seperti item lain
Bila script habis, dipakai balasan bawaan (echo / ringkasan hasil tool).
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import Any, Callable

from providers.base import (
    BaseProvider, ChatResponse, Message, ToolCall, ToolSpec,
)


def call(name: str, **args: Any) -> dict:
    """Item script berisi satu tool call: call("add_note", content="...")."""
    return {"tool_calls": [{"name": name, "args": args}]}


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class MockProvider(BaseProvider):
    name = "mock"
    supports_embedding = True
    max_embed_batch = 1000

    def __init__(self, script: list[Any] | None = None, model: str = "mock-1",
                 embed_dim: int = 256):
        super().__init__(model)
        self.script: list[Any] = list(script or [])
        self.embed_dim = embed_dim
        self.chat_calls: list[dict] = []    # riwayat untuk assert di tes
        self.embed_calls: list[dict] = []

    # ------------------------------------------------------------ chat
    def chat(self, messages: list[Message],
             tools: list[ToolSpec] | None = None) -> ChatResponse:
        self.chat_calls.append({
            "messages": list(messages),
            "tools": [t.name for t in (tools or [])],
        })
        item = self.script.pop(0) if self.script else self._default(messages)
        resp = self._materialize(item, messages, tools)
        resp.provider, resp.model = self.name, self.model
        if resp.input_tokens is None:
            resp.input_tokens = sum(_approx_tokens(m.content or " ") for m in messages)
        if resp.output_tokens is None:
            resp.output_tokens = _approx_tokens(resp.text or " ")
        return resp

    def _materialize(self, item: Any, messages: list[Message],
                     tools: list[ToolSpec] | None) -> ChatResponse:
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return self._materialize(item(messages, tools), messages, tools)
        if isinstance(item, ChatResponse):
            return item
        if isinstance(item, str):
            return ChatResponse(text=item, finish_reason="STOP")
        if isinstance(item, dict):
            calls = [ToolCall(name=c["name"], args=dict(c.get("args", {})))
                     for c in item.get("tool_calls", [])]
            return ChatResponse(text=item.get("text", ""), tool_calls=calls,
                                finish_reason="STOP")
        raise TypeError(f"Item script tidak dikenal: {type(item).__name__}")

    @staticmethod
    def _default(messages: list[Message]) -> str:
        last = messages[-1] if messages else None
        if last and last.role == "tool":
            return f"[mock] Hasil {last.name}: {last.content[:300]}"
        user = next((m for m in reversed(messages) if m.role == "user"), None)
        return f"[mock] Kamu bilang: {user.content[:200]}" if user else "[mock] (kosong)"

    # ------------------------------------------------------------ embedding
    def embed(self, texts: list[str], task: str = "document") -> list[list[float]]:
        """Vektor deterministik: bag-of-words yang di-hash ke `embed_dim` dimensi.

        Teks dengan kata yang sama menghasilkan vektor berdekatan, jadi
        vector store dan retriever bisa diuji offline (bukan untuk lintas bahasa).
        """
        self.embed_calls.append({"n": len(texts), "task": task})
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.embed_dim
        for tok in re.findall(r"\w+", text.lower()):
            h = int(hashlib.md5(tok.encode("utf-8")).hexdigest()[:12], 16)  # 48 bit
            vec[h % self.embed_dim] += 1.0 if (h >> 44) & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in vec))
        if norm == 0:
            vec[0] = 1.0
            return vec
        return [x / norm for x in vec]
