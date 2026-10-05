"""ШАГ 0: Разбор входного файла со списком URL и текущих цен."""
import shutil
from pathlib import Path

from ..core.paths import ProjectPaths
from ..core.logger import ProjectLogger
from ..core import input_parser


def _cleanup_previous_run(paths: ProjectPaths, logger: ProjectLogger):
    """Удаляет артефакты прошлых прогонов (кроме input/ и логов)."""
    for d in (paths.stat_dir, paths.pages_dir,
              paths.products_dir, paths.exports_dir, paths.cache_dir):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
    for f in (paths.learn_progress,):
        try:
            if f.exists():
                f.unlink()
        except Exception:
            pass
    logger.info("   Очищены артефакты предыдущего прогона")


def run(paths: ProjectPaths, source_file: Path, logger: ProjectLogger,
        cleanup: bool = True) -> dict:
    logger.info("=" * 60)
    logger.info("ШАГ 0: Разбор входного файла")
    logger.info("=" * 60)

    logger.info(f"Файл: {source_file}")
    if not source_file.exists():
        logger.error(f"Файл не найден: {source_file}")
        return {"ok": False, "error": "file_not_found"}

    # При resume — НЕ чистим. Если parsed.json уже есть, просто используем его.
    if not cleanup:
        if paths.input_parsed.exists():
            try:
                existing = input_parser.load_parsed(paths)
                if existing and existing.get("rows"):
                    logger.info("   Продолжение: используем готовый parsed.json")
                    return {
                        "ok": True,
                        "total": existing.get("total", 0),
                        "domains": existing.get("unique_domains", 0),
                        "resumed": True,
                    }
            except Exception:
                pass
        logger.info("   Продолжение: parsed.json нет, парсим файл заново")
    else:
        _cleanup_previous_run(paths, logger)

    try:
        rows = input_parser.parse_input_file(source_file)
    except Exception as e:
        logger.error(f"Ошибка парсинга: {e}")
        return {"ok": False, "error": str(e)}

    if not rows:
        logger.error("Файл пуст или нет валидных строк")
        return {"ok": False, "error": "no_rows"}

    paths.ensure_dirs()
    data = input_parser.save_parsed(paths, rows, source_file.name)

    logger.success(f"Загружено строк: {data['total']}")
    logger.info(f"Уникальных доменов: {data['unique_domains']}")

    domains = {}
    for r in rows:
        domains[r["domain"]] = domains.get(r["domain"], 0) + 1
    for d, cnt in sorted(domains.items(), key=lambda x: -x[1])[:20]:
        logger.info(f"  {d}: {cnt} товаров")
    if len(domains) > 20:
        logger.info(f"  ... и ещё {len(domains) - 20} доменов")

    return {
        "ok": True,
        "total": data["total"],
        "domains": data["unique_domains"],
    }