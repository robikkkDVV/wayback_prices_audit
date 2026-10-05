"""Веб-интерфейс для аналитика.

Пайплайн запускается через subprocess (как CLI). Прогресс определяется
по файлам проекта и по web_run.log.
"""
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from flask import (Flask, render_template, request, redirect,
                   url_for, jsonify, send_file, flash)

from ..core.paths import DATA_DIR, ProjectPaths, list_projects


# ============================================================
#  КОНФИГ
# ============================================================

UPLOAD_EXTENSIONS = {".csv", ".xlsx", ".xlsm"}
BASE_DIR = Path(__file__).resolve().parent.parent.parent
RUN_PY = BASE_DIR / "run.py"

PYTHON_EXE = os.environ.get(
    "WAYBACK_PYTHON",
    r"C:\Users\Honor\AppData\Local\Programs\Python\Python313\python.exe",
)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-change-me")
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32 MB

PROCESSES = {}


# ============================================================
#  ЗАПУСК ПАЙПЛАЙНА ЧЕРЕЗ SUBPROCESS
# ============================================================

def _get_active_projects(exclude: str = None) -> list:
    """Возвращает имена проектов, чьи run.py сейчас работают."""
    try:
        import psutil
    except ImportError:
        return []

    active = set()
    for p in psutil.process_iter(["cmdline"]):
        try:
            cmdline = p.info.get("cmdline") or []
            joined = " ".join(cmdline)
            if "run.py" not in joined:
                continue
            # ищем "-p <имя>" или "--project <имя>"
            for i, arg in enumerate(cmdline):
                if arg in ("-p", "--project") and i + 1 < len(cmdline):
                    name = cmdline[i + 1]
                    if exclude and name == exclude:
                        continue
                    active.add(name)
        except Exception:
            continue
    return sorted(active)

def _start_pipeline(project_name: str, input_file: Path,
                    excel_name: str, from_date: str,
                    resume: bool = False) -> bool:
    """Запускает `run.py` как внешний процесс."""
    if not RUN_PY.exists():
        return False

    # === БЛОКИРОВКА: не запускать, если уже работает другой проект ===
    active = _get_active_projects(exclude=project_name)
    if active:
        print(f"[app] Уже запущены проекты: {active}. "
              f"Нельзя запустить {project_name} параллельно.")
        return False
    # === КОНЕЦ БЛОКИРОВКИ ===

    paths = ProjectPaths(project_name)
    paths.ensure_dirs()
    (paths.root / "_run_started_at").write_text(
        datetime.now().isoformat(), encoding="utf-8"
    )

    cmd = [
        PYTHON_EXE,
        str(RUN_PY),
        "-p", project_name,
        "-i", str(input_file),
        "-s", "all",
        "--from-date", from_date,
        "--excel", excel_name,
    ]
    if resume:
        cmd.append("--resume")

    try:
        log_path = paths.logs_dir / "web_run.log"
        mode = "ab" if resume else "wb"
        log_file = open(log_path, mode)
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP

        proc = subprocess.Popen(
            cmd,
            cwd=str(BASE_DIR),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=flags,
            env=env,
        )
        PROCESSES[project_name] = {
            "process": proc,
            "log_file": log_file,
            "started_at": datetime.now().isoformat(),
        }
        return True
    except Exception as e:
        print(f"[app] Не удалось запустить subprocess: {e}")
        return False


# ============================================================
#  ВСПОМОГАТЕЛЬНЫЕ
# ============================================================

def _run_start_time(paths: ProjectPaths):
    f = paths.root / "_run_started_at"
    if f.exists():
        try:
            return datetime.fromisoformat(f.read_text(encoding="utf-8").strip())
        except Exception:
            pass
    for candidate in (paths.root / "state.json", paths.learn_progress):
        if candidate.exists():
            return datetime.fromtimestamp(candidate.stat().st_mtime)
    return None


def _is_fresh(paths: ProjectPaths, f: Path) -> bool:
    start = _run_start_time(paths)
    if start is None:
        return True
    return datetime.fromtimestamp(f.stat().st_mtime) >= start


