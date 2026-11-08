"""Penghitung request lokal dan rate limiter.

- UsageTracker : hitungan request/token per provider+model+hari, disimpan di
                 data/usage.json. Pengganti dashboard kuota harian (/usage).
- RateLimiter  : jendela geser 60 detik per provider+model, dengan margin
                 (15 RPM -> 12 efektif) dan dukungan "tunggu sampai" dari server.
- backoff_delay: jeda retry eksponensial + jitter.

Alur yang diharapkan di llm.py, per percobaan:
    limiter.acquire(provider, model)            # tunggu slot RPM
    resp = adapter.chat(...)                    # panggil API
    tracker.record(provider, model, ok=True, input_tokens=..., output_tokens=...)
    # bila RateLimitError: limiter.penalize(provider, model, e.retry_after)
    #                      time.sleep(backoff_delay(attempt, e.retry_after))
    # sebelum memilih provider: tracker.near_daily_limit(provider, model)
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import config
from providers.base import RateLimitError

try:
    from zoneinfo import ZoneInfo
    _QUOTA_TZ = ZoneInfo(config.QUOTA_TIMEZONE)
except Exception:  # Windows tanpa paket tzdata: pip install tzdata
    _QUOTA_TZ = timezone(timedelta(hours=-8))  # perkiraan (tanpa DST)


# ---------------------------------------------------------------- Waktu kuota
def quota_now() -> datetime:
    return datetime.now(_QUOTA_TZ)


def quota_day(now: datetime | None = None) -> str:
    """Tanggal untuk RPD. Gemini mereset RPD sekitar tengah malam Pasifik."""
    return (now or quota_now()).strftime("%Y-%m-%d")


def seconds_until_reset(now: datetime | None = None) -> int:
    now = now or quota_now()
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((nxt - now).total_seconds())


def _fmt_tokens(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


# ---------------------------------------------------------------- Penghitung
class UsageTracker:
    """Hitungan per hari -> per "provider/model" -> requests, errors, token."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or config.USAGE_PATH)
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, dict[str, int]]] = self._load()

    # -- penyimpanan
    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    def _prune(self) -> None:
        keep = sorted(self._data)[-config.USAGE_RETENTION_DAYS:]
        for day in list(self._data):
            if day not in keep:
                del self._data[day]

    # -- pencatatan
    def record(self, provider: str, model: str, *, ok: bool = True,
               input_tokens: int | None = None,
               output_tokens: int | None = None) -> None:
        """Catat satu request (termasuk yang gagal: dihitung konservatif)."""
        key = f"{provider}/{model}"
        with self._lock:
            bucket = self._data.setdefault(quota_day(), {}).setdefault(
                key, {"requests": 0, "errors": 0, "input_tokens": 0, "output_tokens": 0})
            bucket["requests"] += 1
            if not ok:
                bucket["errors"] += 1
            bucket["input_tokens"] += input_tokens or 0
            bucket["output_tokens"] += output_tokens or 0
            self._prune()
            self._save()

    # -- pembacaan
    def _bucket(self, provider: str, model: str, day: str | None = None) -> dict:
        return self._data.get(day or quota_day(), {}).get(f"{provider}/{model}", {})

    def requests_today(self, provider: str, model: str) -> int:
        return self._bucket(provider, model).get("requests", 0)

    def tokens_today(self, provider: str, model: str) -> tuple[int, int]:
        b = self._bucket(provider, model)
        return b.get("input_tokens", 0), b.get("output_tokens", 0)

    def near_daily_limit(self, provider: str, model: str,
                         ratio: float | None = None) -> bool:
        """True bila request hari ini >= ratio * batas RPD (default 90%)."""
        rpd = config.get_limits(provider, model).get("rpd")
        if not rpd:
            return False
        ratio = config.FAILOVER_RPD_RATIO if ratio is None else ratio
        return self.requests_today(provider, model) >= ratio * rpd

    def over_dev_budget(self, provider: str, model: str) -> bool:
        return self.requests_today(provider, model) >= config.DAILY_DEV_BUDGET

    # -- laporan untuk perintah /usage
    def report(self) -> str:
        today = quota_day()
        keys: list[tuple[str, str]] = []
        for e in config.PROVIDER_CHAIN:
            if e["model"]:
                keys.append((e["provider"], e["model"]))
        emb = config.EMBEDDING
        keys.append((emb["provider"], emb["model"]))
        for k in self._data.get(today, {}):
            prov, _, model = k.partition("/")
            if (prov, model) not in keys:
                keys.append((prov, model))

        rows = []
        for prov, model in keys:
            limits = config.get_limits(prov, model)
            rpd = limits.get("rpd")
            n = self.requests_today(prov, model)
            tin, tout = self.tokens_today(prov, model)
            errors = self._bucket(prov, model).get("errors", 0)
            pct = f" ({n * 100 // rpd}%)" if rpd else ""
            flag = ""
            if self.near_daily_limit(prov, model):
                flag = " !! dekat batas"
            elif prov != "mock" and rpd and self.over_dev_budget(prov, model):
                flag = " ! lewat anggaran"
            rows.append((f"{prov}/{model}", f"{n}/{rpd or '?'}{pct}",
                         f"{_fmt_tokens(tin)}/{_fmt_tokens(tout)}", str(errors), flag))

        reset = seconds_until_reset()
        lines = [
            f"Pemakaian hari ini ({today}, zona {config.QUOTA_TIMEZONE}); "
            f"reset RPD dalam {reset // 3600}j {reset % 3600 // 60}m",
            f"Anggaran pengembangan: {config.DAILY_DEV_BUDGET} request/model/hari",
            "",
        ]
        width = max(len(r[0]) for r in rows)
        lines.append(f"{'Model'.ljust(width)}  {'Request/RPD':<16}{'Token in/out':<16}{'Error':<6}")
        for name, req, tok, err, flag in rows:
            lines.append(f"{name.ljust(width)}  {req:<16}{tok:<16}{err:<6}{flag}")
        return "\n".join(lines)


