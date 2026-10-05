"""Хранение рецептов — один файл на домен со списком рецептов.

Формат data/_recipes_cache/<domain>.json:
{
  "domain": "stella-pro.ru",
  "created_at": "2026-09-30T17:30:00",
  "updated_at": "2026-09-30T17:35:00",
  "recipes": [
    {
      "id": "01",
      "date_from": "2025-07-16",
      "date_to": "2026-12-31",
      "trained_on": "20260618134649",
      "coverage": 0.92,
      "field_groups": {...},
      "specifications": {...},
      "images": {...}
    },
    ...
  ]
}
"""
import json
from datetime import datetime
from pathlib import Path

from ..core.paths import global_recipe_path


def load_recipe(domain: str) -> dict | None:
    """
    Загружает файл рецептов домена.
    Возвращает dict со списком "recipes" или None, если файла нет.
    """
    path = global_recipe_path(domain)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        # Совместимость со старым форматом (один рецепт)
        if "recipes" not in data:
            data = {
                "domain": domain,
                "created_at": data.get("_cached_at", datetime.now().isoformat()),
                "updated_at": data.get("_cached_at", datetime.now().isoformat()),
                "recipes": [{
                    "id": "01",
                    "date_from": "1900-01-01",
                    "date_to": "2099-12-31",
                    "trained_on": data.get("trained_on", "unknown"),
                    "coverage": data.get("coverage", 0.0),
                    **{k: v for k, v in data.items()
                       if k in ("field_groups", "specifications", "images",
                                "notes", "recipe_version", "page_type")}
                }]
            }
        return data
    except Exception:
        return None


def save_recipe(domain: str, recipe_data: dict) -> Path:
    """Сохраняет файл рецептов целиком (перезаписывает)."""
    path = global_recipe_path(domain)
    recipe_data["_cached_at"] = datetime.now().isoformat()
    recipe_data["_domain"] = domain
    path.write_text(
        json.dumps(recipe_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def add_recipe(domain: str, new_recipe: dict) -> dict:
    """
    Добавляет новый рецепт в файл домена.
    Старые рецепты НЕ удаляются.
    Возвращает обновлённый recipe_data.
    """
    existing = load_recipe(domain) or {
        "domain": domain,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "recipes": [],
    }

    existing["recipes"].append(new_recipe)
    # Сортируем: свежие сверху
    existing["recipes"].sort(
        key=lambda r: r.get("date_from", ""),
        reverse=True,
    )
    existing["updated_at"] = datetime.now().isoformat()

    save_recipe(domain, existing)
    return existing


def recipe_exists(domain: str) -> bool:
    return global_recipe_path(domain).exists()


def get_recipe_for_date(domain: str, date_iso: str) -> dict | None:
    """
    Выбирает рецепт, в диапазон которого попадает дата.
    Возвращает первый подходящий (свежайший с диапазоном).
    Если ни один не подходит — берёт ближайший по дате.
    """
    data = load_recipe(domain)
    if not data:
        return None

    recipes = data.get("recipes", [])
    if not recipes:
        return None

    # 1. Точное попадание в диапазон
    for r in recipes:
        d_from = r.get("date_from", "")
        d_to = r.get("date_to", "")
        if d_from and d_to and d_from <= date_iso <= d_to:
            return r

    # 2. Если не попали ни в один диапазон — берём самый близкий
    # по дате (свежайший, у которого date_from <= date_iso)
    candidates = [r for r in recipes if r.get("date_from", "") <= date_iso]
    if candidates:
        return max(candidates, key=lambda r: r.get("date_from", ""))

    # 3. Совсем не попали — берём самый старый из имеющихся
    return min(recipes, key=lambda r: r.get("date_from", "")) if recipes else None


def update_recipe_stats(domain: str, stats: dict):
    """Обновляет статистику использования рецепта."""
    data = load_recipe(domain)
    if not data:
        return
    data.setdefault("_stats", {})
    data["_stats"].update(stats)
    data["_stats"]["last_used"] = datetime.now().isoformat()
    save_recipe(domain, data)