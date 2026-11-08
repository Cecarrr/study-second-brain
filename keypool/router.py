"""Router: chat() dan embed() di atas pool. Agent dan tools tidak tahu provider mana yang dipakai.

Aturan inti:
  - Slot dicoba berurutan (urutan model lalu urutan account); slot pertama yang lolos gerbang dipakai.
  - Jawaban server menang atas hitungan lokal: 429 harian -> blokir sampai reset kuota,
    429 per menit -> tunggu sebentar, 401 -> matikan account, 403/404 -> matikan slot, 5xx -> jeda singkat.
  - Berhenti (PoolExhausted) hanya setelah semua slot habis/diblokir dan tidak ada yang pulih
    dalam batas tunggu (settings.max_wait_s).
  - Embedding hanya memakai model terpilih di [embedding]; chat dan embedding dihitung terpisah.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

from providers.base import Message, ProviderError, RateLimitError

from .errors import ErrorKind, classify
from .pool import Pool, PoolConfigError, Slot
from .quota import UsageStore, fmt_ts, next_reset

log = logging.getLogger("pool")

def flatten_tool_turn(messages):
    """Ratakan tool call + hasilnya (giliran berjalan) jadi teks, untuk slot yang berbeda
    dari penghasil tool call tadi (id dan thought signature tidak berlaku lintas slot)."""
    last_user = max((i for i, m in enumerate(messages) if m.role == "user"), default=None)
    if last_user is None:
        return messages
    tail = messages[last_user + 1:]
    if not any(m.role == "tool" or m.tool_calls for m in tail):
        return messages
    calls, lines = {}, []
    for m in tail:
        for tc in m.tool_calls:
            calls[tc.id] = tc
        if m.role == "tool":
            tc = calls.get(m.tool_call_id or "")
            args = json.dumps(tc.args, ensure_ascii=False) if tc else ""
            lines.append(f"- {m.name}({args}) -> {'ERROR: ' if m.is_error else ''}{m.content}")
    u = messages[last_user]
    merged = Message.user(
        u.content + "\n\n[Tool yang sudah dijalankan untuk permintaan ini dan hasilnya:\n"
        + "\n".join(lines)
        + "\nLanjutkan berdasarkan hasil ini; jangan ulangi aksi yang sudah berhasil.]")
    return messages[:last_user] + [merged]

class PoolExhausted(RateLimitError):
    """Semua slot habis/diblokir. Subclass RateLimitError(retryable=False) supaya kode lama
    (Embedder, agent) memperlakukannya sebagai 'berhenti, jangan retry'."""

    def __init__(self, kind: str, retry_at: Optional[float], report: str):
        self.kind, self.retry_at, self.report = kind, retry_at, report
        when = f" Paling cepat pulih: {fmt_ts(retry_at)}." if retry_at else ""
        super().__init__(f"Semua slot {kind} habis atau diblokir.{when}\n{report}",
                         provider="keypool", retryable=False)


class LLMRouter:
    def __init__(self, pool: Pool, usage: UsageStore, clock=time.time, sleep=time.sleep, notify=None):
        self.pool, self.usage = pool, usage
        self.clock, self.sleep = clock, sleep
        self.notify = notify or (lambda msg: log.info(msg))
        self.last_slot: Optional[Slot] = None
        for w in pool.warnings:
            log.warning(w)

    # ---------- API publik ----------

    def chat(self, messages, tools=None):
        """-> ChatResponse (providers.base). Atribut tambahan: res.account = nama account penjawab."""
        def call(s: Slot):
            msgs = messages
            if self.last_slot is not None and self.last_slot.sid != s.sid:
                msgs = flatten_tool_turn(messages)
            res = s.impl.chat(msgs, tools)
            res.account = s.account
            return res

        return self._execute("chat", self.pool.chat_slots, call,
                             tokens_of=lambda r: (r.input_tokens or 0) + (r.output_tokens or 0), units=1)

    def embed(self, texts, task="document"):
        cfg = self.pool.embedding
        out = []
        for i in range(0, len(texts), cfg.batch_size):
            batch = list(texts[i:i + cfg.batch_size])
            est = sum(len(t) for t in batch) // 3            # perkiraan token untuk gerbang TPM
            out.extend(self._execute("embed", self.pool.embed_slots,
                                     lambda s, b=batch: s.impl.embed(b, task),
                                     tokens_of=lambda r, est=est: est,
                                     units=len(batch) if cfg.count == "item" else 1))
        return out

    @property
    def embedding_signature(self) -> str:
        return self.pool.embedding.signature

    # ---------- inti ----------

    def _execute(self, kind, slots, call, tokens_of, units):
        if not slots:
            raise PoolConfigError(f"Tidak ada slot '{kind}' di pool.toml")
        st = self.pool.settings
        tried_bad, last_bad, bad = set(), None, 0
        attempts, slept = 0, 0.0
        max_attempts = st["max_attempts"] + len(slots)
        while True:
            now = self.clock()
            pick, waits = None, []
            for s in slots:
                if s.sid in tried_bad:
                    continue
                ok, wait, why = self.usage.check(s, self.pool.safety, now)
                if ok:
                    pick = s
                    break
                waits.append(wait)
            if pick is None:
                if last_bad is not None and not waits:
                    raise last_bad
                soonest = min(waits) if waits else None
                if soonest is not None and slept + soonest <= st["max_wait_s"]:
                    self.notify(f"[pool] semua slot {kind} penuh sementara, menunggu {soonest:.0f}s")
                    self.sleep(soonest + 0.05)
                    slept += soonest + 0.05
                    continue
                raise PoolExhausted(kind, now + soonest if soonest is not None else None,
                                    self.usage_report(kind))
            attempts += 1
            if attempts > max_attempts:
                raise PoolExhausted(kind, None, self.usage_report(kind))
            try:
                result = call(pick)
            except ProviderError as e:
                self._on_error(pick, e, now)
                if classify(e)[0] == ErrorKind.BAD_REQUEST:
                    tried_bad.add(pick.sid)
                    last_bad, bad = e, bad + 1
                    if bad >= 2:
                        raise
                continue
            self.last_slot = pick
            self.usage.record(pick, tokens=tokens_of(result), count=units, now=self.clock())
            self.usage.consec429.pop(pick.qkey, None)
            self.usage.fails.pop(pick.sid, None)
            return result

    # ---------- penanganan error ----------

    def _on_error(self, s: Slot, e: ProviderError, now: float):
        u = self.usage
        k, retry = classify(e)
        reset = next_reset(now, s.reset_tz)
        msg = str(e)[:140]
        if k == ErrorKind.RATE_UNKNOWN:
            n = u.consec429[s.qkey] = u.consec429[s.qkey] + 1
            used, _ = u.used_today(s.qkey, s.reset_tz, now)
            near_cap = bool(s.limits.rpd) and used >= 0.9 * s.limits.rpd
            k = ErrorKind.QUOTA_DAILY if (near_cap or n >= 5) else ErrorKind.RATE_MINUTE
            retry = min(15 * 2 ** (n - 1), 300)
        if k == ErrorKind.QUOTA_DAILY:
            until = reset
            reset_in = getattr(e, "reset_in", None)        # hanya diisi adapter yang tahu waktu reset pasti
            if reset_in and 0 < reset_in < reset - now:
                until = now + reset_in + 1
            u.block(s.qkey, until, "kuota harian habis")
            self.notify(f"[pool] {s.label}: kuota harian habis, diblokir sampai {fmt_ts(until)}")
        elif k == ErrorKind.RATE_MINUTE:
            cool = min(max(retry or 20, 1), 3600) + 1
            if s.scope == "account":      # limit upstream per model: jangan matikan seluruh account
                u.disable(s.sid, now + cool, "rate limit")
            else:
                u.block(s.qkey, now + cool, "rate limit per menit")
            self.notify(f"[pool] {s.label}: rate limit, jeda {cool:.0f}s lalu coba slot berikutnya")
        elif k == ErrorKind.AUTH:
            u.disable(f"{s.account}|*", now + 6 * 3600, "key ditolak")
            self.notify(f"[pool] account '{s.account}' dimatikan (key ditolak): {msg}")
        elif k == ErrorKind.FORBIDDEN:
            u.disable(s.sid, reset, "tidak bisa dipakai")
            self.notify(f"[pool] {s.label}: ditolak, dinonaktifkan sampai {fmt_ts(reset)}: {msg}")
        elif k == ErrorKind.TRANSIENT:
            n = u.fails[s.sid] = u.fails[s.sid] + 1
            cool = min(10 * 2 ** (n - 1), 300)
            u.disable(s.sid, now + cool, "error sementara")
            self.notify(f"[pool] {s.label}: error sementara, jeda {cool}s: {msg}")
        else:
            self.notify(f"[pool] {s.label}: permintaan ditolak atau respons kosong: {msg}")

    # ---------- laporan ----------

    def usage_report(self, kind: Optional[str] = None) -> str:
        now = self.clock()
        groups = []
        if kind in (None, "chat"):
            groups.append(("CHAT", self.pool.chat_slots))
        if kind in (None, "embed") and self.pool.embed_slots:
            groups.append((f"EMBEDDING ({self.pool.embedding.signature})", self.pool.embed_slots))
        lines = []
        for title, slots in groups:
            lines.append(f"== {title} ==")
            rows = self.usage.rows(slots, self.pool.safety, now)
            for r in rows:
                rpd = f"{r['used']}/{r['rpd']}" if r["rpd"] else f"{r['used']}/-"
                rpm = f"{r['rpm_now']}/{r['rpm']}" if r["rpm"] else f"{r['rpm_now']}/-"
                status = "OK" if r["ok"] else f"TUNGGU {r['wait']:.0f}s ({r['why']})"
                lines.append(f"  {r['label']:<48} RPD {rpd:<10} RPM {rpm:<7} {status}")
            seen, left = set(), 0
            for r in rows:                     # jumlahkan sisa RPD sekali per kuota bersama
                if r["qkey"] in seen or not r["rpd"]:
                    continue
                seen.add(r["qkey"])
                left += max(r["rpd"] - r["used"], 0)
            if left:
                lines.append(f"  sisa RPD (perkiraan lokal, tanpa slot tak berbatas): {left}")
        return "\n".join(lines)

    def reset_blocks(self):
        self.usage.clear_blocks()


# ---------- antarmuka tingkat modul ----------

_router: Optional[LLMRouter] = None
_ROOT = Path(__file__).resolve().parent.parent


def get_router(pool_path=None, db_path=None) -> LLMRouter:
    global _router
    if _router is None:
        import config   # memuat .env dan memberi default model/dimensi embedding
        emb = dict(config.EMBEDDING)
        _router = LLMRouter(
            Pool.from_file(pool_path or _ROOT / "pool.toml", embedding_defaults=emb),
            UsageStore(str(db_path or _ROOT / "data" / "keypool.db")))
    return _router


def chat(messages, tools=None):
    return get_router().chat(messages, tools)


def embed(texts, task="document"):
    return get_router().embed(texts, task)


def embedding_signature() -> str:
    return get_router().embedding_signature


def usage_report() -> str:
    return get_router().usage_report()
