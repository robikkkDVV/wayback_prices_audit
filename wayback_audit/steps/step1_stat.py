"""ШАГ 1: Сбор статистики снимков из CDX API — гибридная оптимизация.

Логика:
  1. Читает input/parsed.json — список URL.
  2. Группирует URL по домену.
  3. Для каждого домена:
     - если URL <= PER_URL_THRESHOLD (20) — запрашивает КАЖДЫЙ URL отдельно
       (per-URL, точно, быстро для малых доменов);
     - если URL > 20 — запрашивает домен целиком (per-domain),
       с разбивкой по годам если ответ > 10 МБ, потом фильтрует в Python.
  4. Сохраняет в stat/stat.json (после каждого домена).

Поддержка resume: при resume=True пропускает домены, уже попавшие в stat.json.
"""
import json
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qsl, urlencode, quote


import requests

from ..core.paths import ProjectPaths
from ..core.logger import ProjectLogger


# ============================================================
#  КОНФИГ
# ============================================================

CDX_API_URL = "https://web.archive.org/cdx/search/cdx"
CDX_MAX_RETRIES = 3
CDX_RETRY_PAUSE = 5

CDX_FROM_YEAR = 2013
CDX_TO_YEAR = 2026
CDX_YEAR_PAUSE = 1.5

BIG_RESPONSE_BYTES = 10 * 1024 * 1024

PER_URL_THRESHOLD = 20
# Домены, которые всегда разбивать по годам (большие / медленные)
FORCE_YEARLY_DOMAINS = {
    "www.dns-shop.ru",
    "www.citilink.ru",
    "www.tinko.ru",
    "www.etm.ru",
    "market.yandex.ru",
    "www.wildberries.ru",
    "www.avito.ru",
}

TRACKING_PARAMS = {
    'ysclid', 'utm_source', 'utm_medium', 'utm_campaign',
    'utm_term', 'utm_content', 'utm_referrer',
    'gclid', 'fbclid', 'yclid', 'from', 'ref',
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
}


# ============================================================
#  УТИЛИТЫ
# ============================================================

def clean_url(raw: str) -> str:
    """Убирает схему и UTM-метки, приводит к виду для CDX."""
    raw = raw.strip()
    if not raw:
        return ''
    if not raw.startswith(('http://', 'https://')):
        raw = 'http://' + raw
    parsed = urlparse(raw)
    if parsed.query:
        kept = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                if k.lower() not in TRACKING_PARAMS]
        # quote_via=quote → пробел кодируется как %20 (а не +),
        # иначе CDX не находит снимки с оригинальным URL
        query = urlencode(kept, quote_via=quote)
    else:
        query = ''
    netloc = parsed.netloc.lower()
    path = parsed.path or '/'
    result = netloc + path
    if query:
        result += '?' + query
    return result


def safe_filename(s: str) -> str:
    return re.sub(r'[^a-zA-Z0-9.-]', '_', s)[:150]


def _parse_cdx_response(data):
    if not data or len(data) < 2:
        return []
    headers = data[0]
    idx_ts = headers.index("timestamp")
    idx_orig = headers.index("original")
    idx_mime = headers.index("mimetype") if "mimetype" in headers else None
    idx_st = headers.index("statuscode") if "statuscode" in headers else None
    idx_dg = headers.index("digest") if "digest" in headers else None

    result = []
    for row in data[1:]:
        item = {'timestamp': row[idx_ts], 'original': row[idx_orig]}
        if idx_mime is not None: item['mimetype'] = row[idx_mime]
        if idx_st is not None: item['statuscode'] = row[idx_st]
        if idx_dg is not None: item['digest'] = row[idx_dg]
        result.append(item)
    return result