def _is_pipeline_alive_by_cmdline(project_name: str) -> bool:
    try:
        import psutil
    except ImportError:
        return False
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (p.info.get("name") or "").lower()
            if "python" not in name:
                continue
            cmdline = p.info.get("cmdline") or []
            joined = " ".join(cmdline)
            if "run.py" in joined and "-p" in joined and project_name in joined:
                return True
        except Exception:
            continue
    return False


def _is_process_running(project_name: str) -> bool:
    entry = PROCESSES.get(project_name)
    if entry and entry["process"].poll() is None:
        return True
    return _is_pipeline_alive_by_cmdline(project_name)


def _cleanup_process(project_name: str):
    entry = PROCESSES.pop(project_name, None)
    if not entry:
        return
    try:
        entry["log_file"].close()
    except Exception:
        pass


def _kill_pipeline_by_cmdline(project_name: str) -> bool:
    try:
        import psutil
    except ImportError:
        return False
    found = False
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if p.info["name"] and "python" not in p.info["name"].lower():
                continue
            cmdline = p.info["cmdline"] or []
            joined = " ".join(cmdline)
            if "run.py" in joined and project_name in joined and "-p" in joined:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except Exception:
                    p.kill()
                found = True
        except Exception:
            continue
    return found


# ============================================================
#  ОПРЕДЕЛЕНИЕ ТЕКУЩЕГО ШАГА ПО ФАЙЛАМ
# ============================================================

def _pipeline_step_from_files(paths: ProjectPaths) -> dict:
    """Определяет текущий шаг по факту наличия файлов + по домену."""
    # Шаг 4 — готов Excel
    if paths.exports_dir.exists():
        xlsx = list(paths.exports_dir.glob("*.xlsx"))
        if xlsx:
            return {"step": 5, "name": "Готово", "done": True, "failed": False}

    # Шаг 3 — есть распарсенные товары
    if paths.products_dir.exists():
        products = [f for f in paths.products_dir.glob("*/*.json")
                    if f.name != "progress.json"]
        if products:
            return {"step": 4, "name": "Парсинг данных",
                    "done": False, "failed": False}

    # Шаг 2.5 — все снимки скачаны → обучение
    if paths.pages_progress.exists():
        try:
            data = json.loads(paths.pages_progress.read_text(encoding="utf-8"))
            done_total = (
                len(data.get("downloaded", []))
                + len(data.get("not_in_archive", []))
                + len(data.get("network_error", []))
                + len(data.get("retry_error", []))
            )
            expected = 0
            stat_file = paths.stat_dir / "stat.json"
            if stat_file.exists():
                try:
                    sd = json.loads(stat_file.read_text(encoding="utf-8"))
                    for v in sd.get("urls", {}).values():
                        expected += len(v.get("days", []))
                except Exception:
                    pass

            if expected > 0 and done_total >= expected:
                return {"step": 3, "name": "Обучение рецептов",
                        "done": False, "failed": False}
        except Exception:
            pass
        return {"step": 2, "name": "Скачивание HTML",
                "done": False, "failed": False}

    # Шаг 1 — идёт сбор CDX. Проверяем, все ли домены уже в stat.json
    stat_file = paths.stat_dir / "stat.json"
    if stat_file.exists():
        try:
            sd = json.loads(stat_file.read_text(encoding="utf-8"))
            urls_dict = sd.get("urls", {})
            domains_in_stat = set(
                v.get("domain") for v in urls_dict.values() if v.get("domain")
            )

            all_domains = set()
            if paths.input_parsed.exists():
                p = json.loads(paths.input_parsed.read_text(encoding="utf-8"))
                all_domains = set(r["domain"] for r in p.get("rows", []))

            # Если ещё не все домены собраны — ШАГ 1
            if all_domains and domains_in_stat < all_domains:
                return {"step": 1, "name": "Сбор снимков из архива",
                        "done": False, "failed": False}
        except Exception:
            pass

        # Все домены в stat.json → шаг 2
        return {"step": 2, "name": "Скачивание HTML",
                "done": False, "failed": False}

    # parsed.json есть, stat.json нет
    if paths.input_parsed.exists():
        return {"step": 1, "name": "Сбор снимков из архива",
                "done": False, "failed": False}

    return {"step": 0, "name": "Разбор входного файла",
            "done": False, "failed": False}


