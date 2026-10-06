"""Локальный парсер HTML по рецепту.

Рецепт выбирается по дате снимка. Если для даты нет точного
попадания — берётся ближайший по дате.
"""
import json
import re
from bs4 import BeautifulSoup

from . import recipe_store


# ============================================================
#  ПАРСИНГ ОДНОГО ТОВАРА (карточка)
# ============================================================

def parse_html(html: str, recipe: dict) -> dict:
    """
    Применяет рецепт к HTML карточки товара.
    Возвращает dict с товаром и метаданными.
    """
    soup = BeautifulSoup(html, "lxml")
    result = {}
    hits = 0
    misses = 0

    field_groups = recipe.get("field_groups") or {}

    for group_name, fields in field_groups.items():
        result[group_name] = {}

        # ЗАЩИТА: если LLM вернул null вместо словаря
        if not isinstance(fields, dict):
            continue

        for field_name, rules in fields.items():
            value = _extract_field(soup, rules)
            result[group_name][field_name] = value
            if value is not None:
                hits += 1
            else:
                misses += 1

    if "specifications" in recipe:
        specs = _extract_specs(soup, recipe["specifications"])
        result["specifications"] = specs
        if specs:
            hits += 1
        else:
            misses += 1

    if "images" in recipe:
        images = _extract_images(soup, recipe["images"])
        result["images"] = images
        if images.get("main") or images.get("gallery"):
            hits += 1
        else:
            misses += 1

    total = hits + misses
    confidence = hits / total if total > 0 else 0

    return {
        "data": result,
        "_meta": {
            "hits": hits,
            "misses": misses,
            "confidence": round(confidence, 3),
        }
    }


# ============================================================
#  ПАРСИНГ ПО ДАТЕ (для step3_parse)
# ============================================================

def parse_html_for_date(html: str, domain: str, date_iso: str) -> tuple:
    """
    Выбирает рецепт по дате и парсит HTML.
    Возвращает (parsed, recipe_used|None).
    """
    recipe = recipe_store.get_recipe_for_date(domain, date_iso)
    if not recipe:
        return None, None
    return parse_html(html, recipe), recipe


# ============================================================
#  ПАРСИНГ СПИСКА (категория)
# ============================================================

def parse_list_page(html: str, recipe: dict,
                    max_items: int = 100) -> list[dict]:
    """Парсит страницу-список (категория)."""
    soup = BeautifulSoup(html, "lxml")

    product_rules = recipe.get("field_groups", {}).get("product", {})
    price_rules = recipe.get("field_groups", {}).get("price", {})

    name_selectors = _get_selectors(product_rules.get("name", {}))
    price_selectors = _get_selectors(price_rules.get("current", {}))
    old_price_selectors = _get_selectors(price_rules.get("old", {}))
    brand_selectors = _get_selectors(product_rules.get("brand", {}))
    sku_selectors = _get_selectors(product_rules.get("sku", {}))

    if not name_selectors:
        return []

    primary_name_sel = name_selectors[0]
    try:
        name_elements = soup.select(primary_name_sel)
    except Exception:
        return []

    if not name_elements:
        return []

    name_elements = name_elements[:max_items]

    results = []
    for name_el in name_elements:
        item = _extract_list_item(
            name_el,
            name_selectors=name_selectors,
            price_selectors=price_selectors,
            old_price_selectors=old_price_selectors,
            brand_selectors=brand_selectors,
            sku_selectors=sku_selectors,
        )
        if item and item.get("name"):
            results.append(item)

    return results


def parse_list_page_for_date(html: str, domain: str, date_iso: str) -> tuple:
    """Выбирает рецепт по дате и парсит список."""
    recipe = recipe_store.get_recipe_for_date(domain, date_iso)
    if not recipe:
        return [], None
    return parse_list_page(html, recipe), recipe


def _get_selectors(rules) -> list:
    """Достаёт список селекторов из правил (строка, dict или список)."""
    if not rules:
        return []
    if isinstance(rules, str):
        return [rules]
    if isinstance(rules, list):
        return [s for s in rules if isinstance(s, str)]
    if isinstance(rules, dict):
        sels = rules.get("selectors")
        if isinstance(sels, str):
            return [sels]
        if isinstance(sels, list):
            return [s for s in sels if isinstance(s, str)]
    return []


