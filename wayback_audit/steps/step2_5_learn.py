"""ШАГ 2.5: Итеративное обучение рецептов парсинга.

Логика:
  1. Применить все известные рецепты ко всем снимкам.
  2. Найти снимки, где ЦЕНА не найдена ни одним рецептом.
  3. Взять самый свежий непокрытый снимок → обучить новый рецепт.
  4. Определить date_from / date_to нового рецепта
     (проверяя на соседних снимках).
  5. Добавить рецепт в файл домена (старые НЕ удаляются).
  6. Повторить, максимум MAX_ITERATIONS раз.

Режимы очистки:
  - Итерация 1 — SMART BLOCK (умный блок через _find_content_block)
  - Итерация 2+ — FULL BODY (весь <body> без системных тегов)
"""
import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from bs4 import BeautifulSoup

from ..core.paths import ProjectPaths
from ..core.logger import ProjectLogger
from ..parsers import recipe_store, recipe_parser
from ..core.page_paths import find_html_files  # noqa: F401

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


MAX_ITERATIONS = 3
MIN_PRICE_CONFIDENCE = 0.5   # порог, чтобы считать снимок "покрытым"


SYSTEM_PROMPT = """Ты — эксперт по веб-скрейпингу.
Твоя задача — изучить HTML-страницу товара и вернуть JSON-рецепт
для извлечения данных через CSS-селекторы.

Возвращаешь ТОЛЬКО валидный JSON. Без markdown, без комментариев.
Не выдумывай селекторы — только те, что реально есть в HTML."""


USER_PROMPT = """Изучи HTML-страницу товара и составь рецепт парсинга.

Если в начале HTML есть блок «СОХРАНЁННЫЕ STRUCTURED DATA» —
в первую очередь ориентируйся на него (schema.org Product/Offer).

Верни JSON-объект:

{
  "recipe_version": "1.0",
  "page_type": "product | listing",
  "field_groups": {
    "product": {
      "name":        {"selectors": ["<CSS>", "<fallback1>"], "type": "text"},
      "sku":         {"selectors": [...], "type": "text", "regex": "Артикул[:\\s]*([\\w-]+)"},
      "brand":       {"selectors": [...], "type": "text", "attr": "data-brand"}
    },
    "price": {
      "current":     {"selectors": [...], "type": "text", "attr": "data-price", "regex": "([\\d\\s]+)"},
      "old":         {"selectors": [...], "type": "text", "regex": "([\\d\\s]+)"},
      "currency":    {"value": "RUB"}
    },
    "description": {
      "short":       {"selectors": [...], "type": "text"},
      "full":        {"selectors": [...], "type": "text"}
    }
  },
  "specifications": {
    "container": "<селектор>", "row": "tr",
    "name": "td:first-child", "value": "td:last-child"
  },
  "images": {"main": "<селектор>", "gallery": "<селектор>"},
  "notes": "<комментарий>"
}

================================================================
КРИТИЧНО ПРО ЦЕНУ
================================================================

Ищи цену в таком порядке (от надёжного к менее):

1. АТРИБУТ на контейнере карточки: data-price, data-cost,
   data-product-price, data-amount.
   В рецепте: {"selectors": [".item"], "attr": "data-price"}

2. АТРИБУТ на вложенном: data-value у .price, .price_value,
   content у <meta itemprop="price">.

3. ТЕКСТ внутри: .price_value, .price-current, [itemprop='price'].

4. МИКРОРАЗМЕТКА: [itemprop='price'],
   <meta property="product:price:amount">.

ВАЖНО:
- Если цена в атрибуте data-price — ВСЕГДА указывай "attr": "data-price"
  и селектор КОНТЕЙНЕРА.
- Если это каталог — селектор должен находить цену у КАЖДОГО товара.

================================================================
ПРАВИЛА
================================================================

1. Для каждого поля — МАССИВ селекторов от точного к общему.
2. Если значение — атрибут, укажи "attr".
3. Если нужно вырезать — "regex" с одной группой.
4. Если значение фиксированное — "value": "RUB".
5. Селекторы — валидный CSS.
6. Ориентируйся на СМЫСЛ блоков.

HTML:
<HTML>
{HTML}
</HTML>
"""


