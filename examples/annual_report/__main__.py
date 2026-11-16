#!/usr/bin/env python3
"""独立年度报告示例的入口：把微信解密库汇总成一份年报。

这个目录不属于 wechat-msg-mcp 包，也没有被包或 CLI 引用：
删掉整个目录，工具照常工作。它只是给需要年报的人一个现成起点，
可以照抄、改写，或者完全自己写。

用法（仓库根目录，需要项目依赖与 decrypted/ 数据）：
    .venv/Scripts/python.exe examples/annual_report/__main__.py --year 2025 --out reports/2025
    uv run python examples/annual_report/__main__.py --year 2025

产物默认写到 reports/<year>/，六份文件由 annual.run 决定。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
# 仓库内运行时让 src 可导入；装成包（pip install）时 src 在 site-packages，这行无副作用。
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from annual import run  # noqa: E402
from src.service import Service  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="微信年度报告（独立示例，不属于 wechat-msg-mcp 包）"
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