def _can_resume(paths: ProjectPaths, status: str) -> bool:
    """Можно ли продолжить прерванный прогон."""
    if status == "done" or status == "running":
        return False
    return paths.input_parsed.exists()


# ============================================================
#  ROUTES
# ============================================================

@app.route("/")
def index():
    projects = []
    for name in list_projects():
        paths = ProjectPaths(name)
        state_file = paths.root / "state.json"
        state = {}
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        excel_files = (sorted(paths.exports_dir.glob("*.xlsx"))
                       if paths.exports_dir.exists() else [])

        progress = _project_progress(paths, name)
        running = _is_process_running(name)
        status = _project_status_label(running, bool(excel_files), progress)

        if status[0] == "idle":
            has_any = (
                paths.input_parsed.exists()
                or (paths.stat_dir / "stat.json").exists()
                or paths.pages_progress.exists()
            )
            if has_any:
                status = ("stopped", "Остановлен")

        projects.append({
            "name": name,
            "target": state.get("target", _guess_target_from_input(paths)),
            "has_excel": bool(excel_files),
            "excel": excel_files[0].name if excel_files else None,
            "progress": progress,
            "status": status,
        })

    return render_template("index.html", projects=projects)


@app.route("/new", methods=["GET", "POST"])
def new_project():
    if request.method == "GET":
        return render_template("new_project.html")

    scenario = request.form.get("scenario", "single").strip()
    name = request.form.get("name", "").strip()
    excel = request.form.get("excel", "report.xlsx").strip() or "report.xlsx"
    from_date = request.form.get("from_date", "2020-01-01").strip()

    if not name:
        flash("Укажите название проекта", "error")
        return redirect(url_for("new_project"))

    if (DATA_DIR / name).exists():
        flash(f"Проект '{name}' уже существует.", "error")
        return redirect(url_for("new_project"))

    paths = ProjectPaths(name)
    paths.ensure_dirs()

    if scenario == "single":
        url = request.form.get("url", "").strip()
        if not url:
            flash("Укажите ссылку на товар", "error")
            return redirect(url_for("new_project"))

        csv_path = paths.input_dir / "single_input.csv"
        _write_single_csv(csv_path, url)
        _start_pipeline(name, csv_path, excel, from_date)
        return redirect(url_for("project_view", name=name))

    file = request.files.get("input_file")
    if not file or not file.filename:
        flash("Загрузите файл (CSV или XLSX)", "error")
        return redirect(url_for("new_project"))

    ext = Path(file.filename).suffix.lower()
    if ext not in UPLOAD_EXTENSIONS:
        flash(f"Неподдерживаемый формат: {ext}", "error")
        return redirect(url_for("new_project"))

    saved_path = paths.input_dir / f"input{ext}"
    file.save(saved_path)
    _start_pipeline(name, saved_path, excel, from_date)
    return redirect(url_for("project_view", name=name))


def _write_single_csv(path: Path, url: str):
    import csv as _csv
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = _csv.writer(f, delimiter=",")
        writer.writerow(["url", "name", "current_price"])
        writer.writerow([url, "", ""])


@app.route("/project/<name>")
def project_view(name):
    paths = ProjectPaths(name)
    if not paths.root.exists():
        flash("Проект не найден", "error")
        return redirect(url_for("index"))

    state = {}
    state_file = paths.root / "state.json"
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    excel_files = (sorted(p.name for p in paths.exports_dir.glob("*.xlsx"))
                   if paths.exports_dir.exists() else [])

    running = _is_process_running(name)
    if running:
        status = "running"
    elif excel_files:
        status = "done"
    else:
        has_any = (
            paths.input_parsed.exists()
            or (paths.stat_dir / "stat.json").exists()
            or paths.pages_progress.exists()
        )
        status = "stopped" if has_any else "idle"


    step_info = _pipeline_step_from_files(paths)
    task = {
        "status": status,
        "step": step_info["step"],
        "step_name": step_info["name"],
        "can_resume": _can_resume(paths, status),
    }

    return render_template(
        "project.html",
        name=name,
        target=state.get("target", _guess_target_from_input(paths)),
        task=task,
        progress=_project_progress(paths, name),
        excel_files=excel_files,
    )


