#!/usr/bin/env python3
"""联网类 Agent 工具：网页搜索（Tavily）与期货行情（新浪财经）。纯标准库实现。

    tools = build_net_tools({"web_search": {"provider": "tavily", "api_key": "tvly-..."}, "futures": {"enabled": True}})

- web_search / web_extract：Tavily /search 与 /extract。读网页正文也交给 Tavily，
  本服务不直接抓任意 URL（避免被诱导访问内网地址）。未配置 api_key 时不提供这两个工具。
- futures_quote：新浪 hq.sinajs.cn 的期货行情（非官方免费接口，需 Referer，GBK 编码）。
  上期所品种用「品种+月份」（CU2611 / 沪铜2611），「CU0 / 沪铜主力」为主力连续；LME 用「伦铜」等。
  输出里的 benchmark 字段可直接作为 compare_quotes 的 benchmark（名称/合约/价格/时间/来源）。

命令行自测（会联网）：
    python -m server.net_tools futures CU2611
    TAVILY_API_KEY=tvly-... python -m server.net_tools search "沪铜 今日 现货升贴水"
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

from .tool_schema import S

CN_TZ = dt.timezone(dt.timedelta(hours=8))
TAVILY_URL = "https://api.tavily.com"
SINA_URL = "https://hq.sinajs.cn/list="
SINA_SOURCE = "新浪财经 hq.sinajs.cn"
FRESH_MS = 5 * 60 * 1000  # compare_quotes 要求基准在比较时点前 5 分钟内

# 上期所 / 上期能源 / 广期所常用品种：代码 → 基准名（与报价提取的 benchmark 写法一致）
SHFE = {"CU": "沪铜", "AL": "沪铝", "ZN": "沪锌", "PB": "沪铅", "NI": "沪镍", "SN": "沪锡",
        "AU": "沪金", "AG": "沪银", "AO": "氧化铝", "BC": "国际铜", "RB": "螺纹钢", "HC": "热卷",
        "SS": "不锈钢", "SI": "工业硅"}
LME = {"CAD": "伦铜", "AHD": "伦铝", "ZSD": "伦锌", "PBD": "伦铅", "NID": "伦镍", "SND": "伦锡"}
ALIASES = {name: code for code, name in SHFE.items()}
ALIASES.update({name[1:]: code for code, name in SHFE.items() if name.startswith("沪")})
ALIASES.update({"国际铜": "BC", "硅": "SI"})
LME_ALIASES = {name: code for code, name in LME.items()}
LME_ALIASES.update({"LME" + name[1:]: code for code, name in LME.items()})


class NetToolError(RuntimeError):
    pass


def _http(url: str, *, data: dict | None = None, headers: dict | None = None, timeout: float = 15) -> bytes:
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers={
        **({"Content-Type": "application/json"} if body is not None else {}), **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        try:
            detail = e.read()[:200].decode("utf-8", "replace")
        finally:
            e.close()
        raise NetToolError(f"HTTP {e.code}: {detail}") from e
    except Exception as e:
        raise NetToolError(f"{type(e).__name__}: {e}") from e


# ---------------------------------------------------------------------------
# 期货行情（新浪）
# ---------------------------------------------------------------------------

def parse_symbol(text: str) -> dict:
    """「CU2611 / 沪铜2611 / cu0 / 沪铜主力 / 伦铜 / nf_CU2611」→ {code, name, contract}。"""
    raw = re.sub(r"\s+", "", str(text or ""))
    if not raw:
        raise NetToolError("需要品种或合约，例如 CU2611、沪铜主力、伦铜")
    upper = raw.upper()
    if upper.startswith(("NF_", "HF_")):
        body = raw[3:].upper()
        if upper.startswith("HF_"):
            return {"code": "hf_" + body, "name": LME.get(body, body), "contract": "LME3M"}
        m = re.match(r"^([A-Z]{1,2})(\d{4}|0)$", body)
        if not m:
            raise NetToolError(f"无法识别的新浪代码 {raw}")
        return _domestic(m.group(1), m.group(2))
    lme = LME_ALIASES.get(raw) or LME_ALIASES.get(upper) or (upper if upper in LME else None)
    if lme:
        return {"code": "hf_" + lme, "name": LME[lme], "contract": "LME3M"}
    m = re.match(r"^([A-Za-z]{1,2}|[一-龥]{1,4}?)(\d{4})?(主力|连续|0)?$", raw)
    if not m:
        raise NetToolError(f"无法识别的品种/合约 {raw}，示例：CU2611、沪铜主力、伦铜")
    variety = m.group(1).upper() if m.group(1).isascii() else ALIASES.get(m.group(1))
    if not variety or variety not in SHFE:
        raise NetToolError(f"不支持的品种 {m.group(1)}，支持：{'、'.join(SHFE.values())}；LME：{'、'.join(LME.values())}")
    return _domestic(variety, m.group(2) or "0")


def _domestic(variety: str, month: str) -> dict:
    if variety not in SHFE:
        raise NetToolError(f"不支持的品种 {variety}")
    contract = "主力连续" if month == "0" else month
    return {"code": f"nf_{variety}{month}", "name": SHFE[variety], "contract": contract}


def _num(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def parse_sina(code: str, text: str) -> dict:
    """解析一行 var hq_str_<code>="...";。空串表示代码无效（如已到期合约）。"""
    m = re.search(r'hq_str_%s="([^"]*)"' % re.escape(code), text)
    if not m or not m.group(1).strip():
        raise NetToolError(f"新浪没有 {code} 的行情（合约可能已到期或代码不对）")
    f = m.group(1).split(",")
    if code.startswith("nf_"):
        if len(f) < 18:
            raise NetToolError(f"{code} 行情字段不完整")
        hhmmss = f[1].zfill(6)
        at = dt.datetime.strptime(f"{f[17]} {hhmmss}", "%Y-%m-%d %H%M%S").replace(tzinfo=CN_TZ)
        last, prev = _num(f[8]), _num(f[10])
        return {"title": f[0], "price": last, "open": _num(f[2]), "high": _num(f[3]), "low": _num(f[4]),
                "bid": _num(f[6]), "ask": _num(f[7]), "prevSettle": prev, "openInterest": _num(f[13]),
                "volume": _num(f[14]), "at": at,
                "change": round(last - prev, 3) if last is not None and prev else None,
                "changePct": round((last - prev) / prev * 100, 2) if last is not None and prev else None}
    if len(f) < 14:
        raise NetToolError(f"{code} 行情字段不完整")
    at = dt.datetime.strptime(f"{f[12]} {f[6]}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN_TZ)
    last, prev = _num(f[0]), _num(f[7])
    return {"title": f[13], "price": last, "open": _num(f[8]), "high": _num(f[4]), "low": _num(f[5]),
            "bid": _num(f[2]), "ask": _num(f[3]), "prevSettle": prev, "at": at,
            "change": round(last - prev, 3) if last is not None and prev else None,
            "changePct": round((last - prev) / prev * 100, 2) if last is not None and prev else None}


def futures_quote(symbol: str, *, fetch=None, now_ms: int | None = None) -> dict:
    spec = parse_symbol(symbol)
    fetch = fetch or (lambda code: _http(SINA_URL + code, headers={
        "Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}, timeout=10).decode("gbk", "replace"))
    data = parse_sina(spec["code"], fetch(spec["code"]))
    if data["price"] is None or data["price"] <= 0:
        raise NetToolError(f"{spec['code']} 没有有效成交价")
    at_ms = int(data["at"].timestamp() * 1000)
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    fresh = 0 <= now_ms - at_ms <= FRESH_MS
    source = f"{SINA_SOURCE} {spec['code']}"
    out = {"symbol": spec["code"], "name": spec["name"], "contract": spec["contract"], "title": data["title"],
           **{k: v for k, v in data.items() if k not in ("title", "at")},
           "at": data["at"].isoformat(), "source": source, "fresh": fresh}
    if spec["contract"] in ("主力连续", "LME3M"):
        out["note"] = "主力连续/LME 行情不对应具体合约月份，不能当作某个合约的基准；需要换算时请指定合约月份，如 CU2611。"
    else:
        out["benchmark"] = {"name": spec["name"], "contract": spec["contract"], "price": data["price"],
                            "at": out["at"], "source": source}
        out["note"] = ("行情在 5 分钟内，可把 benchmark 原样传给 compare_quotes。" if fresh else
                       "最新成交不在 5 分钟内（可能休市），不能用于换算「现在」的价格；历史比较请把 as_of 设为行情时间。")
    return out


# ---------------------------------------------------------------------------
# 网页搜索（Tavily）
# ---------------------------------------------------------------------------

class Tavily:
    def __init__(self, api_key: str, *, max_results: int = 5, timeout: float = 20, post=None):
        self.api_key = api_key
        self.max_results = max_results
        self.timeout = timeout
        self._post = post or (lambda path, body: json.loads(_http(
            TAVILY_URL + path, data=body, headers={"Authorization": "Bearer " + self.api_key},
            timeout=self.timeout).decode("utf-8")))

    def search(self, query: str, max_results: int | None = None, days: int | None = None,
               topic: str = "general") -> dict:
        body = {"query": query, "max_results": max_results or self.max_results, "topic": topic,
                "search_depth": "basic", "include_answer": False}
        if days:
            body["days"] = days
            body["topic"] = "news"
        data = self._post("/search", body)
        return {"query": query, "results": [
            {"title": r.get("title"), "url": r.get("url"), "content": str(r.get("content") or "")[:800],
             **({"published": r["published_date"]} if r.get("published_date") else {})}
            for r in data.get("results") or []]}

    def extract(self, url: str, max_chars: int = 4000) -> dict:
        data = self._post("/extract", {"urls": [url]})
        results = data.get("results") or []
        if not results:
            failed = data.get("failed_results") or []
            raise NetToolError(f"读取失败：{(failed[0].get('error') if failed else '') or '没有内容'}")
        text = str(results[0].get("raw_content") or "")
        return {"url": results[0].get("url") or url, "content": text[:max_chars],
                **({"truncated": len(text)} if len(text) > max_chars else {})}


def build_net_tools(cfg: dict | None, *, tavily: Tavily | None = None, fetch=None) -> list[dict]:
    """按 agent.config.json 生成联网工具。cfg 为整个 agent 配置（取 web_search / futures 段）。"""
    cfg = cfg or {}
    tools = []
    search_cfg = cfg.get("web_search") or {}
    key = str(search_cfg.get("api_key") or os.environ.get("TAVILY_API_KEY") or "")
    if tavily is None and key and str(search_cfg.get("provider") or "tavily").lower() == "tavily":
        tavily = Tavily(key, max_results=int(search_cfg.get("max_results") or 5))
    if tavily is not None:
        def web_search(params):
            try:
                return json.dumps(tavily.search(params["query"], params.get("max_results"), params.get("days")),
                                  ensure_ascii=False)
            except NetToolError as e:
                return f"搜索失败：{e}"

        def web_extract(params):
            try:
                return json.dumps(tavily.extract(params["url"]), ensure_ascii=False)
            except NetToolError as e:
                return f"读取网页失败：{e}"

        tools += [
            {"name": "web_search", "label": "网页搜索",
             "description": "联网搜索（Tavily），返回标题、链接、摘要。用于行情新闻、政策、公司信息等站外资料；"
                            "回答时注明来源链接，搜索结果不能替代群里的报价记录。",
             "schema": S.object({
                 "query": S.string().max(300).required().description("搜索词，中文即可"),
                 "max_results": S.integer().min(1).max(8).description("结果条数，默认 5"),
                 "days": S.integer().min(1).max(30).description("只要最近 N 天的新闻，可选"),
             }), "handler": web_search},
            {"name": "web_extract", "label": "读取网页",
             "description": "读取一个网页的正文（Tavily extract），用于展开 web_search 找到的链接。",
             "schema": S.object({"url": S.string().max(1000).required().description("http(s) 链接")}),
             "handler": web_extract},
        ]
    if (cfg.get("futures") or {}).get("enabled", True):
        def quote(params):
            try:
                return json.dumps(futures_quote(params["symbol"], fetch=fetch), ensure_ascii=False)
            except NetToolError as e:
                return f"查询行情失败：{e}"

        tools.append({
            "name": "futures_quote", "label": "期货行情",
            "description": "查询期货实时行情（新浪财经，非官方数据源）：上期所沪铜/沪铝等指定合约或主力连续，及 LME 伦铜等。"
                           "指定合约且行情在 5 分钟内时，返回的 benchmark 可直接传给 compare_quotes 换算点价报价。",
             "schema": S.object({"symbol": S.string().max(20).required().description(
                 "合约或品种，如 CU2611、沪铜2611、沪铜主力、AL0、伦铜")}),
            "handler": quote})
    return tools


def _main(argv):
    if len(argv) >= 2 and argv[0] == "futures":
        print(json.dumps(futures_quote(argv[1]), ensure_ascii=False, indent=1))
    elif len(argv) >= 2 and argv[0] == "search":
        key = os.environ.get("TAVILY_API_KEY") or ""
        print(json.dumps(Tavily(key).search(" ".join(argv[1:])), ensure_ascii=False, indent=1))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