class RecipeLearner:
    def __init__(self, llm_cfg: dict, logger: ProjectLogger):
        from openai import OpenAI

        self.logger = logger
        self.model = llm_cfg.get("model", "deepseek-ai/DeepSeek-V4-Flash")
        self.temperature = llm_cfg.get("temperature", 0)
        self.max_tokens = llm_cfg.get("max_tokens", 4000)
        self.timeout = llm_cfg.get("timeout", 120)

        api_key = os.environ.get("OPENAI_API_KEY")
        base_url = os.environ.get("OPENAI_BASE_URL")
        if not api_key or not base_url:
            raise RuntimeError("Не заданы OPENAI_API_KEY или OPENAI_BASE_URL")

        self.client = OpenAI(api_key=api_key, base_url=base_url,
                             timeout=self.timeout)

    def learn(self, html_clean: str) -> dict | None:
        content = USER_PROMPT.replace("{HTML}", html_clean)
        try:
            self.logger.info(
                f"   → LLM: изучаю структуру ({len(content):,} символов)"
            )
            t0 = time.time()
            r = self.client.chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": content},
                ],
            )
            elapsed = time.time() - t0
            raw = r.choices[0].message.content
            usage = r.usage
            in_tok = usage.prompt_tokens if usage else 0
            out_tok = usage.completion_tokens if usage else 0
            self.logger.info(
                f"   ← LLM ответ за {elapsed:.1f} сек "
                f"(in {in_tok:,} / out {out_tok:,} токенов)"
            )
            recipe = self._safe_parse_json(raw)
            if recipe is None:
                self.logger.warn("   Пытаюсь исправить JSON через второй запрос")
                recipe = self._fix_json_with_llm(raw)
            return recipe
        except Exception as e:
            self.logger.error(f"   LLM ошибка: {e}")
            return None

    def _safe_parse_json(self, raw: str) -> dict | None:
        if not raw:
            return None
        text = raw.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        fixed = self._sanitize_backslashes(text)
        try:
            return json.loads(fixed)
        except json.JSONDecodeError:
            pass
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            candidate = self._sanitize_backslashes(text[start:end + 1])
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
        return None

    @staticmethod
    def _sanitize_backslashes(text: str) -> str:
        text = text.replace("\\\\", "\x00DOUBLE\x00")
        text = text.replace("\\", "\\\\")
        text = text.replace("\x00DOUBLE\x00", "\\\\")
        return text

    def _fix_json_with_llm(self, raw: str) -> dict | None:
        try:
            fix_prompt = (
                "Ниже — твой предыдущий ответ. Он невалидный JSON. "
                "Верни ТОЛЬКО исправленный валидный JSON без markdown.\n\n"
                f"Ответ:\n{raw}"
            )
            r = self.client.chat.completions.create(
                model=self.model, temperature=0,
                max_tokens=self.max_tokens,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": fix_prompt},
                ],
            )
            return self._safe_parse_json(r.choices[0].message.content)
        except Exception as e:
            self.logger.error(f"   Ретрай не удался: {e}")
            return None


# ============================================================
#  УМНАЯ ОЧИСТКА HTML
# ============================================================