@app.route("/project/<name>/status")
def project_status(name):
    paths = ProjectPaths(name)
    running = _is_process_running(name)
    step_info = _pipeline_step_from_files(paths)

    excel_files = (sorted(p.name for p in paths.exports_dir.glob("*.xlsx"))
                   if paths.exports_dir.exists() else [])

    if running:
        status = "running"
    elif excel_files:
        status = "done"
    else:
        has_any = (
            paths.input_parsed.exists()
            or (paths.stat_dir / "stat.json").exists()
            or paths.pages_progress.exists()
        )
        status = "stopped" if has_any else "idle"

    # Определяем режим реестра
    return jsonify({
        "task": {
            "status": status,
            "step": step_info["step"],
            "step_name": step_info["name"],
            "can_resume": _can_resume(paths, status),
        },
        "progress": _project_progress(paths, name),
    })


@app.route("/project/<name>/logs")
def project_logs(name):
    paths = ProjectPaths(name)
    if not paths.root.exists():
        return jsonify({"ok": False, "error": "not_found"}), 404

    log_file = paths.logs_dir / "web_run.log"
    if not log_file.exists():
        return jsonify({"ok": True, "mode": "empty", "lines": [],
                        "size": 0, "total_lines": 0, "modified": None})

    try:
        after = request.args.get("after", type=int)
        tail = request.args.get("tail", default=500, type=int)
        tail = max(50, min(tail, 2000))

        with open(log_file, "rb") as f:
            f.seek(0, 2)
            current_size = f.tell()
            modified = datetime.fromtimestamp(
                log_file.stat().st_mtime
            ).strftime("%H:%M:%S")

            if after is not None and after <= current_size:
                if after == current_size:
                    return jsonify({"ok": True, "mode": "incremental",
                                    "lines": [], "size": current_size,
                                    "modified": modified})
                f.seek(after)
                chunk = f.read()
                text = chunk.decode("utf-8", errors="ignore")
                lines = text.splitlines()
                if lines and not text.startswith("\n"):
                    f.seek(after - 1)
                    prev_byte = f.read(1)
                    if prev_byte != b"\n":
                        lines = lines[1:]
                return jsonify({"ok": True, "mode": "incremental",
                                "lines": lines, "size": current_size,
                                "modified": modified})

            f.seek(0)
            text = f.read().decode("utf-8", errors="ignore")
            all_lines = text.splitlines()
            return jsonify({"ok": True, "mode": "tail",
                            "lines": all_lines[-tail:],
                            "size": current_size,
                            "total_lines": len(all_lines),
                            "modified": modified})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/project/<name>/download/<filename>")
def download_excel(name, filename):
    paths = ProjectPaths(name)
    file_path = paths.exports_dir / filename
    if not file_path.exists():
        flash("Файл не найден", "error")
        return redirect(url_for("project_view", name=name))
    return send_file(file_path, as_attachment=True)


@app.route("/project/<name>/stop", methods=["POST"])
def stop_project(name):
    entry = PROCESSES.get(name)
    if entry and entry["process"].poll() is None:
        proc = entry["process"]
        try:
            if os.name == "nt":
                proc.send_signal(subprocess.signal.CTRL_BREAK_EVENT)
            else:
                proc.terminate()
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        _cleanup_process(name)
        return jsonify({"ok": True, "killed_by": "process_registry"})

    killed = _kill_pipeline_by_cmdline(name)
    if killed:
        return jsonify({"ok": True, "killed_by": "cmdline"})
    return jsonify({"ok": False, "error": "no_process"}), 404


@app.route("/project/<name>/restart", methods=["POST"])
def restart_project(name):
    paths = ProjectPaths(name)
    if not paths.root.exists():
        return jsonify({"ok": False, "error": "not_found"}), 404

    entry = PROCESSES.get(name)
    if entry:
        proc = entry["process"]
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
        except Exception:
            pass
        _cleanup_process(name)
    _kill_pipeline_by_cmdline(name)

    input_dir = paths.input_dir
    candidates = (list(input_dir.glob("input.*"))
                  + list(input_dir.glob("single_input.csv")))
    if not candidates:
        return jsonify({"ok": False, "error": "no_input_file"}), 400
    input_file = candidates[0]

    for d in (paths.stat_dir, paths.pages_dir, paths.products_dir,
              paths.exports_dir, paths.cache_dir):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
    try:
        if paths.learn_progress.exists():
            paths.learn_progress.unlink()
    except Exception:
        pass

    ok = _start_pipeline(name, input_file, "report.xlsx", "2020-01-01")
    if not ok:
        return jsonify({"ok": False, "error": "start_failed"}), 500
    return jsonify({"ok": True})


