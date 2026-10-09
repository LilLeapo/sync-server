"""微信「引用消息」解析。

群里的引用回复在 message 表里是 appmsg：外层 type 低 16 位为 49
（常见值 822083633），XML 里第一个 <type> 为 57。<title> 是发送者新写的内容，
被引用的那条在 <refermsg> 里（它自己的 <type> / <content> / <displayname>）。
"""
from __future__ import annotations

import html
import re

REFER_LABEL = {
    1: "文本",
    3: "图片",
    34: "语音",
    42: "名片",
    43: "视频",
    47: "表情",
    48: "位置",
    49: "应用消息",
}


def cdata(text: str, tag: str) -> str:
    """取第一个 <tag> 的文本，并反转义 XML 实体。"""
    m = re.search(
        r"<%s\b[^>]*>(?:<!\[CDATA\[)?([\s\S]*?)(?:\]\]>)?</%s>"
        % (re.escape(tag), re.escape(tag)),
        text or "",
    )
    return html.unescape(m.group(1).strip()) if m else ""


def tag_block(text: str, tag: str) -> str:
    """取第一个 <tag>…</tag> 的内部 XML，不做反转义。"""
    m = re.search(
        r"<%s\b[^>]*>([\s\S]*?)</%s>" % (re.escape(tag), re.escape(tag)),
        text or "",
    )
    return m.group(1) if m else ""


def app_subtype(content: str) -> int | None:
    raw = cdata(content, "type")
    return int(raw) if raw.isdigit() else None


def base_type(mtype) -> int:
    try:
        return int(mtype or 0) & 0xFFFF
    except (TypeError, ValueError):
        return 0


def is_quote_message(msg: dict) -> bool:
    """外层是引用回复：appmsg 子类型 57，且带 <refermsg>。"""
    if not isinstance(msg, dict):
        return False
    content = str(msg.get("content") or "")
    if "<refermsg" not in content:
        return False
    if app_subtype(content) == 57:
        return True
    return base_type(msg.get("type")) == 49 and "<type>57</type>" in content


def parse_quote(content: str) -> dict | None:
    """解析引用 XML。不是引用消息时返回 None。

    返回 {reply, refer:{type, label, sender, text}}。reply 是发送者新写的正文。
    """
    if app_subtype(content) != 57 and "<type>57</type>" not in (content or ""):
        return None
    block = tag_block(content, "refermsg")
    if not block:
        return None
    reply = cdata(content, "title") or cdata(content, "des")
    rtype_raw = cdata(block, "type")
    rtype = int(rtype_raw) if rtype_raw.isdigit() else 0
    sender = cdata(block, "displayname") or cdata(block, "chatusr")
    return {
        "reply": reply.strip(),
        "refer": {
            "type": rtype,
            "label": REFER_LABEL.get(rtype, "消息" if rtype else "文本"),
            "sender": sender.strip(),
            "text": refer_text(rtype, cdata(block, "content")),
        },
    }


def refer_text(rtype: int, raw: str) -> str:
    """被引用消息里能拿来分析/展示的可读文本。图片等无正文时给类型占位。"""
    raw = html.unescape(raw or "").strip()
    if not raw:
        return f"[{REFER_LABEL[rtype]}]" if rtype in REFER_LABEL and rtype != 1 else ""
    looks_xml = raw.lstrip().startswith("<")
    if rtype in (3, 34, 43, 47, 42, 48) and looks_xml:
        return f"[{REFER_LABEL.get(rtype, '消息')}]"
    if looks_xml or rtype == 49:
        title = cdata(raw, "title")
        des = cdata(raw, "des")
        text = "\n".join(part for part in (title, des) if part).strip()
        if text:
            return text
        if rtype in REFER_LABEL and rtype != 1:
            return f"[{REFER_LABEL[rtype]}]"
        plain = re.sub(r"<[^>]+>", " ", raw)
        return re.sub(r"\s+", " ", plain).strip()
    return _strip_sender_prefix(raw)


def quote_analysis_text(msg: dict) -> str | None:
    """送给模型的正文：发送内容与被引用内容分开，两段都算有效值。"""
    if not is_quote_message(msg):
        return None
    parsed = parse_quote(str(msg.get("content") or ""))
    if not parsed:
        return None
    parts = []
    if parsed["reply"]:
        parts.append("发送的消息：\n" + parsed["reply"])
    refer = parsed["refer"]
    if refer["text"]:
        who = refer["sender"] or "未知"
        parts.append(f"被引用的消息（{who}，{refer['label']}）：\n" + refer["text"])
    return "\n\n".join(parts) if parts else None


def _strip_sender_prefix(text: str) -> str:
    """群消息原文偶尔是 `wxid:\\n正文`，分析时去掉这层发送者前缀。"""
    m = re.match(r"^[0-9A-Za-z_.\-]{2,80}:\n([\s\S]+)$", text or "")
    return m.group(1) if m else text
