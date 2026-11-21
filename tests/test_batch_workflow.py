"""Coverage and routing regressions on fake split WeChat databases."""
import asyncio
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

from build_batch_fixture import build
from src import messages, mcp_tools
from src.cli import main
from src.service import Service


def chat(i):
    return f"{9900 + i}@chatroom"


@pytest.fixture
def service(tmp_path):
    root = tmp_path / "db"
    build(root)
    return Service(str(root), auto_refresh=False)


def _receipt(result):
    return json.loads(Path(result["coverage"]["path"]).read_text(encoding="utf-8"))


def _ndjson(result):
    return [json.loads(line) for path in result["files"]
            for line in Path(path).read_text(encoding="utf-8").splitlines()]


def test_coverage_counts_shards_aliases_empty_and_unresolved(service, tmp_path):
    out = service.export_messages(str(tmp_path), "ndjson", with_media=False,
                                  targets=[chat(1), chat(3), "体验样例3", chat(5), chat(6)],
                                  start="2026-11-14", end="2026-11-21", max_rows=3)
    assert not out["is_partial"]  # Exactly the limit with no additional matching row.
    assert not out["coverage"]["is_complete"]
    assert out["coverage"]["requested"] == 5
    assert out["coverage"]["resolved_unique"] == 3
    rows = _ndjson(out)
    assert [r["chat_id"] for r in rows] == [chat(3), chat(3), chat(1)]
    receipt = _receipt(out)
    assert [(r["status"], r["rows"]) for r in receipt["targets"]] == [
        ("complete", 1), ("complete", 2), ("complete", 2), ("empty", 0), ("unresolved", 0)
    ]
    assert receipt["targets"][-1]["reason"] == "no_message_table"


def test_partial_target_is_not_reported_as_empty(service, tmp_path):
    out = service.export_messages(str(tmp_path), "ndjson", with_media=False,
                                  targets=[chat(4), chat(1), chat(2)], max_rows=1)
    assert out["is_partial"] and not out["coverage"]["is_complete"]
    assert [(r["status"], r["rows"]) for r in _receipt(out)["targets"]] == [
        ("complete", 1), ("partial", 0), ("partial", 0)
    ]


def test_read_failure_is_not_an_empty_window(service, tmp_path, monkeypatch):
    connect = messages.connect_ro

    def fail_one_shard(path):
        if path.name == "message_1.db":
            raise sqlite3.OperationalError("fixture read error")
        return connect(path)

    # Freeze resolution, then simulate a read failure during the export itself.
    tables = messages.resolve_tables(service.src, chat(3))
    monkeypatch.setattr(messages, "resolve_tables", lambda *_: tables)
    monkeypatch.setattr(messages, "connect_ro", fail_one_shard)
    out = service.export_messages(str(tmp_path), "ndjson", with_media=False, targets=[chat(3)])
    assert out["rows"] == 1
    assert out["coverage"]["is_complete"] is False
    assert _receipt(out)["targets"][0]["status"] == "read_error"


def test_cli_and_mcp_export_parity_and_timestamp_boundary(service, tmp_path, capsys, monkeypatch):
    assert main(["groups", "--root", str(service.src.root), "--no-fresh", "--members", "小林", "小唐", "--json"]) == 0
    discovery = json.loads(capsys.readouterr().out)
    ids = [g["id"] for g in discovery["groups"]]
    assert len(ids) == 6 and not discovery["has_more"]
    assert main(["export", "--root", str(service.src.root), "--no-fresh", "--targets", *ids,
                 "--start", "2026-11-14", "--end", "2026-11-21", "--format", "ndjson",
                 "--no-media", "--out", str(tmp_path / "cli")]) == 0
    cli = json.loads(capsys.readouterr().out)
    server = FastMCP("fixture")
    mcp_tools.register(server, lambda: service)
    monkeypatch.setattr(service, "get_messages", lambda **kw: pytest.fail("unexpected preview scan"))
    raw = asyncio.run(server.call_tool("get_messages", {
        "targets": ids, "start_date": "2026-11-14", "end_date": "2026-11-21", "order": "asc",
        "output": "file", "format": "ndjson", "include_media": False, "output_dir": str(tmp_path / "mcp"),
        "limit": 1, "per_target_limit": 1,
    }))
    response = json.loads(raw[0].text)
    assert response["mode"] == "file" and "messages" not in response and "has_more" not in response
    mcp = response["export"]
    assert cli["coverage"]["by_status"] == mcp["coverage"]["by_status"] == {"complete": 4, "empty": 1, "unresolved": 1}
    rows = _ndjson(cli)
    assert rows == _ndjson(mcp)
    cutoff = datetime.fromisoformat("2026-11-21 14:01")
    paid = {r["chat_id"] for r in rows if r["text"] == "已确认收到学费"}
    overdue = {r["chat_id"] for r in rows if r["type"] == "system" and r["chat_id"] not in paid
               and cutoff - datetime.fromisoformat(r["time"]) >= timedelta(hours=72)}
    assert paid == {chat(3)} and overdue == {chat(1), chat(4)}