@app.route("/project/<name>/resume", methods=["POST"])
def resume_project(name):
    """Продолжает прерванный прогон с того места, где остановились."""
    paths = ProjectPaths(name)
    if not paths.root.exists():
        return jsonify({"ok": False, "error": "not_found"}), 404

    if not paths.input_parsed.exists():
        return jsonify({
            "ok": False,
            "error": "Нет данных для продолжения. Запустите проект заново."
        }), 400

    entry = PROCESSES.get(name)
    if entry:
        proc = entry["process"]
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
        except Exception:
            pass
        _cleanup_process(name)
    _kill_pipeline_by_cmdline(name)

    input_dir = paths.input_dir
    candidates = (list(input_dir.glob("input.*"))
                  + list(input_dir.glob("single_input.csv")))
    if not candidates:
        return jsonify({"ok": False, "error": "no_input_file"}), 400
    input_file = candidates[0]

    from_date = "2020-01-01"
    state_file = paths.root / "state.json"
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            from_date = state.get("from_date", from_date)
        except Exception:
            pass

    excel_name = "report.xlsx"
    if paths.exports_dir.exists():
        xlsx = list(paths.exports_dir.glob("*.xlsx"))
        if xlsx:
            excel_name = xlsx[0].name

    ok = _start_pipeline(name, input_file, excel_name, from_date, resume=True)
    if not ok:
        return jsonify({"ok": False, "error": "start_failed"}), 500
    return jsonify({"ok": True})


@app.route("/project/<name>/delete", methods=["POST"])
def delete_project(name):
    paths = ProjectPaths(name)
    if not paths.root.exists():
        return jsonify({"ok": False, "error": "not_found"}), 404

    entry = PROCESSES.get(name)
    if entry:
        proc = entry["process"]
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
        except Exception:
            pass
        _cleanup_process(name)

    _kill_pipeline_by_cmdline(name)

    import gc
    gc.collect()
    time.sleep(0.5)

    for attempt in range(3):
        try:
            shutil.rmtree(paths.root, ignore_errors=False)
            return jsonify({"ok": True})
        except PermissionError as e:
            if attempt == 2:
                return jsonify({"ok": False, "error": str(e)}), 500
            time.sleep(1)
    return jsonify({"ok": True})


# ============================================================
#  ПРОГРЕСС ПО ЭТАПАМ
# ============================================================

def _project_progress(paths: ProjectPaths, project_name: str = None) -> dict:
    start = _run_start_time(paths)
    running = _is_process_running(project_name) if project_name else False

    s0_done = paths.input_parsed.exists()
    s0 = _step0_progress(paths, start) if s0_done else {
        "status": "running" if running else "waiting",
        "label": "В работе" if running else "Ожидание",
        "detail": "",
    }

    s1 = _step1_progress(paths, start, running)
    s1_done = s1["status"] == "done"

    s2 = _step2_progress(paths, start, running) if s1_done else {
        "status": "waiting", "label": "Ожидание", "detail": ""
    }
    s2_done = s2["status"] == "done"

    s2_5 = _step2_5_progress(paths, start, running) if s2_done else {
        "status": "waiting", "label": "Ожидание", "detail": ""
    }
    s2_5_done = s2_5["status"] == "done"

    s3 = _step3_progress(paths, start, running) if s2_5_done else {
        "status": "waiting", "label": "Ожидание", "detail": ""
    }
    s3_done = s3["status"] == "done"

    s4 = _step4_progress(paths, start, running) if s3_done else {
        "status": "waiting", "label": "Ожидание", "detail": ""
    }

    return {"step0": s0, "step1": s1, "step2": s2,
            "step2_5": s2_5, "step3": s3, "step4": s4}

def _step0_progress(paths, start=None):
    try:
        data = json.loads(paths.input_parsed.read_text(encoding="utf-8"))
        return {"status": "done", "label": "Файл разобран",
                "detail": f"URL: {data.get('total', 0)}, доменов: {data.get('unique_domains', 0)}"}
    except Exception:
        return {"status": "done", "label": "Файл разобран", "detail": ""}


