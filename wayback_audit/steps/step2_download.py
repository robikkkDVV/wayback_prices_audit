"""ШАГ 2: Скачивание HTML-снимков для всех URL из stat.json.

Режим:
  - ПОСЛЕДОВАТЕЛЬНОЕ скачивание (1 поток) — не блокируется Wayback.
  - Пауза 8 сек между снимками.
  - Живой лог: печатает URL и результат перед каждым снимком.
  - Дедупликация по digest.
  - При 429/503 — пауза 60+ сек и retry.
"""
import hashlib
import json
import random
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

from ..core.paths import ProjectPaths
from ..core.logger import ProjectLogger
from ..core.progress import JsonProgress
from ..core.page_paths import html_path


# ============================================================
#  КОНФИГ
# ============================================================

REQUEST_TIMEOUT = 45
MAX_RETRIES = 3
REQUEST_DELAY = 8.0
DOWNLOAD_DUPLICATES = False

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Edg/120.0.0.0",
]

BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://web.archive.org/",
}


def make_headers() -> dict:
    headers = dict(BASE_HEADERS)
    headers["User-Agent"] = random.choice(USER_AGENTS)
    return headers


def url_hash(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def build_archive_url(ts: str, url: str) -> str:
    return f"https://web.archive.org/web/{ts}id_/{url}"


# ============================================================
#  ДЕДУПЛИКАЦИЯ
# ============================================================

def select_snapshots(snapshots, download_duplicates=False):
    if not snapshots:
        return [], []
    groups = [[snapshots[0]]]
    for snap in snapshots[1:]:
        prev_d = groups[-1][-1].get("digest") or ""
        curr_d = snap.get("digest") or ""
        if prev_d and curr_d and prev_d == curr_d:
            groups[-1].append(snap)
        else:
            groups.append([snap])

    selected, dups_info = [], []
    for group in groups:
        selected.append(group[0])
        if len(group) > 1:
            if download_duplicates:
                selected.extend(group[1:])
            else:
                dups_info.append({
                    "digest": group[0].get("digest", ""),
                    "kept_timestamp": group[0]["timestamp"],
                    "kept_date": group[0].get("date", ""),
                    "skipped": [
                        {"timestamp": s["timestamp"], "date": s.get("date", "")}
                        for s in group[1:]
                    ],
                })
    return selected, dups_info


# ============================================================
#  СКАЧИВАНИЕ ОДНОГО СНИМКА
# ============================================================

def download_snapshot(ts: str, url: str, local_path: Path, logger=None):
    archive_url = build_archive_url(ts, url)
    last_info = "?"

    for attempt in range(1, MAX_RETRIES + 1):
        headers = make_headers()
        ua_short = headers["User-Agent"][:40] + "..."

        try:
            r = requests.get(
                archive_url,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
            )

            if r.status_code == 200:
                local_path.parent.mkdir(parents=True, exist_ok=True)
                with open(local_path, "wb") as f:
                    f.write(r.content)
                return {
                    "timestamp": ts,
                    "original_url": url,
                    "archive_url": archive_url,
                    "local_path": str(local_path),
                    "status_code": 200,
                    "size_kb": round(len(r.content) / 1024, 1),
                    "user_agent": headers["User-Agent"],
                    "downloaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }, "downloaded", "200"

            if r.status_code == 404:
                return None, "not_in_archive", "404"

            if r.status_code in (503, 429):
                last_info = f"{r.status_code}"
                wait = 60 + random.randint(0, 15)
                if logger:
                    logger.warn(
                        f"       {r.status_code} (UA: {ua_short}), "
                        f"ждём {wait} сек (попытка {attempt}/{MAX_RETRIES})"
                    )
                time.sleep(wait)
                continue

            last_info = f"HTTP {r.status_code}"
            if attempt < MAX_RETRIES:
                time.sleep(15 * attempt)
                continue
            return None, "retry_error", last_info

        except requests.exceptions.Timeout:
            last_info = "timeout"
            if attempt < MAX_RETRIES:
                time.sleep(10 * attempt)
            else:
                return None, "network_error", last_info

        except (requests.exceptions.ConnectionError,
                ConnectionResetError,
                ConnectionAbortedError) as e:
            last_info = f"conn: {type(e).__name__}"
            if attempt < MAX_RETRIES:
                time.sleep(10 * attempt)
            else:
                return None, "network_error", last_info

        except requests.exceptions.RequestException as e:
            last_info = f"req: {type(e).__name__}"
            if attempt < MAX_RETRIES:
                time.sleep(10 * attempt)
            else:
                return None, "retry_error", last_info

    return None, "retry_error", last_info


# ============================================================
#  ОСНОВНОЙ ЗАПУСК
# ============================================================

def run(paths: ProjectPaths, logger: ProjectLogger) -> dict:
    logger.info("=" * 60)
    logger.info("ШАГ 2: Скачивание HTML-снимков (последовательно)")
    logger.info("=" * 60)

    paths.ensure_dirs()

    stat_file = paths.stat_dir / "stat.json"
    if not stat_file.exists():
        logger.error("Нет stat/stat.json. Запусти шаг 1.")
        return {"ok": False, "error": "no_stat"}

    stat_data = json.loads(stat_file.read_text(encoding="utf-8"))
    urls_data = stat_data.get("urls", {})

    if not urls_data:
        logger.error("В stat.json нет URL")
        return {"ok": False, "error": "empty_stat"}

    logger.info(f"URL в stat.json: {len(urls_data)}")

    progress = JsonProgress(paths.pages_progress, {
        "downloaded": [],
        "not_in_archive": [],
        "network_error": [],
        "retry_error": [],
        "metadata": [],
        "skipped_duplicates": [],
    })

    done = set(progress.get("downloaded", []))
    done_404 = set(progress.get("not_in_archive", []))
    done_net = set(progress.get("network_error", []))
    done_retry = set(progress.get("retry_error", []))
    all_done = done | done_404 | done_net | done_retry

    metadata = progress.get("metadata", [])

    tasks = []
    dups_list = []

    for url, info in urls_data.items():
        if info.get("status") != "ok":
            continue
        days = info.get("days", [])
        if not days:
            continue

        selected, dups_info = select_snapshots(days, DOWNLOAD_DUPLICATES)

        for d in dups_info:
            dups_list.append(d)

        for snap in selected:
            ts, orig = snap["timestamp"], snap["original"]
            key = f"{ts}|{orig}"

            if key in all_done:
                continue

            local_path = html_path(paths.pages_dir, orig, ts)
            tasks.append((ts, orig, local_path, key))

    total = len(tasks)
    logger.info(f"Снимков к скачиванию: {total}")
    logger.info(f"Дубликатов (по digest): {len(dups_list)}")
    logger.info(f"Пауза между снимками: {REQUEST_DELAY} сек")
    logger.info("")

    if total == 0:
        logger.success("Нечего скачивать — все снимки уже на месте")
        progress.set("metadata", metadata)
        progress.set("skipped_duplicates", dups_list)
        progress.save()
        return {
            "ok": True,
            "downloaded": 0,
            "not_in_archive": 0,
            "network_errors": 0,
            "retry_errors": 0,
            "skipped_duplicates": len(dups_list),
        }

    counters = {"ok": 0, "404": 0, "net": 0, "retry": 0}
    start_time = time.time()
    completed = 0

    try:
        for task in tasks:
            completed += 1
            ts, url, local_path, key = task

            logger.info(f"   [{completed}/{total}] Скачиваю {ts}...")

            t0 = time.time()
            result, category, info = download_snapshot(ts, url, local_path, logger)
            elapsed = time.time() - t0

            logger.info(f"       → {category}  ({info}, {elapsed:.0f} сек)")

            if category == "downloaded":
                metadata.append(result)
                progress.append("downloaded", key)
                counters["ok"] += 1
                logger.success(
                    f"       ✅ {result['size_kb']} КБ  "
                    f"({time.time() - start_time:.0f} сек от старта)"
                )
            elif category == "not_in_archive":
                progress.append("not_in_archive", key)
                counters["404"] += 1
            elif category == "network_error":
                progress.append("network_error", key)
                counters["net"] += 1
            elif category == "retry_error":
                progress.append("retry_error", key)
                counters["retry"] += 1

            progress.set("metadata", metadata)
            progress.save()

            if completed < total:
                time.sleep(REQUEST_DELAY)

    except KeyboardInterrupt:
        logger.warn("Прервано пользователем")

    progress.set("metadata", metadata)
    progress.set("skipped_duplicates", dups_list)
    progress.save()

    elapsed = time.time() - start_time

    logger.info("")
    logger.success(
        f"Скачано: {counters['ok']}, "
        f"404: {counters['404']}, "
        f"сеть: {counters['net']}, "
        f"retry: {counters['retry']}"
    )
    logger.info(f"Время: {elapsed / 60:.1f} мин")

    return {
        "ok": True,
        "downloaded": counters["ok"],
        "not_in_archive": counters["404"],
        "network_errors": counters["net"],
        "retry_errors": counters["retry"],
        "skipped_duplicates": len(dups_list),
    }