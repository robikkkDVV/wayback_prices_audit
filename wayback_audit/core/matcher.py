"""Сопоставление названий товаров: поиск наиболее похожего в списке."""
import re

try:
    from rapidfuzz import fuzz
    _HAS_RAPIDFUZZ = True
except ImportError:
    _HAS_RAPIDFUZZ = False

    # Простой fallback, если rapidfuzz нет
    from difflib import SequenceMatcher

    class fuzz:
        @staticmethod
        def token_set_ratio(a: str, b: str) -> float:
            return SequenceMatcher(None, a, b).ratio() * 100


def normalize(s: str) -> str:
    """Приводит название к сравнимому виду."""
    if not s:
        return ""
    s = s.lower()
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def find_best_match(input_name: str, candidates: list[dict],
                    min_score: float = 70.0) -> dict | None:
    """
    Ищет в списке товаров тот, чьё название ближе всего к input_name.

    Параметры:
        input_name — название из входного файла
        candidates — список словарей с ключом "name"
        min_score — минимальный порог совпадения (0-100)

    Возвращает:
        dict с ключами name, price, url, _match_score, _match_status
        или None, если ничего не найдено
    """
    if not input_name or not candidates:
        return None

    norm_input = normalize(input_name)
    if not norm_input:
        return None

    best = None
    best_score = 0.0

    for c in candidates:
        cand_name = c.get("name") or ""
        norm_cand = normalize(cand_name)
        if not norm_cand:
            continue

        # token_set_ratio устойчив к разному порядку слов
        score = fuzz.token_set_ratio(norm_input, norm_cand)

        if score > best_score:
            best_score = score
            best = c

    if best is None:
        return None

    result = dict(best)
    result["_match_score"] = round(best_score, 1)

    if best_score >= 90:
        result["_match_status"] = "exact"
    elif best_score >= min_score:
        result["_match_status"] = "fuzzy"
    else:
        result["_match_status"] = "low_confidence"

    return result


def find_all_matches(input_name: str, candidates: list[dict],
                     min_score: float = 50.0) -> list[dict]:
    """Возвращает ВСЕ совпадения выше порога, отсортированные по score."""
    if not input_name or not candidates:
        return []

    norm_input = normalize(input_name)
    results = []

    for c in candidates:
        cand_name = c.get("name") or ""
        norm_cand = normalize(cand_name)
        if not norm_cand:
            continue
        score = fuzz.token_set_ratio(norm_input, norm_cand)
        if score >= min_score:
            item = dict(c)
            item["_match_score"] = round(score, 1)
            results.append(item)

    results.sort(key=lambda x: -x["_match_score"])
    return results