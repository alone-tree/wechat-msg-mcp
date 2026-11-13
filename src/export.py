"""导出：把查询结果写成文件。

依据 docs/重构方案-2026-11-13.md 第 9 节：
超过 500 条或 80 KB 自动转文件；格式 md/json/csv/html；大批量分片写入。
"""
from __future__ import annotations

import csv
import io
import json
import time
from pathlib import Path

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


def _name(prefix: str, fmt: str, index: int = 0) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    suffix = f"-{index}" if index else ""
    return f"{prefix}-{stamp}{suffix}.{fmt}"


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
    out = Path(out_dir) if out_dir else _default_dir()
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
