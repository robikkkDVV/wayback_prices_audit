"""Загрузка YAML-конфигов."""
import os
import yaml
from pathlib import Path

from .paths import CONFIG_DIR


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_all_configs() -> dict:
    return {
        "app": load_yaml(CONFIG_DIR / "app.yaml"),
        "llm": load_yaml(CONFIG_DIR / "llm.yaml"),
        "clean": load_yaml(CONFIG_DIR / "clean.yaml"),
        "extract": load_yaml(CONFIG_DIR / "extract.yaml"),
    }