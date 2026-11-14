"""导出：把查询结果写成文件。

依据 docs/重构方案-2026-11-13.md 第 9 与 20 节：
超过阈值自动落盘；格式 md/json/csv/html；大批量分片写入。
每个进程写自己的 exports/run-<pid>-<启动时间>/ 目录，多实例互不覆盖；
启动时清理 run 目录下超过 12 小时的文件。AI 指定目录时不清理。
"""
from __future__ import annotations

import csv
import io
import json
import os
import time
from pathlib import Path

RUN_RETENTION_HOURS = 12.0
_RUN_TAG = time.strftime("%Y%m%d-%H%M%S")
_RUN_DIR: Path | None = None
_SEQ = 0

_MESSAGE_COLUMNS = [
    "time",
    "direction",
    "chat_name",
    "sender_name",
    "type",
    "subtype",
    "local_id",
    "text",
]


def _default_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "exports"


def run_dir() -> Path:
    """本进程专属导出目录。首次调用时创建，并顺带清理过期导出。"""
    global _RUN_DIR
    if _RUN_DIR is None:
        path = _default_dir() / f"run-{os.getpid()}-{_RUN_TAG}"
        path.mkdir(parents=True, exist_ok=True)
        _RUN_DIR = path
        cleanup()
    return _RUN_DIR


def cleanup(max_age_hours: float = RUN_RETENTION_HOURS) -> dict:
    """删除 run-* 目录里超过保留期的文件。只碰程序自己的目录，不碰 AI 指定的路径。"""
    root = _default_dir()
    removed_files = 0
    removed_dirs = 0
    if not root.exists():
        return {"removed_files": 0, "removed_dirs": 0}
    cutoff = time.time() - max_age_hours * 3600
    for d in sorted(root.glob("run-*")):
        if not d.is_dir():
            continue
        for f in list(d.rglob("*")):
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed_files += 1
            except OSError:
                pass
        dirs = [p for p in d.rglob("*") if p.is_dir()]
        dirs.append(d)
        for sub in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
            try:
                if not any(sub.iterdir()):
                    sub.rmdir()
                    removed_dirs += 1
            except OSError:
                pass
    return {"removed_files": removed_files, "removed_dirs": removed_dirs}


def _name(prefix: str, fmt: str, index: int = 0) -> str:
    """进程内自增序号保证同秒不撞名；目录本身按进程隔离，跨进程不会冲突。"""
    global _SEQ
    _SEQ += 1
    stamp = time.strftime("%Y%m%d-%H%M%S")
    part = f"-part{index}" if index else ""
    return f"{prefix}-{stamp}{part}-{_SEQ:04d}.{fmt}"


def render_csv(rows: list[dict], columns: list[str]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") for c in columns})
    return buf.getvalue()


def render_md(rows: list[dict], columns: list[str]) -> str:
    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(c, "")).replace("|", "\\|") for c in columns) + " |")
    return "\n".join(lines) + "\n"


def render_html(rows: list[dict], columns: list[str], title: str = "导出结果") -> str:
    head = "".join(f"<th>{c}</th>" for c in columns)
    body = "".join(
        "<tr>" + "".join(f"<td>{str(row.get(c, ''))}</td>" for c in columns) + "</tr>" for row in rows
    )
    return (
        "<!doctype html><meta charset='utf-8'><title>%s</title>"
        "<style>body{font:14px system-ui;margin:24px}table{border-collapse:collapse}"
        "td,th{border:1px solid #ddd;padding:4px 8px;text-align:left}</style>"
        "<h3>%s</h3><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>"
        % (title, title, head, body)
    )


def write_rows(
    rows: list[dict],
    fmt: str = "csv",
    out_dir: str = "",
    prefix: str = "export",
    columns: list[str] | None = None,
    chunk: int = 50000,
) -> dict:
    fmt = fmt if fmt in ("json", "csv", "md", "html") else "csv"
    out = Path(out_dir) if out_dir else run_dir()
    out.mkdir(parents=True, exist_ok=True)
    cols = columns or (list(rows[0].keys()) if rows else _MESSAGE_COLUMNS)
    files = []
    total = 0
    if not rows:
        path = out / _name(prefix, fmt)
        if fmt == "csv":
            path.write_text(render_csv([], columns or _MESSAGE_COLUMNS), encoding="utf-8-sig", newline="")
        elif fmt == "json":
            path.write_text("[]", encoding="utf-8")
        elif fmt == "md":
            path.write_text(render_md([], columns or _MESSAGE_COLUMNS), encoding="utf-8")
        else:
            path.write_text(render_html([], columns or _MESSAGE_COLUMNS), encoding="utf-8")
        return {
            "exported": True,
            "path": str(path),
            "empty": True,
            "files": [str(path)],
            "rows": 0,
            "bytes": path.stat().st_size,
            "note": "无匹配数据，已写入仅含表头的空文件",
        }
    for i in range(0, len(rows), chunk):
        part = rows[i : i + chunk]
        path = out / _name(prefix, fmt, i // chunk)
        if fmt == "json":
            path.write_text(json.dumps(part, ensure_ascii=False, indent=2), encoding="utf-8")
        elif fmt == "csv":
            path.write_text(render_csv(part, cols), encoding="utf-8-sig", newline="")
        elif fmt == "md":
            path.write_text(render_md(part, cols), encoding="utf-8")
        else:
            path.write_text(render_html(part, cols), encoding="utf-8")
        files.append(str(path))
        total += len(part)
    return {
        "exported": True,
        "path": files[0],
        "files": files,
        "rows": total,
        "bytes": sum(Path(f).stat().st_size for f in files),
    }
