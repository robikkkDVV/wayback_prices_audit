"""Единая работа с JSON-прогрессом."""
import json
import time
from pathlib import Path
from threading import Lock


class JsonProgress:
    """Потокобезопасный прогресс, сохраняемый в JSON."""

    def __init__(self, path: Path, default: dict):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self.data = self._load(default)

    def _load(self, default):
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return dict(default)

    def save(self):
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            payload = json.dumps(self.data, ensure_ascii=False, indent=2)

            # Пишем во временный файл
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)

            # Пытаемся заменить с ретраями (Windows: антивирус/лок)
            last_err = None
            for attempt in range(10):
                try:
                    tmp.replace(self.path)
                    return
                except PermissionError as e:
                    last_err = e
                    time.sleep(0.2 * (attempt + 1))
                except OSError as e:
                    last_err = e
                    time.sleep(0.2 * (attempt + 1))

            # Последняя попытка: пишем напрямую (не атомарно, но работает)
            try:
                with open(self.path, "w", encoding="utf-8") as f:
                    f.write(payload)
                try:
                    tmp.unlink()
                except Exception:
                    pass
            except Exception:
                # Сдаёмся — но не роняем весь пайплайн
                print(f"[progress] Не удалось сохранить {self.path}: {last_err}")

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value

    def append(self, key, value):
        self.data.setdefault(key, []).append(value)