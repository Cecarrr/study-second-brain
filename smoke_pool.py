"""Uji hidup keypool memakai pool.toml dan .env asli. Memakai sekitar 4 request.

  python smoke_pool.py              -> chat, tool call 2 giliran, embedding, laporan kuota
  python smoke_pool.py --failover   -> giliran ke-2 SENGAJA dipaksa pindah ke slot lain, untuk
                                       menguji riwayat tool call (thought signature) yang diputar
                                       ulang ke model/account berbeda. Butuh >= 2 slot chat.
"""
import sys
import time

import keypool
from providers.base import Message, ToolSpec

SPEC = ToolSpec(
    name="add_task",
    description="Menambah tugas atau deadline kuliah.",
    parameters={"type": "object", "properties": {
        "title": {"type": "string", "description": "Judul tugas"},
        "due_date": {"type": "string", "description": "Tanggal format YYYY-MM-DD"}},
        "required": ["title"]})


def who(r):
    return f"{r.provider}:{getattr(r, 'account', '?')}/{r.model}"


def run(name, fn):
    try:
        fn()
        print(f"[OK]   {name}")
    except Exception as e:
        print(f"[FAIL] {name}: {type(e).__name__}: {e}")


def main():
    router = keypool.get_router()
    pool = router.pool
    print(f"Slot chat: {len(pool.chat_slots)} | slot embed: {len(pool.embed_slots)} "
          f"| embedding: {pool.embedding.signature}")
    for w in pool.warnings:
        print("[peringatan]", w)
    router.notify = print            # tampilkan kejadian failover di layar
    failover = "--failover" in sys.argv

    def test_chat():
        r = keypool.chat([Message.user("Jawab satu kata saja: 2+2 berapa?")])
        print(f"       dijawab {who(r)}: {r.text!r}")

    def test_tool_roundtrip():
        msgs = [Message.user("Tambah tugas: laporan praktikum jaringan, deadline 2026-11-20")]
        r1 = keypool.chat(msgs, [SPEC])
        if not r1.tool_calls:
            raise RuntimeError(f"model tidak memanggil tool (teks: {r1.text[:80]!r})")
        print(f"       giliran 1 oleh {who(r1)}: {r1.tool_calls[0].name} {r1.tool_calls[0].args}")
        msgs.append(r1.to_message())
        for c in r1.tool_calls:
            msgs.append(Message.tool(c, '{"ok": true, "task_id": 1}'))
        if failover:
            first = router.last_slot
            router.usage.block(first.qkey, time.time() + 120, "uji failover")
        try:
            r2 = keypool.chat(msgs, [SPEC])
        finally:
            if failover:
                router.usage.clear_blocks()
        print(f"       giliran 2 oleh {who(r2)}: {r2.text[:100]!r}")
        if failover and (r2.provider, r2.account, r2.model) == (r1.provider, r1.account, r1.model):
            raise RuntimeError("failover tidak berpindah slot (hanya ada satu slot chat?)")

    def test_embed():
        docs = keypool.embed(["halo dunia", "hello world"], task="document")
        q = keypool.embed(["sapaan"], task="query")
        print(f"       dokumen: {len(docs)} vektor, dimensi {len(docs[0])} | query dimensi {len(q[0])}")

    run("chat", test_chat)
    run("tool call 2 giliran" + (" + failover" if failover else ""), test_tool_roundtrip)
    if pool.embed_slots:
        run("embedding", test_embed)
    print()
    print(keypool.usage_report())


if __name__ == "__main__":
    main()
