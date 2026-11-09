"""Pemeriksaan sederhana jawaban terhadap sumber (tanpa LLM, tanpa request).

Mengambil angka dan singkatan huruf kapital (35%, 3NF, QUIC) dari jawaban, lalu melaporkan
yang TIDAK muncul di teks sumber atau pertanyaan. Presisinya tinggi untuk halusinasi angka dan
istilah; parafrase biasa tidak ditandai. Bukan pembuktian kebenaran.
"""
from __future__ import annotations

import re

_LIST_MARK = re.compile(r"(?m)^\s*(?:\d{1,2}[.)]|[-*])\s+")
_NUM = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?\s?%?")
_ACR = re.compile(r"\b[A-Z][A-Z0-9]{1,}\b|\b\d[A-Z]{2,}\b")
_IGNORE = {"ID", "EN", "OK", "AI", "CLI", "PDF", "CSV", "UTS", "UAS", "A", "B", "C", "D", "AM", "PM", "WIB"}


def _norm_num(s: str) -> str:
    return s.replace(" ", "").replace("%", "").replace(",", ".")


def _facts(text: str) -> tuple[set[str], set[str]]:
    text = _LIST_MARK.sub("", text)
    nums = {_norm_num(m) for m in _NUM.findall(text)}
    accs = {m for m in _ACR.findall(text) if m not in _IGNORE}
    return nums, accs


def unsupported(answer: str, sources: list[str], question: str = "") -> list[str]:
    """Angka/singkatan di `answer` yang tidak ada di sumber maupun pertanyaan."""
    ref = " ".join(sources) + " " + question
    ref_nums, _ = _facts(ref)
    ref_low = ref.lower()
    nums, accs = _facts(answer)
    bad = []
    for n in sorted(nums):
        if n not in ref_nums and n not in {"0", "1"}:
            bad.append(n)
    for a in sorted(accs):
        if not re.search(r"(?<![a-z0-9])" + re.escape(a.lower()) + r"(?![a-z0-9])", ref_low):
            bad.append(a)
    return bad
