"""导出流与旁挂集合：参数校验与多集合不丢数据。"""
import json
from types import SimpleNamespace

import pytest

from src import mcp_tools
from src.messages import export_stream


def test_export_stream_rejects_bad_detail():
    with pytest.raises(ValueError) as err:
        export_stream(SimpleNamespace(), detail="nope")
    assert "详细度" in str(err.value)


def test_export_stream_rejects_bad_order():
    with pytest.raises(ValueError) as err:
        export_stream(SimpleNamespace(), order="sideways")
    assert "排序方向" in str(err.value)


def test_export_stream_rejects_unknown_types():
    with pytest.raises(ValueError) as err:
        export_stream(SimpleNamespace(), types=["nonsense"])
    assert "类型" in str(err.value)


def test_respond_keeps_extra_collections_in_sidecar(tmp_path):
    obj = {
        "ok": True,
        "kind": "activity",
        "total": 3,
        "by_day": [{"day": f"2026-01-0{i}", "count": i} for i in range(1, 4)],
        "by_hour": [{"hour": i, "count": i} for i in range(24)],
        "top_senders": [{"name": "某人", "messages": 2}],
        "warnings": ["只是提示，不该当数据落盘"],
    }
    text = mcp_tools._respond(
        obj, "relations", ("by_day",), output="file", fmt="csv", out_dir=str(tmp_path)
    )
    out = json.loads(text)
    exported = out["export"]
    assert exported["rows"] == 3
    assert exported["extra_collections"] == ["by_hour", "top_senders"]
    payload = json.load(open(exported["extra"], encoding="utf-8"))
    assert sorted(payload) == ["by_hour", "top_senders"]
    assert len(payload["by_hour"]) == 24
    assert "warnings" not in payload


def test_save_row_media_copies_file_message(tmp_path):
    from src.messages import _save_row_media

    origin = tmp_path / "wechat" / "a.pdf"
    origin.parent.mkdir()
    origin.write_bytes(b"%PDF-1.4 fake")
    row = {
        "media": {
            "kind": "file",
            "name": "a.pdf",
            "size": origin.stat().st_size,
            "path": str(origin),
            "exists": True,
        }
    }
    budget = {"saved": 0, "images": 0, "voice": 0, "video": 0, "video_files": 0, "files": 0,
              "skipped": 0, "missing": {}, "limit": 10}
    _save_row_media(None, row, tmp_path / "out", budget)
    media = row["media"]
    assert media["path"] != str(origin)
    assert media["source_path"] == str(origin)
    assert (tmp_path / "out" / "files" / "a.pdf").exists()
    assert budget["files"] == 1 and budget["saved"] == 1


def test_save_row_media_respects_limit_and_marks_reason(tmp_path):
    from src.messages import _save_row_media

    # 超过上限：不改路径，但标清楚原因
    over = {"media": {"kind": "file", "name": "x.pdf", "size": 1, "path": "", "exists": False}}
    budget = {"saved": 99, "images": 0, "voice": 0, "video": 0, "video_files": 0, "files": 0,
              "skipped": 0, "missing": {}, "limit": 5}
    _save_row_media(None, over, tmp_path / "out", budget)
    assert over["media"]["missing_reason"] == "media_limit_reached"
    assert budget["skipped"] == 1

    # 没超上限但本地没有文件：标 no_local_file
    missing = {"media": {"kind": "file", "name": "y.pdf", "size": 1, "path": "", "exists": False}}
    budget2 = {"saved": 0, "images": 0, "voice": 0, "video": 0, "video_files": 0, "files": 0,
               "skipped": 0, "missing": {}, "limit": 5}
    _save_row_media(None, missing, tmp_path / "out", budget2)
    assert missing["media"]["missing_reason"] == "no_local_file"
    assert budget2["missing"]["no_local_file"] == 1
