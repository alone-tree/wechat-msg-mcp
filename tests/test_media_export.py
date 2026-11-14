"""视频原件导出：有本地 mp4 时复制并改写路径。"""
from src.mcp_tools import _export_videos


def _obj(path):
    return {"messages": [{"media": {"kind": "video", "path": str(path), "exists": True, "size": path.stat().st_size}}]}


def test_export_video_copies_and_rewrites_path(tmp_path):
    src_dir = tmp_path / "wechat"
    src_dir.mkdir()
    mp4 = src_dir / "a.mp4"
    mp4.write_bytes(b"\x00" * 2048)
    obj = _obj(mp4)
    target = tmp_path / "export"
    result = _export_videos(obj, target)
    assert result["exported"] == 1
    media = obj["messages"][0]["media"]
    assert media["path"] != str(mp4)
    assert media["source_path"] == str(mp4)
    assert (target / "a.mp4").exists()


def test_export_video_skips_missing_file(tmp_path):
    obj = {"messages": [{"media": {"kind": "video", "missing_reason": "no_local_video"}}]}
    result = _export_videos(obj, tmp_path / "export")
    assert result["exported"] == 0
    assert result["skipped"] == 1


def test_export_video_skips_other_kinds(tmp_path):
    obj = {"messages": [{"media": {"kind": "image", "path": "x.jpg", "exists": True}}]}
    result = _export_videos(obj, tmp_path / "export")
    assert result == {"exported": 0, "skipped": 0, "dir": str(tmp_path / "export")}
