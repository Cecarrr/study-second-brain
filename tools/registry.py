"""Daftar tool, validasi argumen, eksekusi aman, dan pencatatan log."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import config
from providers.base import Message, ProviderError, ToolCall, ToolSpec
import privacy
from agent import WRITE_TOOLS
from tools import knowledge, notes, quiz, tasks
from undo import Journal
from tools.common import ToolContext, ToolError

log = logging.getLogger(__name__)


def coerce_args(spec: ToolSpec, raw: dict | None) -> dict:
    """Rapikan argumen dari LLM: tipe, enum, batas angka; buang argumen asing dan nilai kosong."""
    props = spec.parameters.get("properties", {})
    out: dict = {}
    for k, v in (raw or {}).items():
        p = props.get(k)
        if p is None or v is None or (isinstance(v, str) and not v.strip()):
            continue
        t = p.get("type")
        try:
            if t == "integer":
                v = int(float(v))
                v = max(p.get("minimum", v), min(p.get("maximum", v), v))
            elif t == "boolean":
                v = v if isinstance(v, bool) else str(v).strip().lower() in ("true", "1", "ya", "yes")
            elif t == "string":
                v = str(v).strip()
            elif t == "array":
                items = v.split(",") if isinstance(v, str) else list(v)
                v = [str(x).strip() for x in items if str(x).strip()]
        except (TypeError, ValueError):
            raise ToolError(f"Parameter '{k}' harus bertipe {t}.")
        if "enum" in p and v not in p["enum"]:
            raise ToolError(f"'{k}' harus salah satu dari: {', '.join(p['enum'])}.")
        out[k] = v
    missing = [r for r in spec.parameters.get("required", []) if r not in out]
    if missing:
        raise ToolError(f"Parameter wajib belum diisi: {', '.join(missing)}.")
    return out


class Registry:
    def __init__(self, ctx: ToolContext):
        self.ctx = ctx
        self.journal = Journal()
        self._tools: dict[str, tuple[ToolSpec, Callable]] = {
            s.name: (s, h) for s, h in [
                (knowledge.SEARCH_SPEC, knowledge.search_knowledge),
                (knowledge.NOTE_SPEC, knowledge.add_note),
                (tasks.ADD_SPEC, tasks.add_task),
                (tasks.LIST_SPEC, tasks.list_tasks),
                (tasks.COMPLETE_SPEC, tasks.complete_task),
                (tasks.UPDATE_SPEC, tasks.update_task),
                (tasks.DELETE_SPEC, tasks.delete_task),
                (notes.SPEC, notes.list_notes),
                (notes.UPDATE_SPEC, notes.update_note),
                (notes.DELETE_SPEC, notes.delete_note),
                (quiz.SPEC, quiz.make_quiz),
            ]}

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self) -> list[ToolSpec]:
        return [s for s, _ in self._tools.values()]

    def execute(self, call: ToolCall) -> Message:
        """Jalankan satu tool call -> Message role 'tool'. Tidak pernah raise."""
        t0, ok = time.time(), True
        before = self.journal.snapshot(self.ctx) if call.name in WRITE_TOOLS else None
        entry = self._tools.get(call.name)
        if entry is None:
            out, ok = (f"Tool '{call.name}' tidak ada. Tool yang tersedia: {', '.join(self.names)}.", False)
        else:
            spec, handler = entry
            try:
                out = handler(self.ctx, coerce_args(spec, call.args))
            except ToolError as e:
                out, ok = f"Error: {e}", False
            except ProviderError as e:
                first = str(e).splitlines()[0][:160]
                out, ok = (f"Error: layanan model/embedding sedang tidak tersedia ({first}). Beri tahu "
                           "pengguna; jangan mengulang panggilan yang sama.", False)
            except Exception as e:  # noqa: BLE001
                log.exception("Tool %s gagal", call.name)
                out, ok = f"Error tak terduga: {type(e).__name__}: {e}", False
        if before is not None and ok and self.journal.snapshot(self.ctx) != before:
            self.journal.push(f"{call.name} {json.dumps(call.args, ensure_ascii=False)[:80]}", before)
        self._log(call, ok, out, time.time() - t0)
        return Message.tool(call, out, is_error=not ok)

    def _log(self, call: ToolCall, ok: bool, out: str, secs: float) -> None:
        try:
            red = privacy.mode() != "off"
            redact_obj = privacy.redact_obj
            path = self.ctx.log_path or (config.LOG_DIR / "tools.jsonl")
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"t": datetime.now().isoformat(timespec="seconds"), "tool": call.name,
                                    "args": redact_obj(call.args) if red else call.args, "ok": ok,
                                    "ms": int(secs * 1000),
                                    "out": (redact_obj(out) if red else out)[:300]}, ensure_ascii=False) + "\n")
        except OSError:
            pass


def build_registry(ctx: ToolContext) -> Registry:
    return Registry(ctx)
