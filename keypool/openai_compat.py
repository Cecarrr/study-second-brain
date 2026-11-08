"""Adapter API berformat OpenAI (OpenRouter, Groq, Cerebras, Mistral, ...) di atas BaseProvider proyek.

Hanya chat + tool calling. Memakai `requests`. Tidak ada retry/limiter di sini: itu tugas router.
"""
from __future__ import annotations

import json
import re
import time
from typing import Optional

from providers.base import (AuthError, BadRequestError, BaseProvider, ChatResponse,
                            EmptyResponseError, Message, ProviderError, ProviderTimeout,
                            RateLimitError, ServerError, ToolCall, ToolSpec, normalize_args,
                            new_call_id)

_DAILY_RE = re.compile(r"per[- ]day|daily|\brpd\b|\btpd\b")


def _seconds(value) -> Optional[float]:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v > 1e12:                       # epoch milidetik (X-RateLimit-Reset OpenRouter)
        return max(v / 1000 - time.time(), 0)
    if v > 1e9:                        # epoch detik
        return max(v - time.time(), 0)
    return v                           # relatif


def classify_http(status, body, headers, *, provider="openai_compat", model=None) -> ProviderError:
    err = body.get("error") if isinstance(body, dict) else None
    msg, meta = "", {}
    if isinstance(err, dict):
        msg, meta = str(err.get("message") or ""), err.get("metadata") or {}
    elif isinstance(err, str):
        msg = err
    msg = msg or f"HTTP {status}"
    low = msg.lower()
    hdr = {str(k).lower(): v for k, v in (headers or {}).items()}
    mh = {}
    if isinstance(meta, dict) and isinstance(meta.get("headers"), dict):
        mh = {str(k).lower(): v for k, v in meta["headers"].items()}
    kw = {"provider": provider, "model": model, "status": status}

    if status == 429:
        if _DAILY_RE.search(low):
            e = RateLimitError(msg, retryable=False, **kw)
            e.reset_in = _seconds(mh.get("x-ratelimit-reset") or hdr.get("x-ratelimit-reset"))
            return e
        return RateLimitError(msg, retry_after=_seconds(hdr.get("retry-after")), retryable=True, **kw)
    if status == 402:
        return RateLimitError(msg, retryable=False, **kw)
    if status in (401, 403):
        return AuthError(msg, **kw)
    if status in (400, 404, 413, 422):
        return BadRequestError(msg, **kw)
    return ServerError(msg, **kw)


class OpenAICompatProvider(BaseProvider):
    name = "openai_compat"
    supports_embedding = False

    def __init__(self, model: str, *, api_key: str, base_url: str, name: Optional[str] = None,
                 timeout: float = 60):
        super().__init__(model)
        if not api_key:
            raise AuthError("api_key kosong", provider=name or self.name, model=model)
        self.name = name or self.name
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @staticmethod
    def _convert(messages: list) -> list:
        out = []
        for m in messages:
            if m.role == "assistant" and m.tool_calls:
                out.append({"role": "assistant", "content": m.content or None, "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.name, "arguments": json.dumps(tc.args, ensure_ascii=False)}}
                    for tc in m.tool_calls]})
            elif m.role == "tool":
                content = f"Error: {m.content}" if m.is_error else m.content
                out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": content})
            else:
                out.append({"role": m.role, "content": m.content})
        return out

    def chat(self, messages: list, tools: Optional[list] = None) -> ChatResponse:
        import requests
        payload = {"model": self.model, "messages": self._convert(messages)}
        if tools:
            payload["tools"] = [{"type": "function", "function": {
                "name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in tools]
            payload["tool_choice"] = "auto"
        kw = {"provider": self.name, "model": self.model}
        try:
            r = requests.post(f"{self.base_url}/chat/completions", json=payload, timeout=self.timeout,
                              headers={"Authorization": f"Bearer {self.api_key}",
                                       "Content-Type": "application/json"})
        except requests.exceptions.Timeout as e:
            raise ProviderTimeout(f"Timeout: {e}", **kw) from e
        except requests.exceptions.RequestException as e:
            raise ServerError(f"Koneksi gagal: {e}", **kw) from e
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code != 200:
            raise classify_http(r.status_code, body, r.headers, **kw)
        if isinstance(body, dict) and body.get("error"):         # error terselip di respons 200
            code = body["error"].get("code") if isinstance(body["error"], dict) else None
            raise classify_http(int(code) if str(code).isdigit() else 502, body, r.headers, **kw)
        choices = (body or {}).get("choices") or []
        if not choices:
            raise EmptyResponseError("Respons tanpa choices", retryable=True, **kw)
        ch = choices[0]
        msg = ch.get("message") or {}
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except ValueError:
                args = {"__invalid_json__": str(raw)}
            calls.append(ToolCall(name=fn.get("name", ""), args=normalize_args(args),
                                  id=tc.get("id") or new_call_id()))
        text = (msg.get("content") or "").strip()
        if not text and not calls:
            raise EmptyResponseError(f"Respons kosong (finish_reason={ch.get('finish_reason')})",
                                     retryable=True, **kw)
        u = body.get("usage") or {}
        return ChatResponse(text=text, tool_calls=calls, input_tokens=u.get("prompt_tokens"),
                            output_tokens=u.get("completion_tokens"),
                            finish_reason=ch.get("finish_reason"), provider=self.name, model=self.model)
