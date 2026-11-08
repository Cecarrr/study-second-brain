"""Pemetaan error provider proyek (providers/base.py) ke tindakan pool."""
from __future__ import annotations

from enum import Enum
from typing import Optional

from providers.base import (AuthError, BadRequestError, EmptyResponseError, ProviderError,
                            RateLimitError)


class ErrorKind(str, Enum):
    RATE_MINUTE = "rate_minute"    # limit per menit: jeda singkat
    QUOTA_DAILY = "quota_daily"    # kuota harian habis: blokir sampai reset
    RATE_UNKNOWN = "rate_unknown"  # 429 tanpa rincian
    AUTH = "auth"                  # key ditolak: matikan seluruh account
    FORBIDDEN = "forbidden"        # model/akses tidak tersedia untuk slot ini
    TRANSIENT = "transient"        # 5xx, timeout, jaringan
    BAD_REQUEST = "bad_request"    # permintaan ditolak / respons kosong: coba slot lain tanpa hukuman


def classify(e: ProviderError) -> tuple[ErrorKind, Optional[float]]:
    """-> (jenis, retry_after detik)."""
    msg = str(e)
    low = msg.lower()
    norm = low.replace(" ", "").replace("_", "")
    status = getattr(e, "status", None)

    if isinstance(e, RateLimitError):
        daily = (not e.retryable) or "perday" in norm or "requestsperday" in norm or "tokensperday" in norm
        if daily:
            return ErrorKind.QUOTA_DAILY, None
        if e.retry_after:
            return ErrorKind.RATE_MINUTE, e.retry_after
        return ErrorKind.RATE_UNKNOWN, None
    if isinstance(e, AuthError):
        if status == 401 or any(k in low for k in ("api key", "leaked", "suspended", "expired")):
            return ErrorKind.AUTH, None
        return ErrorKind.FORBIDDEN, None
    if isinstance(e, BadRequestError):
        if "api key not valid" in low or "api_key_invalid" in low or "api key expired" in low:
            return ErrorKind.AUTH, None
        if status == 404 or "failedprecondition" in norm or "not available in your country" in low \
                or "location is not supported" in low:
            return ErrorKind.FORBIDDEN, None
        return ErrorKind.BAD_REQUEST, None
    if isinstance(e, EmptyResponseError):
        return ErrorKind.BAD_REQUEST, None
    return ErrorKind.TRANSIENT, None
