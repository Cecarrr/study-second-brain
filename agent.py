"""Agent loop: LLM memilih tool atau menjawab; hasil tool dikembalikan sampai ada jawaban akhir.

Pengaman: batas langkah, panggilan identik tidak dieksekusi dua kali dalam satu giliran,
riwayat lama hanya menyimpan (pertanyaan, jawaban akhir) tanpa pertukaran tool.
"""
from __future__ import annotations

import json
from typing import Callable

import config
import grounding
from prompts import build_system_prompt
from providers.base import Message, ProviderError
from tools.select import asked_clarification, groups_of, select_specs

# Tool yang mengubah data. Setelah salah satunya jalan, hasil baca yang di-cache pada giliran
# yang sama sudah basi; lihat pembersihan `done` di Agent.ask.
WRITE_TOOLS = {"add_note", "add_task", "complete_task", "update_task", "delete_task",
               "update_note", "delete_note"}


class Agent:
    def __init__(self, registry, chat_fn: Callable, courses_fn: Callable[[], list] | None = None,
                 today_fn: Callable | None = None, notify: Callable[[str], None] | None = None):
        self.reg, self.chat_fn = registry, chat_fn
        self.courses_fn, self.today_fn = courses_fn or (lambda: []), today_fn
        self.notify = notify or (lambda s: None)
        self.history: list[Message] = []        # pasangan user/assistant dari giliran lama
        self._last_groups: frozenset = frozenset()   # kelompok tool yang dipertahankan bila asisten bertanya balik
        self.style = "normal"                   # "normal" | "simple" (/mode)

    def reset(self) -> None:
        self.history.clear()
        self._last_groups = frozenset()

    @staticmethod
    def _grounding_note(msgs, question: str, answer: str) -> str:
        """Tandai angka/singkatan di jawaban yang tidak ada di sumber (hanya bila search_knowledge dipakai)."""
        if getattr(config, "GROUNDING_CHECK", "flag") == "off":
            return ""
        if not any(m.role == "tool" and m.name == "search_knowledge" and not m.is_error for m in msgs):
            return ""
        bad = grounding.unsupported(answer, [m.content for m in msgs if m.role == "tool"], question)
        return (f"\n\n(Periksa: {', '.join(bad[:6])} tidak ada di sumber yang ditemukan; "
                "mungkin dugaan model.)") if bad else ""

    def _trim(self) -> None:
        keep = config.HISTORY_MAX_TURNS * 2
        if len(self.history) > keep:
            self.history = self.history[-keep:]

    def ask(self, text: str) -> str:
        system = build_system_prompt(self.today_fn() if self.today_fn else None, self.courses_fn(),
                                     style=self.style)
        user = Message.user(text)
        msgs = [Message.system(system), *self.history, user]
        all_specs = self.reg.specs()
        specs = (select_specs(text, self.reg, self._last_groups)
                 if getattr(config, "TOOL_SUBSET", True) else all_specs)
        widened = len(specs) == len(all_specs)  # True = sudah semua tool, tidak perlu melebar lagi
        picked = None if widened else groups_of(s.name for s in specs) - {"pengetahuan"}
        called: list[str] = []                  # tool yang dijalankan pada giliran ini
        done: dict[str, tuple[str, str]] = {}   # kunci panggilan -> (nama tool, hasil) pada giliran ini

        for _ in range(config.MAX_ITERATIONS):
            try:
                resp = self.chat_fn(msgs, specs)
            except ProviderError as e:
                err_msg = str(e).lower()
                # Tangani kasus model mencoba memanggil tool di luar subset dan ditolak oleh provider API
                if not widened and any(k in err_msg for k in ["tool", "function", "unknown", "not found", "invalid"]):
                    specs, widened = all_specs, True
                    continue
                first = str(e).splitlines()[0][:200]
                return f"Maaf, layanan model sedang tidak tersedia: {first}\nCoba lagi nanti atau cek /usage."
            
            if resp.tool_calls and not widened:
                sent, known = {s.name for s in specs}, set(self.reg.names)
                if any(c.name in known and c.name not in sent for c in resp.tool_calls):
                    # Model butuh tool yang tidak dikirim: ulangi sekali dengan semua tool
                    specs, widened = all_specs, True
                    continue
            msgs.append(resp.to_message())
            if not resp.tool_calls:
                answer = resp.text or "(model tidak memberi jawaban; coba ulangi pertanyaannya)"
                self.history += [user, Message.assistant(answer)]
                self._trim()
                # Bila jawabannya pertanyaan balik, giliran berikutnya masih butuh kelompok tool yang sama.
                self._last_groups = (groups_of(called) or picked or frozenset()) \
                    if asked_clarification(answer) else frozenset()
                return answer + self._grounding_note(msgs, text, answer)
            for call in resp.tool_calls:
                key = call.name + json.dumps(call.args, sort_keys=True, ensure_ascii=False)
                if key in done:
                    msgs.append(Message.tool(call, "Panggilan identik sudah dijalankan pada giliran ini "
                                             f"dengan hasil: {done[key][1][:200]}. Jangan diulang; lanjutkan "
                                             "atau jawab pengguna.", is_error=True))
                    continue
                self.notify(f"  [{call.name}] {json.dumps(call.args, ensure_ascii=False)[:120]}")
                called.append(call.name)
                res = self.reg.execute(call)
                if call.name in WRITE_TOOLS:
                    # Data berubah: pembacaan lama tidak berlaku lagi, jadi model boleh memverifikasi
                    # (mis. list_tasks setelah complete_task). Panggilan tulis tetap diingat supaya
                    # tidak dieksekusi dua kali (catatan/tugas ganda, hapus ganda).
                    done = {k: v for k, v in done.items() if v[0] in WRITE_TOOLS}
                done[key] = (call.name, res.content)
                msgs.append(res)
        last = next((m.content for m in reversed(msgs) if m.role == "tool"), "")
        return ("Aku mencapai batas langkah untuk satu pesan. Hasil terakhir:\n" + last[:600]
                + "\nCoba pecah permintaanmu menjadi lebih kecil.")
