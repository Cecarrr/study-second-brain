"""Konteks bersama dan helper untuk semua tool."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable

import config
from db.database import TaskDB, norm


class ToolError(Exception):
    """Kesalahan yang bisa diperbaiki model/pengguna; pesannya dikirim balik ke model."""


@dataclass
class ToolContext:
    retriever: Any                      # rag.retriever.Retriever
    store: Any                          # rag.store.VectorStore
    embedder: Any                       # rag.embedder.Embedder
    tasks: TaskDB
    notes_dir: Path = field(default_factory=lambda: config.NOTES_DIR)
    llm_chat: Callable | None = None    # llm_chat(messages, tools=None) -> ChatResponse
    log_path: Path | None = None        # None = logs/tools.jsonl
    last_sources: list = field(default_factory=list)   # hasil search_knowledge terakhir (/source N)

    def today(self) -> date:
        return self.tasks.today()

    def now(self):
        return self.tasks.now_fn()

    def chat(self, messages, tools=None):
        if self.llm_chat is not None:
            return self.llm_chat(messages, tools)
        import keypool                  # lazy: hanya dibutuhkan bila tool memanggil LLM
        return keypool.chat(messages, tools)

    def known_courses(self) -> list[str]:
        try:
            base = list(self.store.courses())
        except Exception:               # noqa: BLE001
            base = []
        try:
            extra = self.tasks.sched_courses()      # mata kuliah dari jadwal ikut dikenal
        except Exception:               # noqa: BLE001
            extra = []
        seen = {norm(c) for c in base}
        return base + [c for c in extra if norm(c) not in seen]

    def resolve_course(self, name: str | None) -> tuple[str | None, bool]:
        """(nama_kanonik, dikenal). Nama yang cocok dengan mata kuliah di indeks dipakai apa adanya
        dari indeks ('Basis Data' -> 'basis-data'); nama baru dikembalikan seperti diberikan."""
        if not name:
            return None, False
        want = norm(name)
        for c in self.known_courses():
            if norm(c) == want:
                return c, True
        for c in self.known_courses():
            if want and (want in norm(c) or norm(c) in want):
                return c, True
        return name.strip(), False
