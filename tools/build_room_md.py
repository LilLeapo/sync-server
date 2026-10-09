#!/usr/bin/env python3
"""把 relay 的 messages.jsonl 整理成一份带图片的 Markdown 聊天记录。

与 export_wechat_room.py 的区别：这个直接吃服务器原始 jsonl（含 imgFile 字段），
把图片一并放进 md 目录，便于离线阅读/存档。

用法：
  python3 tools/build_room_md.py --messages data/wechat_export/server_messages.jsonl \
      --images /opt/wabtest-sync/data/images --room 53580264331@chatroom \
      --out data/wechat_export
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))

APPMSG_LABEL = {
    5: "链接", 6: "文件", 19: "聊天记录", 33: "小程序", 36: "小程序", 40: "聊天记录",
    47: "表情", 48: "位置", 49: "引用", 50: "视频号", 53: "接龙", 57: "引用",
    74: "文件", 87: "群公告", 2000: "转账", 2001: "红包",
}
DATAITEM_LABEL = {1: "text", 2: "image", 3: "image", 4: "voice", 5: "video",
                  6: "file", 8: "file", 17: "link", 19: "record", 47: "sticker", 48: "location"}
IMG_RANK = {"original": 3, "full": 3, "mid": 2, "thumbnail": 1}


def cdata(text: str, tag: str) -> str:
    m = re.search(r"<%s>(?:<!\[CDATA\[)?([\s\S]*?)(?:\]\]>)?</%s>" % (tag, tag), text or "")
    return html.unescape(m.group(1).strip()) if m else ""


def attr(text: str, tag: str, name: str) -> str:
    m = re.search(r"<%s[^>]*\b%s=\"([^\"]*)\"" % (tag, name), text or "")
    return html.unescape(m.group(1)) if m else ""


def fmt(ms: int | None) -> str:
    return datetime.fromtimestamp((ms or 0) / 1000, CST).strftime("%Y-%m-%d %H:%M:%S") if ms else ""


def render_sysmsg(content: str) -> str:
    plain = cdata(content, "plain")
    if plain:
        return plain
    tmpl = cdata(content, "template")
    if not tmpl:
        return ""
    values, hidden = {}, set()
    for block in re.findall(r"<link\b([\s\S]*?)</link>", content):
        name = attr("<link " + block, "link", "name")
        if not name:
            continue
        if 'hidden="1"' in block:
            hidden.add(name)
        members = re.findall(r"<member>([\s\S]*?)</member>", block)
        if members:
            sep = cdata(block, "separator") or "、"
            values[name] = sep.join(cdata(m, "nickname") or cdata(m, "username") for m in members)
        else:
            values[name] = cdata(block, "plain") or cdata(block, "title") or cdata(block, "username")
    out = re.sub(r"\$(\w+)\$", lambda m: "" if m.group(1) in hidden else values.get(m.group(1), m.group(0)), tmpl)
    return re.sub(r"\s{2,}", " ", out).strip()


def parse_dataitems(recorditem: str) -> list[dict]:
    items = []
    for body in re.findall(r"(<dataitem\b[\s\S]*?)</dataitem>", recorditem or ""):
        dt = attr(body, "dataitem", "datatype")
        n = int(dt) if dt.isdigit() else None
        item = {
            "kind": DATAITEM_LABEL.get(n, f"type{n}" if n else None),
            "sender": cdata(body, "sourcename"),
            "content": cdata(body, "datadesc") or cdata(body, "desc"),
            "time": cdata(body, "sourcetime") or fmt(int(cdata(body, "srcMsgCreateTime") or 0) * 1000),
        }
        items.append(item)
    return items


def split_preview(des: str, names: list[str]) -> list[dict]:
    buckets: dict[str, list[str]] = {}
    for nm in names:
        if nm:
            buckets.setdefault(nm[0], []).append(nm)
    marks, i = [], 0
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
    out = []
    for k, (pos, nm) in enumerate(marks):
        end = marks[k + 1][0] if k + 1 < len(marks) else len(des)
        content = des[pos + len(nm) + 2:end].strip()
        if content:
            out.append({"kind": "text", "sender": nm, "content": content, "time": "", "partial": True})
    return out


def escape(text: str) -> str:
    """Markdown 里保持换行、避免被当成语法。"""
    return text.replace("\\", "\\\\").replace("<", "&lt;").replace(">", "&gt;")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--messages", required=True)
    ap.add_argument("--images", required=True, help="relay data/images 目录")
    ap.add_argument("--room", required=True)
    ap.add_argument("--out", default="data/wechat_export")
    ap.add_argument("--copy-images", action="store_true", default=True)
    args = ap.parse_args()

    raw = [json.loads(l) for l in open(args.messages, encoding="utf-8")]
    room = [r for r in raw if (r.get("msg") or {}).get("roomId") == args.room]
    room.sort(key=lambda r: (r["msg"].get("createTime") or 0, r.get("seq") or 0))

    names = sorted({m.get("senderName") for m in (r["msg"] for r in room)
                    if m.get("senderName") and not str(m["senderName"]).endswith("@chatroom")}, key=len, reverse=True)

    # 图片按 msgSvrId 归并，保留最清晰的一张 + 全部变体
    variants: dict[str, list[str]] = {}
    for r in room:
        if r.get("imgFile"):
            variants.setdefault(str(r["msg"]["msgSvrId"]), []).append(r["imgFile"])
    best = {}
    for svr, files in variants.items():
        best[svr] = sorted(files, key=lambda p: IMG_RANK.get(p.rsplit("_", 1)[-1].split(".")[0], 0), reverse=True)[0]

    os.makedirs(args.out, exist_ok=True)
    tag = args.room.split("@")[0]
    img_out = os.path.join(args.out, "images")
    copied = set()
    md = os.path.join(args.out, f"{tag}.chat.md")

    day = None
    n_img = n_rec = n_sub = 0
    with open(md, "w", encoding="utf-8") as f:
        f.write(f"# 微信群「语义治理」聊天记录（{tag}）\n\n")
        f.write(f"- roomId: `{args.room}`\n- 消息数: {len(room)}\n")
        f.write(f"- 时间范围: {fmt(room[0]['msg']['createTime'])} ~ {fmt(room[-1]['msg']['createTime'])}\n")
        f.write(f"- 生成时间: {datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S %z')}\n\n---\n")

        for r in room:
            msg = r["msg"]
            t = fmt(msg.get("createTime"))
            d, stamp = t[:10], t[11:]
            if d != day:
                day = d
                f.write(f"\n## {d}\n\n")
            who = msg.get("senderName") or msg.get("sender")
            mtype = (msg.get("type") or 0) & 0xFFFF
            content = msg.get("content") or ""

            if mtype == 3:
                svr = str(msg.get("msgSvrId"))
                rel = best.get(svr)
                if rel:
                    src = os.path.join(args.images, os.path.basename(rel))
                    dst = os.path.join(img_out, os.path.basename(rel))
                    if os.path.exists(src) and os.path.abspath(src) != os.path.abspath(dst):
                        os.makedirs(img_out, exist_ok=True)
                        if os.path.basename(rel) not in copied:
                            shutil.copy2(src, dst)
                            copied.add(os.path.basename(rel))
                    if os.path.exists(dst):
                        f.write(f"**{stamp} {who}**\n\n![图片](images/{os.path.basename(rel)})\n\n")
                        n_img += 1
                        continue
                f.write(f"**{stamp} {who}** [图片缺失 {rel or svr}]\n\n")
            elif mtype == 49 and "<sysmsg" in content:
                f.write(f"*{stamp} {render_sysmsg(content)}*\n\n")
            elif mtype == 49 or content.lstrip().startswith("<"):
                subtype = cdata(content, "type")
                real = cdata(content, "realinnertype")
                st = int(subtype) if subtype.isdigit() else None
                recorditem = cdata(content, "recorditem")
                if recorditem and "<dataitem" in recorditem:
                    items = parse_dataitems(recorditem)
                    n_rec += 1
                    n_sub += len(items)
                    f.write(f"**{stamp} {who}** [合并转发 · {len(items)} 条]\n\n")
                    for it in items:
                        body = escape(it["content"] or "").replace("\n", "\n  ")
                        tag2 = "" if it["kind"] in (None, "text") else f"[{it['kind']}] "
                        f.write(f"- {it['time']} **{it['sender']}**: {tag2}{body}\n")
                    f.write("\n")
                elif st == 40 or real == "19":
                    items = split_preview(cdata(content, "des"), names)
                    n_rec += 1
                    n_sub += len(items)
                    f.write(f"**{stamp} {who}** [合并转发 · 仅预览 {len(items)} 条，"
                            f"完整 XML {cdata(content, 'xmlfulllen') or '?'} 字节未下发]\n\n")
                    for it in items:
                        body = escape(it["content"] or "").replace("\n", "\n  ")
                        f.write(f"- **{it['sender']}**: {body}\n")
                    f.write("\n")
                else:
                    label = APPMSG_LABEL.get(st, "应用消息")
                    title = cdata(content, "title")
                    des = cdata(content, "des")
                    extra = cdata(content, "fileext") or cdata(content, "totallen")
                    f.write(f"**{stamp} {who}** [{label}] {title}"
                            + (f" — {escape(des or extra)}" if (des or extra) else "") + "\n\n")
            elif mtype == 10000:
                f.write(f"*{stamp} {escape(content.strip())}*\n\n")
            else:
                text = content.strip()
                if text:
                    f.write(f"**{stamp} {who}**: {escape(text)}\n\n")

    print(json.dumps({"md": md, "messages": len(room), "images": n_img,
                      "mergedRecords": n_rec, "subMessages": n_sub,
                      "imagesCopied": len(copied), "imagesDir": img_out},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())