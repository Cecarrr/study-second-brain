"""Uji pool tanpa jaringan dan tanpa SDK. Jalankan: python -m unittest keypool.tests.test_keypool -v"""
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from providers.base import (AuthError, BadRequestError, BaseProvider, ChatResponse,
                            EmptyResponseError, Message, ProviderError, RateLimitError, ServerError)
from keypool.errors import ErrorKind, classify
from keypool.openai_compat import classify_http
from keypool.pool import Pool, ProviderSpec
from keypool.quota import UsageStore, next_reset
from keypool.router import LLMRouter, PoolExhausted

T0 = datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc).timestamp()   # 12:00 UTC


class Clock:
    def __init__(self, t=T0):
        self.t = t
    def now(self):
        return self.t
    def sleep(self, s):
        self.t += s


class Fake(BaseProvider):
    name = "mock"
    supports_embedding = True

    def __init__(self, model, key, handler, log):
        super().__init__(model)
        self.api_key, self.handler, self.log = key, handler, log

    def chat(self, messages, tools=None):
        self.log.append(("chat", self.api_key, self.model))
        out = self.handler("chat", self.api_key, self.model)
        if isinstance(out, Exception):
            raise out
        return ChatResponse(text=str(out), input_tokens=5, output_tokens=5, provider="mock", model=self.model)

    def embed(self, texts, task="document"):
        self.log.append(("embed", self.api_key, self.model))
        out = self.handler("embed", self.api_key, self.model)
        if isinstance(out, Exception):
            raise out
        return [[float(len(t)), 1.0] for t in texts]


class Script:
    """Skrip hasil per key: daftar outcome yang dikonsumsi; yang terakhir berulang."""
    def __init__(self, **by_key):
        self.by_key = by_key
    def __call__(self, kind, key, model):
        seq = self.by_key.get(key, ["ok"])
        out = seq.pop(0) if len(seq) > 1 else seq[0]
        return out if isinstance(out, Exception) else f"ok:{key}/{model}"


def daily():
    return RateLimitError("429 ... GenerateRequestsPerDayPerProjectPerModel-FreeTier", retryable=False, status=429)


def make(accounts, handler=None, db=":memory:", **extra):
    clock = Clock()
    log = []
    h = handler or (lambda kind, key, model: f"ok:{key}/{model}")
    reg = {"mock": ProviderSpec(lambda *, model, key, kind, **_: Fake(model, key, h, log),
                                scope="model", reset_tz="UTC", embedding=True)}
    pool = Pool.from_dict({"account": accounts, **extra}, env={}, registry=reg)
    usage = UsageStore(db, clock=clock.now)
    notes = []
    router = LLMRouter(pool, usage, clock=clock.now, sleep=clock.sleep, notify=notes.append)
    return router, log, clock, notes


def acct(name, key, chat=("m1",), embed=(), **kw):
    return {"name": name, "provider": "mock", "key": key, "chat": list(chat), "embed": list(embed), **kw}


MSG = [Message.user("hai")]


