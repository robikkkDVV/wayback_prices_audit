"""Единая логика путей страниц: slug + hash. Используется в step2, step2_5, step3."""
import hashlib
import re
from pathlib import Path
from urllib.parse import urlparse


def normalize_url_for_path(url: str) -> str:
    """Приводит URL к виду, пригодному для urlparse (со схемой)."""
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    return url


def url_to_slug_hash(url: str) -> tuple:
    """Hash НЕ зависит от схемы (http/https) — иначе URL со схемой и без
    дают разные папки."""
    if not url:
        return ("index", "0" * 12)

    u = url
    if u.startswith("https://"):
        u = u[8:]
    elif u.startswith("http://"):
        u = u[7:]

    clean = u.split("?", 1)[0].split("#", 1)[0]
    segments = [s for s in clean.strip("/").split("/") if s]
    segments = segments[1:] if segments else []      # пропускаем netloc
    slug = segments[-1] if segments else "index"
    slug = re.sub(r"[^a-zA-Z0-9_-]", "_", slug)[:40]

    h = hashlib.sha1(u.encode("utf-8")).hexdigest()[:12]
    return slug, h


def page_dir(pages_dir: Path, url: str) -> Path:
    """Папка для HTML-снимков URL."""
    slug, h = url_to_slug_hash(url)
    return pages_dir / f"{slug}_{h}"


def find_html_files(pages_dir: Path, url: str) -> list:
    """Все HTML-файлы для URL, отсортированные по имени."""
    d = page_dir(pages_dir, url)
    if not d.exists():
        return []
    return sorted(d.glob("*.html"))


def html_path(pages_dir: Path, url: str, ts: str) -> Path:
    """Путь к HTML-файлу конкретного снимка."""
    return page_dir(pages_dir, url) / f"{ts}.html"