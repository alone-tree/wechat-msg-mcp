"""导出与渲染：分节文本、CSV 剔空列、NDJSON。"""
import json

from src import export, render

ROWS = [
    {
        "id": 2,
        "time": "2026-11-14 10:01:00",
        "from": "李四",
        "chat_name": "某群",
        "text": "第一行\n第二行",
        "type": "text",
    },
    {
        "id": 1,
        "time": "2026-11-14 10:00:00",
        "from": "我",
        "chat_name": "某群",
        "text": "带竖线|和换行\n的正文",
        "type": "text",
    },
    {
        "id": 0,
        "time": "2026-11-14 09:00:00",
        "from": "王五",
        "chat_name": "另一个群",
        "text": "[图片]",
        "type": "image",
        "media": {"kind": "image", "path": "D:/x/a.jpg"},
    },
]

OBJ = {"ok": True, "total": 120, "returned": 3, "has_more": True, "data_until": "2026-11-14 10:02:00", "messages": ROWS}


def test_text_render_groups_by_chat_and_indents_continuation():
    text = render.render_text(OBJ)
    assert "=== 某群（2 条）===" in text
    assert "=== 另一个群（1 条）===" in text
    assert "[2026-11-14 10:01:00] 李四: 第一行" in text
    assert "\n  第二行" in text
    assert "本次 3 条，命中 120 条，还有更多" in text


def test_text_render_keeps_media_path():
    assert "path: D:/x/a.jpg" in render.render_text(OBJ)


def test_md_render_uses_headings_not_tables():
    md = render.render_md(OBJ)
    assert md.startswith("## 某群（2 条）")
    assert "|---|" not in md
    assert "带竖线|和换行" in md


def test_ndjson_one_object_per_line():
    lines = render.render_ndjson(OBJ).strip().split("\n")
    assert len(lines) == 3
    first = json.loads(lines[0])
    assert first["chat"] == "某群"
    assert first["from"] == "李四"


def test_csv_drops_all_empty_columns():
    rows = [{"a": 1, "b": "", "c": None}, {"a": 2, "b": "", "c": None}]
    cols = export.columns_of(rows, ["a", "b", "c"])
    assert cols == ["a"]
    assert export.render_csv(rows, cols).splitlines()[0] == "a"


def test_csv_keeps_container_as_json():
    import csv
    import io

    cols = export.columns_of(ROWS, export.MESSAGE_COLUMNS)
    rows = list(csv.DictReader(io.StringIO(export.render_csv(ROWS, cols))))
    assert "media" in rows[0]
    assert json.loads(rows[2]["media"])["kind"] == "image"


def test_md_table_escapes_pipe_and_newline():
    cols = export.columns_of(ROWS, export.MESSAGE_COLUMNS)
    md = export.render_md_table(ROWS, cols)
    assert "\\|" in md
    for line in md.splitlines():
        assert "\n" not in line


def test_write_rows_text_writes_single_readable_file(tmp_path):
    result = export.write_rows(ROWS, fmt="text", out_dir=str(tmp_path), kind="messages", envelope=OBJ)
    assert result["rows"] == 3
    body = open(result["path"], encoding="utf-8").read()
    assert "=== 某群（2 条）===" in body


def test_write_rows_ndjson(tmp_path):
    result = export.write_rows(ROWS, fmt="ndjson", out_dir=str(tmp_path), kind="messages", envelope=OBJ)
    lines = open(result["path"], encoding="utf-8").read().strip().split("\n")
    assert len(lines) == 3
    assert json.loads(lines[-1])["id"] == 0


def test_write_rows_empty_keeps_header(tmp_path):
    result = export.write_rows([], fmt="csv", out_dir=str(tmp_path), kind="messages")
    assert result["empty"] is True
    assert open(result["path"], encoding="utf-8-sig").read().startswith("time")


def test_html_render_is_sectioned_not_table(tmp_path):
    result = export.write_rows(ROWS, fmt="html", out_dir=str(tmp_path), kind="messages", envelope=OBJ)
    body = open(result["path"], encoding="utf-8").read()
    assert "<table>" not in body
    assert "<h3>" in body
    assert "第一行\n第二行" in body
    assert "的另一行" in body or "带竖线|和换行" in body
