#!/usr/bin/env python3
"""年度报告独立脚本的命令行入口。

本来在 CLI 里（python -m src.cli report），2026-11-16 移出包与 CLI：
年度报告不是工具的固有功能，用户需要时自己跑这个脚本或改写它。
没有本目录时包与 CLI 完全正常。

用法（在仓库根目录，需要项目依赖与 decrypted/ 数据）：
    .venv/Scripts/python.exe -m scripts.annual_report --year 2025 --out reports/2025
    uv run python -m scripts.annual_report --year 2025

产物默认写到 reports/<year>/，六份文件由 annual.run 决定。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.service import Service  # noqa: E402

from .annual import run  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="微信年度报告（独立脚本，不属于 wechat-msg-mcp 包）"
    )
    parser.add_argument("--year", type=int, required=True, help="统计年份，如 2025")
    parser.add_argument("--out", default="", help="输出目录，默认 reports/<year>")
    parser.add_argument("--root", default="", help="decrypted 目录，默认自动探测")
    parser.add_argument("--no-fresh", action="store_true", help="不按 TTL 自动刷新")
    args = parser.parse_args()

    out = args.out or str(Path("reports") / str(args.year))
    svc = Service(args.root or None, auto_refresh=not args.no_fresh)
    obj = run(svc.src, args.year, out)
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    return 0 if obj.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