# ---------------------------------------------------------------- Rate limiter
class RateLimiter:
    """Jendela geser 60 detik per "provider/model".

    Batas efektif = rpm * RPM_SAFETY_RATIO (15 -> 12). `acquire` memesan slot
    secara atomik dan memblokir sampai slot tersedia.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._blocked_until: dict[str, float] = {}

    @staticmethod
    def effective_rpm(rpm: int) -> int:
        return max(1, int(rpm * config.RPM_SAFETY_RATIO))

    def penalize(self, provider: str, model: str, seconds: float | None) -> None:
        """Blokir model ini selama `seconds` (petunjuk waktu tunggu dari server)."""
        if not seconds or seconds <= 0:
            return
        key = f"{provider}/{model}"
        with self._lock:
            until = self._clock() + seconds
            self._blocked_until[key] = max(self._blocked_until.get(key, 0.0), until)

    def acquire(self, provider: str, model: str, *,
                max_wait: float | None = None) -> float:
        """Tunggu sampai boleh mengirim, catat slot, kembalikan detik menunggu.

        Bila total tunggu akan melebihi `max_wait`, raise RateLimitError supaya
        router bisa langsung pindah provider alih-alih menunggu lama.
        """
        key = f"{provider}/{model}"
        rpm = config.get_limits(provider, model).get("rpm")
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                events = self._events[key]
                while events and now - events[0] >= 60.0:
                    events.popleft()
                wait = max(0.0, self._blocked_until.get(key, 0.0) - now)
                if not wait and rpm and len(events) >= self.effective_rpm(rpm):
                    wait = events[0] + 60.0 - now
                if not wait:
                    events.append(now)
                    return waited
            if max_wait is not None and waited + wait > max_wait:
                raise RateLimitError(
                    f"Harus menunggu {wait:.0f}s untuk {key} (batas tunggu {max_wait:.0f}s)",
                    provider=provider, model=model, retry_after=wait)
            self._sleep(wait + 0.05)
            waited += wait + 0.05

    def current_rpm(self, provider: str, model: str) -> int:
        key = f"{provider}/{model}"
        with self._lock:
            now = self._clock()
            return sum(1 for t in self._events[key] if now - t < 60.0)


# ---------------------------------------------------------------- Retry
def backoff_delay(attempt: int, retry_after: float | None = None) -> float:
    """Jeda sebelum percobaan berikutnya. `attempt` mulai dari 1.

    Eksponensial + jitter; bila server memberi petunjuk waktu tunggu, dipakai
    bila lebih besar.
    """
    delay = min(config.RETRY_MAX_DELAY,
                config.RETRY_BASE_DELAY * (2 ** (attempt - 1)))
    delay *= 1 + random.uniform(0, config.RETRY_JITTER)
    if retry_after:
        delay = max(delay, min(retry_after + 0.5, config.RETRY_MAX_DELAY * 2))
    return delay


# Instans bersama untuk seluruh aplikasi.
tracker = UsageTracker()
limiter = RateLimiter()
