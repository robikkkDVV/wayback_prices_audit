"""Единое логирование: и в консоль, и в файл, и в веб."""
import logging
import sys
from datetime import datetime
from pathlib import Path
from threading import Lock

from .paths import ProjectPaths


class ProjectLogger:
    """Логгер для одного проекта. Пишет в файл и хранит последние N строк в памяти."""

    _lock = Lock()
    _buffer_size = 500

    def __init__(self, paths: ProjectPaths, name: str = "main"):
        self.paths = paths
        self.paths.ensure_dirs()
        self.name = name
        self.buffer = []  # последние строки для веба

        log_file = {
            "main": paths.main_log,
            "download": paths.download_log,
            "analyze": paths.analyze_log,
        }.get(name, paths.main_log)

        self.log_file = log_file

    def _write(self, level: str, message: str):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] [{level}] {message}"

        with self._lock:
            self.buffer.append(line)
            if len(self.buffer) > self._buffer_size:
                self.buffer = self.buffer[-self._buffer_size:]

            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")

        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            enc = sys.stdout.encoding or "utf-8"
            safe = line.encode(enc, errors="replace").decode(enc, errors="replace")
            print(safe, flush=True)

    def info(self, msg):    self._write("INFO", msg)
    def warn(self, msg):    self._write("WARN", msg)
    def error(self, msg):   self._write("ERROR", msg)
    def success(self, msg): self._write("OK", msg)

    def tail(self, n: int = 100):
        return self.buffer[-n:]