def _request_cdx_raw(params, logger):
    """Запрос к CDX. Возвращает (parsed_data | None, size_bytes)."""
    for attempt in range(1, CDX_MAX_RETRIES + 1):
        try:
            r = requests.get(CDX_API_URL, params=params,
                             headers=HEADERS, timeout=300)

            if r.status_code == 503:
                wait = CDX_RETRY_PAUSE * attempt
                logger.warn(f"CDX 503, ждём {wait} сек")
                time.sleep(wait)
                continue

            if r.status_code == 429:
                logger.warn("CDX 429, ждём 30 сек")
                time.sleep(30)
                continue

            if r.status_code == 504:
                wait = CDX_RETRY_PAUSE * attempt * 2
                logger.warn(f"CDX 504 Gateway Timeout, ждём {wait} сек")
                time.sleep(wait)
                continue

            r.raise_for_status()

            content = r.content
            size_bytes = len(content)

            try:
                data = json.loads(content)
            except json.JSONDecodeError as e:
                logger.error(f"CDX вернул не-JSON: {e}")
                return None, size_bytes

            return _parse_cdx_response(data), size_bytes

        except requests.exceptions.Timeout:
            logger.warn(f"CDX таймаут (попытка {attempt})")
            if attempt < CDX_MAX_RETRIES:
                time.sleep(CDX_RETRY_PAUSE * attempt)
        except requests.exceptions.RequestException as e:
            logger.error(f"CDX ошибка: {e} (попытка {attempt})")
            if attempt < CDX_MAX_RETRIES:
                time.sleep(CDX_RETRY_PAUSE * attempt)

    return None, 0


# ============================================================
#  ЗАПРОСЫ
# ============================================================

def _fetch_single_url(url_cleaned: str, from_year: int, to_year: int, logger):
    params = {
        'url': url_cleaned,
        'output': 'json',
        'fl': 'timestamp,original,mimetype,statuscode,digest',
        'from': f'{from_year}0101',
        'to': f'{to_year}1231',
        # prefix надёжнее exact: Wayback хранит URL то с %20, то с +,
        # exact не находит при малейшем расхождении. Фильтруем сами.
        'matchType': 'prefix',
    }
    data, size = _request_cdx_raw(params, logger)
    if data is None:
        return None
    return data


def _fetch_domain_all(domain: str, from_year: int, to_year: int, logger):
    logger.info(f"      CDX per-domain: {domain}/* ({from_year}..{to_year})")

    params = {
        'url': f'{domain}/*',
        'output': 'json',
        'fl': 'timestamp,original,mimetype,statuscode,digest',
        'from': f'{from_year}0101',
        'to': f'{to_year}1231',
        'collapse': 'digest',
    }

    data, size = _request_cdx_raw(params, logger)

    if data is None:
        return [], size, 'error'

    size_mb = size / (1024 * 1024)
    logger.info(f"      Получено: {len(data):,} записей, {size_mb:.1f} МБ")

    # Разбиваем по годам, если:
    #   1. Ответ большой (> BIG_RESPONSE_BYTES)
    #   2. ИЛИ домен в списке FORCE_YEARLY_DOMAINS (даже если ответ мал)
    if size > BIG_RESPONSE_BYTES or domain in FORCE_YEARLY_DOMAINS:
        reason = "> 10 МБ" if size > BIG_RESPONSE_BYTES else "в списке FORCE_YEARLY"
        logger.warn(f"      ⚠️  {reason} — разбиваю по годам")
        return data, size, 'too_big'

    return data, size, 'all'


def _fetch_domain_by_years(domain: str, from_year: int, to_year: int, logger):
    total_snapshots = []
    years = list(range(from_year, to_year + 1))
    total_years = len(years)

    for i, year in enumerate(years, 1):
        logger.info(f"      [{i}/{total_years}] {year}...")
        params = {
            'url': f'{domain}/*',
            'output': 'json',
            'fl': 'timestamp,original,mimetype,statuscode,digest',
            'from': f'{year}0101',
            'to': f'{year}1231',
            'collapse': 'digest',
        }
        data, size = _request_cdx_raw(params, logger)

        if data is None:
            logger.warn(f"      ❌ {year}: не удалось получить данные")
        elif not data:
            logger.info(f"      ⏹  {year}: пусто")
        else:
            size_mb = size / (1024 * 1024)
            logger.info(f"      ✅ {year}: {len(data):,} записей, {size_mb:.1f} МБ")
            total_snapshots.extend(data)

        if i < total_years:
            time.sleep(CDX_YEAR_PAUSE)

    return total_snapshots


