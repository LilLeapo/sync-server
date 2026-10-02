#!/usr/bin/env python3
"""问答工具集 —— 各机器人（飞书 / 企业微信）共用的 Agent 工具定义。

`build_qa_tools(store, quote_log)` 返回一组可直接注册给 pi_bridge 会话的工具：
handler 均为同步函数，入参 dict，返回 str。
"""
from __future__ import annotations

import datetime as dt

from .pi_bridge import _fmt_row


def label(ms) -> str:
    try:
        return dt.datetime.fromtimestamp(int(ms) / 1000).strftime("%m-%d %H:%M")
    except Exception:
        return ""


def rows_text(rows: list[dict]) -> str:
    """把报价行渲染成多行纯文本（给模型当资料）。"""
    lines = []
    for r in rows:
        row = dict(r)
        row["timeLabel"] = row.get("timeLabel") or label(row.get("time"))
        lines.append(_fmt_row(row))
    return "\n".join(lines)


def room_name(r: dict) -> str:
    return str(r.get("roomName") or "").strip() or \
        str(r.get("roomId") or "").replace("@chatroom", "")


def _clamp(value, default: int, lo: int, hi: int) -> int:
    try:
        return min(max(int(value if value is not None else default), lo), hi)
    except (TypeError, ValueError):
        return default


def build_qa_tools(store, quote_log) -> list[dict]:
    """给 Agent 的工具：检索报价 / 最近报价 / 群聊统计。"""

    def search_quotes(params: dict) -> str:
        kw = str(params.get("keyword") or "").strip().lower()
        limit = _clamp(params.get("limit"), 10, 1, 30)
        rows, _ = quote_log.list(0, 2000) if quote_log else ([], 0)
        picked = [r for r in rows if (
            not kw or kw in " ".join(str(r.get(k) or "") for k in
                                     ("项目", "备注", "sender", "roomName", "单位", "价格")).lower())]
        return rows_text(picked[:limit]) or "没有找到匹配的报价。"

    def latest_quotes(params: dict) -> str:
        limit = _clamp(params.get("limit"), 10, 1, 30)
        rows, _ = quote_log.list(0, 2000) if quote_log else ([], 0)
        return rows_text(rows[-limit:]) or "还没有报价记录。"

    def room_stats(params: dict) -> str:
        if not store:
            return "存储不可用。"
        stats = store.stats()
        rooms = store.rooms()[:8]
        lines = [f"累计消息 {stats.get('count', 0)} 条，图片 {stats.get('images', 0)} 张。"]
        if rooms:
            lines.append("活跃群：" + "、".join(room_name(r) for r in rooms))
        return "\n".join(lines)

    return [
        {
            "name": "search_quotes",
            "label": "查报价",
            "description": "按关键词检索已提取的报价记录（项目/备注/发送人/单位），返回匹配列表。参数 keyword 关键词、limit 1-30 条数。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "检索关键词"},
                    "limit": {"type": "integer", "description": "最多返回条数，默认 10"},
                },
            },
            "handler": search_quotes,
        },
        {
            "name": "latest_quotes",
            "label": "最近报价",
            "description": "返回最近的报价记录。参数 limit 1-30 条数，默认 10。",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "最多返回条数，默认 10"},
                },
            },
            "handler": latest_quotes,
        },
        {
            "name": "room_stats",
            "label": "群聊统计",
            "description": "查询群聊捕获统计：消息总数、图片数、活跃群列表。无参数。",
            "parameters": {"type": "object", "properties": {}},
            "handler": room_stats,
        },
    ]
