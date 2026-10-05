"""Оркестратор: последовательный запуск всех шагов."""
import json
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from .core.paths import ProjectPaths
from .core.logger import ProjectLogger
from .core.config import load_all_configs
from .steps import (step0_input, step1_stat, step2_download,
                    step2_5_learn, step3_parse, step4_excel)


class Pipeline:
    def __init__(self, project_name: str):
        self.paths = ProjectPaths(project_name)
        self.paths.ensure_dirs()
        self.logger = ProjectLogger(self.paths, "main")
        self.configs = load_all_configs()
        self.state_file = self.paths.root / "state.json"

    def _load_state(self):
        if self.state_file.exists():
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        return {}

    def _save_state(self, state):
        self.state_file.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _update_state(self, **kwargs):
        state = self._load_state()
        state.update(kwargs)
        self._save_state(state)

    # --- отдельные шаги ---

    def run_step0(self, source_file: Path, from_date: str = "2013-01-01",
                  cleanup: bool = True):
        result = step0_input.run(self.paths, source_file, self.logger,
                                 cleanup=cleanup)
        if result.get("ok"):
            self._update_state(step0=result, from_date=from_date)
        return result

    def run_step1(self, from_date: str = None, resume: bool = False):
        state = self._load_state()
        if from_date is None:
            from_date = state.get("from_date", "2013-01-01")
        result = step1_stat.run(self.paths, from_date, self.logger,
                                resume=resume)
        if result.get("ok"):
            self._update_state(step1=result)
        return result

    def run_step2(self):
        result = step2_download.run(self.paths, self.logger)
        if result.get("ok"):
            self._update_state(step2=result)
        return result

    def run_step2_5(self, force=False):
        result = step2_5_learn.run(
            self.paths,
            self.configs["llm"],
            self.configs.get("clean", {}),
            self.logger,
            force=force,
        )
        if result.get("ok"):
            self._update_state(step2_5=result)
        return result

    def run_step3(self, force=False):
        result = step3_parse.run(self.paths, self.logger, force=force)
        if result.get("ok"):
            self._update_state(step3=result)
        return result

    def run_step4(self, output_name="report.xlsx"):
        result = step4_excel.run(self.paths, self.logger, output_name)
        if result.get("ok"):
            self._update_state(step4=result)
        return result

    # --- полный прогон ---

    def run_all(self, source_file: Path, excel_name: str = "report.xlsx",
                from_date: str = "2013-01-01", resume: bool = False):
        mode = "ПРОДОЛЖЕНИЕ" if resume else "ПОЛНЫЙ ПРОГОН"
        self.logger.info(f"🚀 {mode}: {source_file}")

        r0 = self.run_step0(source_file, from_date, cleanup=not resume)
        if not r0.get("ok"): return r0
        r1 = self.run_step1(from_date, resume=resume)
        if not r1.get("ok"): return r1
        r2 = self.run_step2()
        if not r2.get("ok"): return r2
        r25 = self.run_step2_5()
        if not r25.get("ok"): return r25
        r3 = self.run_step3()
        if not r3.get("ok"): return r3
        r4 = self.run_step4(excel_name)
        return r4