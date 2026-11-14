"""系统消息折叠：把 XML 压成一句人话。"""
from src import sysmsg

PLAIN = '"二妈" 撤回了一条消息'


def test_plain_text_passthrough():
    text, kind = sysmsg.fold(PLAIN)
    assert kind == "plain"
    assert text == PLAIN


def test_revokemsg_extracts_content():
    raw = (
        '<?xml version="1.0"?><sysmsg type="revokemsg"><revokemsg>'
        "<content>\"10-2-1703\" 撤回了一条消息</content><revoketime>0</revoketime>"
        "</revokemsg></sysmsg>"
    )
    text, kind = sysmsg.fold(raw)
    assert kind == "revokemsg"
    assert text == '"10-2-1703" 撤回了一条消息'
    assert "<" not in text


def test_sysmsgtemplate_fills_links():
    raw = (
        '<sysmsg type="sysmsgtemplate"><sysmsgtemplate>'
        '<content_template type="tmpl_type_profile">'
        "<plain><![CDATA[]]></plain>"
        '<template><![CDATA["$username$"邀请"$names$"加入了群聊]]></template>'
        '<link_list><link name="username" type="link_profile"><memberlist><member>'
        "<username><![CDATA[wxid_a]]></username><nickname><![CDATA[王育欣]]></nickname>"
        "</member></memberlist></link>"
        '<link name="names" type="link_profile"><memberlist><member>'
        "<username><![CDATA[wxid_b]]></username><nickname><![CDATA[香花]]></nickname>"
        "</member></memberlist></link></link_list>"
        "</content_template></sysmsgtemplate></sysmsg>"
    )
    text, kind = sysmsg.fold(raw)
    assert kind == "sysmsgtemplate"
    assert text == '"王育欣"邀请"香花"加入了群聊'


def test_sysmsgtemplate_unresolved_variable_has_no_dollar():
    raw = (
        '<sysmsg type="sysmsgtemplate"><sysmsgtemplate>'
        '<content_template type="tmpl_type_profile">'
        '<template><![CDATA["$username$"修改群名为"$tolocalstatenames$"]]></template>'
        "</content_template></sysmsgtemplate></sysmsg>"
    )
    text, _kind = sysmsg.fold(raw)
    assert "$" not in text
    assert "修改群名为" in text


def test_pat_detected_from_appmsg_title():
    raw = (
        '<msg><appmsg appid="" sdkver="0"><title>"土豆" 拍了拍 "彤彤"</title>'
        "<des></des><type>62</type><template_id></template_id></appmsg>"
        '<fromusername>wxid_x</fromusername></msg>'
    )
    assert sysmsg.is_pat(raw)
    assert sysmsg.pat_text(raw) == '"土豆" 拍了拍 "彤彤"'
    text, kind = sysmsg.fold(raw)
    assert kind == "pat"
    assert text == '"土豆" 拍了拍 "彤彤"'


def test_channels_message_is_not_pat():
    raw = (
        '<msg><appmsg><title>某视频号作品</title><des></des><type>62</type>'
        "</appmsg></msg>"
    )
    assert not sysmsg.is_pat(raw)


def test_unknown_xml_strips_tags_and_resolves_wxid():
    raw = (
        '<img src="SystemMessages_HongbaoIcon.png"/>  你领取了$wxid_r3xzlds0kdm822$的'
        '<_wc_custom_link_ href="weixin://x">红包</_wc_custom_link_>'
    )
    text, _kind = sysmsg.fold(raw, lookup=lambda wxid: "张三" if wxid == "wxid_r3xzlds0kdm822" else "")
    assert "$" not in text
    assert "你领取了张三的红包" in text


def test_paymsg_and_delchatroommember():
    pay = (
        '<?xml version="1.0"?><sysmsg type="paymsg"><content><![CDATA[你有一笔待接收的'
        '<_wc_custom_link_ href="weixin://wxpay">转账</_wc_custom_link_>]]></content></sysmsg>'
    )
    assert sysmsg.fold(pay)[0] == "你有一笔待接收的转账"
    leave = (
        '45662936746@chatroom:<sysmsg type="delchatroommember"><delchatroommember>'
        '<plain><![CDATA[你邀请"yurinana"加入了群聊]]></plain></delchatroommember></sysmsg>'
    )
    assert sysmsg.fold(leave)[0] == '你邀请"yurinana"加入了群聊'


def test_mmchatroomtopmsg_is_human_readable():
    text, _kind = sysmsg.fold('<sysmsg type="mmchatroomtopmsg"><mmchatroomtopmsg/></sysmsg>')
    assert "群公告" in text


def test_empty_content():
    assert sysmsg.fold("")[1] == "empty"
