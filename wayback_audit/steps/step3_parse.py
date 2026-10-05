"""ШАГ 3: Локальный парсинг по рецептам."""
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from ..core.paths import ProjectPaths
from ..core.logger import ProjectLogger
from ..core import matcher
from ..core.page_paths import find_html_files, url_to_slug_hash
from ..parsers import recipe_store, recipe_parser


def _ts_to_date(ts: str) -> str:
    if not ts or len(ts) < 8:
        return ""
    return f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"


def _url_hash(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def _find_html_for_url(pages_dir: Path, url: str) -> list:
    return find_html_files(pages_dir, url)


def _product_path(paths: ProjectPaths, url: str, timestamp: str) -> Path:
    slug, h = url_to_slug_hash(url)
    return paths.products_dir / f"{slug}_{h}" / f"{timestamp}.json"


def _save_unparsed(paths: ProjectPaths, url: str, hf: Path,
                   input_name, reason: str):
    fallback_dir = paths.root / "fallback"
    fallback_dir.mkdir(parents=True, exist_ok=True)

    item = {
        "url": url,
        "html_file": str(hf),
        "timestamp": hf.stem,
        "input_name": input_name,
        "reason": reason,
        "added_at": datetime.now().isoformat(),
    }
    fallback_file = fallback_dir / f"{hf.stem}_{_url_hash(url)}.json"
    fallback_file.write_text(
        json.dumps(item, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _enrich_result(result: dict, url: str, hf: Path, row_num,
                   input_price, input_name, recipe_used: dict) -> dict:
    result.setdefault("meta", {})
    meta = result["meta"]

    meta["source_url"] = url
    meta["source_file"] = hf.name
    meta["timestamp"] = hf.stem
    meta["date"] = _ts_to_date(hf.stem)
    meta["extracted_at"] = datetime.now().isoformat()
    meta["method"] = "local_recipe_matched" if input_name else "local_recipe"
    meta["row_num"] = row_num
    meta["input_price"] = input_price
    meta["input_name"] = input_name

    if recipe_used:
        meta["recipe_id"] = recipe_used.get("id")
        meta["recipe_date_from"] = recipe_used.get("date_from")
        meta["recipe_date_to"] = recipe_used.get("date_to")

    if "_meta" in result:
        inner = result.pop("_meta")
        meta.update(inner)

    return result


def _parse_with_matcher(html: str, recipe: dict, input_name: str,
                        logger: ProjectLogger):
    items = recipe_parser.parse_list_page(html, recipe)
    if not items:
        return None

    match = matcher.find_best_match(input_name, items)
    if not match:
        return None

    return {
        "product": {
            "name": match.get("name"),
            "sku": match.get("sku"),
            "brand": match.get("brand"),
        },
        "price": {
            "current": match.get("price_current"),
            "old": match.get("price_old"),
            "currency": "RUB",
        },
        "images": {},
        "specifications": [],
        "_meta": {
            "match_score": match.get("_match_score"),
            "match_status": match.get("_match_status"),
            "matched_name": match.get("name"),
            "matched_url": match.get("url"),
        }
    }


def _parse_single(html: str, recipe: dict, logger: ProjectLogger):
    parsed = recipe_parser.parse_html(html, recipe)
    if not parsed or not recipe_parser.validate_result(parsed):
        return None

    data = parsed["data"]
    data["_meta"] = {
        "confidence_score": parsed["_meta"]["confidence"],
    }
    return data


def run(paths: ProjectPaths, logger: ProjectLogger,
        force: bool = False) -> dict:
    logger.info("=" * 60)
    logger.info("ШАГ 3: Локальный парсинг по рецептам (по дате снимка)")
    logger.info("=" * 60)

    if not paths.input_parsed.exists():
        logger.error("Нет input/parsed.json")
        return {"ok": False, "error": "no_input"}

    input_data = json.loads(paths.input_parsed.read_text(encoding="utf-8"))
    rows = input_data.get("rows", [])

    by_domain = {}
    for row in rows:
        d = row["domain"]
        by_domain.setdefault(d, []).append(row)

    total_parsed = 0
    total_missed = 0
    total_no_recipe = 0
    total_no_name = 0
    total_low_confidence = 0

    for domain, domain_rows in by_domain.items():
        logger.info(f"\n🌐 {domain}")

        recipe_data = recipe_store.load_recipe(domain)
        if not recipe_data or not recipe_data.get("recipes"):
            logger.warn(f"   Нет рецептов — пропускаем {len(domain_rows)} товаров")
            total_no_recipe += len(domain_rows)
            continue

        recipes_count = len(recipe_data["recipes"])
        logger.info(f"   Загружено рецептов: {recipes_count}")

        for row in domain_rows:
            url = row["url"]
            input_name = row.get("name")
            input_price = row.get("current_price")
            row_num = row.get("row_num")

            html_files = _find_html_for_url(paths.pages_dir, url)

            if not html_files:
                logger.warn(f"   Нет HTML для {url[:60]}")
                total_missed += 1
                continue

            logger.info(
                f"   {url[:70]} ({len(html_files)} снимков)"
                + (f"  ищу: {input_name[:40]}" if input_name else "")
            )

            for hf in html_files:
                product_file = _product_path(paths, url, hf.stem)

                if product_file.exists() and not force:
                    total_parsed += 1
                    continue

                date_iso = _ts_to_date(hf.stem)

                recipe_used = recipe_store.get_recipe_for_date(domain, date_iso)
                if not recipe_used:
                    logger.warn(f"     {hf.stem}: нет рецепта для {date_iso}")
                    total_no_recipe += 1
                    _save_unparsed(paths, url, hf, input_name,
                                   reason=f"no_recipe_for_{date_iso}")
                    continue

                try:
                    html = hf.read_text(encoding="utf-8")
                except Exception as e:
                    logger.error(f"     Ошибка чтения {hf.name}: {e}")
                    continue

                if input_name:
                    result = _parse_with_matcher(html, recipe_used, input_name, logger)
                    if result is None:
                        total_no_name += 1
                        _save_unparsed(paths, url, hf, input_name,
                                       reason="no_match_in_list")
                        continue
                    if result.get("_meta", {}).get("match_status") == "low_confidence":
                        total_low_confidence += 1
                else:
                    result = _parse_single(html, recipe_used, logger)
                    if result is None:
                        total_missed += 1
                        _save_unparsed(paths, url, hf, None,
                                       reason="low_confidence_single")
                        continue

                result = _enrich_result(
                    result, url, hf, row_num, input_price, input_name,
                    recipe_used
                )

                product_file.parent.mkdir(parents=True, exist_ok=True)
                product_file.write_text(
                    json.dumps(result, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                total_parsed += 1

    logger.info("")
    logger.success(
        f"Разобрано: {total_parsed}, "
        f"нет рецепта: {total_no_recipe}, "
        f"не найдено в списке: {total_no_name}, "
        f"низкое совпадение: {total_low_confidence}, "
        f"пропущено: {total_missed}"
    )

    return {
        "ok": True,
        "parsed": total_parsed,
        "no_recipe": total_no_recipe,
        "no_name": total_no_name,
        "low_confidence": total_low_confidence,
        "missed": total_missed,
    }

# ============================================================
#  РЕЖИМ "РЕЕСТР": парсинг ОДНОГО домена
# ============================================================

def run_for_domain(paths: ProjectPaths, logger: ProjectLogger,
                   domain: str, domain_rows: list,
                   force: bool = False) -> dict:
    """Парсит снимки одного домена по готовым рецептам."""
    recipe_data = recipe_store.load_recipe(domain)
    if not recipe_data or not recipe_data.get("recipes"):
        logger.warn(f"   Нет рецептов для {domain} — пропуск")
        return {"ok": True, "parsed": 0, "no_recipe": len(domain_rows),
                "no_name": 0, "low_confidence": 0, "missed": 0}

    recipes_count = len(recipe_data["recipes"])
    logger.info(f"   Рецептов: {recipes_count}")

    total_parsed = 0
    total_missed = 0
    total_no_recipe = 0
    total_no_name = 0
    total_low_confidence = 0

    for row in domain_rows:
        url = row["url"]
        input_name = row.get("name")
        input_price = row.get("current_price")
        row_num = row.get("row_num")

        html_files = _find_html_for_url(paths.pages_dir, url)

        if not html_files:
            logger.warn(f"     Нет HTML для {url[:70]}")
            total_missed += 1
            continue

        for hf in html_files:
            product_file = _product_path(paths, url, hf.stem)

            if product_file.exists() and not force:
                total_parsed += 1
                continue

            date_iso = _ts_to_date(hf.stem)

            recipe_used = recipe_store.get_recipe_for_date(domain, date_iso)
            if not recipe_used:
                total_no_recipe += 1
                _save_unparsed(paths, url, hf, input_name,
                               reason=f"no_recipe_for_{date_iso}")
                continue

            try:
                html = hf.read_text(encoding="utf-8")
            except Exception as e:
                logger.error(f"     Ошибка чтения {hf.name}: {e}")
                continue

            if input_name:
                result = _parse_with_matcher(html, recipe_used, input_name, logger)
                if result is None:
                    total_no_name += 1
                    _save_unparsed(paths, url, hf, input_name,
                                   reason="no_match_in_list")
                    continue
                if result.get("_meta", {}).get("match_status") == "low_confidence":
                    total_low_confidence += 1
            else:
                result = _parse_single(html, recipe_used, logger)
                if result is None:
                    total_missed += 1
                    _save_unparsed(paths, url, hf, None,
                                   reason="low_confidence_single")
                    continue

            result = _enrich_result(
                result, url, hf, row_num, input_price, input_name, recipe_used
            )

            product_file.parent.mkdir(parents=True, exist_ok=True)
            product_file.write_text(
                json.dumps(result, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            total_parsed += 1

    return {
        "ok": True,
        "parsed": total_parsed,
        "no_recipe": total_no_recipe,
        "no_name": total_no_name,
        "low_confidence": total_low_confidence,
        "missed": total_missed,
    }