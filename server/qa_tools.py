#!/usr/bin/env python3
"""问答工具集 —— 各机器人与群 @ 回复共用的 Agent 工具定义。

`build_qa_tools(store, quote_log, device=None)` 返回一组可直接注册给 pi_bridge 会话的工具：
每个工具是 {name, label, description, schema, parameters, handler, opt_in}，handler 为同步函数，
入参是按 schema（server/tool_schema.py）规范化后的 dict，返回 str。

- device 非 None 时所有工具只看该设备的数据（微信群 @ 回复按设备各开一个 Agent）；
  飞书/钉钉等机器人不传，看全部设备。"" 只看无设备标识的旧数据。
- room 参数既可以填 roomId，也可以填群名（唯一匹配时自动换成 roomId）。
- opt_in 工具（发送消息/图片等对外动作）默认不提供：只有 opt_in=True 时才返回，
  设备设置里需要手动勾选。
- 联网工具（web_search / futures_quote，见 net_tools.py）、数据卡片（send_card，见 cards.py）
  与插件注册的工具在启动时经 configure() 注入。
"""
from __future__ import annotations

import ast
import datetime as dt
import json
import operator
import os
import re
import sys
import threading
import time
from decimal import Decimal, DivisionByZero, InvalidOperation

from .cards import CARD_FIELDS, CardError, CardFailed, build_maker
from .pi_bridge import _fmt_row
from .quote_messages import timestamp
from .quote_rules import QuoteLedger, compare_quotes
from .skills import list_skills, read_skill
from .tool_schema import S

# 启动时 configure() 注入；默认没有联网工具、插件工具与制图（测试保持离线、不需要 Chrome）
_ENV: dict = {"net": [], "plugins": [], "cards": None}

_LEDGERS: dict[str, QuoteLedger] = {}
_LEDGERS_LOCK = threading.Lock()


def configure(agent_cfg: dict | None = None, plugin_tools: list[dict] | None = None, *,
              net_tools: list[dict] | None = None, card_maker=None):
    """注入联网工具与制图（按 agent.config.json；agent_cfg 为 None 时都没有）和插件工具（PluginHost.agent_tools）。"""
    if net_tools is None:
        from .net_tools import build_net_tools
        net_tools = build_net_tools(agent_cfg)
    _ENV["net"] = list(net_tools)
    _ENV["plugins"] = list(plugin_tools or [])
    _ENV["cards"] = card_maker if card_maker is not None else build_maker(agent_cfg)


def shared_ledger(events_path: str | None) -> QuoteLedger:
    """同一个 events.jsonl 只重放一次；refresh() 会增量读新事件，并识别回填时的整文件替换。"""
    if not events_path:
        return QuoteLedger(None)
    key = os.path.abspath(events_path)
    with _LEDGERS_LOCK:
        ledger = _LEDGERS.get(key)
        if ledger is None:
            ledger = _LEDGERS[key] = QuoteLedger(key)
        return ledger


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


def resolve_room(store, room, device=None, must_exist: bool = False) -> tuple[str | None, str | None]:
    """roomId 或群名 → (roomId, 错误提示)。room 为空返回 (None, None) 表示不限群。

    先精确匹配 roomId，再精确匹配群名，最后模糊匹配；多个候选时返回提示让模型选。
    没有消息库或找不到时：must_exist 为假则原样当 roomId 用（报价库里的群不一定有消息）。
    """
    room = str(room or "").strip()
    if not room:
        return None, None
    rooms = store.rooms(device) if store is not None else []
    if any(r["roomId"] == room for r in rooms):
        return room, None
    for match in ([r for r in rooms if room_name(r) == room],
                  [r for r in rooms if room in room_name(r) or room in r["roomId"]]):
        if len(match) == 1:
            return match[0]["roomId"], None
        if len(match) > 1:
            names = "；".join(f"{room_name(r)}（{r['roomId']}）" for r in match[:8])
            return None, f"有多个群匹配「{room}」：{names}。请用 roomId 指定。"
    if must_exist or (rooms and "@" not in room):
        return None, f"没有找到群「{room}」，可以先用 list_rooms 查看群名与 roomId。"
    return room, None


