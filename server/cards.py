#!/usr/bin/env python3
"""数据卡片：把 Agent 给出的表格做成 PNG 图片，供 send_card 发到微信群。

    maker = build_maker(agent_cfg)                # None：未启用或找不到 Chrome
    made = maker.make(params, store.data_dir)     # → Made(rel="images/cards/20261023/<hash>.png", rows=3)

- 卡片 = 标题 + 1~4 节；每节一张表：columns 声明列名与列类型，rows 按列顺序给值。
  一节里的行可以互相比较，不能比较的方案（compare_quotes 的不同组、待确认）分节放。
- parse_card 是唯一的入口检查：schema 规范化后的参数 → 冻结的 Card，之后的代码只信类型。
- 列类型（text / number / change / time）决定单元格怎么解析、怎么显示，规则只在 KINDS 一处：
  模型填原始数字和工具返回的原始时间，千分位、涨跌颜色、北京时间都由这里统一处理。
- card_html 是纯函数：同一张卡片在同一分钟生成同样的 HTML，文件名取 HTML 的哈希，重试不会重画。
- 截图交给 headless_chrome（只在 build_maker 里引入）；其余部分只认 render(html, element_id) → PNG。

命令行自测（要本机 Chrome，用来检查中文字体）：
    python -m server.cards selftest /tmp/card.png [chrome 路径]
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import html
import math
import os
import re
import secrets
import shutil
import struct
import sys
import time
from decimal import Decimal
from typing import Callable, Union

from .tool_schema import S

CARD_TZ = dt.timezone(dt.timedelta(hours=8))  # 卡片上的时间一律按北京时间显示
CARD_ELEMENT_ID = "card"
MAX_ROWS = 40
MAX_CELL_CHARS = 40
MAX_PROBLEMS = 8
WIDTHS = ((4, 640), (6, 760), (8, 900))  # 最宽一节的列数 → 卡片宽度（CSS px）

Cell = Union[Decimal, int, str, None]
Render = Callable[[str, str], bytes]  # (html, element_id) → PNG 字节


class CardError(ValueError):
    """模型能改的问题（未知字段、格数不对、单元格类型不对）；消息原样回给模型。"""


class CardFailed(RuntimeError):
    """服务端失败（Chrome 或写盘）；同样的参数重试也没用。"""


# ---------------------------------------------------------------------------
# 列类型：解析与显示
# ---------------------------------------------------------------------------

NUMBER_RE = re.compile(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
ISO_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?\s*(Z|[+-]\d{2}:?\d{2})?")
EPOCH_MS = (10 ** 12, 41 * 10 ** 11)  # 2001-09 ~ 2099-12 的毫秒时间戳
_TEXT = S.string()  # 数字转文字与字符串参数同一规则（无损）


def _verbatim(text: str) -> str:
    if len(text) > MAX_CELL_CHARS:
        raise ValueError(f"超过 {MAX_CELL_CHARS} 字")
    return text


def _parse_text(value) -> str:
    return _verbatim(_TEXT(value))


def _parse_number(value) -> Decimal | str:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("不是有限数字")
        return Decimal(repr(value))
    if isinstance(value, int):
        return Decimal(value)
    text = value.strip()
    if NUMBER_RE.fullmatch(text):
        return Decimal(text.replace(",", ""))
    return _verbatim(text)  # 沪铜2611+550、电议：原样显示


def _parse_time(value) -> int | str:
    if isinstance(value, (int, float)):
        if not EPOCH_MS[0] <= value < EPOCH_MS[1]:
            raise ValueError(f"应填毫秒时间戳或 ISO 时间，收到 {value!r}")
        return int(value)
    m = ISO_RE.fullmatch(value.strip())
    if not m:
        return _verbatim(value.strip())
    year, month, day, hour, minute, second, zone = m.groups()
    if zone is None:
        tz = CARD_TZ
    elif zone == "Z":
        tz = dt.timezone.utc
    else:
        offset = dt.timedelta(hours=int(zone[1:3]), minutes=int(zone[-2:]))
        tz = dt.timezone(-offset if zone[0] == "-" else offset)
    try:
        at = dt.datetime(int(year), int(month), int(day), int(hour), int(minute), int(second or 0), tzinfo=tz)
    except ValueError:
        raise ValueError(f"不是有效的时间：{value}") from None
    return (at - dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)) // dt.timedelta(milliseconds=1)


def _grouped(d: Decimal) -> str:
    """千分位；小数位按给的显示，整数值不带 .0。"""
    if d == d.to_integral_value():
        d = d.to_integral_value() if d else Decimal(0)
    return format(d, ",f")


def _show_text(v) -> tuple[str, str]:
    return "", v


def _show_number(v) -> tuple[str, str]:
    return ("", _grouped(v)) if isinstance(v, Decimal) else ("", v)


def _show_change(v) -> tuple[str, str]:
    if not isinstance(v, Decimal):
        return "", v
    if v > 0:
        return "up", "+" + _grouped(v)
    return ("down" if v < 0 else "flat"), _grouped(v)


def _show_time(v) -> tuple[str, str]:
    if isinstance(v, int):
        return "", dt.datetime.fromtimestamp(v / 1000, CARD_TZ).strftime("%m-%d %H:%M")
    return "", v


@dataclasses.dataclass(frozen=True)
class Kind:
    parse: Callable[[object], Cell]           # 单元格原值（str/int/float）→ Cell；不合法抛 ValueError（消息给模型看）
    show: Callable[[Cell], tuple[str, str]]   # 非 None 的 Cell → (颜色 class, 显示文字)
    right: bool                               # 表头与单元格右对齐、等宽数字


KINDS: dict[str, Kind] = {
    "text": Kind(_parse_text, _show_text, right=False),
    "number": Kind(_parse_number, _show_number, right=True),
    "change": Kind(_parse_number, _show_change, right=True),
    "time": Kind(_parse_time, _show_time, right=True),
}

# ---------------------------------------------------------------------------
# 输入形状：同一份 schema 既发给模型，也是 parse_card 判断未知字段的依据。
# 对象都是 loose=True，未知字段才能到 parse_card 被指出来，而不是被 schema 悄悄丢掉。
# ---------------------------------------------------------------------------

COLUMN_FIELDS = {
    "name": S.string().max(12).required().description("列名，如 供应商、价格"),
    "kind": S.enum(list(KINDS)).default("text").description(
        "text 文字；number 数字；change 涨跌（正红负绿）；time 时间（填工具返回的原始时间）"),
    "unit": S.string().max(8).description("单位，显示在列名后，如 元/吨、%"),
}
SECTION_FIELDS = {
    "heading": S.string().max(40).description("本节标题，如 compare_quotes 的组条件、待确认"),
    "columns": S.array(S.object(COLUMN_FIELDS, loose=True)).max(8).required(),
    "rows": S.array(S.array(S.any()).max(8)).max(30).required().description(
        "每行一个数组，按 columns 顺序给值；缺失填 null"),
}
CARD_FIELDS = {
    "title": S.string().max(30).required().description("卡片标题，如 电解铜报价"),
    "subtitle": S.string().max(60).description("副标题：口径或条件，如 送到宜兴 · 含税"),
    "sections": S.array(S.object(SECTION_FIELDS, loose=True)).max(4).required().description(
        "1-4 节，每节一张表；不能放在一起比较的方案分节放"),
    "note": S.string().max(120).description("备注，显示在表格下方"),
}


@dataclasses.dataclass(frozen=True)
class Column:
    name: str
    kind: str  # KINDS 的键
    unit: str = ""


@dataclasses.dataclass(frozen=True)
class Section:
    heading: str
    columns: tuple[Column, ...]
    rows: tuple[tuple[Cell, ...], ...]  # 每行与 columns 等长，单元格按列的 kind 解析过


@dataclasses.dataclass(frozen=True)
class Card:
    title: str
    subtitle: str
    sections: tuple[Section, ...]  # 1~4 节，合计不超过 MAX_ROWS 行
    note: str


def parse_card(params: dict) -> Card:
    """入口检查：schema 规范化后的参数（不含 room）→ Card；有问题就把前 8 处一起写进 CardError。

    schema 已经管了字段类型、长度和个数上限；这里补它表达不了的：未知字段、至少 1 节 / 1 列 / 1 行、
    每行格数等于列数、按列类型解析单元格、全卡合计行数。
    """
    problems: list[str] = []
    _unknown(params, CARD_FIELDS, "卡片", problems)
    raw_sections = params.get("sections") or []
    if not raw_sections:
        problems.append("sections 至少要有 1 节")
    sections = []
    for i, raw in enumerate(raw_sections, 1):
        where = f"第{i}节"
        _unknown(raw, SECTION_FIELDS, where, problems)
        columns = []
        for j, col in enumerate(raw.get("columns") or [], 1):
            _unknown(col, COLUMN_FIELDS, f"{where}第{j}列", problems)
            columns.append(Column(col["name"], col["kind"], col.get("unit", "")))
        if not columns:
            problems.append(f"{where}没有 columns")
        if not raw.get("rows"):
            problems.append(f"{where}没有 rows")
        rows = []
        for r, row in enumerate(raw.get("rows") or [], 1):
            if len(row) != len(columns):
                problems.append(f"{where}第{r}行有 {len(row)} 格，但 columns 有 {len(columns)} 列")
                continue
            rows.append(tuple(_cell(value, col, f"{where}第{r}行「{col.name}」", problems)
                              for value, col in zip(row, columns)))
        sections.append(Section(raw.get("heading", ""), tuple(columns), tuple(rows)))
    total = sum(len(raw.get("rows") or []) for raw in raw_sections)
    if total > MAX_ROWS:
        problems.append(f"全卡合计 {total} 行，最多 {MAX_ROWS} 行")
    if problems:
        more = f"（另有 {len(problems) - MAX_PROBLEMS} 处）" if len(problems) > MAX_PROBLEMS else ""
        raise CardError("；".join(problems[:MAX_PROBLEMS]) + more)
    return Card(params["title"], params.get("subtitle", ""), tuple(sections), params.get("note", ""))


def _unknown(obj: dict, fields: dict, where: str, problems: list[str]):
    extra = [k for k in obj if k not in fields]
    if extra:
        problems.append(f"{where}有未知字段 {'、'.join(extra)}，可用：{'、'.join(fields)}")


def _cell(value, column: Column, where: str, problems: list[str]) -> Cell:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        got = "true/false" if isinstance(value, bool) else {list: "数组", dict: "对象"}.get(type(value), "其他类型")
        problems.append(f"{where}应填文字、数字或 null，收到 {got}")
        return None
    try:
        return KINDS[column.kind].parse(value)
    except ValueError as e:
        problems.append(f"{where}{e}")
        return None


# ---------------------------------------------------------------------------
# HTML（纯函数）
# ---------------------------------------------------------------------------

CSP = """<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">"""
STYLE = """
*{box-sizing:border-box}
body{margin:0;background:#fff;color:#1f2329;font-family:"PingFang SC","Hiragino Sans GB","Noto Sans CJK SC","Source Han Sans SC","Microsoft YaHei",sans-serif}
#card{padding:24px 28px 20px;background:#fff}
h1{font-size:22px;line-height:30px;margin:0}
.sub{color:#8f959e;font-size:13px;margin-top:4px}
section{margin-top:18px}
h2{font-size:15px;font-weight:600;margin:0 0 8px}
table{border-collapse:collapse;width:100%;font-size:15px}
th{background:#f2f3f5;color:#646a73;font-weight:500;text-align:left;padding:8px 10px}
td{padding:9px 10px;border-bottom:1px solid #eef0f2;overflow-wrap:anywhere}
.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.up{color:#d83931}.down{color:#2ea121}.flat,.nil{color:#8f959e}
.note{color:#646a73;font-size:13px;line-height:20px;margin-top:14px}
.foot{color:#8f959e;font-size:12px;margin-top:12px}
"""