class PoolTests(unittest.TestCase):
    def test_daily_429_rotates_and_is_remembered(self):
        r, log, clock, notes = make([acct("A", "kA"), acct("B", "kB")], Script(kA=[daily()], kB=["ok"]))
        res = r.chat(MSG)
        self.assertEqual(res.account, "B")
        r.chat(MSG); r.chat(MSG)
        self.assertEqual([c[1] for c in log], ["kA", "kB", "kB", "kB"])   # A tidak dicoba lagi

    def test_local_rpd_cap_drains_then_stops_without_wasted_calls(self):
        r, log, *_ = make([acct("A", "kA"), acct("B", "kB")], limits={"m1": {"rpd": 3}})
        for _ in range(6):
            r.chat(MSG)
        keys = [c[1] for c in log]
        self.assertEqual((keys.count("kA"), keys.count("kB")), (3, 3))     # A dikuras dulu, lalu B
        with self.assertRaises(PoolExhausted):
            r.chat(MSG)
        self.assertEqual(len(log), 6)

    def test_rpm_waits_when_only_option(self):
        r, log, clock, _ = make([acct("A", "kA")], limits={"m1": {"rpm": 2}},
                                safety={"rpm": 1.0}, settings={"max_wait_s": 120})
        r.chat(MSG); r.chat(MSG)
        before = clock.t
        r.chat(MSG)
        self.assertGreaterEqual(clock.t - before, 59)
        self.assertEqual(len(log), 3)

    def test_all_exhausted_raises_ratelimit_subclass_then_recovers_after_reset(self):
        r, log, clock, _ = make([acct("A", "kA")], limits={"m1": {"rpd": 1}})
        r.chat(MSG)
        with self.assertRaises(PoolExhausted) as cm:
            r.chat(MSG)
        self.assertIsInstance(cm.exception, RateLimitError)
        self.assertFalse(cm.exception.retryable)                           # Embedder: berhenti, tanpa retry
        self.assertAlmostEqual(cm.exception.retry_at, next_reset(T0, "UTC"), delta=1)
        clock.t = next_reset(T0, "UTC") + 5
        self.assertTrue(r.chat(MSG).text)

    def test_auth_error_disables_whole_account(self):
        r, log, *_ = make([acct("A", "kA", chat=("m1", "m2")), acct("B", "kB")],
                          Script(kA=[AuthError("bad", status=401)], kB=["ok"]))
        self.assertEqual(r.chat(MSG).account, "B")
        r.chat(MSG)
        self.assertEqual([c[1] for c in log].count("kA"), 1)               # m2 milik A juga tidak dicoba

    def test_invalid_key_reported_as_400_is_still_auth(self):
        r, log, *_ = make([acct("A", "kA", chat=("m1", "m2")), acct("B", "kB")],
                          Script(kA=[BadRequestError("400 API key not valid. Please pass a valid API key.",
                                                      status=400)], kB=["ok"]))
        self.assertEqual(r.chat(MSG).account, "B")
        self.assertEqual([c[1] for c in log].count("kA"), 1)

    def test_shared_project_shares_quota(self):
        r, log, *_ = make([acct("A", "kA", chat=("m1", "m2"), project="P"),
                           acct("B", "kB", chat=("m1",), project="P")],
                          Script(kA=[daily(), "ok"], kB=["ok"]))
        res = r.chat(MSG)                    # A/m1 habis -> B/m1 ikut terblokir (satu project) -> A/m2
        self.assertEqual(res.model, "m2")
        self.assertNotIn("kB", [c[1] for c in log])

    def test_embed_and_chat_counted_separately(self):
        r, log, *_ = make([acct("A", "kA", chat=("e1",), embed=("e1",))], limits={"e1": {"rpd": 2}},
                          embedding={"provider": "mock", "model": "e1", "batch_size": 2, "count": "call"})
        r.chat(MSG)
        self.assertEqual(len(r.embed(["a", "b", "c"])), 3)                 # 2 batch = 2 request embed
        self.assertTrue(r.chat(MSG).text)                                  # jatah chat masih ada
        with self.assertRaises(PoolExhausted):
            r.embed(["d"])
        self.assertEqual(sum(1 for c in log if c[0] == "embed"), 2)

    def test_embed_count_per_item(self):
        r, log, *_ = make([acct("A", "kA", embed=("e1",))], limits={"e1": {"rpd": 3}},
                          embedding={"provider": "mock", "model": "e1", "batch_size": 2, "count": "item"})
        r.embed(["a", "b"])                                                # 2 teks terhitung
        r.embed(["c", "d"])                                                # total 4 >= 3 -> batch berikut diblokir
        with self.assertRaises(PoolExhausted):
            r.embed(["e"])

    def test_embedding_uses_only_pinned_model(self):
        reg = {"mock": ProviderSpec(lambda **kw: None, embedding=True)}
        pool = Pool.from_dict({"account": [acct("A", "kA", embed=("e1", "e2"))],
                               "embedding": {"provider": "mock", "model": "e1"}}, env={}, registry=reg)
        self.assertEqual([s.model for s in pool.embed_slots], ["e1"])
        self.assertTrue(any("e2" in w for w in pool.warnings))

    def test_embedding_defaults_from_config(self):
        pool = Pool.from_dict({"account": [acct("A", "kA", embed=("e1",))]}, env={},
                              registry={"mock": ProviderSpec(lambda **kw: None, embedding=True)},
                              embedding_defaults={"provider": "mock", "model": "e1", "dim": 768})
        self.assertEqual(pool.embedding.signature, "mock:e1:768")
        self.assertEqual(len(pool.embed_slots), 1)

    def test_persistence_across_restart(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "usage.db")
            r, log, clock, _ = make([acct("A", "kA")], Script(kA=[daily()]), db=db)
            with self.assertRaises(PoolExhausted):
                r.chat(MSG)
            u2 = UsageStore(db, clock=clock.now)
            ok, wait, why = u2.check(r.pool.chat_slots[0], r.pool.safety, clock.now())
            self.assertFalse(ok)
            r.usage.db.close()                                   # Windows: berkas terkunci selama koneksi terbuka
            u2.db.close()

    def test_model_priority_over_account_order(self):
        r, *_ = make([acct("A", "kA", chat=("m1", "m2")), acct("B", "kB", chat=("m1", "m2"))])
        self.assertEqual([s.label for s in r.pool.chat_slots],
                         ["mock:A/m1", "mock:B/m1", "mock:A/m2", "mock:B/m2"])

    def test_bad_request_stops_after_two_slots_and_keeps_original_type(self):
        bad = BadRequestError("schema salah", status=400)
        r, log, *_ = make([acct("A", "kA"), acct("B", "kB"), acct("C", "kC")],
                          Script(kA=[bad], kB=[bad], kC=["ok"]))
        with self.assertRaises(BadRequestError):
            r.chat(MSG)
        self.assertEqual(len(log), 2)

    def test_empty_response_tries_other_slot_without_penalty(self):
        r, log, *_ = make([acct("A", "kA"), acct("B", "kB")],
                          Script(kA=[EmptyResponseError("kosong", retryable=True), "ok"], kB=["ok"]))
        self.assertEqual(r.chat(MSG).account, "B")
        self.assertEqual(r.chat(MSG).account, "A")                         # A tidak dihukum


