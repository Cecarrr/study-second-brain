"""Smoke test: cek library, API key, chat, embedding, dan tool call.

Langkah:
  1. python smoke_test.py --list   -> lihat nama model yang tersedia, salin ke .env
  2. python smoke_test.py          -> jalankan semua tes (pakai sekitar 3 request)
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()

CHAT_MODEL = os.getenv("CHAT_MODEL", "")  # isi di .env dari hasil --list
EMBED_MODEL = os.getenv("EMBED_MODEL", "gemini-embedding-001")


def check_imports():
    ok = True
    for m in ["google.genai", "numpy", "dotenv", "pypdf", "docx", "pptx", "pandas", "requests"]:
        try:
            __import__(m)
            print(f"[OK]   import {m}")
        except Exception as e:
            ok = False
            print(f"[FAIL] import {m}: {e}")
    return ok


def run(name, fn):
    try:
        fn()
        print(f"[OK]   {name}")
    except Exception as e:
        print(f"[FAIL] {name}: {e}")


def main():
    if not check_imports():
        sys.exit("Perbaiki import dulu (lihat bagian 5B masterfile).")

    from google import genai
    from google.genai import types

    key = os.getenv("GEMINI_API_KEY")
    if not key:
        sys.exit("GEMINI_API_KEY belum diisi di .env")
    client = genai.Client(api_key=key)

    if "--list" in sys.argv:
        for m in client.models.list():
            print(m.name, getattr(m, "supported_actions", ""))
        return

    if not CHAT_MODEL:
        sys.exit("CHAT_MODEL belum diisi di .env (jalankan: python smoke_test.py --list)")

    def test_chat():
        r = client.models.generate_content(model=CHAT_MODEL, contents="Jawab satu kata saja: 2+2 berapa?")
        print("       jawaban:", (r.text or "").strip())

    def test_embed():
        r = client.models.embed_content(model=EMBED_MODEL, contents=["halo dunia", "hello world"])
        print("       jumlah vektor:", len(r.embeddings), "| dimensi:", len(r.embeddings[0].values))

    def test_tool():
        decl = types.FunctionDeclaration(
            name="add_task",
            description="Menambah tugas atau deadline kuliah.",
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Judul tugas"},
                    "due_date": {"type": "string", "description": "Tanggal format YYYY-MM-DD"},
                },
                "required": ["title"],
            },
        )
        r = client.models.generate_content(
            model=CHAT_MODEL,
            contents="Tambah tugas: laporan praktikum jaringan, deadline 2026-11-20",
            config=types.GenerateContentConfig(tools=[types.Tool(function_declarations=[decl])]),
        )
        calls = r.function_calls or []
        if not calls:
            raise RuntimeError("model tidak memanggil tool")
        print("       tool call:", calls[0].name, dict(calls[0].args))

    run(f"chat ({CHAT_MODEL})", test_chat)
    run(f"embedding ({EMBED_MODEL})", test_embed)
    run("tool call", test_tool)


if __name__ == "__main__":
    main()
