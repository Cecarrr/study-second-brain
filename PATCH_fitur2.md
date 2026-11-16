# Fitur 2: cari/ganti untuk file yang sudah ada

Tiap blok CARI harus cocok persis satu kali. `agent.py` dan `config.py` di zip ini sudah berisi hasilnya; daftar ini hanya untuk menambal salinan lain. `eval_models.py` dikirim utuh (menggantikan versi fitur 1).

## agent.py

### agent.py #1
CARI:
```python
from providers.base import Message, ProviderError
```
GANTI DENGAN:
```python
from providers.base import Message, ProviderError
from tools.select import asked_clarification, groups_of, select_specs
```

### agent.py #2
CARI:
```python
        self.history: list[Message] = []        # pasangan user/assistant dari giliran lama
```
GANTI DENGAN:
```python
        self.history: list[Message] = []        # pasangan user/assistant dari giliran lama
        self._last_groups: frozenset = frozenset()   # kelompok tool yang dipertahankan bila asisten bertanya balik
```

### agent.py #3
CARI:
```python
    def reset(self) -> None:
        self.history.clear()
```
GANTI DENGAN:
```python
    def reset(self) -> None:
        self.history.clear()
        self._last_groups = frozenset()
```

### agent.py #4
CARI:
```python
        specs = self.reg.specs()
        done: dict[str, tuple[str, str]] = {}   # kunci panggilan -> (nama tool, hasil) pada giliran ini
```
GANTI DENGAN:
```python
        all_specs = self.reg.specs()
        specs = (select_specs(text, self.reg, self._last_groups)
                 if getattr(config, "TOOL_SUBSET", True) else all_specs)
        widened = len(specs) == len(all_specs)  # True = sudah semua tool, tidak perlu melebar lagi
        picked = None if widened else groups_of(s.name for s in specs) - {"pengetahuan"}
        called: list[str] = []                  # tool yang dijalankan pada giliran ini
        done: dict[str, tuple[str, str]] = {}   # kunci panggilan -> (nama tool, hasil) pada giliran ini
```

### agent.py #5
CARI:
```python
            msgs.append(resp.to_message())
            if not resp.tool_calls:
```
GANTI DENGAN:
```python
            if resp.tool_calls and not widened:
                sent, known = {s.name for s in specs}, set(self.reg.names)
                if any(c.name in known and c.name not in sent for c in resp.tool_calls):
                    # Model butuh tool yang tidak dikirim: buang respons ini, ulangi sekali dengan semua tool.
                    specs, widened = all_specs, True
                    continue
            msgs.append(resp.to_message())
            if not resp.tool_calls:
```

### agent.py #6
CARI:
```python
                self._trim()
                return answer + self._grounding_note(msgs, text, answer)
```
GANTI DENGAN:
```python
                self._trim()
                # Bila jawabannya pertanyaan balik, giliran berikutnya masih butuh kelompok tool yang sama.
                self._last_groups = (groups_of(called) or picked or frozenset()) \
                    if asked_clarification(answer) else frozenset()
                return answer + self._grounding_note(msgs, text, answer)
```

### agent.py #7
CARI:
```python
                self.notify(f"  [{call.name}] {json.dumps(call.args, ensure_ascii=False)[:120]}")
                res = self.reg.execute(call)
```
GANTI DENGAN:
```python
                self.notify(f"  [{call.name}] {json.dumps(call.args, ensure_ascii=False)[:120]}")
                called.append(call.name)
                res = self.reg.execute(call)
```

## config.py

### config.py #1
CARI:
```python
HISTORY_MAX_TURNS = 6         # giliran terakhir yang disimpan
```
GANTI DENGAN:
```python
HISTORY_MAX_TURNS = 6         # giliran terakhir yang disimpan
TOOL_SUBSET = True            # kirim hanya tool yang relevan per pesan (tools/select.py); False = semua tool
```