TYPE_LABELS = {3: "图片", 34: "语音", 43: "视频", 47: "表情", 48: "位置", 49: "链接/文件/引用", 10000: "系统"}


def message_line(r: dict, with_room: bool = False) -> str:
    msg = r.get("msg") or {}
    who = str(msg.get("senderName") or msg.get("sender") or "").strip()
    try:
        mtype = int(msg.get("type") or 0)
    except (TypeError, ValueError):
        mtype = 0
    content = re.sub(r"\s+", " ", str(msg.get("content") or "")).strip()
    if r.get("imgFile") or mtype == 3:
        body = f"[图片 img={r['imgFile']}]" if r.get("imgFile") else "[图片]"
    elif mtype in (1, 0):
        body = content[:300] + ("…" if len(content) > 300 else "")
    else:
        body = f"[{TYPE_LABELS.get(mtype, f'类型{mtype}')}] " + content[:120]
    head = f"[{label(msg.get('createTime') or r.get('receivedAt'))}]"
    if with_room:
        head += f"【{room_name(msg)}】"
    return f"{head} {who}：{body}" if who else f"{head} {body}"


# ---------------------------------------------------------------------------
# 计算器：只允许数字与四则运算，Decimal 精确计算
# ---------------------------------------------------------------------------

_CALC_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
             ast.Div: operator.truediv, ast.Mod: operator.mod, ast.Pow: operator.pow}


def _calc_node(node) -> Decimal:
    if isinstance(node, ast.Expression):
        return _calc_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return Decimal(str(node.value))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _calc_node(node.operand)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp) and type(node.op) in _CALC_OPS:
        left, right = _calc_node(node.left), _calc_node(node.right)
        if isinstance(node.op, ast.Pow) and (abs(right) > 10 or right != right.to_integral_value()):
            raise ValueError("乘方只支持 -10 到 10 的整数指数")
        return _CALC_OPS[type(node.op)](left, right)
    raise ValueError("只支持数字、+ - * / % ** 和括号")


def calculate(expression: str) -> str:
    expr = str(expression or "")
    for a, b in (("（", "("), ("）", ")"), ("×", "*"), ("÷", "/"), ("－", "-"), ("＋", "+"), ("x", "*"), ("X", "*")):
        expr = expr.replace(a, b)
    expr = re.sub(r"(?<=\d),(?=\d{3})", "", expr)                      # 千分位
    expr = re.sub(r"(\d+(?:\.\d+)?)%(?![\d(.])", r"(\1/100)", expr)     # 5% → 0.05
    try:
        value = _calc_node(ast.parse(expr.strip(), mode="eval"))
    except (SyntaxError, ValueError) as e:
        return f"无法计算：{e if isinstance(e, ValueError) else '表达式格式不对'}"
    except (DivisionByZero, InvalidOperation, ZeroDivisionError):
        return "无法计算：除数为 0 或结果无效"
    text = format(value.quantize(Decimal("0.00000001")).normalize(), "f") if value == value else "NaN"
    return f"{expression} = {text}"


# ---------------------------------------------------------------------------
# 工具集
# ---------------------------------------------------------------------------

def _tool(name, label_, description, schema, handler, opt_in=False) -> dict:
    return {"name": name, "label": label_, "description": description, "schema": schema,
            "parameters": schema.to_json(), "handler": handler, "opt_in": opt_in}


ROOM = S.string().description("微信群 roomId 或群名，可选")
QUOTE_FILTERS = {"room": ROOM,
                 "supplier": S.string().description("供应商完整名称，精确匹配"),
                 "product": S.string().description("产品关键词"),
                 "limit": S.integer().min(1).max(50).description("最多返回条数")}


