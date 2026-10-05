"""Парсер входного файла: Excel или CSV со списком URL, названий и текущих цен.

Особенности:
  - Автоопределение кодировки CSV: UTF-8 (с BOM и без) → CP1251 (Excel-локаль).
  - Автоопределение разделителя: ',', ';', '\\t', '|'.
  - Чтение XLSX через openpyxl.
  - Распознавание колонок по синонимам (url/ссылка, name/название, price/цена).
"""
import csv
import json
from pathlib import Path
from urllib.parse import urlparse

try:
    from openpyxl import load_workbook
except ImportError:
    load_workbook = None


# Возможные названия колонок (регистронезависимо)
URL_ALIASES = ["url", "ссылка", "link", "адрес", "page", "страница", "href"]
NAME_ALIASES = ["name", "название", "наименование", "товар",
                "product", "title", "продукт"]
PRICE_ALIASES = ["price", "цена", "стоимость", "текущая цена", "current_price",
                 "current price", "cost", "сумма"]


def _normalize(s: str) -> str:
    return (s or "").strip().lower()


def _find_col(headers: list, aliases: list) -> int:
    """Возвращает индекс колонки по списку возможных названий."""
    normalized = [_normalize(h) for h in headers]
    for i, h in enumerate(normalized):
        if h in aliases:
            return i
    for i, h in enumerate(normalized):
        for a in aliases:
            if a in h:
                return i
    return -1


def _extract_domain(url: str) -> str:
    """Извлекает домен из URL."""
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    return urlparse(url).netloc.lower()


def _read_text_auto(path: Path) -> tuple:
    """
    Читает текстовый файл, автоматически определяя кодировку.

    Возвращает: (raw_content: str, encoding_used: str)

    Логика:
      1. utf-8-sig — если файл начинается с BOM.
      2. utf-8     — строгая проверка. Если в файле есть байты, не
                     являющиеся валидным UTF-8, чтение упадёт, и мы
                     перейдём к следующей кодировке.
      3. cp1251    — Excel-локаль для русской Windows. Читает почти
                     любой файл, но для UTF-8-файлов испортит данные.

    ВАЖНО: именно из-за такой последовательности мы сначала пробуем
    строгий utf-8, чтобы cp1251 не «съел» UTF-8-файл.
    """
    last_error = None

    for enc in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            with open(path, "r", encoding=enc, newline="") as f:
                content = f.read()
            # Проверяем, что действительно нет кракозябр
            # (если это возможно — cp1251 не притворяется utf-8)
            return content, enc
        except UnicodeDecodeError as e:
            last_error = e
            continue

    raise ValueError(
        f"Не удалось определить кодировку CSV: {last_error}. "
        f"Сохраните файл в UTF-8 или CP1251."
    )


def _detect_delimiter(first_line: str) -> str:
    """Определяет разделитель CSV."""
    try:
        dialect = csv.Sniffer().sniff(first_line, delimiters=",;\t|")
        return dialect.delimiter
    except csv.Error:
        counts = {d: first_line.count(d) for d in [",", ";", "\t", "|"]}
        if max(counts.values()) == 0:
            return ","
        return max(counts, key=counts.get)


def parse_csv(path: Path) -> list[dict]:
    """Парсит CSV-файл с автоопределением кодировки и разделителя."""
    raw_content, encoding_used = _read_text_auto(path)
    print(f"[input_parser] CSV прочитан в кодировке: {encoding_used}")

    if not raw_content:
        return []

    lines = raw_content.splitlines()
    if not lines:
        return []

    delimiter = _detect_delimiter(lines[0])
    print(f"[input_parser] Разделитель: {delimiter!r}")

    reader = csv.reader(lines, delimiter=delimiter)
    all_rows = [r for r in reader if r]

    if not all_rows:
        return []

    headers = all_rows[0]
    url_idx = _find_col(headers, URL_ALIASES)
    name_idx = _find_col(headers, NAME_ALIASES)
    price_idx = _find_col(headers, PRICE_ALIASES)

    if url_idx < 0:
        raise ValueError(
            f"Не найдена колонка с URL. Заголовки: {headers}. "
            f"Ожидается одно из: {URL_ALIASES}"
        )

    rows = []
    for row_num, row in enumerate(all_rows[1:], start=2):
        if not row or url_idx >= len(row):
            continue
        url = row[url_idx].strip()
        if not url:
            continue

        name = None
        if name_idx >= 0 and name_idx < len(row):
            name = row[name_idx].strip() or None

        price = None
        if price_idx >= 0 and price_idx < len(row):
            raw_price = row[price_idx].strip()
            price = _parse_price(raw_price)

        rows.append({
            "row_num": row_num,
            "url": url,
            "name": name,
            "current_price": price,
            "domain": _extract_domain(url),
        })

    return rows


