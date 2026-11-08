"""Format internal netral untuk pesan, tool, dan tool call.

Agent dan tools hanya mengenal tipe di file ini. Tiap adapter (gemini.py,
openrouter.py, mock.py) menerjemahkannya ke format API masing-masing, sehingga
failover di tengah percakapan cukup mengirim ulang riwayat dari format ini.
"""
from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


def new_call_id() -> str:
    return "call_" + uuid.uuid4().hex[:8]


# ---------------------------------------------------------------- Tipe data
@dataclass
class ToolSpec:
    """Definisi tool untuk LLM. `parameters` = JSON Schema (type: object)."""
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=new_call_id)
    # Data khusus provider yang harus dikirim balik apa adanya
    # (mis. thought signature Gemini). Provider lain mengabaikannya.
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Message:
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)  # role "assistant"
    tool_call_id: str | None = None                            # role "tool"
    name: str | None = None                                    # role "tool": nama tool
    is_error: bool = False                                     # role "tool"

    @classmethod
    def system(cls, content: str) -> "Message":
        return cls("system", content)

    @classmethod
    def user(cls, content: str) -> "Message":
        return cls("user", content)

    @classmethod
    def assistant(cls, content: str = "", tool_calls: list[ToolCall] | None = None) -> "Message":
        return cls("assistant", content, tool_calls=list(tool_calls or []))

    @classmethod
    def tool(cls, call: ToolCall, content: str, is_error: bool = False) -> "Message":
        return cls("tool", content, tool_call_id=call.id, name=call.name, is_error=is_error)


@dataclass
class ChatResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None
    finish_reason: str | None = None
    provider: str | None = None
    model: str | None = None

    def to_message(self) -> Message:
        return Message.assistant(self.text, self.tool_calls)


# ---------------------------------------------------------------- Error
class ProviderError(Exception):
    """Dasar semua error provider.

    retryable   : layak dicoba ulang di provider yang sama (backoff).
    failover_ok : boleh pindah ke provider berikutnya bila retry habis.
    """
    retryable = False
    failover_ok = False

    def __init__(self, message: str, *, provider: str | None = None,
                 model: str | None = None, status: int | None = None,
                 retry_after: float | None = None,
                 retryable: bool | None = None):
        super().__init__(message)
        self.provider = provider
        self.model = model
        self.status = status
        self.retry_after = retry_after
        if retryable is not None:
            self.retryable = retryable


class RateLimitError(ProviderError):
    retryable = True
    failover_ok = True


class ServerError(ProviderError):
    retryable = True
    failover_ok = True


class ProviderTimeout(ProviderError):
    retryable = True
    failover_ok = True


class EmptyResponseError(ProviderError):
    """Model tidak mengembalikan teks maupun tool call."""
    retryable = True
    failover_ok = True


class AuthError(ProviderError):
    """API key salah/kurang, atau tidak punya akses ke model."""


class BadRequestError(ProviderError):
    """Permintaan ditolak (biasanya bug di sisi kita); jangan di-failover."""


# ---------------------------------------------------------------- Antarmuka
class BaseProvider(ABC):
    name: str = "base"
    supports_embedding: bool = False
    max_embed_batch: int = 100

    def __init__(self, model: str):
        self.model = model

    @property
    def key(self) -> str:
        return f"{self.name}/{self.model}"

    @abstractmethod
    def chat(self, messages: list[Message],
             tools: list[ToolSpec] | None = None) -> ChatResponse:
        """Satu request ke LLM. Pesan role "system" (jika ada) diambil adapter."""

    def embed(self, texts: list[str], task: str = "document") -> list[list[float]]:
        """Satu request embedding. task: "document" atau "query".

        Pemecahan ke batch dan pencatatan kuota dilakukan pemanggil (llm.py),
        jadi `len(texts)` tidak boleh melebihi `max_embed_batch`.
        """
        raise NotImplementedError(f"{self.name} tidak mendukung embedding")


# ---------------------------------------------------------------- Helper adapter
def split_system(messages: list[Message]) -> tuple[str, list[Message]]:
    """Pisahkan prompt sistem dari pesan lain."""
    system = "\n\n".join(m.content for m in messages if m.role == "system" and m.content)
    rest = [m for m in messages if m.role != "system"]
    return system, rest


def normalize_args(value: Any) -> Any:
    """Ubah float bulat (5.0) menjadi int (5), rekursif.

    API Gemini mengembalikan semua angka JSON sebagai float, padahal
    argumen seperti task_id atau top_k harus int.
    """
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: normalize_args(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize_args(v) for v in value]
    return value
