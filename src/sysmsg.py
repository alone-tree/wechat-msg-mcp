"""系统消息折叠：把 XML 原文压成一句人话。

依据 docs/重构方案-消息模型与输出规范-2026-11-14.md 第 3 节。
折叠后的句子不带前缀，调用方按需补「[系统] 」。

实测（最近约 30 万条样本）system 主类型占 2.7%，其中 60% 本来就是纯文本
（「某某 撤回了一条消息」），其余是 sysmsg=sysmsgtemplate / revokemsg /
paymsg / delchatroommember / mmchatroomtopmsg 与其它 XML。
拍一拍不走 system 主类型，它是 appmsg type=62、标题里带「拍了拍」。
"""
from __future__ import annotations

import html
import re
from typing import Callable, Optional

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_CDATA = re.compile(r"^\s*<!\[CDATA\[(.*?)\]\]>\s*$", re.S)
_VAR = re.compile(r"\$([^$<>\s]{1,64})\$")
_SYSMSG_TYPE = re.compile(r'<sysmsg\s+type="([^"]*)"')
_LINK = re.compile(r'<link\s+name="([^"]*)"[^>]*>(.*?)</link>', re.S)
_MEMBER = re.compile(r"<member>(.*?)</member>", re.S)
_TEMPLATE = re.compile(r"<template>(.*?)</template>", re.S)
_SEPARATOR = re.compile(r"<separator>(.*?)</separator>", re.S)
_PAT_RE = re.compile(r"拍了拍")
_PREFIX = re.compile(r"^[\w@._-]{2,64}:(?=<)")

MAX_LEN = 200


def _clean(value: str) -> str:
    """去 CDATA 包裹与 HTML 实体。"""
    if not value:
        return ""
    text = value.strip()
    m = _CDATA.match(text)
    if m:
        text = m.group(1)
    return html.unescape(text).strip()


def _strip(value: str) -> str:
    """去标签、还原实体、压空白。标签直接删掉不留空格，中文才不会被拆开。"""
    if not value:
        return ""
    return _WS.sub(" ", html.unescape(_TAG.sub("", value))).strip()


def _first(value: str, tag: str) -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", value or "", re.S)
    return _clean(m.group(1)) if m else ""


def _clip(text: str) -> str:
    text = (text or "").strip()
    return text[:MAX_LEN] + ("…" if len(text) > MAX_LEN else "")


def is_pat(content: str) -> bool:
    """拍一拍：appmsg type=62 且标题含「拍了拍」。实测它就是 62 号 appmsg。"""
    body = content or ""
    if _PAT_RE.search(body) is None:
        return False
    return "<appmsg" in body


def pat_text(content: str) -> str:
    """拍一拍的一句话：标题本身就是「A 拍了拍 B」，直接取。"""
    return _clip(_clean(_first(content, "title")))


def _links(body: str) -> dict[str, str]:
    """sysmsgtemplate 的 link_list：变量名 -> 成员昵称串。"""
    out: dict[str, str] = {}
    for name, inner in _LINK.findall(body):
        names: list[str] = []
        for member in _MEMBER.findall(inner):
            nick = _first(member, "nickname")
            user = _first(member, "username")
            if nick or user:
                names.append(nick or user)
        sep = _clean(_first(inner, "separator")) or "、"
        out[name] = sep.join(n for n in names if n)
    return out


def _fill(template: str, links: dict[str, str], lookup: Optional[Callable[[str], str]]) -> str:
    """把模板里的 $变量$ 换成具体名字；换不到的用「某人」占位，不留美元符号。"""

    def repl(m: re.Match) -> str:
        key = m.group(1)
        if key in links and links[key]:
            return links[key]
        if lookup is not None:
            try:
                name = lookup(key) or ""
            except Exception:
                name = ""
            if name and name != key:
                return name
        return "某人"

    return _VAR.sub(repl, template)


def fold(content: str, lookup: Optional[Callable[[str], str]] = None) -> tuple[str, str]:
    """返回 (一句话, 结构类型)。结构类型仅用于排查，不进正文。"""
    body = (content or "").strip()
    if not body:
        return "空系统消息", "empty"
    # 群消息正文可能带「会话 id:」前缀，且没有换行，先去掉
    m_prefix = _PREFIX.match(body)
    if m_prefix:
        body = body[m_prefix.end():].strip()
    if not body.startswith("<"):
        return _clip(_WS.sub(" ", body)), "plain"
    if is_pat(body):
        return pat_text(body), "pat"
    if "<content_template" in body or 'type="sysmsgtemplate"' in body:
        templates = _TEMPLATE.findall(body)
        links = _links(body)
        if templates:
            sentence = _fill(_clean(templates[0]), links, lookup)
            return _clip(_WS.sub(" ", sentence)), "sysmsgtemplate"
        plain = _first(body, "plain")
        if plain:
            return _clip(_fill(plain, links, lookup)), "sysmsgtemplate"
        return _clip(_strip(body)), "sysmsgtemplate"
    kind = ""
    m = _SYSMSG_TYPE.search(body)
    if m:
        kind = m.group(1)
    if kind == "revokemsg" or "<revokemsg>" in body:
        sentence = _first(body, "content") or _first(body, "replacemsg")
        return _clip(sentence or "一条消息被撤回"), "revokemsg"
    if kind == "delchatroommember":
        sentence = _first(body, "plain") or _first(body, "text")
        return _clip(sentence or "群成员变动"), "delchatroommember"
    if kind == "paymsg":
        inner = _first(body, "content") or body
        return _clip(_strip(_fill(inner, {}, lookup))), "paymsg"
    if kind == "mmchatroomtopmsg":
        return "群公告或置顶消息有更新", "mmchatroomtopmsg"
    plain = _first(body, "plain")
    if plain and len(plain) >= 4:
        return _clip(plain), kind or "plainx"
    return _clip(_strip(_fill(body, {}, lookup))), kind or "other"