def test_bad_range_is_not_an_unbounded_query(service, tmp_path):
    assert service.get_messages(targets=[chat(1)], start="11-18")["error"]["code"] == "bad_date_range"
    out = service.export_messages(str(tmp_path), targets=[chat(1)], start="2026-11-21", end="2026-11-18")
    assert out["ok"] is False


def test_require_all_members_does_not_shrink_the_intersection(service):
    assert service.groups_by_members(["小林", "不存在的人"])["error"]["code"] == "members_unresolved"


def test_freshness_report_belongs_to_data_read(service, monkeypatch):
    steps = []
    monkeypatch.setattr(service, "_fresh", lambda: steps.append("refresh") or {"refreshed": True})
    monkeypatch.setattr(messages, "list_messages", lambda *_a, **_kw: steps.append("read") or {})
    assert service.get_messages()["refresh"]["refreshed"]
    assert steps == ["refresh", "read"]


def test_custom_snapshot_cannot_auto_refresh_or_sync(service, tmp_path, monkeypatch):
    from src import config, freshness

    monkeypatch.delenv("WECHAT_REFRESH_CUSTOM_ROOT", raising=False)
    monkeypatch.setattr(freshness, "ensure", lambda *_a, **_kw: pytest.fail("snapshot refreshed"))
    monkeypatch.setenv("WECHAT_DECRYPTED_DIR", str(service.src.root))
    isolated = Service()
    assert isolated.groups_by_members(["小林", "小唐"])["refresh"]["skipped"] == "snapshot_root"
    assert isolated.refresh()["error"]["code"] == "snapshot_root"
    assert not (service.src.root / "_index").exists()
    monkeypatch.setenv("WECHAT_REFRESH_CUSTOM_ROOT", "1")
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    protected = tmp_path / "exports" / "fixture"
    protected.mkdir(parents=True)
    assert Service(str(protected)).refresh()["error"]["code"] == "snapshot_root"


def test_image_block_overflow_keeps_correct_message_paths(tmp_path, monkeypatch):
    from src import export, media_inline

    monkeypatch.setattr(mcp_tools, "INLINE_IMAGE_BLOCKS", 2)
    monkeypatch.setattr(mcp_tools, "_tiles_for", lambda data, **kw: [b"part1", b"part2"] if data == b"long" else [])
    monkeypatch.setattr(export, "run_dir", lambda: tmp_path)
    monkeypatch.setattr(media_inline, "save_jpeg", lambda _src, data, _kind, _path: {"path": data.decode() + ".jpg"})
    obj = {"messages": [{"id": 1, "media": {}}, {"id": 1, "media": {}}]}
    blocks, stats = mcp_tools._inline_images(obj, [
        {"message_index": 0, "local_id": 1, "bytes": b"long"},
        {"message_index": 1, "local_id": 1, "bytes": b"short"},
    ], None)
    assert len(blocks) == stats["inlined"] == 2
    assert stats["not_inlined_by_reason"] == {"image_block_limit": 1}
    first, second = [r["media"] for r in obj["messages"]]
    assert first["inlined"] and first["path"] == "long.jpg"
    assert not second["inlined"] and second["path"] == "short.jpg"