def card_html(card: Card, made_ms: int) -> str:
    """自包含的 HTML，<body> 里只有 #card；模型给的文字全部经 html.escape。CSP 禁止任何外部请求。

    页脚「生成于 …」是卡片上唯一由我们写的文字；同样的卡片和分钟得到同样的字符串。
    """
    widest = max(len(s.columns) for s in card.sections)
    width = next(w for n, w in WIDTHS if widest <= n)
    parts = [_tag("h1", "", card.title)]
    if card.subtitle:
        parts.append(_tag("div", "sub", card.subtitle))
    parts += [_section_html(s) for s in card.sections]
    if card.note:
        parts.append(_tag("div", "note", card.note))
    stamp = dt.datetime.fromtimestamp(made_ms / 1000, CARD_TZ).strftime("%Y-%m-%d %H:%M")
    parts.append(_tag("div", "foot", f"生成于 {stamp}"))
    return (f'<!doctype html><html><head><meta charset="utf-8">{CSP}<style>{STYLE}#card{{width:{width}px}}</style>'
            f'</head><body><div id="{CARD_ELEMENT_ID}">{"".join(parts)}</div></body></html>')


def _section_html(section: Section) -> str:
    kinds = [KINDS[c.kind] for c in section.columns]
    head = "".join(_tag("th", "n" if k.right else "", c.name + (f"（{c.unit}）" if c.unit else ""))
                   for c, k in zip(section.columns, kinds))
    body = "".join("<tr>" + "".join(_td(v, k) for v, k in zip(row, kinds)) + "</tr>" for row in section.rows)
    heading = _tag("h2", "", section.heading) if section.heading else ""
    return f"<section>{heading}<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></section>"


