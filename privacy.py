"""Penyamaran data sensitif (regex, tanpa LLM).

PRIVACY_MODE di config.py:  "logs" (bawaan) = samarkan di log;  "strict" = log + semua pesan
yang dikirim ke model chat;  "off" = mati. Catatan yang MENYIMPAN data sensitif tetap utuh di
file, tetapi pengguna diperingatkan karena teksnya ikut dikirim ke API embedding.
"""
from __future__ import annotations

import re
from dataclasses import replace

import config


def mode() -> str:
    return getattr(config, "PRIVACY_MODE", "logs")


def _luhn(digits: str) -> bool:
    s, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        s, alt = s + d, not alt
    return s % 10 == 0


_API = re.compile(r"\b(?:AIza[0-9A-Za-z_\-]{30,}|sk-[A-Za-z0-9_\-]{20,}|gsk_[A-Za-z0-9]{20,}"
                  r"|ghp_[A-Za-z0-9]{30,}|xox[baprs]-[A-Za-z0-9\-]{10,})")
_CRED = re.compile(r"(?i)\b(password|passwd|kata\s*sandi|sandi|pin|token|secret)(\s*[:=]\s*)\S+")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_NIM = re.compile(r"(?i)(\bnim\b\s*[:=]?\s*)\d{6,14}")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_NIK = re.compile(r"(?<!\d)\d{16}(?!\d)")
_PHONE = re.compile(r"(?<!\d)(?:\+62|62|0)[\s-]?8\d{2}[\s-]?\d{3,4}[\s-]?\d{3,5}(?!\d)")


def redact(text: str) -> tuple[str, dict]:
    """-> (teks tersamar, {jenis: jumlah}). Teks tanpa data sensitif dikembalikan apa adanya."""
    counts: dict[str, int] = {}

    def bump(name):
        counts[name] = counts.get(name, 0) + 1

    def card(m):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            bump("KARTU")
            return "[KARTU]"
        return m.group(0)

    def sub(rx, name, repl):
        nonlocal text
        text = rx.sub(lambda m: (bump(name), m.expand(repl))[1], text)

    sub(_CRED, "KREDENSIAL", r"\1\2[KREDENSIAL]")
    sub(_API, "KUNCI-API", "[KUNCI-API]")
    sub(_EMAIL, "EMAIL", "[EMAIL]")
    sub(_NIM, "NIM", r"\1[NIM]")
    text = _CARD.sub(card, text)
    sub(_NIK, "NIK", "[NIK]")
    sub(_PHONE, "TELEPON", "[TELEPON]")
    return text, counts


def redact_obj(obj):
    if isinstance(obj, str):
        return redact(obj)[0]
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    return obj


def redact_messages(messages):
    """Salinan pesan dengan isi tersamar (untuk mode strict)."""
    out = []
    for m in messages:
        calls = [replace(tc, args=redact_obj(tc.args)) for tc in m.tool_calls]
        out.append(replace(m, content=redact(m.content or "")[0], tool_calls=calls))
    return out


def note_warning(text: str) -> str:
    """Kalimat peringatan untuk hasil add_note/update_note; kosong bila tidak ada data sensitif."""
    if mode() == "off":
        return ""
    _, counts = redact(text)
    if not counts:
        return ""
    kinds = ", ".join(f"{k.lower()} ({n})" for k, n in counts.items())
    return (f" PERINGATAN PRIVASI: catatan ini memuat {kinds}. Teks catatan dikirim ke API embedding "
            "(tier gratis dapat dipakai untuk pengembangan produk). Sampaikan ke pengguna; "
            "hapus datanya lewat update_note bila tidak perlu.")
