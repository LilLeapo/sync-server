#!/usr/bin/env python3
"""从 sync-server / relay 服务器导出某个微信群的全部消息（含合并转发展开）。

手机模块（WABTest）把群聊消息按 ChatMessage JSON 上报到服务端落盘，本脚本走
服务端 HTTP API 把这些消息拉回来，并把「合并转发的聊天记录」展开成子消息。

两种合并转发形态：
  * appmsg <type>19</type>  ：正文里带 <recorditem><recordinfo><datalist>，子消息完整；
  * appmsg <type>40</type> + <realinnertype>19</realinnertype>：只有 <des> 预览 +
    <xmlfulllen>，完整 XML 在微信侧按需下载，服务端拿到的是「壳」，标记 truncated。

用法：
  python3 tools/export_wechat_room.py --base-url http://host:8080 --token TOKEN \
      --room 53580264331@chatroom --out data/wechat_export
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))

# appmsg 子类型标签（与 web/index.html 的 APPMSG_LABEL 对齐）
APPMSG_LABEL = {
    1: "文本", 3: "图片", 4: "语音", 5: "链接", 6: "文件", 8: "文件", 19: "聊天记录",
    33: "小程序", 36: "小程序", 40: "聊天记录", 47: "表情", 48: "位置", 49: "引用",
    50: "视频号", 51: "视频号直播", 53: "接龙", 57: "引用", 62: "视频号", 74: "文件",
    87: "群公告", 2000: "转账", 2001: "红包",
}

# recorditem 里 <dataitem datatype="N"> 的类型
DATAITEM_LABEL = {
    1: "text", 2: "image", 3: "image", 4: "voice", 5: "video", 6: "file",
    8: "file", 17: "link", 19: "record", 47: "sticker", 48: "location",
    50: "channel", 76: "mini", 87: "announce",
}


def cdata(text: str, tag: str) -> str:
    m = re.search(r"<%s>(?:<!\[CDATA\[)?([\s\S]*?)(?:\]\]>)?</%s>" % (tag, tag), text or "")
    return html.unescape(m.group(1).strip()) if m else ""


def attr(text: str, tag: str, name: str) -> str:
    m = re.search(r"<%s[^>]*\b%s=\"([^\"]*)\"" % (tag, name), text or "")
    return html.unescape(m.group(1)) if m else ""


def fmt_time(ms: int | None, with_date: bool = True) -> str:
    if not ms:
        return ""
    dt = datetime.fromtimestamp(ms / 1000, CST)
    return dt.strftime("%Y-%m-%d %H:%M:%S" if with_date else "%H:%M:%S")


def fetch_messages(base_url: str, token: str, limit: int = 1000) -> list[dict]:
    out: list[dict] = []
    while True:
        qs = urllib.parse.urlencode({"after": len(out), "limit": limit})
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/api/messages?{qs}",
            headers={"Authorization": f"Bearer {token}"},
        )
        data = json.load(urllib.request.urlopen(req, timeout=30))
        msgs = data.get("messages") or []
        if not msgs:
            break
        out.extend(msgs)
        total = data.get("total") or len(out)
        if len(out) >= total:
            break
    return out


def fetch_rooms(base_url: str, token: str) -> list[dict]:
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/rooms",
        headers={"Authorization": f"Bearer {token}"},
    )
    return (json.load(urllib.request.urlopen(req, timeout=30)).get("rooms") or [])


def parse_dataitems(recorditem: str) -> list[dict]:
    """把 <recordinfo><datalist> 里的 <dataitem> 解析成子消息。"""
    items = []
    for body in re.findall(r"(<dataitem\b[\s\S]*?)</dataitem>", recorditem or ""):
        datatype = attr(body, "dataitem", "datatype")
        item = {
            "datatype": int(datatype) if datatype.isdigit() else None,
            "kind": DATAITEM_LABEL.get(int(datatype)) if datatype.isdigit() else None,
            "dataid": attr(body, "dataitem", "dataid"),
            "sender": cdata(body, "sourcename"),
            "content": cdata(body, "datadesc"),
            "timeText": cdata(body, "sourcetime"),
            "srcMsgCreateTime": cdata(body, "srcMsgCreateTime"),
            "srcMsgId": cdata(body, "fromnewmsgid"),
            "hashUsername": cdata(body, "hashusername"),
            "desc": cdata(body, "desc"),
            "dataFmt": cdata(body, "datafmt"),
        }
        for key in ("srcMsgCreateTime",):
            v = item[key]
            item[key + "Ms"] = int(v) * 1000 if v.isdigit() else None
        # 嵌套聊天记录（dataitem 里再套 recordinfo）
        nested = cdata(body, "recorditem")
        if nested:
            item["items"] = parse_dataitems(nested)
        items.append(item)
    return items


def parse_appmsg(content: str) -> dict:
    """解析 type=49 的 XML，返回 {subtype, label, title, des, record|None}。"""
    subtype_raw = cdata(content, "type")
    real_inner = cdata(content, "realinnertype")
    subtype = int(subtype_raw) if subtype_raw.isdigit() else None
    app = {
        "subtype": subtype,
        "realInnerType": int(real_inner) if real_inner.isdigit() else None,
        "label": APPMSG_LABEL.get(subtype or -1, "应用消息"),
        "title": cdata(content, "title"),
        "des": cdata(content, "des"),
        "url": cdata(content, "url"),
        "fileExt": cdata(content, "fileext"),
        "totalLen": cdata(content, "totallen"),
        "xmlFullLen": cdata(content, "xmlfulllen"),
        "cdnDataUrl": cdata(content, "cdndataurl"),
        "dataSize": cdata(content, "datasize"),
        "appName": cdata(content, "appname"),
        "record": None,
        "truncated": False,
    }
    recorditem = cdata(content, "recorditem")
    if recorditem and "<dataitem" in recorditem:
        info = cdata(recorditem, "desc") or app["des"]
        items = parse_dataitems(recorditem)
        count = attr(recorditem, "datalist", "count")
        app["record"] = {
            "summary": info,
            "declaredCount": int(count) if count.isdigit() else None,
            "items": items,
            "itemCount": len(items),
        }
    elif app["subtype"] in (19, 40) or app["realInnerType"] == 19:
        # 壳：只有预览，完整 XML 未随消息下发
        app["truncated"] = True
    return app


def is_xml(content: str) -> bool:
    return bool(re.match(r"\s*(<\?xml|<\w+[>\s])", content or ""))


def split_preview(des: str, names: list[str]) -> list[dict]:
    """壳消息的 <des> 是「昵称: 内容昵称: 内容…」拼起来的预览，尽量切成子消息。

    预览会被截断（最后一条不完整），所以结果标记 partial。
    """
    buckets: dict[str, list[str]] = {}
    for nm in names:
        if nm:
            buckets.setdefault(nm[0], []).append(nm)
    marks: list[tuple[int, str]] = []
    i = 0
    while i < len(des):
        hit = None
        for nm in buckets.get(des[i], ()):
            if des.startswith(nm + ": ", i) or des.startswith(nm + "：", i):
                if hit is None or len(nm) > len(hit):
                    hit = nm
        if hit:
            marks.append((i, hit))
            i += len(hit) + 2
        else:
            i += 1
    items = []
    for k, (pos, nm) in enumerate(marks):
        end = marks[k + 1][0] if k + 1 < len(marks) else len(des)
        content = des[pos + len(nm) + 2:end].strip()
        if not content:
            continue
        items.append({
            "datatype": 1, "kind": "text", "dataid": None, "sender": nm,
            "content": content, "timeText": "", "srcMsgCreateTime": None,
            "srcMsgCreateTimeMs": None, "srcMsgId": None, "hashUsername": None,
            "desc": "", "dataFmt": "", "partial": True,
        })
    return items


def render_sysmsg(content: str) -> str:
    """把 <sysmsg type="sysmsgtemplate"> 的模板填成一句人话。"""
    plain = cdata(content, "plain")
    if plain:
        return plain
    tmpl = cdata(content, "template")
    if not tmpl:
        return ""
    # 每个 <link name="x">…</link> 提供 $x$ 的取值
    values: dict[str, str] = {}
    hidden: set[str] = set()
    for block in re.findall(r"<link\b([\s\S]*?)</link>", content):
        name = attr(f"<link {block}", "link", "name")
        if not name:
            continue
        if 'hidden="1"' in block:
            hidden.add(name)
        members = re.findall(r"<member>([\s\S]*?)</member>", block)
        if members:
            sep = cdata(block, "separator") or "、"
            values[name] = sep.join(cdata(m, "nickname") or cdata(m, "username") for m in members)
            continue
        values[name] = cdata(block, "plain") or cdata(block, "title") or cdata(block, "username")
    text = re.sub(r"\$(\w+)\$", lambda m: "" if m.group(1) in hidden else values.get(m.group(1), m.group(0)), tmpl)
    return re.sub(r"\s{2,}", " ", text).strip()


def normalize(rec: dict, room_name: str | None) -> dict:
    msg = rec.get("msg") or {}
    mtype = msg.get("type") or 0
    base = (mtype & 0xFFFF) if mtype else 0
    content = msg.get("content") or ""
    out = {
        "seq": rec.get("seq"),
        "roomId": msg.get("roomId"),
        "roomName": msg.get("roomName") or room_name,
        "sender": msg.get("sender"),
        "senderName": msg.get("senderName") or msg.get("sender"),
        "type": mtype,
        "baseType": base,
        "createTime": msg.get("createTime"),
        "time": fmt_time(msg.get("createTime")),
        "msgSvrId": msg.get("msgSvrId"),
        "image": None,
        "text": None,
        "app": None,
        "raw": content,
    }
    if base == 1:
        out["text"] = content
    elif base == 3:
        out["image"] = {
            "imgFile": rec.get("imgFile"),
            "kind": msg.get("imageKind"),
            "size": msg.get("imageSize") or rec.get("imgSize"),
            "cdnUrl": msg.get("cdnUrl"),
        }
    elif base == 49 and "<sysmsg" in content:
        out["baseType"] = 10000
        out["text"] = render_sysmsg(content)
    elif base == 49 or is_xml(content):
        out["app"] = parse_appmsg(content)
    elif base == 10000:
        out["text"] = cdata(content, "plain") or cdata(content, "text")
    else:
        out["text"] = re.sub(r"<[^>]+>", "", content)[:200]
    return out


def render_record_block(app: dict) -> str:
    lines = []
    rec = app.get("record")
    if rec:
        if rec.get("partial"):
            lines.append(f"  - 合并转发《{app['title'] or '聊天记录'}》预览切片 {rec['itemCount']} 条"
                         f"（完整 XML {app.get('xmlFullLen') or '?'} 字节未随消息下发，末条可能被截断）")
        else:
            lines.append(f"  - 合并转发《{app['title'] or '聊天记录'}》共 {rec['itemCount']} 条")
        for it in rec["items"]:
            who = it.get("sender") or "?"
            when = it.get("timeText") or fmt_time(it.get("srcMsgCreateTimeMs"))
            body = (it.get("content") or it.get("desc") or "").replace("\n", "\n      ")
            tag = "" if it.get("kind") in (None, "text") else f"[{it['kind']}] "
            lines.append(f"    - {when} {who}: {tag}{body}")
            for sub in it.get("items") or []:
                lines.append(f"        - {sub.get('sender')}: {(sub.get('content') or '')[:200]}")
    elif rec and rec.get("partial"):
        lines.append(f"  - 合并转发《{app['title'] or '聊天记录'}》(预览切片 {rec['itemCount']} 条，完整 XML {app.get('xmlFullLen') or '?'} 字节未随消息下发)"
                     f"\n    - " + "\n    - ".join(
                         f"{it['sender']}: {(it['content'] or '')[:300]}" for it in rec["items"]))
    elif app.get("truncated"):
        lines.append(f"  - 合并转发《{app['title'] or '聊天记录'}》(壳，完整内容 {app.get('xmlFullLen') or '?'} 字节未下发)")
        lines.append(f"    预览: {app['des'][:400]}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://39.107.156.47:8080")
    ap.add_argument("--token", default=os.environ.get("SYNC_TOKEN", ""))
    ap.add_argument("--room", required=True, help="roomId 或群名称")
    ap.add_argument("--out", default="data/wechat_export")
    args = ap.parse_args()

    rooms = fetch_rooms(args.base_url, args.token)
    match = [r for r in rooms if r["roomId"] == args.room or r.get("roomName") == args.room]
    if not match:
        print("找不到该群，可选：", ", ".join(f"{r.get('roomName')}({r['roomId']})" for r in rooms))
        return 1
    room_id = match[0]["roomId"]
    room_name = match[0].get("roomName") or room_id

    raw = fetch_messages(args.base_url, args.token)
    msgs = [r for r in raw if (r.get("msg") or {}).get("roomId") == room_id]
    msgs.sort(key=lambda r: (r["msg"].get("createTime") or 0, r.get("seq") or 0))
    norm = [normalize(r, room_name) for r in msgs]

    # 壳消息（type40/realinnertype 19）的完整 XML 未随消息下发，只能把 <des> 预览切成子消息
    known = sorted({n["senderName"] for n in norm if n.get("senderName") and not str(n["senderName"]).endswith("@chatroom")}
                   | {it["sender"] for n in norm if n["app"] and n["app"].get("record")
                      for it in n["app"]["record"]["items"] if it.get("sender")}, key=len, reverse=True)
    for n in norm:
        app = n["app"]
        if app and app.get("truncated") and not app.get("record"):
            items = split_preview(app.get("des") or "", known)
            app["record"] = {
                "summary": app.get("des"), "declaredCount": None, "items": items,
                "itemCount": len(items), "partial": True, "xmlFullLen": app.get("xmlFullLen"),
            }

    os.makedirs(args.out, exist_ok=True)
    tag = room_id.split("@")[0]
    jsonl = os.path.join(args.out, f"{tag}.messages.jsonl")
    with open(jsonl, "w", encoding="utf-8") as f:
        for n in norm:
            f.write(json.dumps(n, ensure_ascii=False) + "\n")

    records = [n for n in norm if n["app"] and (n["app"]["record"] or n["app"]["truncated"])]
    with open(os.path.join(args.out, f"{tag}.records.jsonl"), "w", encoding="utf-8") as f:
        for n in records:
            f.write(json.dumps({
                "seq": n["seq"], "time": n["time"], "sender": n["senderName"],
                "title": n["app"]["title"], "subtype": n["app"]["subtype"],
                "truncated": n["app"]["truncated"], "record": n["app"]["record"],
                "preview": n["app"]["des"], "xmlFullLen": n["app"]["xmlFullLen"],
            }, ensure_ascii=False) + "\n")

    # 同一条图片只保留最清晰的一份（原图 > 中图 > 缩略图）
    rank = {"original": 3, "full": 3, "mid": 2, "thumbnail": 1}
    best_img: dict[str, str] = {}
    for n in norm:
        if n.get("image"):
            key = str(n["msgSvrId"])
            cur = best_img.get(key)
            if cur is None or rank.get(n["image"].get("kind"), 0) > rank.get(cur, 0):
                best_img[key] = n["image"].get("kind") or ""

    md = os.path.join(args.out, f"{tag}.transcript.md")
    day = None
    with open(md, "w", encoding="utf-8") as f:
        f.write(f"# 微信群「{room_name}」消息记录\n\n")
        f.write(f"- roomId: `{room_id}`\n- 消息数: {len(norm)}\n")
        full_n = sum(1 for r in records if not r["app"]["truncated"])
        part_n = sum(1 for r in records if r["app"]["truncated"] and (r["app"].get("record") or {}).get("partial"))
        f.write(f"- 合并转发: {len(records)} 条（完整 {full_n}，仅预览可切片 {part_n}）；"
                f"完整记录共展开 {sum(r['app']['record']['itemCount'] for r in records if not r['app']['truncated'])} 条子消息\n")
        f.write(f"- 导出时间: {datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S %z')}\n\n---\n\n")
        for n in norm:
            d = (n["time"] or "")[:10]
            if d and d != day:
                day = d
                f.write(f"\n## {d}\n\n")
            who = n["senderName"] or n["sender"]
            stamp = (n["time"] or "")[11:]
            if n["app"]:
                app = n["app"]
                if app.get("record") or app.get("truncated"):
                    f.write(f"**{stamp} {who}** [合并转发]\n\n{render_record_block(app)}\n\n")
                else:
                    sub = app.get("des") or app.get("fileExt") or ""
                    f.write(f"**{stamp} {who}** [{app['label']}] {app.get('title','')}"
                            + (f" — {sub[:200]}" if sub else "") + "\n\n")
            elif n.get("image"):
                img = n["image"]
                if best_img.get(str(n["msgSvrId"])) not in (None, img.get("kind")):
                    continue
                f.write(f"**{stamp} {who}** [图片 {img.get('kind') or ''} {img.get('imgFile') or img.get('cdnUrl') or '未落盘'}]\n\n")
            else:
                text = (n.get("text") or "").strip()
                f.write(f"**{stamp} {who}**: {text}\n\n")

    print(json.dumps({
        "roomId": room_id, "roomName": room_name, "messages": len(norm),
        "serverCount": match[0].get("count"),
        "mergedRecords": len(records),
        "mergedFull": sum(1 for r in records if not r["app"]["truncated"]),
        "mergedShell": sum(1 for r in records if r["app"]["truncated"]),
        "mergedShellPreviewItems": sum(r["app"]["record"]["itemCount"] for r in records
                                       if r["app"]["truncated"] and r["app"].get("record")),
        "subMessages": sum(r["app"]["record"]["itemCount"] for r in records if r["app"].get("record")),
        "jsonl": jsonl, "records": os.path.join(args.out, f"{tag}.records.jsonl"), "transcript": md,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