def clean_html_for_llm(html: str, clean_cfg: dict = None,
                       full_body: bool = False) -> str:
    """
    Очищает HTML перед отправкой в LLM.

    full_body=False — умный блок (через _find_content_block).
    full_body=True  — весь <body> без системных тегов.
    """
    cfg = clean_cfg or {}
    soup = BeautifulSoup(html, "lxml")

    # Сохраняем JSON-LD / application/json с Product/Offer/Price
    preserved_scripts = []
    for script in soup.find_all("script"):
        stype = (script.get("type") or "").lower()
        if "ld+json" in stype or "application/json" in stype:
            content = script.string or ""
            low = content.lower()
            if any(k in low for k in ["product", "offer", "price", "sku", '"@type"']):
                preserved_scripts.append(str(script))

    # Удаляем системные теги
    for tag in soup.find_all(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()
    # meta оставляем только если itemprop/property
    for tag in soup.find_all("meta"):
        keep = tag.get("itemprop") or tag.get("property")
        if not keep:
            tag.decompose()
    for tag in soup.find_all("link"):
        tag.decompose()

    # Пользовательские селекторы из clean.yaml
    for sel in cfg.get("remove_selectors", []):
        try:
            for el in soup.select(sel):
                el.decompose()
        except Exception:
            continue

    body = soup.find("body") or soup

    if full_body:
        # ВЕСЬ <body> без системных тегов
        text = str(body)
    else:
        # Умный блок (как было)
        content_block = _find_content_block(body)
        if content_block is not None and len(str(content_block)) < 5000:
            content_block = None
        text = str(content_block) if content_block is not None else str(body)

    if cfg.get("collapse_whitespace", True):
        text = re.sub(r"\s+", " ", text)

    max_chars = cfg.get("max_chars", 80000)
    if len(text) > max_chars:
        text = _smart_truncate(text, max_chars)

    if preserved_scripts:
        json_ld_block = (
            "\n<!-- СОХРАНЁННЫЕ STRUCTURED DATA (schema.org / JSON-LD): -->\n"
            + "\n".join(preserved_scripts)
            + "\n<!-- КОНЕЦ STRUCTURED DATA -->\n\n"
        )
        text = json_ld_block + text

    return text


def _find_content_block(root):
    from collections import Counter

    strict_selectors = [
        '[itemtype*="schema.org/Product"]',
        '[itemtype*="Product"]',
        '[itemprop="product"]',
        "[data-price]", "[data-product-id]", "[data-product_id]",
        "article",
        ".product-card", ".product_card", ".product_item", ".product-item",
        ".catalog-item", ".catalog_item", ".catalog-block-view__item",
        ".js-product", ".product_block", ".product-block",
    ]

    cards = []
    for sel in strict_selectors:
        try:
            cards.extend(root.select(sel))
        except Exception:
            continue

    if not cards:
        return None

    counter = Counter()
    for card in cards:
        current = card
        for _ in range(4):
            if current is None:
                break
            counter[id(current)] += 1
            current = current.parent

    if not counter:
        return None

    best_id = counter.most_common(1)[0][0]
    for card in cards:
        current = card
        for _ in range(4):
            if current is None:
                break
            if id(current) == best_id:
                return current
            current = current.parent
    return None


def _smart_truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    head_end = text.find(">", half)
    if head_end < 0 or head_end > half + 2000:
        head_end = half
    else:
        head_end += 1
    tail_start = len(text) - half
    tail_border = text.rfind("<", 0, tail_start)
    if tail_border < 0 or tail_border < tail_start - 2000:
        tail_border = tail_start
    return text[:head_end] + "\n<!-- ...усечено... -->\n" + text[tail_border:]


# ============================================================
#  ОПРЕДЕЛЕНИЕ ДАТЫ СНИМКА
# ============================================================

def _ts_to_date(ts: str) -> str:
    """20260618134649 → 2026-06-18"""
    if not ts or len(ts) < 8:
        return ""
    return f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"


def _date_to_ts_range(date_iso: str) -> tuple:
    d = date_iso.replace("-", "")
    return f"{d}000000", f"{d}235959"


# ============================================================
#  ПРОВЕРКА, ПОКРЫТ ЛИ СНИМОК РЕЦЕПТАМИ
# ============================================================

def _find_recipe_for_snapshot(recipes: list, date_iso: str):
    for r in recipes:
        d_from = r.get("date_from", "")
        d_to = r.get("date_to", "")
        if d_from and d_to and d_from <= date_iso <= d_to:
            return r
    return None


def _price_found(parsed: dict) -> bool:
    if not parsed:
        return False
    data = parsed.get("data") or {}
    price = data.get("price") or {}
    current = price.get("current")
    if current is None:
        return False
    if isinstance(current, str):
        num_str = "".join(c for c in current if c.isdigit() or c in ".,")
        num_str = num_str.replace(",", ".")
        if not num_str:
            return False
        try:
            float(num_str)
        except ValueError:
            return False
    return True


# ============================================================
#  ОБУЧЕНИЕ
# ============================================================

def run(paths: ProjectPaths, llm_cfg: dict, clean_cfg: dict,
        logger: ProjectLogger, force: bool = False) -> dict:
    logger.info("=" * 60)
    logger.info("ШАГ 2.5: Итеративное обучение рецептов")
    logger.info("=" * 60)

    if not paths.input_parsed.exists():
        logger.error("Нет input/parsed.json. Запусти шаг 0.")
        return {"ok": False, "error": "no_input"}

    input_data = json.loads(paths.input_parsed.read_text(encoding="utf-8"))
    rows = input_data.get("rows", [])

    by_domain = {}
    for row in rows:
        d = row["domain"]
        by_domain.setdefault(d, []).append(row)

    logger.info(f"Доменов: {len(by_domain)}")

    learner = RecipeLearner(llm_cfg, logger)
    total_learned = 0
    total_skipped = 0
    total_failed = 0

    for domain, domain_rows in by_domain.items():
        logger.info(f"\n🌐 {domain}")
        result = _learn_for_domain(
            paths, domain, domain_rows, learner, clean_cfg, logger, force
        )
        total_learned += result.get("learned", 0)
        total_skipped += result.get("skipped", 0)
        total_failed += result.get("failed", 0)

    logger.info("")
    logger.success(
        f"Итого: обучено {total_learned}, "
        f"пропущено {total_skipped}, ошибок {total_failed}"
    )
    return {"ok": True, "learned": total_learned,
            "skipped": total_skipped, "failed": total_failed}


def _learn_for_domain(paths, domain, domain_rows, learner, clean_cfg, logger, force):
    """Итеративное обучение для одного домена."""
    sample_url = domain_rows[0]["url"]
    html_files = _find_html_for_url(paths.pages_dir, sample_url)

    if not html_files:
        logger.warn(f"   Нет HTML для {sample_url}")
        return {"learned": 0, "skipped": 0, "failed": 1}

    html_files_sorted = sorted(html_files, key=lambda p: p.stem, reverse=True)

    existing_data = recipe_store.load_recipe(domain) if not force else None
    recipes = existing_data.get("recipes", []) if existing_data else []

    if not force and recipes:
        logger.info(f"   Загружено рецептов из кэша: {len(recipes)}")
    elif force:
        logger.info(f"   --force: начинаю с нуля (было {len(recipes)})")
        recipes = []

    if not recipes:
        recipe_store.save_recipe(domain, {
            "domain": domain,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "recipes": [],
        })

    learned = 0
    skipped = 0
    failed = 0
    already_tried = set()   # (stem, mode) — что уже пробовали

    for iteration in range(1, MAX_ITERATIONS + 1):
        # Определяем режим очистки для этой итерации
        use_full_body = (iteration >= 2)
        mode = "full" if use_full_body else "smart"
        mode_label = "FULL BODY" if use_full_body else "SMART BLOCK"

        logger.info(f"\n   ── Итерация {iteration}/{MAX_ITERATIONS} ({mode_label}) ──")

        # 1. Определяем, какие снимки НЕ покрыты
        uncovered = _find_uncovered_snapshots(
            html_files_sorted, recipes, domain, logger
        )

        # Убираем те, что уже пробовали в этом режиме
        uncovered = [f for f in uncovered if (f.stem, mode) not in already_tried]

        if not uncovered:
            if already_tried:
                logger.warn(
                    f"   ⚠️  Не получилось на {len(already_tried)} снимках — "
                    f"пропускаем домен"
                )
            else:
                logger.success(f"   ✅ Все снимки покрыты рецептами")
            break

        logger.info(f"   Непокрытых снимков: {len(uncovered)}")

        # 2. Берём самый свежий
        target_file = uncovered[0]
        already_tried.add((target_file.stem, mode))
        date_iso = _ts_to_date(target_file.stem)
        logger.info(f"   Обучаю на: {target_file.stem} ({date_iso})")

        # 3. Обучаем
        try:
            html = target_file.read_text(encoding="utf-8")
        except Exception as e:
            logger.error(f"   Ошибка чтения: {e}")
            failed += 1
            continue

        clean = clean_html_for_llm(html, clean_cfg, full_body=use_full_body)
        logger.info(
            f"   Очистка ({mode_label}): {len(html):,} → {len(clean):,} символов "
            f"({100 - len(clean) / len(html) * 100:.0f}% экономии)"
        )

        recipe = learner.learn(clean)
        if not recipe:
            logger.error(f"   LLM не вернул рецепт")
            failed += 1
            continue

        fg = recipe.get("field_groups")
        if not isinstance(fg, dict) or not fg:
            logger.warn(f"   LLM вернул рецепт без field_groups — пропуск")
            failed += 1
            continue

        # 4. Определяем покрытие
        parsed = recipe_parser.parse_html(html, recipe)
        coverage = parsed["_meta"]["confidence"]
        logger.info(f"   Покрытие на обучающем снимке: {coverage:.0%}")

        if not _price_found(parsed):
            logger.warn(f"   ⚠️  Цена не найдена даже на обучающем снимке")

        # 5. Определяем date_from и date_to
        date_from, date_to = _determine_date_range(
            target_file, html_files_sorted, recipe, date_iso
        )
        logger.info(f"   Диапазон: {date_from} .. {date_to}")

        # 6. Добавляем рецепт
        recipe_id = f"{len(recipes) + 1:02d}"
        new_recipe = {
            "id": recipe_id,
            "date_from": date_from,
            "date_to": date_to,
            "trained_on": target_file.stem,
            "coverage": coverage,
            **{k: v for k, v in recipe.items()
               if k not in ("id", "date_from", "date_to", "trained_on", "coverage")}
        }

        recipes.append(new_recipe)
        recipe_store.add_recipe(domain, new_recipe)
        learned += 1
        logger.success(f"   ✓ Рецепт {recipe_id} добавлен")

    return {"learned": learned, "skipped": skipped, "failed": failed}


def _find_uncovered_snapshots(html_files, recipes, domain, logger):
    """
    Возвращает список снимков (от свежих к старым), где:
      - дата не попадает ни в один диапазон рецепта, ИЛИ
      - рецепт есть, но цена не найдена.
    """
    uncovered = []
    for html_file in html_files:
        date_iso = _ts_to_date(html_file.stem)
        if not date_iso:
            continue

        recipe = _find_recipe_for_snapshot(recipes, date_iso)
        if not recipe:
            uncovered.append(html_file)
            continue

        try:
            html = html_file.read_text(encoding="utf-8")
            parsed = recipe_parser.parse_html(html, recipe)
            if not _price_found(parsed):
                uncovered.append(html_file)
        except Exception:
            uncovered.append(html_file)
    return uncovered


def _determine_date_range(target_file, all_files, recipe, target_date):
    """
    Определяет date_from и date_to для нового рецепта.
    """
    dates_above = []
    dates_below = []

    target_ts = target_file.stem
    for f in all_files:
        if f.stem > target_ts:
            dates_above.append(f)
        elif f.stem < target_ts:
            dates_below.append(f)

    date_to = target_date
    for f in sorted(dates_above, key=lambda p: p.stem):
        try:
            html = f.read_text(encoding="utf-8")
            parsed = recipe_parser.parse_html(html, recipe)
            if _price_found(parsed):
                date_to = _ts_to_date(f.stem)
            else:
                break
        except Exception:
            break

    date_from = target_date
    for f in sorted(dates_below, key=lambda p: p.stem, reverse=True):
        try:
            html = f.read_text(encoding="utf-8")
            parsed = recipe_parser.parse_html(html, recipe)
            if _price_found(parsed):
                date_from = _ts_to_date(f.stem)
            else:
                break
        except Exception:
            break

    return date_from, date_to


def _find_html_for_url(pages_dir: Path, url: str) -> list:
    return find_html_files(pages_dir, url)


# ============================================================
#  РЕЖИМ "РЕЕСТР": обучение ОДНОГО домена
# ============================================================

def run_for_domain(paths: ProjectPaths, llm_cfg: dict, clean_cfg: dict,
                   logger: ProjectLogger, domain: str, domain_rows: list,
                   force: bool = False) -> dict:
    """Обучает рецепты для одного домена. Возвращает статистику."""
    try:
        learner = RecipeLearner(llm_cfg, logger)
    except RuntimeError as e:
        logger.error(f"   LLM недоступен: {e}")
        return {"ok": False, "error": str(e), "learned": 0}

    result = _learn_for_domain(
        paths, domain, domain_rows, learner, clean_cfg, logger, force
    )
    return {"ok": True, **result}