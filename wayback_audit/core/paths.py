"""Все пути проекта в одном месте."""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
GLOBAL_RECIPES_DIR = DATA_DIR / "_recipes_cache"
CONFIG_DIR = BASE_DIR / "config"


class ProjectPaths:
    """Пути внутри одного проекта (data/<name>/)."""

    def __init__(self, project_name: str):
        self.name = project_name
        self.root = DATA_DIR / project_name
        self.input_dir = self.root / "input"
        self.recipes_dir = self.root / "recipes"
        self.stat_dir = self.root / "stat"
        self.cache_dir = self.root / "cache"
        self.pages_dir = self.root / "pages"
        self.products_dir = self.root / "products"
        self.exports_dir = self.root / "exports"
        self.logs_dir = self.root / "logs"

        self.input_source = self.input_dir / "source.xlsx"
        self.input_parsed = self.input_dir / "parsed.json"
        self.pages_progress = self.pages_dir / "progress.json"
        self.products_progress = self.products_dir / "progress.json"
        self.learn_progress = self.root / "learn_progress.json"
        self.registry_progress = self.root / "registry_progress.json"
        self.download_log = self.logs_dir / "download.log"
        self.analyze_log = self.logs_dir / "analyze.log"
        self.main_log = self.logs_dir / "main.log"

    def ensure_dirs(self):
        for d in (self.input_dir, self.recipes_dir, self.stat_dir,
                  self.cache_dir, self.pages_dir, self.products_dir,
                  self.exports_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)


def list_projects():
    if not DATA_DIR.exists():
        return []
    return sorted(
        p.name for p in DATA_DIR.iterdir()
        if p.is_dir() and not p.name.startswith("_") and not p.name.startswith(".")
    )


def global_recipe_path(domain: str) -> Path:
    """Путь к глобальному кэшу рецептов для домена."""
    GLOBAL_RECIPES_DIR.mkdir(parents=True, exist_ok=True)
    safe = domain.replace(":", "_").replace("/", "_")
    return GLOBAL_RECIPES_DIR / f"{safe}.json"