def build_qa_tools(store, quote_log, device: str | None = None, *, sender=None,
                   opt_in: bool = False) -> list[dict]:
    """Expose historical records, replayed state, comparison, evidence, chat history and helpers.

    sender(room_id, room_label, text="", image_path="", server_image="", make_image=None) -> str：提供时加入
    发送消息/图片工具（opt_in，只给微信设备 Agent）。opt_in=False 时不返回任何 opt_in 工具。
    """

    path = getattr(quote_log, "path", None)
    ledger = shared_ledger(os.path.join(os.path.dirname(path), "events.jsonl") if path else None)

    def room_of(params, must_exist=False):
        return resolve_room(store, params.get("room"), device, must_exist)

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
        room, err = room_of(params)
        if err:
            return err
        kw = str(params.get("keyword") or "").strip().lower()
        limit = _clamp(params.get("limit"), 10, 1, 30)
        rows = all_rows(room)
        picked = [r for r in rows if (
            not kw or kw in " ".join(str(r.get(k) or "") for k in
                                     ("供应商", "项目", "备注", "sender", "roomName", "单位", "价格", "details")).lower())]
        return rows_text(picked[-limit:]) or "没有找到匹配的报价。"

    def latest_quotes(params: dict) -> str:
        room, err = room_of(params)
        if err:
            return err
        limit = _clamp(params.get("limit"), 10, 1, 30)
        rows = all_rows(room)
        return rows_text(rows[-limit:]) or "还没有报价记录。"

    def current_quotes(params: dict) -> str:
        room, err = room_of(params)
        if err:
            return err
        rows = ledger.current(room, device)
        if params.get("supplier"):
            rows = [row for row in rows if row["供应商"] == params["supplier"]]
        if params.get("product"):
            rows = [row for row in rows if params["product"] in row["项目"]]
        rows.sort(key=lambda row: row.get("time") or 0, reverse=True)
        return json.dumps({"quotes": rows[:_clamp(params.get("limit"), 20, 1, 50)],
                           "note": "active只表示未被后续事件替代；采购前仍需核实日期及供货。历史六字段记录需重新提取。"}, ensure_ascii=False)

    def compare(params: dict) -> str:
        room, err = room_of(params)
        if err:
            return err
        if params.get("as_of"):
            at = timestamp(params["as_of"])
            if not at:
                return json.dumps({"error": "as_of日期无效"}, ensure_ascii=False)
            rows = ledger.at(at, room, device)
        else:
            rows = ledger.current(room, device)
        if not rows:
            return json.dumps({"groups": [], "needs_confirmation": [],
                               "note": "没有结构化报价事件；旧报价需通过历史回填重新提取，不能只按价格字符串排名。"}, ensure_ascii=False)
        return json.dumps(compare_quotes(rows, {**params, "room": room} if room else params), ensure_ascii=False)

    def quote_history(params: dict) -> str:
        room, err = room_of(params)
        if err:
            return err
        events = ledger.history(str(params.get("supplier") or ""), str(params.get("product") or ""),
                                room, _clamp(params.get("limit"), 20, 1, 50), device)
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

    def list_rooms(params: dict) -> str:
        if not store:
            return "存储不可用。"
        kw = str(params.get("keyword") or "").strip()
        rooms = [r for r in store.rooms(device) if not kw or kw in room_name(r) or kw in r["roomId"]]
        if not rooms:
            return "没有匹配的群。" if kw else "还没有群消息。"
        limit = params.get("limit") or 20
        lines = [f"{room_name(r)}｜{r['roomId']}｜{r['count']} 条｜最近 {label(r['lastAt'])}" for r in rooms[:limit]]
        more = f"\n（另有 {len(rooms) - limit} 个群未列出）" if len(rooms) > limit else ""
        return "群名｜roomId｜消息数｜最近活跃\n" + "\n".join(lines) + more

    def recent_messages(params: dict) -> str:
        if not store:
            return "存储不可用。"
        room, err = room_of(params, must_exist=True)
        if err or not room:
            return err or "需要指定群（room）。"
        since = int(time.time() * 1000) - params["minutes"] * 60000 if params.get("minutes") else 0
        rows = store.query_messages(room=room, device=device, sender=params.get("sender") or "",
                                    since_ms=since, limit=params.get("limit") or 20)
        if not rows:
            return "这个群在指定范围内没有消息。"
        return "\n".join(message_line(r) for r in reversed(rows))

    def search_messages(params: dict) -> str:
        if not store:
            return "存储不可用。"
        room, err = room_of(params, must_exist=True)
        if err:
            return err
        since = int(time.time() * 1000) - (params.get("days") or 7) * 86400000
        rows = store.query_messages(room=room, device=device, keyword=params["keyword"],
                                    sender=params.get("sender") or "", since_ms=since, limit=params.get("limit") or 20)
        if not rows:
            return "没有找到匹配的聊天记录。"
        return "\n".join(message_line(r, with_room=room is None) for r in reversed(rows))

    def list_suppliers(params: dict) -> str:
        room, err = room_of(params)
        if err:
            return err
        product = str(params.get("product") or "").strip()
        groups: dict[str, dict] = {}
        for row in ledger.current(room, device):
            if product and product not in str(row.get("项目") or ""):
                continue
            name = str(row.get("供应商") or "").strip() or "（未注明供应商）"
            g = groups.setdefault(name, {"supplier": name, "active": 0, "needs_confirmation": 0,
                                         "unavailable": 0, "products": [], "latest": 0})
            g[row["status"]] = g.get(row["status"], 0) + 1
            item = str(row.get("项目") or "").strip()
            if item and item not in g["products"]:
                g["products"].append(item)
            g["latest"] = max(g["latest"], row.get("time") or 0)
        rows = sorted(groups.values(), key=lambda g: g["latest"], reverse=True)[:params.get("limit") or 30]
        for g in rows:
            g["latest"] = label(g["latest"])
            g["products"] = g["products"][:8]
        return json.dumps({"suppliers": rows, "note": "按当前未被替代的结构化报价统计；不代表报价仍有效。"},
                          ensure_ascii=False) if rows else "没有结构化报价记录。"

    tools = [
        _tool("search_quotes", "查报价", "按关键词检索历史报价记录（供应商/项目/备注/发送人/单位），历史记录不代表当前有效。",
              S.object({"keyword": S.string().description("检索关键词"),
                        "limit": S.integer().min(1).max(30).description("最多返回条数，默认 10"),
                        "room": ROOM}), search_quotes),
        _tool("latest_quotes", "最近报价", "返回最近追加的历史报价记录，不代表当前有效。参数 limit 1-30 条数，默认 10。",
              S.object({"limit": S.integer().min(1).max(30).description("最多返回条数，默认 10"), "room": ROOM}),
              latest_quotes),
        _tool("room_stats", "群聊统计", "查询群聊捕获统计：消息总数、图片数、活跃群列表。无参数。", S.object({}), room_stats),
        _tool("current_quotes", "报价状态", "查询当前未被替代的结构化报价方案，含条件和待核实标记。active不代表实时有效。",
              S.object(QUOTE_FILTERS), current_quotes),
        _tool("quote_history", "报价历史", "查看原始报价事件及调价、拒价、库存变化、采购记录的规则处理结果。",
              S.object(QUOTE_FILTERS), quote_history),
        _tool("quote_evidence", "报价依据", "按quoteId取得原文、原始时间、状态、结构化条件和派生计算依据。",
              S.object({"quote_id": S.string().required()}), quote_evidence),
        _tool("compare_quotes", "比较报价", "按采购条件筛选和计算。仅组内价格可排名，返回可比较组、待确认和不符合要求的方案。缺失条件不默认满足；不要编造benchmark。",
              S.object({
                  "room": ROOM, "product": QUOTE_FILTERS["product"],
                  "destination": S.string().description("需要送达的地区"),
                  "pickup_region": S.string().description("自提地区，与destination选一"),
                  "tax": S.enum(["included", "excluded"]),
                  "brand": S.string(), "invoice_type": S.string(), "invoice_period": S.string(),
                  "payment": S.string(), "pricing_party": S.string(), "diameter_mm": S.number(),
                  "resistance_max": S.number(),
                  "resistance_unit": S.string().description("默认quote_code；不同单位不自动换算"),
                  "max_delivery_days": S.number().min(0), "quantity": S.number().min(0),
                  "quantity_unit": S.string(),
                  "currency": S.string().description("默认CNY"), "unit": S.string().description("默认元/吨"),
                  "as_of": S.string().description("历史比较时指定ISO时间；默认现在"),
                  "max_age_hours": S.number().description("报价最大年龄，默认24小时，最大720"),
                  "benchmark": S.object({
                      "name": S.string().required(), "contract": S.string(), "price": S.number().required(),
                      "at": S.string().required(), "source": S.string().required(),
                  }).description("仅使用有出处的基准（可用 futures_quote 查询）；缺基准则不能换算点价报价。"),
              }), compare),
        _tool("list_rooms", "群列表", "列出已接入的微信群：群名、roomId、消息数、最近活跃时间。其他工具的 room 参数可用这里的群名或 roomId。",
              S.object({"keyword": S.string().description("按群名或 roomId 过滤，可选"),
                        "limit": S.integer().min(1).max(50).default(20)}), list_rooms),
        _tool("recent_messages", "群最近消息", "读取某个群最近的聊天记录（按时间正序），可限定最近多少分钟、发送人。图片显示为 [图片 img=文件]。",
              S.object({"room": S.string().required().description("微信群 roomId 或群名"),
                        "limit": S.integer().min(1).max(50).default(20),
                        "minutes": S.integer().min(1).max(10080).description("只看最近 N 分钟，可选"),
                        "sender": S.string().description("发送人名称或 ID，可选")}), recent_messages),
        _tool("search_messages", "搜聊天记录", "按关键词搜索聊天记录正文，可限定群、发送人、最近天数（默认 7 天）。",
              S.object({"keyword": S.string().required().description("关键词"),
                        "room": ROOM, "sender": S.string().description("发送人名称或 ID，可选"),
                        "days": S.integer().min(1).max(90).default(7),
                        "limit": S.integer().min(1).max(50).default(20)}), search_messages),
        _tool("list_suppliers", "供应商列表", "按当前结构化报价统计供应商：有效/待确认/缺货方案数、涉及品类、最近报价时间。",
              S.object({"room": ROOM, "product": S.string().description("产品关键词，可选"),
                        "limit": S.integer().min(1).max(50).default(30)}), list_suppliers),
        _tool("calculate", "计算器", "精确计算四则运算（Decimal），用于差价、升贴水、加权、合计等，不要心算。支持 + - * / % ** 和括号，5% 表示 0.05。",
              S.object({"expression": S.string().max(200).required().description("如 (109900-300)*25.5")}),
              lambda params: calculate(params["expression"])),
    ]
    skills = [s["name"] for s in list_skills()]
    if skills:
        tools.append(_tool(
            "read_skill", "读取资料", "按需读取本地知识资料（Skill），如报价黑话、供应商话术、采购比较规则。"
            "只在解读看不懂的报价说法或做采购比较时读取；file 用于读取资料里列出的参考文件。",
            S.object({"name": S.enum(skills).required().description("资料名"),
                      "file": S.string().max(200).description("参考文件，如 references/phrases-and-cases.md，可选")}),
            lambda params: read_skill(params["name"], params.get("file"))))
    tools += [dict(t) for t in _ENV["net"]]
    if sender is not None:
        tools += _send_tools(store, device, sender, resolve=lambda p: room_of(p, must_exist=True))
    ctx = {"store": store, "quote_log": quote_log, "device": device, "ledger": ledger}
    taken = {t["name"] for t in tools}
    for spec in _ENV["plugins"]:
        if spec["name"] in taken:
            continue
        taken.add(spec["name"])
        tools.append({**spec, "handler": (lambda fn: lambda params: fn(params, ctx))(spec["handler"])})
    if not opt_in:
        tools = [t for t in tools if not t.get("opt_in")]
    return tools