def _find_card_container(name_el, price_selectors):
    """Поднимается вверх по DOM, ища элемент-карточку."""
    STRICT_CARD_CLASSES = (
        "catalog_detail", "catalog_item", "catalog-block-view__item",
        "catalog-item", "product-card", "product_item", "product-item",
        "js-notice-block", "inner_wrap", "product_block", "product-block",
        "item_block",
    )

    # 1) По строгим классам + проверка, что внутри есть price-селектор
    current = name_el
    for _ in range(15):
        if current is None:
            break
        classes = current.get("class") or []
        class_str = " ".join(classes).lower()
        if any(c in class_str for c in STRICT_CARD_CLASSES):
            for sel in price_selectors:
                try:
                    if current.select_one(sel):
                        return current
                except Exception:
                    continue
        current = current.parent

    # 2) По data-атрибутам
    current = name_el
    for _ in range(15):
        if current is None:
            break
        if (current.has_attr("data-price") or
                current.has_attr("data-cost") or
                current.has_attr("data-product-price") or
                current.has_attr("data-brand")):
            return current
        current = current.parent

    # 3) Фолбэк: родитель, внутри которого есть price
    current = name_el
    for _ in range(15):
        if current is None:
            break
        for sel in price_selectors:
            try:
                if current.select_one(sel):
                    return current
            except Exception:
                continue
        current = current.parent

    p = name_el
    for _ in range(4):
        if p.parent:
            p = p.parent
    return p


def _extract_list_item(name_el, name_selectors, price_selectors,
                       old_price_selectors, brand_selectors, sku_selectors):
    """Извлекает данные одного товара из карточки."""
    container = _find_card_container(name_el, price_selectors)

    name = name_el.get_text(strip=True) if name_el else None
    if not name and container:
        for sel in name_selectors[1:]:
            try:
                el = container.select_one(sel)
                if el:
                    name = el.get_text(strip=True)
                    break
            except Exception:
                continue
    if not name:
        return None

    url = None
    link = name_el if name_el.name == "a" else name_el.find_parent("a")
    if link:
        url = link.get("href")
    elif container:
        first_link = container.find("a", href=True)
        if first_link:
            url = first_link.get("href")

    price_current = None
    if container:
        for attr in ("data-price", "data-cost", "data-product-price",
                     "data-price-current", "data-amount"):
            if container.has_attr(attr):
                price_current = _clean_price(container.get(attr))
                if price_current is not None:
                    break
        if price_current is None:
            for sel in price_selectors:
                try:
                    el = container.select_one(sel)
                    if el:
                        for attr in ("data-price", "data-value", "content"):
                            if el.has_attr(attr):
                                price_current = _clean_price(el.get(attr))
                                if price_current is not None:
                                    break
                        if price_current is not None:
                            break
                        raw = el.get_text(strip=True)
                        price_current = _clean_price(raw)
                        if price_current is not None:
                            break
                except Exception:
                    continue

    price_old = None
    if container:
        for sel in old_price_selectors:
            try:
                el = container.select_one(sel)
                if el:
                    price_old = _clean_price(el.get_text(strip=True))
                    if price_old is not None:
                        break
            except Exception:
                continue

    brand = None
    if container:
        for attr in ("data-brand", "data-manufacturer"):
            if container.has_attr(attr):
                brand = container.get(attr)
                break
        if not brand:
            for sel in brand_selectors:
                try:
                    el = container.select_one(sel)
                    if el:
                        for attr in ("data-brand", "content"):
                            if el.has_attr(attr):
                                brand = el.get(attr)
                                break
                        if brand:
                            break
                        brand = el.get_text(strip=True)
                        if brand:
                            break
                except Exception:
                    continue

    sku = None
    if container:
        for sel in sku_selectors:
            try:
                el = container.select_one(sel)
                if el:
                    sku = el.get_text(strip=True)
                    if sku:
                        break
            except Exception:
                continue

    return {
        "name": name,
        "price_current": price_current,
        "price_old": price_old,
        "url": url,
        "brand": brand,
        "sku": sku,
    }


def _clean_price(raw):
    if not raw:
        return None
    cleaned = str(raw).replace(" ", "").replace("\xa0", "").replace(",", ".")
    cleaned = cleaned.replace("₽", "").replace("руб.", "").replace("руб", "")
    cleaned = cleaned.replace("$", "").replace("€", "").strip()
    num_str = "".join(c for c in cleaned if c.isdigit() or c in ".-")
    if not num_str:
        return None
    try:
        return float(num_str)
    except ValueError:
        return None

def _sanitize_price_text(text: str) -> str:
    """
    Извлекает ПЕРВОЕ число из текста.
    Защита от склейки: '6 076 ₽ за 1 шт' → '6 076', а не '60761'.
    """
    if not text:
        return text

    s = str(text)

    # 1. Сначала ищем "нормальное" число с разделителями тысяч
    #    Пример: "6 076", "60 761", "1 234 567"
    m = re.search(r"\d{1,3}(?:[\s\u00a0]\d{3})+", s)
    if m:
        return m.group(0)

    # 2. Простое число
    m = re.search(r"\d+", s)
    if m:
        return m.group(0)

    return text

