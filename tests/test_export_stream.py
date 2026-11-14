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
