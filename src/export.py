"""导出：把查询结果写成文件。

依据 docs/重构方案-消息模型与输出规范-2026-11-14.md 第 6 节：
- text / md 走会话分节，一行一条，多行正文缩进续写，不用表格；
- csv 固定列并剔掉全空列，媒体对象压成短 JSON；
- json / ndjson 走同一份数据，ndjson 一行一条。
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

from . import render

RUN_RETENTION_HOURS = 12.0
_RUN_TAG = time.strftime("%Y%m%d-%H%M%S")
_RUN_DIR: Path | None = None
_SEQ = 0

MESSAGE_COLUMNS = ["time", "chat_name", "from", "type", "subtype", "id", "text", "media", "quote"]
FORMATS = ("csv", "json", "ndjson", "md", "text", "html")


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
    # 根目录下散落的旧文件：旧版本直接把导出写在 exports/ 里，一并按时间清掉
    for f in sorted(root.iterdir()):
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                removed_files += 1
        except OSError:
            pass
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


_SUFFIX = {"text": "txt", "md": "md", "csv": "csv", "json": "json", "ndjson": "ndjson", "html": "html"}


def _name(prefix: str, fmt: str, index: int = 0) -> str:
    """进程内自增序号保证同秒不撞名；目录本身按进程隔离，跨进程不会冲突。"""
    global _SEQ
    _SEQ += 1
    stamp = time.strftime("%Y%m%d-%H%M%S")
    part = f"-part{index}" if index else ""
    return f"{prefix}-{stamp}{part}-{_SEQ:04d}.{_SUFFIX.get(fmt, fmt)}"


def _cell(value) -> str:
    """单元格取值：容器压成短 JSON，其余转字符串。"""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def columns_of(rows: list[dict], columns: list[str] | None = None) -> list[str]:
    """列清单：先按给定顺序，再补出现过的键；全空列直接剔掉。"""
    if columns:
        base = [c for c in columns]
        if not rows:
            return base
    else:
        base = []
        for row in rows:
            for key in row:
                if key not in base and not key.startswith("_"):
                    base.append(key)
    keep = []
    for key in base:
        if any(_cell(row.get(key)) != "" for row in rows):
            keep.append(key)
    return keep


def render_csv(rows: list[dict], columns: list[str]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: _cell(row.get(c)) for c in columns})
    return buf.getvalue()


def render_md_table(rows: list[dict], columns: list[str]) -> str:
    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    for row in rows:
        cells = [_cell(row.get(c)).replace("|", "\\|").replace("\n", " ") for c in columns]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def render_html(rows: list[dict], columns: list[str], title: str = "导出结果") -> str:
    head = "".join(f"<th>{c}</th>" for c in columns)
    body = "".join(
        "<tr>" + "".join(f"<td>{_cell(row.get(c))}</td>" for c in columns) + "</tr>" for row in rows
    )
    return (
        "<!doctype html><meta charset='utf-8'><title>%s</title>"
        "<style>body{font:14px system-ui;margin:24px}table{border-collapse:collapse}"
        "td,th{border:1px solid #ddd;padding:4px 8px;text-align:left;white-space:pre-wrap}</style>"
        "<h3>%s</h3><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>"
        % (title, title, head, body)
    )


def _payload(part: list[dict], fmt: str, cols: list[str], kind: str, envelope: dict | None) -> str:
    if kind == "messages":
        obj = {"messages": part}
        if envelope:
            obj.update({k: v for k, v in envelope.items() if k != "messages"})
        obj["returned"] = len(part)
        if fmt == "text":
            return render.render_text(obj)
        if fmt == "md":
            return render.render_md(obj)
        if fmt == "ndjson":
            return render.render_ndjson(obj)
    if fmt == "json":
        return json.dumps(part, ensure_ascii=False, indent=2)
    if fmt == "csv":
        return render_csv(part, cols)
    if fmt == "md":
        return render_md_table(part, cols)
    if fmt == "ndjson":
        return "\n".join(json.dumps(row, ensure_ascii=False) for row in part) + "\n"
    return render_html(part, cols)


def write_rows(
    rows: list[dict],
    fmt: str = "csv",
    out_dir: str = "",
    prefix: str = "export",
    columns: list[str] | None = None,
    chunk: int = 50000,
    kind: str = "rows",
    envelope: dict | None = None,
) -> dict:
    """写文件。kind=messages 时 text/md 走会话分节渲染。"""
    fmt = fmt if fmt in FORMATS else "csv"
    out = Path(out_dir) if out_dir else run_dir()
    out.mkdir(parents=True, exist_ok=True)
    cols = columns_of(rows, columns or (MESSAGE_COLUMNS if kind == "messages" else None))
    encoding = "utf-8-sig" if fmt == "csv" else "utf-8"
    files: list[str] = []
    total = 0
    if not rows:
        path = out / _name(prefix, fmt)
        path.write_text(_payload([], fmt, cols, kind, envelope), encoding=encoding, newline="" if fmt == "csv" else "")
        return {
            "exported": True,
            "path": str(path),
            "empty": True,
            "files": [str(path)],
            "rows": 0,
            "bytes": path.stat().st_size,
            "format": fmt,
            "note": "无匹配数据，已写入仅含表头的空文件",
        }
    for i in range(0, len(rows), chunk):
        part = rows[i : i + chunk]
        path = out / _name(prefix, fmt, i // chunk)
        path.write_text(
            _payload(part, fmt, cols, kind, envelope),
            encoding=encoding,
            newline="" if fmt == "csv" else "",
        )
        files.append(str(path))
        total += len(part)
    return {
        "exported": True,
        "path": files[0],
        "files": files,
        "rows": total,
        "bytes": sum(Path(f).stat().st_size for f in files),
        "format": fmt,
        "columns": cols,
    }