def _extract_field(soup, rules):
    selectors = _get_selectors(rules)
    if not selectors and isinstance(rules, dict):
        fixed = rules.get("value")
        if fixed:
            return fixed

    attr = None
    regex = None
    fixed_value = None
    if isinstance(rules, dict):
        attr = rules.get("attr")
        if isinstance(attr, list):
            attr = attr[0] if attr else None
        regex = rules.get("regex")
        fixed_value = rules.get("value")

    for sel in selectors:
        try:
            el = soup.select_one(sel)
        except Exception:
            continue
        if el is None:
            continue
        val = el.get(attr) if attr else el.get_text(strip=True)
        if not val:
            continue
        if regex:
            try:
                m = re.search(regex, str(val))
                if m:
                    val = m.group(1) if m.groups() else m.group(0)
                else:
                    continue
            except re.error:
                continue

        # Санитайзер: берём ПЕРВОЕ число из текста
        if isinstance(val, str):
            val = _sanitize_price_text(val)

        return val
    return fixed_value


def _extract_specs(soup, rules) -> list:
    if not isinstance(rules, dict):
        return []
    container_sel = rules.get("container")
    if not container_sel or not isinstance(container_sel, str):
        return []
    row_sel = rules.get("row", "tr") or "tr"
    name_sel = rules.get("name", "td:first-child") or "td:first-child"
    value_sel = rules.get("value", "td:last-child") or "td:last-child"
    try:
        container = soup.select_one(container_sel)
    except Exception:
        return []
    if container is None:
        return []
    result = []
    try:
        rows = container.select(row_sel)
    except Exception:
        return []
    for row in rows:
        try:
            name_el = row.select_one(name_sel)
            value_el = row.select_one(value_sel)
        except Exception:
            continue
        if name_el and value_el:
            name = name_el.get_text(strip=True)
            value = value_el.get_text(strip=True)
            if name and value and name != value:
                result.append({"name": name, "value": value})
    return result


def _extract_images(soup, rules) -> dict:
    if not isinstance(rules, dict):
        return {"main": None, "gallery": []}
    main = _extract_image_value(soup, rules.get("main"))
    gallery = _extract_image_list(soup, rules.get("gallery"))
    return {"main": main, "gallery": gallery}


def _extract_image_value(soup, rules):
    if not rules:
        return None
    if isinstance(rules, str):
        return _find_image_by_selector(soup, [rules])
    if isinstance(rules, dict):
        selectors = _get_selectors(rules)
        attrs = rules.get("attr")
        if isinstance(attrs, str):
            attrs = [attrs]
        if not attrs:
            attrs = ["src", "data-src", "data-original", "href"]
        return _find_image_by_selector(soup, selectors, attrs)
    if isinstance(rules, list):
        return _find_image_by_selector(soup, [s for s in rules if isinstance(s, str)])
    return None


def _extract_image_list(soup, rules):
    if not rules:
        return []
    selectors = []
    if isinstance(rules, str):
        selectors = [rules]
    elif isinstance(rules, dict):
        selectors = _get_selectors(rules)
    elif isinstance(rules, list):
        selectors = [s for s in rules if isinstance(s, str)]
    if not selectors:
        return []
    attrs = ["src", "data-src", "data-original", "data-lazyload", "data-lazy", "href"]
    result = []
    for sel in selectors:
        try:
            els = soup.select(sel)
        except Exception:
            continue
        for el in els:
            for attr in attrs:
                val = el.get(attr)
                if val and isinstance(val, str) and val.strip():
                    if val not in result:
                        result.append(val.strip())
                    break
    return result


def _find_image_by_selector(soup, selectors, attrs=None):
    if not selectors:
        return None
    if not attrs:
        attrs = ["src", "data-src", "data-original", "href"]
    for sel in selectors:
        if not isinstance(sel, str):
            continue
        try:
            el = soup.select_one(sel)
        except Exception:
            continue
        if el is None:
            continue
        for attr in attrs:
            val = el.get(attr)
            if val and isinstance(val, str) and val.strip():
                return val.strip()
    return None


def validate_result(parsed: dict, min_confidence: float = 0.5) -> bool:
    meta = parsed.get("_meta", {})
    if meta.get("confidence", 0) < min_confidence:
        return False
    data = parsed.get("data", {})
    product = data.get("product") or {}
    price = data.get("price") or {}
    if not product.get("name") and not price.get("current"):
        return False
    return True