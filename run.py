#!/usr/bin/env python
"""Единая точка входа: CLI для запуска конвейера."""
import argparse
import sys
from pathlib import Path

from wayback_audit.pipeline import Pipeline
from wayback_audit.core.paths import list_projects


def main():
    parser = argparse.ArgumentParser(description="Wayback Audit — пайплайн")
    parser.add_argument("--project", "-p", required=True,
                        help="Имя проекта (папка в data/)")
    parser.add_argument("--input", "-i",
                        help="Входной Excel/CSV со списком URL и цен")
    parser.add_argument("--step", "-s",
                        choices=["0", "1", "2", "2.5", "3", "4",
                                 "all", "registry"],
                        default="all",
                        help="Шаг или режим (registry — домен за доменом)")
    parser.add_argument("--from-date", default="2013-01-01",
                        help="С какой даты тянуть снимки (YYYY-MM-DD)")
    parser.add_argument("--excel", default="report.xlsx")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--resume", action="store_true",
                        help="Продолжить прерванный прогон "
                             "(не очищать артефакты, пропускать готовое)")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()

    if args.list:
        projects = list_projects()
        print("Проекты:")
        for p in projects:
            print(f"  • {p}")
        return

    pipe = Pipeline(args.project)

    if args.step == "registry":
        if not args.input:
            print("❌ Для режима registry нужен --input")
            sys.exit(1)
        result = pipe.run_registry(
            Path(args.input),
            args.excel,
            args.from_date,
            resume=args.resume,
        )
    elif args.step == "all":
        if not args.input:
            print("❌ Для полного прогона нужен --input")
            sys.exit(1)
        result = pipe.run_all(
            Path(args.input),
            args.excel,
            args.from_date,
            resume=args.resume,
        )
    elif args.step == "0":
        if not args.input:
            print("❌ Для шага 0 нужен --input")
            sys.exit(1)
        result = pipe.run_step0(
            Path(args.input),
            args.from_date,
            cleanup=not args.resume,
        )
    elif args.step == "1":
        result = pipe.run_step1(args.from_date, resume=args.resume)
    elif args.step == "2":
        result = pipe.run_step2()
    elif args.step == "2.5":
        result = pipe.run_step2_5(force=args.force)
    elif args.step == "3":
        result = pipe.run_step3(force=args.force)
    elif args.step == "4":
        result = pipe.run_step4(args.excel)

    print()
    print("Результат:", result)


if __name__ == "__main__":
    main()