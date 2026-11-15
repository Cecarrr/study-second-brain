# Study Second Brain - OpenCode Instructions

## Developer Commands
- **Run all tests:** `py -3.13 -m pytest -o pythonpath=. tests`
- **Run keypool tests:** `py -3.13 -m pytest -o pythonpath=. keypool/tests`
- **Main entry point:** `py -3.13 main.py`

## Architecture & Modules
- **`rag/`**: RAG pipeline (chunking, embedding, indexing, retrieval, BM25, storage, ingestion, importers).
- **`tools/`**: Domain tools (notes, jadwal, knowledge, quiz, tasks, summary, sources, trash, select, registry).
- **`keypool/`**: API key rotation pool (`pool.toml`, quota, router, pooled, openai_compat).
- **`providers/`**: LLM providers (`gemini`, `mock`, `base`).
- **`db/`**: SQLite database persistence (`db/database.py`).

## Conventions & Quirks
- **Language:** Indonesian is used for prompts, documentation, UI messages, and agent responses.
- **Environment:** Requires Python 3.13+, `.env`, and `pool.toml`.
