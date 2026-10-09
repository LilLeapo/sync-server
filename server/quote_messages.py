"""Preserve original message boundaries before supplier event extraction."""
from __future__ import annotations

import datetime as dt
import re

from .wechat_quote import app_subtype, cdata, is_quote_message, parse_quote, tag_block

CST = dt.timezone(dt.timedelta(hours=8))


def timestamp(value) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            number = int(value)
            return number * 1000 if 0 < number < 100000000000 else number
        text = str(value).strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M %p"):
            try:
                return int(dt.datetime.strptime(text, fmt).replace(tzinfo=CST).timestamp() * 1000)
            except ValueError:
                pass
        parsed = dt.datetime.fromisoformat(text)
        return int((parsed if parsed.tzinfo else parsed.replace(tzinfo=CST)).timestamp() * 1000)
    except (ValueError, TypeError, OverflowError):
        return None


def is_merged(msg: dict) -> bool:
    text = str(msg.get("content") or "")
    return app_subtype(text) in (19, 40) or cdata(text, "realinnertype") == "19"


def analysis_units(rec: dict) -> list[dict]:
    msg = rec.get("msg") or {}
    received = rec.get("receivedAt") or msg.get("createTime") or 0
    base = {"roomId": msg.get("roomId") or "unknown", "receivedAt": received,
            "parentTime": msg.get("createTime"), "msgSeq": rec.get("seq"),
            "msgSvrId": str(msg.get("msgSvrId") or ""),
            "sender": msg.get("senderName") or msg.get("sender") or "",
            "senderId": msg.get("sender") or "", "partial": False,
            "time": msg.get("createTime") or received, "imgFile": rec.get("imgFile")}
    text = str(msg.get("content") or "")
    if is_quote_message(msg):
        parsed = parse_quote(text)
        if not parsed:
            return []
        refer = parsed["refer"]
        block = tag_block(text, "refermsg")
        original_time = timestamp(cdata(block, "createtime"))
        original_id = cdata(block, "svrid") or cdata(block, "msgid")
        units = []
        if refer["text"] and refer["type"] in (1, 49):
            units.append({**base, "part": "reference", "content": refer["text"],
                          "sender": refer["sender"], "time": original_time,
                          "originalId": original_id, "source": "引用"})
        if parsed["reply"]:
            units.append({**base, "part": "reply", "content": parsed["reply"],
                          "referenceContext": refer["text"], "source": "引用"})
        return units
    if is_merged(msg):
        record = cdata(text, "recorditem")
        units = []
        for index, match in enumerate(re.finditer(r"<dataitem\b([^>]*)>([\s\S]*?)</dataitem>", record)):
            attrs, body = match.groups()
            if not re.search(r'\bdatatype=["\']1["\']', attrs):
                continue
            content = cdata(body, "datadesc") or cdata(body, "desc")
            if not content:
                continue
            raw_time = cdata(body, "sourcetime") or cdata(body, "srcMsgCreateTime")
            id_match = re.search(r'\bdataid=["\']([^"\']+)', attrs)
            units.append({**base, "part": f"forward:{index}", "content": content,
                          "sender": cdata(body, "sourcename"),
                          "time": timestamp(raw_time), "source": "合并转发",
                          "originalId": id_match.group(1) if id_match else ""})
        if units:
            return units
        preview = cdata(text, "des")
        return [{**base, "part": "preview", "content": preview, "time": None,
                 "partial": True, "source": "合并转发预览"}] if preview else []
    return [{**base, "part": "message", "content": text,
             **({"content": ""} if msg.get("type") == 3 else {}),
             "source": "图片" if msg.get("type") == 3 else "文字"}]
