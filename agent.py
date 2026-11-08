"""Agent loop: LLM memilih tool atau menjawab; hasil tool dikembalikan sampai ada jawaban akhir.

Pengaman: batas langkah, panggilan identik tidak dieksekusi dua kali dalam satu giliran,
riwayat lama hanya menyimpan (pertanyaan, jawaban akhir) tanpa pertukaran tool.
"""
from __future__ import annotations

import json
from typing import Callable

import config
from prompts import build_system_prompt
from providers.base import Message, ProviderError

WRITE_TOOLS = {"add_note", "add_task", "complete_task"}


class Agent:
    def __init__(self, registry, chat_fn: Callable, courses_fn: Callable[[], list] | None = None,
                 today_fn: Callable | None = None, notify: Callable[[str], None] | None = None):
        self.reg, self.chat_fn = registry, chat_fn
        self.courses_fn, self.today_fn = courses_fn or (lambda: []), today_fn
        self.notify = notify or (lambda s: None)
        self.history: list[Message] = []        # pasangan user/assistant dari giliran lama

    def reset(self) -> None:
        self.history.clear()

    def _trim(self) -> None:
        keep = config.HISTORY_MAX_TURNS * 2
        if len(self.history) > keep:
            self.history = self.history[-keep:]

    def ask(self, text: str) -> str:
        system = build_system_prompt(self.today_fn() if self.today_fn else None, self.courses_fn())
        user = Message.user(text)
        msgs = [Message.system(system), *self.history, user]
        specs = self.reg.specs()
        done: dict[str, str] = {}               # kunci panggilan -> hasil (giliran ini)

        for _ in range(config.MAX_ITERATIONS):
            try:
                resp = self.chat_fn(msgs, specs)
            except ProviderError as e:
                first = str(e).splitlines()[0][:200]
                return f"Maaf, layanan model sedang tidak tersedia: {first}\nCoba lagi nanti atau cek /usage."
            msgs.append(resp.to_message())
            if not resp.tool_calls:
                answer = resp.text or "(model tidak memberi jawaban; coba ulangi pertanyaannya)"
                self.history += [user, Message.assistant(answer)]
                self._trim()
                return answer
            for call in resp.tool_calls:
                key = call.name + json.dumps(call.args, sort_keys=True, ensure_ascii=False)
                if key in done:
                    msgs.append(Message.tool(call, "Panggilan identik sudah dijalankan pada giliran ini "
                                             f"dengan hasil: {done[key][:200]}. Jangan diulang; lanjutkan "
                                             "atau jawab pengguna.", is_error=True))
                    continue
                self.notify(f"  [{call.name}] {json.dumps(call.args, ensure_ascii=False)[:120]}")
                res = self.reg.execute(call)
                done[key] = res.content
                msgs.append(res)
        last = next((m.content for m in reversed(msgs) if m.role == "tool"), "")
        return ("Aku mencapai batas langkah untuk satu pesan. Hasil terakhir:\n" + last[:600]
                + "\nCoba pecah permintaanmu menjadi lebih kecil.")
