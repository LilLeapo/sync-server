"""报价查询插件：/quotes [关键词] [N] 与「最近报价」自然语句。"""


def _label(ms):
    import datetime as dt
    try:
        return dt.datetime.fromtimestamp(int(ms) / 1000).strftime("%m-%d %H:%M")
    except Exception:
        return ""


def _fmt(r):
    parts = [str(r.get("项目") or ""), str(r.get("价格") or "")]
    for key, pre in (("单位", ""), ("数量", "×")):
        v = str(r.get(key) or "").strip()
        if v:
            parts.append(pre + v)
    tail = " —— ".join(x for x in (str(r.get("备注") or "").strip(),
                                    str(r.get("sender") or "").strip(),
                                    _label(r.get("time"))) if x)
    line = " ".join(x for x in parts if x)
    return line + (" —— " + tail if tail else "")


def _pick(api, kw="", limit=10):
    if api.quotes is None:
        return []
    rows, _ = api.quotes.list(0, 2000)
    if kw:
        needle = kw.lower()
        rows = [r for r in rows if needle in " ".join(
            str(r.get(k) or "") for k in ("项目", "备注", "sender", "单位", "价格")).lower()]
    return rows[-limit:]


def register(api):
    def quotes_cmd(api, event, args):
        limit = 10
        kw = ""
        tokens = (args or "").split()
        if tokens:
            if tokens[-1].isdigit():
                limit = min(max(int(tokens[-1]), 1), 30)
                tokens = tokens[:-1]
            kw = " ".join(tokens)
        rows = _pick(api, kw, limit)
        if not rows:
            return "没有找到匹配的报价。" if kw else "还没有报价记录。"
        head = f"找到 {len(rows)} 条报价："
        return head + "\n" + "\n".join(_fmt(r) for r in rows)

    api.register_command("quotes", quotes_cmd, usage="/quotes [关键词] [N]",
                         description="查询报价，可按关键词过滤，N 为条数")

    def on_text(api, event, text):
        t = (text or "").strip()
        if t.startswith(("最近报价", "最新报价", "查报价", "有哪些报价")):
            rows = _pick(api, "", 10)
            if not rows:
                return "还没有报价记录。"
            return "最近报价：\n" + "\n".join(_fmt(r) for r in rows)
        return None

    api.register_message_hook(on_text)