class ClassifyTests(unittest.TestCase):
    def test_provider_error_mapping(self):
        k = lambda e: classify(e)[0]
        self.assertEqual(k(RateLimitError("x", retryable=False)), ErrorKind.QUOTA_DAILY)
        self.assertEqual(k(RateLimitError("Quota ... PerDay ...", retryable=True)), ErrorKind.QUOTA_DAILY)
        self.assertEqual(classify(RateLimitError("x", retry_after=49.0, retryable=True)),
                         (ErrorKind.RATE_MINUTE, 49.0))
        self.assertEqual(k(RateLimitError("x", retryable=True)), ErrorKind.RATE_UNKNOWN)
        self.assertEqual(k(AuthError("x", status=401)), ErrorKind.AUTH)
        self.assertEqual(k(AuthError("permission denied for model", status=403)), ErrorKind.FORBIDDEN)
        self.assertEqual(k(BadRequestError("model not found", status=404)), ErrorKind.FORBIDDEN)
        self.assertEqual(k(BadRequestError("API key not valid", status=400)), ErrorKind.AUTH)
        self.assertEqual(k(BadRequestError("FAILED_PRECONDITION free tier", status=400)), ErrorKind.FORBIDDEN)
        self.assertEqual(k(BadRequestError("bad schema", status=400)), ErrorKind.BAD_REQUEST)
        self.assertEqual(k(ServerError("503")), ErrorKind.TRANSIENT)

    def test_openai_compat_http(self):
        reset_ms = (time.time() + 3600) * 1000
        body = {"error": {"message": "Rate limit exceeded: free-models-per-day", "code": 429,
                          "metadata": {"headers": {"X-RateLimit-Reset": str(int(reset_ms))}}}}
        e = classify_http(429, body, {})
        self.assertIsInstance(e, RateLimitError)
        self.assertEqual(classify(e)[0], ErrorKind.QUOTA_DAILY)
        self.assertAlmostEqual(e.reset_in, 3600, delta=5)
        e = classify_http(429, {"error": {"message": "Provider returned error"}}, {"Retry-After": "12"})
        self.assertEqual(classify(e), (ErrorKind.RATE_MINUTE, 12.0))
        e = classify_http(404, {"error": {"message": "No endpoints found that support tool use"}}, {})
        self.assertEqual(classify(e)[0], ErrorKind.FORBIDDEN)
        self.assertEqual(classify(classify_http(401, {}, {}))[0], ErrorKind.AUTH)
        self.assertEqual(classify(classify_http(503, {}, {}))[0], ErrorKind.TRANSIENT)


if __name__ == "__main__":
    unittest.main()
