"""Preferensi pengguna yang diingat antar sesi (data/prefs.json)."""
import json

import config

PATH = config.DATA_DIR / "prefs.json"


def load() -> dict:
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save(**kw) -> dict:
    data = {**load(), **kw}
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return data
