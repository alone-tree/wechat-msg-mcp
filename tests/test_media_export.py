"""媒体落盘与视频原件导出：写回的键必须与投影后的行对上。"""
from src.mcp_tools import _export_videos, _save_images


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


def test_save_images_writes_back_path_on_projected_rows(tmp_path):
    """投影后行键是 id（不是 local_id），落盘路径必须还能写回 media。"""
    obj = {"messages": [{"id": 7, "text": "[图片]", "media": {"kind": "image"}}]}
    images = [{"local_id": 7, "kind": "image", "source": "dat", "bytes": b"\xff\xd8fakejpeg"}]
    saved = _save_images(obj, images, None, tmp_path / "media")
    assert saved == 1
    assert obj["messages"][0]["media"]["path"].endswith(".jpg")
    assert (tmp_path / "media").exists()


def test_save_images_writes_back_cover_path_for_video(tmp_path):
    obj = {"messages": [{"id": 9, "text": "[视频 8 秒]", "media": {"kind": "video", "path": "a.mp4"}}]}
    images = [{"local_id": 9, "kind": "video", "source": "cover", "bytes": b"\xff\xd8cover"}]
    _save_images(obj, images, None, tmp_path / "media")
    media = obj["messages"][0]["media"]
    assert media["cover_path"].endswith(".jpg")
    assert media["path"] == "a.mp4"