def _td(value: Cell, kind: Kind) -> str:
    tone, text = ("nil", "—") if value is None else kind.show(value)
    return _tag("td", " ".join(c for c in ("n" if kind.right else "", tone) if c), text)


def _tag(name: str, classes: str, text: str) -> str:
    attr = f' class="{classes}"' if classes else ""
    return f"<{name}{attr}>{html.escape(text)}</{name}>"


# ---------------------------------------------------------------------------
# 出图
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Made:
    rel: str  # 相对数据目录，如 images/cards/20261023/<sha256(html)[:20]>.png
    rows: int

    def summary(self) -> str:
        return f"卡片 {self.rel}：{self.rows} 行。"


class CardMaker:
    """params → Card → HTML → PNG 文件，存在 <data_dir>/images/cards/<YYYYMMDD>/ 下（/images/ 已带鉴权）。"""

    def __init__(self, render: Render, *, clock: Callable[[], float] = time.time):
        self._render = render
        self._clock = clock

    def make(self, params: dict, data_dir: str) -> Made:
        """CardError：模型能改的格式问题；CardFailed：制图或写盘失败。同一份 HTML 的文件已在就不再截图。"""
        card = parse_card(params)
        made_ms = int(self._clock() * 1000)
        doc = card_html(card, made_ms)
        day = dt.datetime.fromtimestamp(made_ms / 1000, CARD_TZ).strftime("%Y%m%d")
        rel = f"images/cards/{day}/{hashlib.sha256(doc.encode('utf-8')).hexdigest()[:20]}.png"
        full = os.path.join(data_dir, *rel.split("/"))
        if not os.path.isfile(full):
            try:
                png = self._render(doc, CARD_ELEMENT_ID)
            except Exception as e:
                raise CardFailed(str(e)) from e
            _write_atomic(full, png)
        return Made(rel, sum(len(s.rows) for s in card.sections))