IMAGE_PATH_RE = re.compile(r"^/(sdcard|storage)/[^\x00]+$")


def _send_tools(store, device, sender, resolve) -> list[dict]:
    def target(params):
        room, err = resolve(params)
        if err or not room:
            return None, None, err or "需要指定目标群（room）。"
        name = next((room_name(r) for r in store.rooms(device) if r["roomId"] == room), room) if store else room
        return room, name, None

    def send_message(params):
        room, name, err = target(params)
        if err:
            return err
        return sender(room, name, text=params["text"])

    def send_image(params):
        room, name, err = target(params)
        if err:
            return err
        path = params.get("image_path") or ""
        server_image = (params.get("server_image") or "").lstrip("/")
        if bool(path) == bool(server_image):
            return "image_path（手机上的图片路径）与 server_image（服务器上的图片文件）必须二选一。"
        if path and (not IMAGE_PATH_RE.match(path) or "/../" in path):
            return "image_path 必须是手机上的绝对路径，以 /sdcard/ 或 /storage/ 开头；不知道路径就请用户提供，不要猜。"
        if server_image and (store is None or not server_image.startswith("images/") or not store.image_path(server_image)):
            return f"服务器上没有图片 {server_image}（应为聊天记录里 [图片 img=…] 给出的文件）。"
        return sender(room, name, image_path=path, server_image=server_image)

    tools = [
        _tool("send_message", "发送消息", "经本设备的微信给某个群发一条文字消息（会真实发出）。只在用户明确要求发送/转发时使用，目标只能是本设备已接入的群；每轮最多 3 条。",
              S.object({"room": S.string().required().description("目标群 roomId 或群名"),
                        "text": S.string().max(1000).required().description("要发送的文字")}),
              send_message, opt_in=True),
        _tool("send_image", "发送图片", "经本设备的微信给某个群发一张图片（会真实发出）。image_path 为用户给出的手机路径（/sdcard/…）；"
              "server_image 为聊天记录里 [图片 img=…] 的服务器文件（需要设备模块支持服务器图片下发）。二选一。",
              S.object({"room": S.string().required().description("目标群 roomId 或群名"),
                        "image_path": S.string().max(500).description("手机上的图片绝对路径"),
                        "server_image": S.string().max(300).description("服务器图片文件，如 images/xxx.jpg")}),
              send_image, opt_in=True),
    ]
    maker = _ENV["cards"]
    if maker is None:
        return tools

    def send_card(params):
        room, name, err = target(params)  # store 为 None 时 resolve 已经报「没有找到群」
        if err:
            return err
        card = {k: v for k, v in params.items() if k != "room"}
        try:
            return sender(room, name, make_image=lambda: maker.make(card, store.data_dir))
        except CardError as e:
            return f"卡片格式不对：{e}。没有制图，也没有发送。"
        except CardFailed as e:
            print(f"[cards] 制图失败 room={room}: {e}", file=sys.stderr)
            return f"制图失败：{e}。没有发送，可以改用文字回复。"

    tools.append(_tool(
        "send_card", "发送数据卡片",
        "把表格数据做成图片卡片发到微信群（会真实发出，每轮最多 3 条，与发送消息共用额度）。只在用户要「做成图/发图」时用。"
        "sections 每节一张表，不能放在一起比较的方案（compare_quotes 的不同组、待确认）分节放；columns 声明列名与类型；"
        "rows 每行按列顺序给值。number/change 列填工具结果里的原始数字，不要加千分位或换算成「万」；"
        "time 列填工具返回的原始时间（毫秒时间戳或 ISO 时间）；差价、比值先用 calculate 算。",
        S.object({"room": S.string().required().description("目标群 roomId 或群名"), **CARD_FIELDS}, loose=True),
        send_card, opt_in=True))
    return tools


def _catalog_sender(*args, **kwargs):  # pragma: no cover - 只用于列目录
    return ""


def tool_catalog() -> list[dict]:
    """可供设备 Agent 勾选的工具（名称/标签/说明/是否需手动开启），给网页设备设置用。"""
    return [{"name": t["name"], "label": t.get("label") or t["name"], "description": t.get("description") or "",
             "optIn": bool(t.get("opt_in"))}
            for t in build_qa_tools(None, None, sender=_catalog_sender, opt_in=True)]