# ============================================================
#  ФИЛЬТРАЦИЯ И АГРЕГАЦИЯ
# ============================================================

def _filter_html_200(snapshots):
    result = []
    for s in snapshots:
        mime = (s.get('mimetype') or '').lower()
        status = str(s.get('statuscode', ''))
        if not mime and not status:
            result.append(s)
            continue
        if ('text/html' in mime or 'xhtml' in mime) and status == '200':
            result.append(s)
    return result


def _filter_by_urls(snapshots, target_urls_set: set):
    result = []
    seen_urls = set()
    for s in snapshots:
        orig = s.get('original', '')
        cleaned_orig = clean_url(orig)
        if cleaned_orig in target_urls_set:
            result.append(s)
            seen_urls.add(cleaned_orig)
    return result, seen_urls


def _aggregate_one_url(snapshots):
    by_day = {}
    for snap in snapshots:
        ts = snap['timestamp']
        if len(ts) < 8:
            continue
        date_str = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
        if date_str not in by_day or ts < by_day[date_str]['timestamp']:
            by_day[date_str] = snap
    return [
        {
            'date': d,
            'timestamp': s['timestamp'],
            'original': s['original'],
            'digest': s.get('digest', ''),
        }
        for d, s in sorted(by_day.items())
    ]


def _empty_stat(row, domain, status):
    return {
        "url": row["url"],
        "cleaned": clean_url(row["url"]),
        "domain": domain,
        "row_num": row.get("row_num"),
        "current_price": row.get("current_price"),
        "status": status,
        "days": [],
        "total_snapshots_raw": 0,
        "total_snapshots_filtered": 0,
    }


def _save_stat_file(stat_file, stat_by_url, rows, by_domain,
                    from_date, total_snapshots_raw, total_snapshots_filtered):
    """Пишет stat.json атомарно."""
    result = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "from_date": from_date,
        "total_urls": len(rows),
        "total_domains": len(by_domain),
        "total_snapshots_raw": total_snapshots_raw,
        "total_snapshots_filtered": total_snapshots_filtered,
        "urls": stat_by_url,
    }
    tmp = stat_file.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(stat_file)


# ============================================================
#  ОСНОВНОЙ ЗАПУСК
# ============================================================

