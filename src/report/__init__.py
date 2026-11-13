"""年度报告模块。

对外只有一个入口：run(src, year, out_dir)。详见 docs/重构方案-2026-11-13.md 第 10 节。
"""
from __future__ import annotations

from .annual import run

__all__ = ["run"]
