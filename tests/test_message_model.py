"""消息模型：text 自足、三档详细度、类型校验。"""
from src.messages import _describe, _project, _wanted_types, valid_types

FILE_XML = (
    '<?xml version="1.0"?><msg><appmsg appid="" sdkver="0">'
    "<title><![CDATA[简历.pdf]]></title><des></des><type>6</type>"
    "<appattach><totallen>34812</totallen></appattach></appmsg></msg>"
)
LINK_XML = (
    "<msg><appmsg><title>标题党</title><des>摘要</des><type>5</type>"
    "<url>https://example.com/a</url></appmsg></msg>"
)
TRANSFER_XML = (
    "<msg><appmsg><title>微信转账</title><des>收到转账</des><type>2000</type>"
    "<feedesc>￥257.00</feedesc></appmsg></msg>"
)
QUOTE_XML = (
    "<msg><appmsg><title>同意</title><type>57</type>"
    "<refermsg><displayname>无喜无忧</displayname><type>1</type>"
    "<content>用袋子栓好</content></refermsg></appmsg></msg>"
)
LOCATION_XML = (
    '<msg><location x="30.4" y="119.8" label="浙江省杭州市" poiname="沐风听竹" />'
    "</msg>"
)
VOIP_XML = "<voipmsg type=\"VoIPBubbleMsg\"><msg><![CDATA[通话时长 25:57]]></msg></voipmsg>"


def test_text_is_self_sufficient():
    cases = [
        ("image", None, '<msg><img md5="d41d8cd98f00b204e9800998ecf8427e"/></msg>', "[图片]"),
        ("emoticon", None, "<msg/>", "[表情]"),
        ("voice", None, '<msg><voicemsg voicelength="12000"/></msg>', "[语音 12 秒]"),
        ("video", None, '<msg><videomsg playlength="8"/></msg>', "[视频 8 秒]"),
        ("file", "file", FILE_XML, "[文件] 简历.pdf"),
        ("link", "link", LINK_XML, "标题党"),
        ("transfer", "transfer", TRANSFER_XML, "[转账] ￥257.00"),
        ("location", None, LOCATION_XML, "[位置] 沐风听竹"),
        ("voip", None, VOIP_XML, "[通话] 通话时长 25:57"),
    ]
    for type_name, subtype, body, expected in cases:
        text, media, _quote = _describe(type_name, subtype, body)
        assert text == expected, (type_name, text)
        assert media is not None, type_name


def test_plain_text_keeps_original():
    text, media, quote = _describe("text", None, "你好")
    assert (text, media, quote) == ("你好", None, None)


def test_quote_carries_referenced_content():
    text, media, quote = _describe("app", "quote", QUOTE_XML)
    assert text == "同意"
    assert media["kind"] == "quote"
    assert quote == {"from": "无喜无忧", "text": "用袋子栓好"}


def test_system_text_is_folded():
    text, media, _quote = _describe(
        "system", None, "<sysmsg type=\"revokemsg\"><revokemsg><content>撤回了一条消息</content></revokemsg></sysmsg>"
    )
    assert text == "[系统] 撤回了一条消息"
    assert media is None


def _row(**over):
    row = {
        "local_id": 7,
        "time": "2026-11-14 10:00:00",
        "from": "张三",
        "sender_id": "wxid_a",
        "is_me": False,
        "unknown_sender": False,
        "type": "text",
        "subtype": None,
        "chat_id": "1@chatroom",
        "chat_name": "某群",
        "local_type": 1,
        "text": "你好",
        "media": None,
        "quote": None,
        "db": "message_0.db",
        "raw": "你好",
    }
    row.update(over)
    return row


def test_lite_has_only_core_fields():
    out = _project(_row(media={"kind": "image", "md5": "abc", "bytes": 12, "path": "/tmp/a.jpg"}), "lite", False)
    assert set(out) == {"id", "time", "from", "text", "media"}
    assert out["media"] == {"kind": "image"}


def test_lite_adds_chat_when_multiple_chats():
    out = _project(_row(), "lite", True)
    assert out["chat"] == "某群"


def test_full_adds_ids_and_type():
    out = _project(_row(type="app", subtype="file"), "full", False)
    assert out["sender_id"] == "wxid_a"
    assert out["chat_id"] == "1@chatroom"
    assert out["chat_name"] == "某群"
    assert out["type"] == "app"
    assert out["subtype"] == "file"
    assert "raw" not in out


def test_raw_keeps_original_xml():
    out = _project(_row(type="system", text="[系统] x", raw="<sysmsg/>"), "raw", False)
    assert out["raw"] == "<sysmsg/>"
    assert out["local_type"] == 1


def test_unknown_sender_flagged():
    out = _project({**_row(), "from": "", "sender_id": "", "unknown_sender": True}, "lite", False)
    assert out["from"] == ""
    assert out["from_unknown"] is True


def test_unknown_types_rejected():
    tags, mains, unknown = _wanted_types(["transfer", "nonsense"])
    assert unknown == ["nonsense"]
    assert "app:transfer" in tags
    assert 49 in mains


def test_transfer_alias_maps_to_app_tag():
    tags, mains, unknown = _wanted_types(["transfer"])
    assert tags == {"app:transfer"}
    assert mains == {49}
    assert unknown == []


def test_valid_types_are_sorted_and_complete():
    values = valid_types()
    assert values == sorted(values)
    assert {"text", "image", "system", "transfer", "red_packet"} <= set(values)