def _write_atomic(path: str, data: bytes):
    """先写同目录的临时文件再 os.replace：手机只会取到完整的图。"""
    tmp = f"{path}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise CardFailed(f"保存卡片失败：{e}") from e


def build_maker(agent_cfg: dict | None, *, which=shutil.which) -> CardMaker | None:
    """按 agent.config.json 的 card 段生成 CardMaker；没有 card 段＝启用并自动找 Chrome。

    agent_cfg 为 None（没配置 Agent）或 card.enabled 为 false 时返回 None；找不到 Chrome 也返回 None，并打一行原因。
    """
    if agent_cfg is None:
        return None
    cfg = agent_cfg.get("card") or {}
    if not cfg.get("enabled", True):
        return None
    from .headless_chrome import HeadlessChrome, find_chrome, sweep_profiles
    configured = str(cfg.get("chrome") or "")
    binary = find_chrome(configured, which)
    if binary is None:
        print(f"[cards] card.chrome={configured} 不可执行，不提供 send_card" if configured else
              "[cards] 没找到 Chrome/Chromium，不提供 send_card；可在 agent.config.json 的 card.chrome 指定路径",
              file=sys.stderr)
        return None
    sweep_profiles()
    print(f"[cards] send_card 可用，Chrome：{binary}")
    return CardMaker(HeadlessChrome(binary, timeout_s=float(cfg.get("timeout") or 20)))


SAMPLE = {
    "title": "电解铜报价（自测）",
    "subtitle": "送到宜兴 · 含税",
    "sections": [
        {"heading": "一口价 · 金川",
         "columns": [{"name": "供应商"}, {"name": "价格", "kind": "number", "unit": "元/吨"},
                     {"name": "较昨日", "kind": "change"}, {"name": "报价时间", "kind": "time"}],
         "rows": [["富利铜业", 111200, 300, 1792726800000],
                  ["湖北桦昌", "沪铜2611+550", -100, "2026-10-23T09:12:00+08:00"],
                  ["<script>alert(1)</script>", 110650.5, 0, None]]},
        {"heading": "期货行情",
         "columns": [{"name": "品种"}, {"name": "最新价", "kind": "number"}, {"name": "涨跌幅", "kind": "change", "unit": "%"},
                     {"name": "涨跌", "kind": "change"}, {"name": "行情时间", "kind": "time"}],
         "rows": [["沪铜2611", 110650, -0.32, -350, "2026-10-23 15:27:58"],
                  ["伦铜 3M", 14448.38, 0.28, 40.88, "收盘价"]]},
    ],
    "note": "点价报价未换算，不能直接比价",
}


def _main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] != "selftest":
        print(__doc__)
        return 2
    from .headless_chrome import HeadlessChrome, RenderError, find_chrome
    binary = find_chrome(argv[2] if len(argv) > 2 else "")
    if binary is None:
        print("没找到 Chrome/Chromium（可在参数里给出路径）", file=sys.stderr)
        return 1
    doc = card_html(parse_card(S.object(CARD_FIELDS, loose=True)(SAMPLE)), int(time.time() * 1000))
    started = time.monotonic()
    try:
        png = HeadlessChrome(binary)(doc, CARD_ELEMENT_ID)
    except RenderError as e:
        print(f"制图失败：{e}", file=sys.stderr)
        return 1
    with open(argv[1], "wb") as f:
        f.write(png)
    width, height = struct.unpack(">II", png[16:24])
    print(f"{argv[1]}：{width}×{height} px，{len(png) // 1024} KB，{time.monotonic() - started:.2f} 秒（{binary}）")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