def run(paths: ProjectPaths, from_date: str, logger: ProjectLogger,
        resume: bool = False) -> dict:
    """ШАГ 1: сбор статистики для всех URL из input/parsed.json."""
    logger.info("=" * 60)
    logger.info("ШАГ 1: Сбор статистики (гибрид per-URL / per-domain)")
    logger.info("=" * 60)
    logger.info(f"Ревизуемый период: с {from_date}")
    logger.info(f"Порог переключения: {PER_URL_THRESHOLD} URL на домен")

    if not paths.input_parsed.exists():
        logger.error("Нет input/parsed.json. Запусти сначала шаг 0.")
        return {"ok": False, "error": "no_input"}

    input_data = json.loads(paths.input_parsed.read_text(encoding="utf-8"))
    rows = input_data.get("rows", [])

    if not rows:
        logger.error("Входной файл пуст")
        return {"ok": False, "error": "empty_input"}

    try:
        from_year = int(from_date[:4])
    except (ValueError, TypeError):
        from_year = CDX_FROM_YEAR
    to_year = CDX_TO_YEAR

    by_domain = {}
    for row in rows:
        d = row["domain"]
        by_domain.setdefault(d, []).append(row)

    logger.info(f"URL к обработке: {len(rows)}")
    logger.info(f"Уникальных доменов: {len(by_domain)}")
    logger.info(f"Период: {from_year} .. {to_year}")

    # === Загружаем существующий stat.json (для resume) ===
    stat_file = paths.stat_dir / "stat.json"
    existing_stat = {}
    if resume and stat_file.exists():
        try:
            existing_stat = json.loads(stat_file.read_text(encoding="utf-8"))
            done_domains = set(
                v.get("domain")
                for v in existing_stat.get("urls", {}).values()
                if v.get("domain")
            )
            logger.info(f"   Продолжение: домены уже в stat.json: {len(done_domains)}")
        except Exception:
            existing_stat = {}

    stat_by_url = dict(existing_stat.get("urls", {}))
    total_snapshots_raw = existing_stat.get("total_snapshots_raw", 0)
    total_snapshots_filtered = existing_stat.get("total_snapshots_filtered", 0)

    start_time = time.time()
    domain_index = 0
    total_domains = len(by_domain)

    for domain, domain_rows in by_domain.items():
        domain_index += 1

        # === Пропускаем уже обработанные домены (resume) ===
        if resume:
            urls_in_stat = sum(
                1 for r in domain_rows
                if r["url"] in stat_by_url
                and stat_by_url[r["url"]].get("status") in
                ("ok", "no_snapshots", "cdx_error")
            )
            total_in_domain = len(domain_rows)
            coverage = urls_in_stat / total_in_domain if total_in_domain else 0

            # Пропускаем, если покрытие ≥ 95%
            if coverage >= 0.95:
                logger.info(
                    f"🌐 DOMAIN [{domain_index}/{total_domains}] {domain} "
                    f"— пропущен (покрытие {urls_in_stat}/{total_in_domain})"
                )
                # Добавляем недостающие URL как cdx_error, чтобы не терять
                for r in domain_rows:
                    if r["url"] not in stat_by_url:
                        stat_by_url[r["url"]] = _empty_stat(
                            r, domain, "cdx_error"
                        )
                continue

        url_count = len(domain_rows)
        mode = "per-URL" if url_count <= PER_URL_THRESHOLD else "per-domain"
        logger.info(f"🌐 DOMAIN [{domain_index}/{total_domains}] {domain} "
                    f"({url_count} URL, режим: {mode})")

        all_snapshots = []

        # === РЕЖИМ PER-URL ===
        if url_count <= PER_URL_THRESHOLD:
            url_index = 0
            for r in domain_rows:
                url_index += 1
                cleaned = clean_url(r["url"])
                logger.info(f"      [{url_index}/{url_count}] {cleaned[:80]}")

                snaps = _fetch_single_url(cleaned, from_year, to_year, logger)

                if snaps is None:
                    logger.warn(f"      ❌ CDX не ответил")
                    stat_by_url[r["url"]] = _empty_stat(r, domain, "cdx_error")
                    continue

                filtered = _filter_html_200(snaps)

                if not filtered:
                    logger.info(f"      → нет снимков")
                    stat_by_url[r["url"]] = _empty_stat(r, domain, "no_snapshots")
                    continue

                by_day = _aggregate_one_url(filtered)
                total_snapshots_raw += len(snaps)
                total_snapshots_filtered += len(filtered)

                logger.info(f"      → {len(by_day)} дней со снимками")

                stat_by_url[r["url"]] = {
                    "url": r["url"],
                    "cleaned": cleaned,
                    "domain": domain,
                    "row_num": r.get("row_num"),
                    "current_price": r.get("current_price"),
                    "status": "ok",
                    "days": by_day,
                    "total_snapshots_raw": len(snaps),
                    "total_snapshots_filtered": len(filtered),
                }

                time.sleep(1)

            # Сохраняем после каждого per-URL домена
            _save_stat_file(stat_file, stat_by_url, rows, by_domain,
                            from_date, total_snapshots_raw, total_snapshots_filtered)
            continue

        # === РЕЖИМ PER-DOMAIN ===
        target_urls_cleaned = set()
        url_to_row = {}
        for r in domain_rows:
            cleaned = clean_url(r["url"])
            target_urls_cleaned.add(cleaned)
            url_to_row[cleaned] = r

        # Если домен в FORCE_YEARLY — СРАЗУ по годам (без первого запроса)
        if domain in FORCE_YEARLY_DOMAINS:
            logger.info(f"      Домен в FORCE_YEARLY — сразу разбиваю по годам")
            all_snapshots = _fetch_domain_by_years(
                domain, from_year, to_year, logger
            )
        else:
            domain_snaps, size, mode_result = _fetch_domain_all(
                domain, from_year, to_year, logger
            )

            if mode_result == 'too_big':
                logger.info(f"      Разбиваю на {to_year - from_year + 1} запросов по годам")
                all_snapshots = _fetch_domain_by_years(
                    domain, from_year, to_year, logger
                )
            elif mode_result == 'error':
                logger.warn(f"      Не удалось получить данные")
                for r in domain_rows:
                    stat_by_url[r["url"]] = _empty_stat(r, domain, "cdx_error")
                _save_stat_file(stat_file, stat_by_url, rows, by_domain,
                                from_date, total_snapshots_raw, total_snapshots_filtered)
                continue
            else:
                all_snapshots = domain_snaps

        logger.info(f"      Всего снимков домена: {len(all_snapshots):,}")

        filtered = _filter_html_200(all_snapshots)
        logger.info(f"      HTML+200: {len(filtered):,}")

        matched, seen_cleaned = _filter_by_urls(filtered, target_urls_cleaned)
        logger.info(f"      Из списка: {len(matched):,} снимков "
                    f"(уникальных URL: {len(seen_cleaned)})")

        total_snapshots_raw += len(all_snapshots)
        total_snapshots_filtered += len(matched)

        by_original = {}
        for s in matched:
            orig = s.get('original', '')
            cleaned = clean_url(orig)
            by_original.setdefault(cleaned, []).append(s)

        not_found = 0
        for cleaned in target_urls_cleaned:
            r = url_to_row[cleaned]
            urls_snaps = by_original.get(cleaned, [])

            if not urls_snaps:
                not_found += 1
                stat_by_url[r["url"]] = _empty_stat(r, domain, "no_snapshots")
                continue

            by_day = _aggregate_one_url(urls_snaps)

            stat_by_url[r["url"]] = {
                "url": r["url"],
                "cleaned": cleaned,
                "domain": domain,
                "row_num": r.get("row_num"),
                "current_price": r.get("current_price"),
                "status": "ok",
                "days": by_day,
                "total_snapshots_raw": len(urls_snaps),
                "total_snapshots_filtered": len(by_day),
            }

        logger.info(f"      URL с данными: {len(target_urls_cleaned) - not_found}, "
                    f"без снимков: {not_found}")

        # Сохраняем после каждого per-domain домена
        _save_stat_file(stat_file, stat_by_url, rows, by_domain,
                        from_date, total_snapshots_raw, total_snapshots_filtered)

    # Итоговая запись
    _save_stat_file(stat_file, stat_by_url, rows, by_domain,
                    from_date, total_snapshots_raw, total_snapshots_filtered)

    elapsed = time.time() - start_time
    ok_count = sum(1 for v in stat_by_url.values() if v["status"] == "ok")
    no_snaps = sum(1 for v in stat_by_url.values() if v["status"] == "no_snapshots")
    errors = sum(1 for v in stat_by_url.values() if v["status"] == "cdx_error")

    logger.info("")
    logger.success(f"Stat-файл сохранён: {stat_file}")
    logger.info(f"  URL с данными:       {ok_count}")
    logger.info(f"  URL без снимков:     {no_snaps}")
    logger.info(f"  URL с ошибкой CDX:   {errors}")
    logger.info(f"  Всего снимков (raw): {total_snapshots_raw:,}")
    logger.info(f"  Снимков HTML+200:    {total_snapshots_filtered:,}")
    logger.info(f"  Время: {elapsed/60:.1f} мин")

    return {
        "ok": True,
        "stat_file": str(stat_file),
        "urls": len(rows),
        "urls_with_data": ok_count,
        "urls_no_snapshots": no_snaps,
        "urls_error": errors,
        "total_snapshots_filtered": total_snapshots_filtered,
    }