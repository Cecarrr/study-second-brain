"""Konfigurasi pool: daftar account/key/model -> daftar Slot berurutan, masing-masing membawa
instance provider (BaseProvider milik proyek) yang siap dipanggil.

Kuota dihitung per (project, model, jenis):
  - key dalam project yang sama berbagi kuota (beri `project` yang sama),
  - chat dan embedding tidak saling memakan hitungan,
  - provider ber-scope "account" (OpenRouter) membagi kuota harian ke semua modelnya.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


class PoolConfigError(Exception):
    pass


@dataclass(frozen=True)
class Limits:
    rpm: Optional[int] = None
    rpd: Optional[int] = None
    tpm: Optional[int] = None


@dataclass(frozen=True)
class ProviderSpec:
    factory: Callable[..., Any]              # factory(model=, key=, base_url=, kind=) -> BaseProvider
    scope: str = "model"                     # "model" | "account"
    reset_tz: str = "UTC"
    default_base_url: Optional[str] = None
    embedding: bool = False


@dataclass(frozen=True)
class Slot:
    account: str
    provider: str
    project: str
    model: str
    kind: str                                # "chat" | "embed"
    limits: Limits
    reset_tz: str
    scope: str
    rank: int
    impl: Any = field(repr=False, compare=False, default=None)   # instance BaseProvider

    @property
    def sid(self) -> str:
        return f"{self.account}|{self.model}|{self.kind}"

    @property
    def qkey(self) -> str:
        return f"{self.project}|{'*' if self.scope == 'account' else self.model}|{self.kind}"

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.account}/{self.model}" + ("" if self.kind == "chat" else " [embed]")


@dataclass(frozen=True)
class EmbeddingConfig:
    provider: Optional[str] = None
    model: Optional[str] = None
    dim: Optional[int] = None
    batch_size: int = 20
    count: str = "item"                      # "item": kuota per teks; "call": per request

    @property
    def signature(self) -> str:              # format sama dengan config.embedding_id()
        return f"{self.provider}:{self.model}:{self.dim or 'default'}"


def _gemini(*, model, key, kind, **_):
    from providers.gemini import GeminiProvider
    return GeminiProvider(model, embed_model=model if kind == "embed" else None, api_key=key)


def _openai(*, model, key, base_url, name="openai_compat", **_):
    from .openai_compat import OpenAICompatProvider
    return OpenAICompatProvider(model, api_key=key, base_url=base_url, name=name)


def default_registry() -> dict:
    return {
        "gemini": ProviderSpec(_gemini, scope="model", reset_tz="America/Los_Angeles", embedding=True),
        "openrouter": ProviderSpec(lambda **kw: _openai(name="openrouter", **kw), scope="account",
                                   reset_tz="UTC", default_base_url="https://openrouter.ai/api/v1"),
        "openai_compat": ProviderSpec(_openai, scope="model", reset_tz="UTC"),
    }


_DEFAULT_SAFETY = {"rpm": 0.8, "tpm": 0.8, "rpd": 1.0}
_DEFAULT_SETTINGS = {"max_wait_s": 90, "max_attempts": 30}


class Pool:
    def __init__(self, chat_slots, embed_slots, embedding, safety, settings, warnings):
        self.chat_slots, self.embed_slots = chat_slots, embed_slots
        self.embedding, self.safety = embedding, safety
        self.settings, self.warnings = settings, warnings

    @classmethod
    def from_file(cls, path="pool.toml", env=None, registry=None, embedding_defaults=None):
        import tomllib
        with open(path, "rb") as f:
            return cls.from_dict(tomllib.load(f), env=env, registry=registry,
                                 embedding_defaults=embedding_defaults)

    @classmethod
    def from_dict(cls, data: dict, env=None, registry=None, embedding_defaults=None):
        env = os.environ if env is None else env
        registry = registry or default_registry()
        errors, warnings = [], []
        safety = {**_DEFAULT_SAFETY, **data.get("safety", {})}
        settings = {**_DEFAULT_SETTINGS, **data.get("settings", {})}
        global_limits = data.get("limits", {})
        d, e = embedding_defaults or {}, data.get("embedding", {})
        emb = EmbeddingConfig(provider=e.get("provider", d.get("provider")),
                              model=e.get("model", d.get("model")), dim=e.get("dim", d.get("dim")),
                              batch_size=int(e.get("batch_size", d.get("batch_size", 20))),
                              count=e.get("count", "item"))
        chat_order = list(data.get("chat_order", []))

        raw, rank_by_model, seen_keys = [], {}, {}
        accounts = data.get("account", [])
        for i, acc in enumerate(accounts):
            name = acc.get("name") or f"account{i + 1}"
            if acc.get("enabled", True) is False:
                continue
            pname = acc.get("provider")
            spec = registry.get(pname)
            if spec is None:
                errors.append(f"{name}: provider '{pname}' tidak dikenal ({', '.join(registry)})")
                continue
            key = acc.get("key") or env.get(acc.get("key_env", ""), "")
            if not key:
                warnings.append(f"{name}: dilewati, key kosong "
                                f"(key_env={acc.get('key_env')!r} tidak ada di environment/.env)")
                continue
            if key in seen_keys:
                warnings.append(f"{name}: key sama dengan account '{seen_keys[key]}'. "
                                f"Beri `project` yang sama bila memang satu project.")
            seen_keys[key] = name
            base_url = acc.get("base_url") or spec.default_base_url
            if pname == "openai_compat" and not base_url:
                errors.append(f"{name}: provider openai_compat wajib punya base_url")
                continue
            common = dict(account=name, provider=pname, project=acc.get("project", name),
                          reset_tz=acc.get("reset_tz", spec.reset_tz), scope=spec.scope)
            acc_limits = acc.get("limits", {})

            def limits_for(model):
                m = {**global_limits.get(model, {}), **acc_limits.get(model, {})}
                return Limits(rpm=m.get("rpm"), rpd=m.get("rpd"), tpm=m.get("tpm"))

            for model in acc.get("chat", []):
                rank_by_model.setdefault(model, len(rank_by_model))
                raw.append(("chat", i, model, common, spec, key, base_url, limits_for(model)))
            for model in acc.get("embed", []):
                if not spec.embedding:
                    errors.append(f"{name}: embedding belum didukung provider '{pname}'")
                    continue
                raw.append(("embed", i, model, common, spec, key, base_url, limits_for(model)))

        for m in chat_order:
            if m not in rank_by_model:
                warnings.append(f"chat_order menyebut model '{m}' yang tidak ada di account mana pun")
        order = {m: idx for idx, m in enumerate(chat_order)}
        base = len(order)
        for m, r in rank_by_model.items():
            order.setdefault(m, base + r)

        chat_slots, embed_slots = [], []
        for kind, i, model, common, spec, key, base_url, lim in raw:
            if kind == "embed" and not (emb.model and model == emb.model and common["provider"] == emb.provider):
                warnings.append(f"{common['account']}: embed '{model}' diabaikan (bukan model embedding "
                                f"terpilih {emb.provider}:{emb.model}; vektor beda model tidak kompatibel)")
                continue
            try:
                impl = spec.factory(model=model, key=key, base_url=base_url, kind=kind)
            except Exception as ex:  # noqa: BLE001
                errors.append(f"{common['account']}/{model}: gagal membuat provider: {ex}")
                continue
            slot = Slot(model=model, kind=kind, limits=lim, rank=order.get(model, 0) if kind == "chat" else 0,
                        impl=impl, **common)
            (chat_slots if kind == "chat" else embed_slots).append((i, slot))
        chat_slots = [s for _, s in sorted(chat_slots, key=lambda t: (t[1].rank, t[0]))]
        embed_slots = [s for _, s in sorted(embed_slots, key=lambda t: t[0])]

        if not errors and not chat_slots:
            errors.append("tidak ada slot chat yang aktif (cek key di .env dan daftar account)")
        if errors:
            raise PoolConfigError("Konfigurasi pool bermasalah:\n- " + "\n- ".join(errors))
        return cls(chat_slots, embed_slots, emb, safety, settings, warnings)