def _step1_progress(paths, start=None, running=False):
    stat_file = paths.stat_dir / "stat.json"

    # Всегда пытаемся прочитать последний домен из лога — для деталей
    last_domain_line = None
    web_log = paths.logs_dir / "web_run.log"
    if web_log.exists():
        try:
            text = web_log.read_text(encoding="utf-8", errors="ignore")
            for line in reversed(text.splitlines()):
                m = re.search(r"DOMAIN\s+\[(\d+)/(\d+)\]\s+([\w\.\-]+)", line)
                if m:
                    last_domain_line = m
                    break
        except Exception:
            pass

    if stat_file.exists():
        try:
            data = json.loads(stat_file.read_text(encoding="utf-8"))
        except Exception:
            data = {}

        urls_dict = data.get("urls", {})
        total_snaps = data.get("total_snapshots_filtered", 0)
        urls_ok = sum(1 for v in urls_dict.values() if v.get("status") == "ok")

        # Домены в stat.json
        domains_in_stat = set(
            v.get("domain") for v in urls_dict.values() if v.get("domain")
        )

        # Все домены из parsed.json
        all_domains = set()
        if paths.input_parsed.exists():
            try:
                p = json.loads(paths.input_parsed.read_text(encoding="utf-8"))
                all_domains = set(r["domain"] for r in p.get("rows", []))
            except Exception:
                pass

        # Проверяем свежесть stat.json
        is_fresh = (start is None or
                    datetime.fromtimestamp(stat_file.stat().st_mtime) >= start)

        # Если обработаны ВСЕ домены → done
        if all_domains and domains_in_stat >= all_domains:
            return {
                "status": "done",
                "label": "Снимки найдены",
                "detail": f"URL: {urls_ok} из {data.get('total_urls', 0)}, "
                          f"снимков: {total_snaps}",
            }

        # Иначе — running, с деталями
        if last_domain_line:
            cur = int(last_domain_line.group(1))
            total = int(last_domain_line.group(2))
            domain = last_domain_line.group(3)
            detail = (f"Домен {cur} из {total}: {domain} · "
                      f"URL: {urls_ok}, снимков: {total_snaps}")
        else:
            detail = (f"Доменов: {len(domains_in_stat)} из "
                      f"{len(all_domains) or '?'}")

        if not running and not is_fresh:
            return {"status": "failed", "label": "Прервано",
                    "detail": "Процесс не активен"}

        return {
            "status": "running",
            "label": "Сбор снимков из архива",
            "detail": detail,
        }

    # stat.json ещё нет — читаем лог
    if last_domain_line:
        return {
            "status": "running",
            "label": "Сбор снимков из архива",
            "detail": f"Домен {last_domain_line.group(1)} из "
                      f"{last_domain_line.group(2)}: {last_domain_line.group(3)}",
        }

    if not running:
        return {"status": "failed", "label": "Прервано",
                "detail": "Процесс не активен"}

    return {"status": "running", "label": "Сбор снимков из архива",
            "detail": "Инициализация..."}


def _step2_progress(paths, start=None, running=False):
    """Прогресс шага 2. Опирается на pages/progress.json, а НЕ на лог."""
    if paths.pages_progress.exists():
        if start is None or datetime.fromtimestamp(
                paths.pages_progress.stat().st_mtime) >= start:
            try:
                data = json.loads(paths.pages_progress.read_text(encoding="utf-8"))
                downloaded = len(data.get("downloaded", []))
                not_in_archive = len(data.get("not_in_archive", []))
                net_err = len(data.get("network_error", []))
                retry_err = len(data.get("retry_error", []))
                total_done = downloaded + not_in_archive + net_err + retry_err

                # Сколько ВСЕГО снимков ожидается (из stat.json)
                expected = 0
                stat_file = paths.stat_dir / "stat.json"
                if stat_file.exists():
                    try:
                        sd = json.loads(stat_file.read_text(encoding="utf-8"))
                        for v in sd.get("urls", {}).values():
                            expected += len(v.get("days", []))
                    except Exception:
                        pass

                # Всё скачано → done
                if expected > 0 and total_done >= expected:
                    return {
                        "status": "done",
                        "label": "HTML скачаны",
                        "detail": f"Скачано: {downloaded}, нет в архиве: {not_in_archive}",
                    }

                # Часть скачана и процесс жив → running
                if running and total_done > 0:
                    return {
                        "status": "running",
                        "label": "Скачивание HTML",
                        "detail": f"Снимков: {total_done} из {expected or '?'}",
                    }

                # Часть скачана, процесс не жив → прервано
                if total_done > 0 and not running:
                    return {
                        "status": "failed",
                        "label": "Прервано",
                        "detail": f"Скачано: {total_done} из {expected}, процесс не активен",
                    }
            except Exception:
                pass
        # progress.json есть, но старый (не в этом прогоне)
        return {"status": "done", "label": "HTML скачаны", "detail": ""}

    # progress.json ещё нет → шаг 2 не начинался
    if not running:
        return {"status": "waiting", "label": "Ожидание", "detail": ""}
    return {"status": "running", "label": "Скачивание HTML",
            "detail": "Инициализация..."}


