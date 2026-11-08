"""Adapter Gemini (SDK `google-genai`): chat + function calling + embedding.

Adapter ini tipis: tidak melakukan retry, rate limit, atau pencatatan kuota.
Itu tugas router di llm.py (lewat usage.py). Di sini hanya penerjemahan format
dan pemetaan error ke tipe di providers/base.py.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

import config
from providers.base import (
    AuthError, BadRequestError, BaseProvider, ChatResponse, EmptyResponseError,
    Message, ProviderError, ProviderTimeout, RateLimitError, ServerError,
    ToolCall, ToolSpec, normalize_args, split_system,
)

log = logging.getLogger(__name__)

_SIG_KEY = "gemini_thought_signature"
_ID_KEY = "gemini_call_id"

_EMBED_TASKS = {"document": "RETRIEVAL_DOCUMENT", "query": "RETRIEVAL_QUERY"}

# finish_reason yang layak dicoba ulang (kegagalan sesaat di sisi model)
_RETRYABLE_FINISH = {"MALFORMED_FUNCTION_CALL", "UNEXPECTED_TOOL_CALL", "OTHER", None}


def _parse_retry_after(text: str) -> float | None:
    """Ambil petunjuk waktu tunggu dari pesan error 429 (detik)."""
    for pattern in (r"retry in ([\d.]+)\s*s", r"retryDelay['\"]?\s*[:=]\s*['\"]?([\d.]+)s"):
        m = re.search(pattern, text, re.I)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
    return None


def _class_names(e: BaseException) -> set[str]:
    return {c.__name__ for c in type(e).__mro__}


def _is_timeout(e: BaseException) -> bool:
    """Timeout dari pustaka HTTP apa pun (httpx, httpx2, requests, builtin).

    SDK google-genai terbaru memakai httpx2, bukan httpx, sehingga isinstance
    terhadap kelas httpx tidak boleh diandalkan.
    """
    return isinstance(e, TimeoutError) or any("Timeout" in n for n in _class_names(e))


def _is_network(e: BaseException) -> bool:
    names = _class_names(e)
    return isinstance(e, ConnectionError) or bool(
        names & {"TransportError", "NetworkError", "ConnectError", "ConnectionError"})


class GeminiProvider(BaseProvider):
    name = "gemini"
    supports_embedding = True
    max_embed_batch = 100

    def __init__(self, model: str, *, embed_model: str | None = None,
                 api_key: str | None = None):
        super().__init__(model)
        key = api_key or config.GEMINI_API_KEY
        if not key:
            raise AuthError("GEMINI_API_KEY belum diisi di .env",
                            provider=self.name, model=model)
        self.embed_model = embed_model or config.EMBEDDING["model"]
        self.max_embed_batch = int(config.EMBEDDING.get("batch_size", 100))
        self.client = genai.Client(
            api_key=key,
            http_options=types.HttpOptions(timeout=int(config.REQUEST_TIMEOUT_S * 1000)),
        )

    # ------------------------------------------------------------ chat
    def chat(self, messages: list[Message],
             tools: list[ToolSpec] | None = None) -> ChatResponse:
        system, rest = split_system(messages)
        cfg_kwargs: dict[str, Any] = {
            "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
        }
        if system:
            cfg_kwargs["system_instruction"] = system
        if tools:
            cfg_kwargs["tools"] = [types.Tool(
                function_declarations=[self._declaration(t) for t in tools])]
        if config.LLM_TEMPERATURE is not None:
            cfg_kwargs["temperature"] = config.LLM_TEMPERATURE
        if config.LLM_MAX_OUTPUT_TOKENS:
            cfg_kwargs["max_output_tokens"] = config.LLM_MAX_OUTPUT_TOKENS

        try:
            resp = self.client.models.generate_content(
                model=self.model,
                contents=self._to_contents(rest),
                config=types.GenerateContentConfig(**cfg_kwargs),
            )
        except Exception as e:  # noqa: BLE001 - dipetakan ke tipe error sendiri
            raise self._map_error(e) from e
        return self._parse_response(resp)

    @staticmethod
    def _declaration(spec: ToolSpec) -> types.FunctionDeclaration:
        try:  # SDK baru: terima JSON Schema apa adanya
            return types.FunctionDeclaration(
                name=spec.name, description=spec.description,
                parameters_json_schema=spec.parameters)
        except Exception:  # noqa: BLE001 - SDK lama
            return types.FunctionDeclaration(
                name=spec.name, description=spec.description,
                parameters=spec.parameters)

    @staticmethod
    def _to_contents(messages: list[Message]) -> list[types.Content]:
        """Format internal -> contents Gemini.

        Hasil tool berurutan digabung dalam satu giliran "user" (aturan Gemini).
        Thought signature disertakan lagi bila ada; Gemini 3 mewajibkannya untuk
        function call pada giliran berjalan.
        """
        contents: list[types.Content] = []
        pending: list[types.Part] = []
        calls: dict[str, ToolCall] = {}

        def flush() -> None:
            if pending:
                contents.append(types.Content(role="user", parts=list(pending)))
                pending.clear()

        for m in messages:
            if m.role == "tool":
                call = calls.get(m.tool_call_id or "")
                fr = types.FunctionResponse(
                    name=m.name or (call.name if call else "tool"),
                    id=(call.extra.get(_ID_KEY) if call else None),
                    response={"error" if m.is_error else "result": m.content},
                )
                pending.append(types.Part(function_response=fr))
                continue

            flush()
            if m.role == "user":
                contents.append(types.Content(
                    role="user", parts=[types.Part.from_text(text=m.content)]))
            elif m.role == "assistant":
                parts: list[types.Part] = []
                if m.content:
                    parts.append(types.Part.from_text(text=m.content))
                for tc in m.tool_calls:
                    calls[tc.id] = tc
                    fc = types.FunctionCall(
                        name=tc.name, args=tc.args, id=tc.extra.get(_ID_KEY))
                    parts.append(types.Part(
                        function_call=fc, thought_signature=tc.extra.get(_SIG_KEY)))
                if parts:
                    contents.append(types.Content(role="model", parts=parts))
        flush()
        return contents

    def _parse_response(self, resp: Any) -> ChatResponse:
        candidates = resp.candidates or []
        if not candidates:
            reason = getattr(getattr(resp, "prompt_feedback", None), "block_reason", None)
            raise EmptyResponseError(
                f"Respons kosong dari Gemini (block_reason={reason})",
                provider=self.name, model=self.model, retryable=False)

        cand = candidates[0]
        finish = getattr(cand.finish_reason, "name", None) or (
            str(cand.finish_reason) if cand.finish_reason else None)
        parts = (cand.content.parts if cand.content and cand.content.parts else []) or []

        texts: list[str] = []
        calls: list[ToolCall] = []
        for p in parts:
            if p.function_call:
                fc = p.function_call
                extra: dict[str, Any] = {}
                if p.thought_signature:
                    extra[_SIG_KEY] = p.thought_signature
                if fc.id:
                    extra[_ID_KEY] = fc.id
                calls.append(ToolCall(
                    name=fc.name, args=normalize_args(dict(fc.args or {})), extra=extra))
            elif p.text and not p.thought:
                texts.append(p.text)

        text = "".join(texts).strip()
        if not text and not calls:
            raise EmptyResponseError(
                f"Gemini tidak mengembalikan teks atau tool call (finish_reason={finish})",
                provider=self.name, model=self.model,
                retryable=finish in _RETRYABLE_FINISH)

        usage = getattr(resp, "usage_metadata", None)
        out_tokens = None
        if usage is not None:
            out_tokens = (usage.candidates_token_count or 0) + (
                getattr(usage, "thoughts_token_count", 0) or 0)
        return ChatResponse(
            text=text, tool_calls=calls,
            input_tokens=getattr(usage, "prompt_token_count", None),
            output_tokens=out_tokens,
            finish_reason=finish, provider=self.name, model=self.model)

    # ------------------------------------------------------------ embedding
    def embed(self, texts: list[str], task: str = "document") -> list[list[float]]:
        if not texts:
            return []
        if len(texts) > self.max_embed_batch:
            raise ValueError(
                f"Batch embedding {len(texts)} melebihi batas {self.max_embed_batch}")
        if task not in _EMBED_TASKS:
            raise ValueError(f"task embedding tidak dikenal: {task!r}")

        kwargs: dict[str, Any] = {}
        if config.EMBEDDING.get("use_task_type"):
            kwargs["task_type"] = _EMBED_TASKS[task]
        if config.EMBEDDING.get("dim"):
            kwargs["output_dimensionality"] = config.EMBEDDING["dim"]

        try:
            resp = self.client.models.embed_content(
                model=self.embed_model, contents=texts,
                config=types.EmbedContentConfig(**kwargs) if kwargs else None)
        except Exception as e:  # noqa: BLE001
            raise self._map_error(e, model=self.embed_model) from e

        vectors = [list(e.values) for e in (resp.embeddings or [])]
        if len(vectors) != len(texts):
            raise ProviderError(
                f"Jumlah vektor ({len(vectors)}) tidak sama dengan jumlah teks ({len(texts)})",
                provider=self.name, model=self.embed_model)
        return vectors

    # ------------------------------------------------------------ error
    def _map_error(self, e: Exception, model: str | None = None) -> ProviderError:
        model = model or self.model
        kw = {"provider": self.name, "model": model}
        msg = str(e)

        if isinstance(e, genai_errors.APIError):
            code = getattr(e, "code", None)
            kw["status"] = code
            if code == 429:
                # Kuota harian habis: retry tidak ada gunanya, langsung failover.
                daily = "perday" in msg.lower().replace(" ", "").replace("_", "")
                return RateLimitError(msg, retry_after=_parse_retry_after(msg),
                                      retryable=not daily, **kw)
            if code is not None and code >= 500:
                return ServerError(msg, **kw)
            if code in (401, 403):
                return AuthError(msg, **kw)
            return BadRequestError(msg, **kw)

        if _is_timeout(e):
            return ProviderTimeout(f"Timeout memanggil Gemini: {msg}", **kw)
        if _is_network(e):
            return ServerError(f"Koneksi ke Gemini gagal: {msg}", **kw)
        return ProviderError(f"{type(e).__name__}: {msg}", **kw)
