"""年度报告独立脚本（2026-11-16 从包与 CLI 移出）。

微信年度报告属于用户按需自己编排的东西，不是工具的固有功能，
所以从包内删除、从 CLI 移除子命令，只保留本目录作为参考实现。
删掉本目录不影响 wechat-msg-mcp 包的任何功能。

用法（在仓库根目录）：
    python -m scripts.annual_report --year 2025 --out reports/2025
入口是 run(src, year, out_dir)，src 是 LocalSource。
"""
from __future__ import annotations

from .annual import run

__all__ = ["run"]