def _step2_5_progress(paths, start=None, running=False):
    if not paths.input_parsed.exists():
        return {"status": "waiting", "label": "Ожидание", "detail": ""}
    try:
        inp = json.loads(paths.input_parsed.read_text(encoding="utf-8"))
        domains = set(r["domain"] for r in inp.get("rows", []))
        if not domains:
            return {"status": "waiting", "label": "Ожидание", "detail": ""}

        from ..core.paths import global_recipe_path
        total_recipes = 0
        covered = 0
        for d in domains:
            p = global_recipe_path(d)
            if p.exists():
                covered += 1
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    total_recipes += len(data.get("recipes", []))
                except Exception:
                    pass

        # Если продукт уже парсится — обучение закончено
        if paths.products_dir.exists():
            if any(paths.products_dir.glob("*/*.json")):
                return {"status": "done", "label": "Рецепты готовы",
                        "detail": f"Все {len(domains)} доменов покрыты, "
                                  f"рецептов: {total_recipes}"}

        # Если есть рецепт, но идёт обучение — показываем running
        if covered > 0:
            return {"status": "running", "label": "Обучение рецептов",
                    "detail": f"Доменов с рецептом: {covered} из {len(domains)} "
                              f"· рецептов: {total_recipes}"}

        return {"status": "running", "label": "Обучение рецептов",
                "detail": f"Доменов с рецептом: 0 из {len(domains)}"}
    except Exception:
        return {"status": "waiting", "label": "Ожидание", "detail": ""}


def _step3_progress(paths, start=None, running=False):
    if not paths.products_dir.exists():
        return {"status": "waiting", "label": "Ожидание", "detail": ""}
    files = [f for f in paths.products_dir.glob("*/*.json") if f.name != "progress.json"]
    if start:
        files = [f for f in files
                 if datetime.fromtimestamp(f.stat().st_mtime) >= start]
    if not files:
        return {"status": "waiting", "label": "Ожидание", "detail": ""}
    not_product = 0
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(d, dict) and d.get("error") == "not_a_product_page":
                not_product += 1
        except Exception:
            continue
    good = len(files) - not_product
    detail = f"Товаров: {good}"
    if not_product:
        detail += f", не товаров: {not_product}"
    return {"status": "done", "label": "Парсинг завершён", "detail": detail}


def _step4_progress(paths, start=None, running=False):
    if not paths.exports_dir.exists():
        return {"status": "waiting", "label": "Ожидание", "detail": ""}
    files = list(paths.exports_dir.glob("*.xlsx"))
    if start:
        files = [f for f in files
                 if datetime.fromtimestamp(f.stat().st_mtime) >= start]
    if not files:
        return {"status": "waiting", "label": "Ожидание", "detail": ""}
    return {"status": "done", "label": "Excel готов",
            "detail": files[0].name, "file": files[0].name}


def _project_status_label(running, has_excel, progress):
    if running:
        return ("running", "В работе")
    if has_excel:
        return ("done", "Готово")
    steps_done = sum(1 for v in progress.values() if v.get("status") == "done")
    if steps_done == 0:
        return ("idle", "Не запускался")
    if steps_done < 6:
        return ("stopped", "Остановлен")
    return ("idle", "Не завершён")