def parse_xlsx(path: Path) -> list[dict]:
    """Парсит XLSX-файл (первый лист)."""
    if load_workbook is None:
        raise RuntimeError("Для XLSX установите openpyxl: pip install openpyxl")

    wb = load_workbook(path, data_only=True)
    ws = wb.active

    all_rows = list(ws.iter_rows(values_only=True))
    if not all_rows:
        return []

    headers = [str(h) if h is not None else "" for h in all_rows[0]]
    url_idx = _find_col(headers, URL_ALIASES)
    name_idx = _find_col(headers, NAME_ALIASES)
    price_idx = _find_col(headers, PRICE_ALIASES)

    if url_idx < 0:
        raise ValueError(
            f"Не найдена колонка с URL. Заголовки: {headers}. "
            f"Ожидается одно из: {URL_ALIASES}"
        )

    rows = []
    for row_num, row in enumerate(all_rows[1:], start=2):
        if url_idx >= len(row) or row[url_idx] is None:
            continue
        url = str(row[url_idx]).strip()
        if not url:
            continue

        name = None
        if name_idx >= 0 and name_idx < len(row) and row[name_idx] is not None:
            name = str(row[name_idx]).strip() or None

        price = None
        if price_idx >= 0 and price_idx < len(row) and row[price_idx] is not None:
            price = _parse_price(str(row[price_idx]))

        rows.append({
            "row_num": row_num,
            "url": url,
            "name": name,
            "current_price": price,
            "domain": _extract_domain(url),
        })

    return rows


def _parse_price(raw: str):
    """Парсит цену: '35 190 руб.' → 35190.0"""
    if not raw:
        return None
    cleaned = str(raw).replace(" ", "").replace("\xa0", "").replace(",", ".")
    cleaned = cleaned.replace("₽", "").replace("руб.", "").replace("руб", "")
    cleaned = cleaned.replace("$", "").replace("€", "")
    cleaned = cleaned.replace("грн", "").replace("₸", "").replace("тг", "")
    cleaned = cleaned.strip()

    num_str = "".join(c for c in cleaned if c.isdigit() or c in ".-")
    if not num_str:
        return None
    try:
        return float(num_str)
    except ValueError:
        return None


def parse_input_file(path: Path) -> list[dict]:
    """Универсальный парсер: смотрит на расширение."""
    if not path.exists():
        raise FileNotFoundError(f"Файл не найден: {path}")

    ext = path.suffix.lower()
    if ext == ".csv":
        return parse_csv(path)
    elif ext in (".xlsx", ".xlsm"):
        return parse_xlsx(path)
    else:
        raise ValueError(f"Неподдерживаемый формат: {ext}. Нужен CSV или XLSX.")


def group_by_domain(rows: list[dict]) -> dict:
    """Группирует строки по домену."""
    result = {}
    for row in rows:
        domain = row["domain"]
        if domain not in result:
            result[domain] = []
        result[domain].append(row)
    return result


def save_parsed(paths, rows: list[dict], source_filename: str):
    """Сохраняет распарсенный список в JSON."""
    data = {
        "source_file": source_filename,
        "total": len(rows),
        "unique_domains": len(set(r["domain"] for r in rows)),
        "rows": rows,
    }
    paths.input_parsed.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return data


def load_parsed(paths) -> dict:
    """Загружает распарсенный список из JSON."""
    if not paths.input_parsed.exists():
        return {}
    return json.loads(paths.input_parsed.read_text(encoding="utf-8"))