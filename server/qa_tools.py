#!/usr/bin/env python3
"""问答工具集 —— 各机器人（飞书 / 企业微信）共用的 Agent 工具定义。

`build_qa_tools(store, quote_log, device=None)` 返回一组可直接注册给 pi_bridge 会话的工具：
handler 均为同步函数，入参 dict，返回 str。device 非 None 时所有工具只看该设备的数据
（微信群 @ 回复按设备各开一个 Agent）；飞书/钉钉等机器人不传，看全部设备。
"""
from __future__ import annotations

import datetime as dt
import json
import os

from .pi_bridge import _fmt_row
from .quote_messages import timestamp
from .quote_rules import QuoteLedger, compare_quotes


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


def build_qa_tools(store, quote_log, device: str | None = None) -> list[dict]:
    """Expose historical records, replayed state, comparison, and evidence."""

    path = getattr(quote_log, "path", None)
    ledger = QuoteLedger(os.path.join(os.path.dirname(path), "events.jsonl") if path else None)

    def all_rows(room=None):
        rows, after = [], 0
        if not quote_log:
            return rows
        while True:
            page, _ = quote_log.list(after, 2000, room, device)
            if not page:
                break
            rows.extend(page)
            after = page[-1]["qseq"]
            if len(page) < 2000:
                break
        return rows

    def search_quotes(params: dict) -> str:
        kw = str(params.get("keyword") or "").strip().lower()
        limit = _clamp(params.get("limit"), 10, 1, 30)
        rows = all_rows(params.get("room"))
        picked = [r for r in rows if (
            not kw or kw in " ".join(str(r.get(k) or "") for k in
                                     ("供应商", "项目", "备注", "sender", "roomName", "单位", "价格", "details")).lower())]
        return rows_text(picked[-limit:]) or "没有找到匹配的报价。"

    def latest_quotes(params: dict) -> str:
        limit = _clamp(params.get("limit"), 10, 1, 30)
        rows = all_rows(params.get("room"))
        return rows_text(rows[-limit:]) or "还没有报价记录。"

    def current_quotes(params: dict) -> str:
        rows = ledger.current(params.get("room"), device)
        if params.get("supplier"):
            rows = [row for row in rows if row["供应商"] == params["supplier"]]
        if params.get("product"):
            rows = [row for row in rows if params["product"] in row["项目"]]
        rows.sort(key=lambda row: row.get("time") or 0, reverse=True)
        return json.dumps({"quotes": rows[:_clamp(params.get("limit"), 20, 1, 50)],
                           "note": "active只表示未被后续事件替代；采购前仍需核实日期及供货。历史六字段记录需重新提取。"}, ensure_ascii=False)

    def compare(params: dict) -> str:
        if params.get("as_of"):
            at = timestamp(params["as_of"])
            if not at:
                return json.dumps({"error": "as_of日期无效"}, ensure_ascii=False)
            rows = ledger.at(at, params.get("room"), device)
        else:
            rows = ledger.current(params.get("room"), device)
        if not rows:
            return json.dumps({"groups": [], "needs_confirmation": [],
                               "note": "没有结构化报价事件；旧报价需通过历史回填重新提取，不能只按价格字符串排名。"}, ensure_ascii=False)
        return json.dumps(compare_quotes(rows, params), ensure_ascii=False)

    def quote_history(params: dict) -> str:
        events = ledger.history(str(params.get("supplier") or ""), str(params.get("product") or ""),
                                params.get("room"), _clamp(params.get("limit"), 20, 1, 50), device)
        return json.dumps({"events": events}, ensure_ascii=False)

    def quote_evidence(params: dict) -> str:
        ledger.refresh()
        with ledger.lock:
            row = ledger.quotes.get(params.get("quote_id"))
            if row and (device is None or row["deviceId"] == device):
                return json.dumps(row, ensure_ascii=False)
        return "没有找到该quoteId的证据。"

    def room_stats(params: dict) -> str:
        if not store:
            return "存储不可用。"
        stats = store.stats(device)
        rooms = store.rooms(device)[:8]
        lines = [f"累计消息 {stats.get('count', 0)} 条，图片 {stats.get('images', 0)} 张。"]
        if rooms:
            lines.append("活跃群：" + "、".join(room_name(r) for r in rooms))
        return "\n".join(lines)

    tools = [
        {
            "name": "search_quotes",
            "label": "查报价",
            "description": "按关键词检索历史报价记录（供应商/项目/备注/发送人/单位），历史记录不代表当前有效。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "检索关键词"},
                    "limit": {"type": "integer", "description": "最多返回条数，默认 10"},
                    "room": {"type": "string", "description": "微信群roomId，可选"},
                },
            },
            "handler": search_quotes,
        },
        {
            "name": "latest_quotes",
            "label": "最近报价",
            "description": "返回最近追加的历史报价记录，不代表当前有效。参数 limit 1-30 条数，默认 10。",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "最多返回条数，默认 10"},
                    "room": {"type": "string", "description": "微信群roomId，可选"},
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
    filters = {"room": {"type": "string", "description": "微信群roomId，可选"},
               "supplier": {"type": "string", "description": "供应商完整名称，精确匹配"},
               "product": {"type": "string", "description": "产品关键词"},
               "limit": {"type": "integer", "minimum": 1, "maximum": 50}}
    tools.extend([
        {"name": "current_quotes", "label": "报价状态", "description": "查询当前未被替代的结构化报价方案，含条件和待核实标记。active不代表实时有效。",
         "parameters": {"type": "object", "properties": filters}, "handler": current_quotes},
        {"name": "quote_history", "label": "报价历史", "description": "查看原始报价事件及调价、拒价、库存变化、采购记录的规则处理结果。",
         "parameters": {"type": "object", "properties": filters}, "handler": quote_history},
        {"name": "quote_evidence", "label": "报价依据", "description": "按quoteId取得原文、原始时间、状态、结构化条件和派生计算依据。",
         "parameters": {"type": "object", "properties": {"quote_id": {"type": "string"}}, "required": ["quote_id"]},
         "handler": quote_evidence},
        {"name": "compare_quotes", "label": "比较报价", "description": "按采购条件筛选和计算。仅组内价格可排名，返回可比较组、待确认和不符合要求的方案。缺失条件不默认满足；不要编造benchmark。",
         "parameters": {"type": "object", "properties": {
             "room": filters["room"], "product": filters["product"],
             "destination": {"type": "string", "description": "需要送达的地区"},
             "pickup_region": {"type": "string", "description": "自提地区，与destination选一"},
             "tax": {"type": "string", "enum": ["included", "excluded"]},
             "brand": {"type": "string"}, "invoice_type": {"type": "string"},
             "invoice_period": {"type": "string"}, "payment": {"type": "string"},
             "pricing_party": {"type": "string"}, "diameter_mm": {"type": "number"},
             "resistance_max": {"type": "number"}, "resistance_unit": {"type": "string", "description": "默认quote_code；不同单位不自动换算"},
             "max_delivery_days": {"type": "number", "minimum": 0},
             "quantity": {"type": "number", "minimum": 0}, "quantity_unit": {"type": "string"},
             "currency": {"type": "string", "description": "默认CNY"}, "unit": {"type": "string", "description": "默认元/吨"},
             "as_of": {"type": "string", "description": "历史比较时指定ISO时间；默认现在"},
             "max_age_hours": {"type": "number", "description": "报价最大年龄，默认24小时，最大720"},
             "benchmark": {"type": "object", "description": "仅使用有出处的基准；本工具没有联网行情源。缺基准则不能换算点价报价。",
                           "properties": {"name": {"type": "string"}, "contract": {"type": "string"}, "price": {"type": "number"},
                                          "at": {"type": "string"}, "source": {"type": "string"}}, "required": ["name", "price", "at", "source"]}
         }}, "handler": compare},
    ])
    return tools