def _guess_target_from_input(paths):
    if not paths.input_parsed.exists():
        return "—"
    try:
        data = json.loads(paths.input_parsed.read_text(encoding="utf-8"))
        rows = data.get("rows", [])
        if rows:
            domains = set(r["domain"] for r in rows)
            if len(domains) == 1:
                return list(domains)[0]
            return f"{len(domains)} доменов"
    except Exception:
        pass
    return "—"

# ============================================================
#  АНАЛИТИКА — сбор данных для аналитической справки
# ============================================================

def _load_products_for_project(paths: ProjectPaths) -> list:
    """Собирает все распарсенные товары проекта с ценами по датам."""
    if not paths.products_dir.exists():
        return []

    # Собираем по source_url
    by_url = {}

    for page_dir in sorted(paths.products_dir.iterdir()):
        if not page_dir.is_dir():
            continue
        for json_file in sorted(page_dir.glob("*.json")):
            if json_file.name == "progress.json":
                continue
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue

            meta = data.get("meta") or {}
            src_url = meta.get("source_url") or ""
            if not src_url:
                continue

            product = data.get("product") or {}
            price = data.get("price") or {}
            price_current = _to_float_safe(price.get("current"))
            price_old = _to_float_safe(price.get("old"))

            entry = by_url.setdefault(src_url, {
                "url": src_url,
                "name": product.get("name") or "",
                "input_name": meta.get("input_name") or "",
                "input_price": _to_float_safe(meta.get("input_price")),
                "brand": product.get("brand") or "",
                "sku": product.get("sku") or "",
                "currency": price.get("currency") or "RUB",
                "points": [],
            })

            if not entry["name"] and product.get("name"):
                entry["name"] = product["name"]

            # Путь к локальному файлу снимка
            entry["points"].append({
                "date": meta.get("date") or "",
                "timestamp": meta.get("timestamp") or json_file.stem,
                "price": price_current,
                "price_old": price_old,
                "recipe_id": meta.get("recipe_id"),
            })

    # Сортируем точки по timestamp
    for entry in by_url.values():
        entry["points"].sort(key=lambda p: p.get("timestamp") or "")

    return list(by_url.values())


def _to_float_safe(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        s = str(v).replace(" ", "").replace("\xa0", "").replace(",", ".")
        s = "".join(c for c in s if c.isdigit() or c in ".-")
        return float(s) if s else None
    except (ValueError, TypeError):
        return None


@app.route("/project/<name>/analytics")
def project_analytics(name):
    """JSON со сводкой по товарам: цены по датам, метрики."""
    paths = ProjectPaths(name)
    if not paths.root.exists():
        return jsonify({"ok": False, "error": "not_found"}), 404

    try:
        products = _load_products_for_project(paths)

        # Только товары, у которых есть хотя бы одна цена
        summary = []
        for p in products:
            prices = [pt["price"] for pt in p["points"] if pt["price"] is not None]
            if not prices:
                continue

            first = p["points"][0] if p["points"] else {}
            last = p["points"][-1] if p["points"] else {}

            change_pct = None
            if len(prices) >= 2 and prices[0] > 0:
                change_pct = round((prices[-1] - prices[0]) / prices[0] * 100, 1)

            diff_from_input = None
            if p["input_price"] and prices[-1] and p["input_price"] > 0:
                diff_from_input = round(
                    (prices[-1] - p["input_price"]) / p["input_price"] * 100, 1
                )

            summary.append({
                "url": p["url"],
                "name": p["name"] or p["input_name"] or p["url"],
                "input_name": p["input_name"],
                "brand": p["brand"],
                "sku": p["sku"],
                "currency": p["currency"],
                "input_price": p["input_price"],
                "points_count": len(p["points"]),
                "prices_count": len(prices),
                "price_min": min(prices),
                "price_max": max(prices),
                "price_first": prices[0],
                "price_last": prices[-1],
                "change_pct": change_pct,
                "diff_from_input": diff_from_input,
                "period_from": first.get("date", ""),
                "period_to": last.get("date", ""),
                "points": p["points"],
            })

        # Сортируем по имени
        summary.sort(key=lambda x: (x["name"] or "").lower())

        return jsonify({
            "ok": True,
            "total_products": len(summary),
            "products": summary,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500



if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)