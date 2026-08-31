#!/usr/bin/env python3
"""群聊报价提取 Agent。

对收到的文本/图片消息调用 OpenAI 兼容的视觉模型，抽取报价信息，
按群分文件、按日期分节整理进 Markdown（<data-dir>/quotes/<roomId>.md），
同时输出结构化 quotes.jsonl 供 /api/quotes 与前端报价视图使用。

两种用法：
- 内嵌：sync_server 在 Store.add_message 后调用 create_agent() + enqueue(rec)
- 回填：python3 -m server.quote_agent --backfill

配置（默认 <项目根>/agent.config.json，可用 --config 指定）：
{
  "enabled": true,
  "base_url": "https://your-endpoint/v1",
  "api_key": "sk-...",
  "model": "your-vision-model"
}
"""
import argparse
import base64
import datetime as dt
import heapq
import itertools
import json
import os
import re
import sys
import threading
import time
import urllib.request

IMAGE_WAIT_S = 10          # 图片延迟处理，等同消息的原图到达
LLM_TIMEOUT_S = 60
LLM_RETRIES = 2            # 额外重试次数（共 3 次调用）
MAX_IMAGE_BYTES = 12 * 1024 * 1024
SUMMARY_MAX = 50
STATE_KEEP = 50000         # state.json 里最多保留的已处理 seq 数
SVR_KEEP = 5000            # 记录的 msgSvrId 处理状态上限

SYSTEM_PROMPT = """你是一个群聊消息报价信息提取助手。从群聊文本或图片中识别商品/项目/资产等的报价信息。

规则：
- 只输出 JSON，不要输出任何其他文字、解释或 markdown 代码块。
- 没有报价信息时输出：{"has_quote": false}
- 有报价信息时输出：{"has_quote": true, "items": [{"项目": "...", "价格": "...", "单位": "...", "数量": "...", "备注": "..."}]}
- “价格”保留原始币种与数值写法（如 12000、¥1.2万、$30）；“单位”如 元/月、元/件；“数量”无则留空字符串。
- 一条消息含多个报价时 items 给多个；信息不全的字段留空字符串，不要编造。
- 闲聊、转账凭证、无价格的内容都不算报价。"""

IMAGE_USER_PROMPT = "提取这张群聊图片中的报价信息。按约定只输出 JSON。"

MD_HEADER = "| 时间 | 发送人 | 项目 | 价格 | 单位 | 数量 | 备注 | 来源 | 原文摘要 |"
MD_SEP = "|---|---|---|---|---|---|---|---|---|"


def load_config(path: str) -> dict | None:
    """读取 agent 配置；文件缺失、解析失败或 enabled!=true 时返回 None。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"[quote_agent] 配置解析失败 {path}: {e}", file=sys.stderr)
        return None
    if not cfg.get("enabled"):
        return None
    if not cfg.get("model"):
        print(f"[quote_agent] 配置缺少 model: {path}", file=sys.stderr)
        return None
    provider = str(cfg.get("provider") or "http").lower()
    if provider != "mock" and (not cfg.get("base_url") or not cfg.get("api_key")):
        print(f"[quote_agent] 配置缺少 base_url/api_key: {path}", file=sys.stderr)
        return None
    return cfg


def one_line(text: str, limit: int = SUMMARY_MAX) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def md_cell(text) -> str:
    return str(text or "").replace("|", "\\|").replace("\n", " ").strip()


class QuoteAgent:
    def __init__(self, config: dict, data_dir: str, on_quote=None, bridge=None):
        self.base_url = str(config["base_url"]).rstrip("/")
        self.api_key = str(config["api_key"])
        self.model = str(config["model"])
        self.data_dir = data_dir
        self.bridge = bridge  # pi_bridge.PiBridge | None：文本消息走 Agent SDK
        self.on_quote = [f for f in (on_quote or []) if callable(f)]
        self.quotes_dir = os.path.join(data_dir, "quotes")
        os.makedirs(self.quotes_dir, exist_ok=True)
        self.log_path = os.path.join(self.quotes_dir, "agent.log")
        self.state_path = os.path.join(self.quotes_dir, "state.json")
        self.quotes_jsonl = os.path.join(self.quotes_dir, "quotes.jsonl")
        self._qseq = self._count_lines(self.quotes_jsonl)

        self._heap = []  # (not_before, counter, rec)
        self._cv = threading.Condition()
        self._counter = itertools.count()
        self._processed: set[int] = set()          # 已处理的 seq
        self._svr_state: dict[str, dict] = {}      # msgSvrId -> {seq, size}
        self._load_state()

        self._worker = threading.Thread(target=self._work_loop, daemon=True, name="quote-agent")
        self._worker.start()

    # ---------- 状态持久化 ----------

    @staticmethod
    def _count_lines(path: str) -> int:
        try:
            with open(path, "rb") as f:
                return sum(1 for _ in f)
        except OSError:
            return 0

    def _load_state(self):
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._processed = set(data.get("processed") or [])
            self._svr_state = dict(data.get("svr") or {})
        except Exception:
            pass

    def _save_state(self):
        try:
            processed = sorted(self._processed)[-STATE_KEEP:]
            svr_items = list(self._svr_state.items())[-SVR_KEEP:]
            self._svr_state = dict(svr_items)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"processed": processed, "svr": self._svr_state}, f)
            os.replace(tmp, self.state_path)
        except Exception as e:
            self._log(f"state save failed: {e}")

    def _log(self, msg: str):
        line = f"{dt.datetime.now():%F %T} {msg}\n"
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass

    # ---------- 入队 ----------

    def enqueue(self, rec: dict):
        """chat_viewer 在消息落盘后调用。文本立即处理；图片延迟等原图并按 svr 合并。"""
        msg = rec.get("msg") or {}
        mtype = msg.get("type")
        seq = rec.get("seq")
        if seq in self._processed:
            return
        if mtype == 1:
            self._push(rec, time.time())
        elif mtype == 3:
            if not rec.get("imgFile"):
                return
            svr = str(msg.get("msgSvrId") or "")
            with self._cv:
                # 队列里已有同 svr 的图片：保留较大的版本，维持原等待时间
                for i, (nb, _, old) in enumerate(self._heap):
                    old_msg = old.get("msg") or {}
                    if old_msg.get("type") == 3 and str(old_msg.get("msgSvrId") or "") == svr:
                        if (rec.get("imgSize") or 0) > (old.get("imgSize") or 0):
                            self._heap[i] = (nb, next(self._counter), rec)
                            heapq.heapify(self._heap)
                        return
            self._push(rec, time.time() + IMAGE_WAIT_S)
        # 其他类型（转账/文件/表情等）不分析

    def _push(self, rec: dict, not_before: float):
        with self._cv:
            heapq.heappush(self._heap, (not_before, next(self._counter), rec))
            self._cv.notify()

    # ---------- 主循环 ----------

    def _work_loop(self):
        while True:
            with self._cv:
                now = time.time()
                if not self._heap:
                    self._cv.wait()
                    continue
                nb = self._heap[0][0]
                if nb > now:
                    self._cv.wait(nb - now)
                    continue
                _, _, rec = heapq.heappop(self._heap)
            try:
                self._process(rec)
            except Exception as e:
                self._log(f"process failed seq={rec.get('seq')}: {e}")
                self._mark_processed(rec)

    def _process(self, rec: dict):
        msg = rec.get("msg") or {}
        svr = str(msg.get("msgSvrId") or "")
        size = rec.get("imgSize") or 0
        if msg.get("type") == 3 and svr:
            # 该 svr 已按文字处理过：只有更大的图片版本才重新分析
            st = self._svr_state.get(svr)
            if st and (st.get("size") or 0) >= size:
                self._mark_processed(rec)
                return
        items = self._analyze(rec)
        self._mark_processed(rec, svr, size)
        self._save_state()
        if items:
            self._write_md(rec, items)
            self._append_jsonl(rec, items)
            self._emit_quote(rec, items)
            self._log(f"quote seq={rec.get('seq')} room={msg.get('roomId')} items={len(items)}")
        else:
            self._log(f"no-quote seq={rec.get('seq')}")

    def _emit_quote(self, rec: dict, items: list[dict]):
        """把本轮提取到的报价行转发给 on_quote 钩子（如飞书推送）。"""
        if not self.on_quote:
            return
        rows = self._quote_rows(rec, items)
        if not rows:
            return
        for fn in self.on_quote:
            try:
                fn(rows)
            except Exception as e:
                self._log(f"on_quote hook failed: {e}")

    def _mark_processed(self, rec: dict, svr: str = "", size: int = 0):
        seq = rec.get("seq")
        if seq is not None:
            self._processed.add(seq)
        if svr:
            self._svr_state[svr] = {"seq": seq, "size": size}

    # ---------- LLM ----------

    def _analyze(self, rec: dict) -> list[dict] | None:
        msg = rec.get("msg") or {}
        t = msg.get("createTime") or rec.get("receivedAt")
        stamp = dt.datetime.fromtimestamp((t or 0) / 1000).strftime("%F %T")
        sender = msg.get("sender") or ""

        if msg.get("type") == 3:
            img_file = rec.get("imgFile")
            if not img_file:
                return None
            full = os.path.join(self.data_dir, img_file)
            try:
                with open(full, "rb") as f:
                    raw = f.read(MAX_IMAGE_BYTES + 1)
            except OSError:
                return None
            if not raw or len(raw) > MAX_IMAGE_BYTES:
                return None
            b64 = base64.b64encode(raw).decode("ascii")
            user_content = [
                {"type": "text", "text": f"发送人：{sender}，时间：{stamp}\n{IMAGE_USER_PROMPT}"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]
        else:
            text = msg.get("content") or ""
            if not text.strip():
                return None
            user_content = f"群聊消息（发送人：{sender}，时间：{stamp}）：\n{text}"

        content = self._call_llm(user_content)
        if content is None:
            return None
        return self._parse_items(content)

    def _call_llm(self, user_content) -> str | None:
        # 文本消息优先走 pi-agent SDK 桥（若已接入），失败退回原生 urllib 路径
        bridge = getattr(self, "bridge", None)
        if bridge is not None and isinstance(user_content, str):
            try:
                out = bridge.complete_chat(SYSTEM_PROMPT, user_content)
                if out:
                    return out
            except Exception as e:
                self._log(f"bridge call failed, fallback native: {e}")
        body = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0,
        }).encode("utf-8")
        last_err = None
        for attempt in range(LLM_RETRIES + 1):
            try:
                req = urllib.request.Request(
                    self.base_url + "/chat/completions",
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": "Bearer " + self.api_key,
                    },
                )
                with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_S) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                return data["choices"][0]["message"]["content"]
            except Exception as e:
                last_err = e
                if attempt < LLM_RETRIES:
                    time.sleep(2 * (attempt + 1) ** 2)
        self._log(f"llm call failed: {last_err}")
        return None

    @staticmethod
    def _parse_items(content: str) -> list[dict] | None:
        """从模型输出提取 JSON；无报价返回 None，有报价返回 items 列表。"""
        m = re.search(r"\{.*\}", content or "", re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except Exception:
            return None
        if not data.get("has_quote"):
            return None
        items = data.get("items")
        if not isinstance(items, list) or not items:
            return None
        return [it for it in items if isinstance(it, dict)]

    # ---------- Markdown 输出 ----------

    def _write_md(self, rec: dict, items: list[dict]):
        msg = rec.get("msg") or {}
        room = str(msg.get("roomId") or "unknown")
        t = msg.get("createTime") or rec.get("receivedAt") or 0
        day = dt.datetime.fromtimestamp(t / 1000).strftime("%F")
        hm = dt.datetime.fromtimestamp(t / 1000).strftime("%H:%M")
        sender = msg.get("sender") or ""
        source = "图片" if msg.get("type") == 3 else "文字"
        if msg.get("type") == 3:
            summary = f"[原图](../{rec.get('imgFile')})" if rec.get("imgFile") else "（图片）"
        else:
            summary = one_line(msg.get("content") or "")

        rows = []
        for it in items:
            rows.append(
                "| " + " | ".join([
                    hm, md_cell(sender), md_cell(it.get("项目")), md_cell(it.get("价格")),
                    md_cell(it.get("单位")), md_cell(it.get("数量")), md_cell(it.get("备注")),
                    source, md_cell(summary),
                ]) + " |"
            )

        path = os.path.join(self.quotes_dir, f"{room}.md")
        existing = ""
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                existing = f.read()

        if not existing:
            existing = f"# {room} 报价汇总\n"
        if f"\n## {day}\n" in existing or existing.rstrip().endswith(f"## {day}"):
            body = existing if existing.endswith("\n") else existing + "\n"
            body += "\n".join(rows) + "\n"
        else:
            body = existing if existing.endswith("\n") else existing + "\n"
            body += f"\n## {day}\n\n{MD_HEADER}\n{MD_SEP}\n" + "\n".join(rows) + "\n"
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp, path)

    # ---------- 结构化输出（供 /api/quotes 与前端「报价」页） ----------

    def _quote_rows(self, rec: dict, items: list[dict]) -> list[dict]:
        """把 rec + items 展开成报价行（无 qseq；jsonl 与 on_quote 钩子共用）。"""
        msg = rec.get("msg") or {}
        t = msg.get("createTime") or rec.get("receivedAt") or 0
        if msg.get("type") == 3:
            summary = ""
        else:
            summary = one_line(msg.get("content") or "")
        base = {
            "roomId": str(msg.get("roomId") or "unknown"),
            "time": t,
            "sender": msg.get("sender") or "",
            "source": "图片" if msg.get("type") == 3 else "文字",
            "imgFile": rec.get("imgFile"),
            "summary": summary,
            "msgSeq": rec.get("seq"),
        }
        rows = []
        for it in items:
            row = dict(base)
            for k in ("项目", "价格", "单位", "数量", "备注"):
                row[k] = str(it.get(k) or "")
            rows.append(row)
        return rows

    def _append_jsonl(self, rec: dict, items: list[dict]):
        lines = []
        for row in self._quote_rows(rec, items):
            self._qseq += 1
            row["qseq"] = self._qseq
            lines.append(json.dumps(row, ensure_ascii=False))
        try:
            with open(self.quotes_jsonl, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except OSError as e:
            self._log(f"quotes.jsonl append failed: {e}")


def create_agent(config_path: str, data_dir: str, on_quote=None, bridge=None) -> QuoteAgent | None:
    """供 chat_viewer 内嵌使用；配置缺失/未启用时返回 None。
    on_quote: quote 行列表 -> None 的钩子（飞书推送）；bridge: 可选 PiBridge。"""
    cfg = load_config(config_path)
    if not cfg:
        return None
    agent = QuoteAgent(cfg, data_dir, on_quote=on_quote, bridge=bridge)
    kind = "bridge:" + bridge.backend_kind if bridge is not None else "native"
    print(f"[sync_server] quote agent enabled: model={agent.model} ({kind}) -> {agent.quotes_dir}")
    return agent


# ---------- 回填 ----------

def backfill(config_path: str, data_dir: str):
    cfg = load_config(config_path)
    if not cfg:
        print("配置缺失或未启用，无法回填", file=sys.stderr)
        return 1

    log_path = os.path.join(data_dir, "quotes", "agent.log")
    os.makedirs(os.path.join(data_dir, "quotes"), exist_ok=True)

    def log(msg):
        line = f"{dt.datetime.now():%F %T} [backfill] {msg}\n"
        print(line, end="")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)

    # 第一遍：收集记录；图片按 svr 取最佳（imgSize 最大）
    records = []
    best_img: dict[str, dict] = {}
    log_path_jsonl = os.path.join(data_dir, "messages.jsonl")
    with open(log_path_jsonl, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            msg = rec.get("msg") or {}
            msg.pop("imageBase64", None)  # 释放大字段
            if msg.get("type") == 1:
                records.append(rec)
            elif msg.get("type") == 3 and rec.get("imgFile"):
                svr = str(msg.get("msgSvrId") or "")
                cur = best_img.get(svr)
                if cur is None or (rec.get("imgSize") or 0) > (cur.get("imgSize") or 0):
                    if cur is None:
                        records.append(rec)
                    else:
                        records[records.index(cur)] = rec
                    best_img[svr] = rec

    agent = QuoteAgent.__new__(QuoteAgent)  # 不启动 worker，只复用方法
    agent.base_url = str(cfg["base_url"]).rstrip("/")
    agent.api_key = str(cfg["api_key"])
    agent.model = str(cfg["model"])
    agent.data_dir = data_dir
    agent.quotes_dir = os.path.join(data_dir, "quotes")
    agent.log_path = log_path
    agent._log = lambda msg: log(msg)

    # 全量重建：room -> day -> rows；flat_rows 为结构化输出（quotes.jsonl）
    tables: dict[str, dict[str, list[str]]] = {}
    titles: dict[str, str] = {}
    flat_rows: list[dict] = []
    done = skipped = 0
    for i, rec in enumerate(records, 1):
        msg = rec.get("msg") or {}
        try:
            items = agent._analyze(rec)
        except Exception as e:
            log(f"analyze failed seq={rec.get('seq')}: {e}")
            items = None
        if not items:
            skipped += 1
        else:
            room = str(msg.get("roomId") or "unknown")
            t = msg.get("createTime") or rec.get("receivedAt") or 0
            day = dt.datetime.fromtimestamp(t / 1000).strftime("%F")
            hm = dt.datetime.fromtimestamp(t / 1000).strftime("%H:%M")
            sender = msg.get("sender") or ""
            source = "图片" if msg.get("type") == 3 else "文字"
            if msg.get("type") == 3:
                summary = f"[原图](../{rec.get('imgFile')})" if rec.get("imgFile") else "（图片）"
            else:
                summary = one_line(msg.get("content") or "")
            days = tables.setdefault(room, {})
            titles[room] = room
            rows = days.setdefault(day, [])
            for it in items:
                rows.append(
                    "| " + " | ".join([
                        hm, md_cell(sender), md_cell(it.get("项目")), md_cell(it.get("价格")),
                        md_cell(it.get("单位")), md_cell(it.get("数量")), md_cell(it.get("备注")),
                        source, md_cell(summary),
                    ]) + " |"
                )
                jrow = {
                    "roomId": room, "time": t, "sender": sender, "source": source,
                    "imgFile": rec.get("imgFile"),
                    "summary": "" if msg.get("type") == 3 else one_line(msg.get("content") or ""),
                    "msgSeq": rec.get("seq"),
                }
                for k in ("项目", "价格", "单位", "数量", "备注"):
                    jrow[k] = str(it.get(k) or "")
                flat_rows.append(jrow)
            done += 1
        if i % 10 == 0:
            log(f"progress {i}/{len(records)} quotes={done}")

    with open(os.path.join(agent.quotes_dir, "quotes.jsonl"), "w", encoding="utf-8") as f:
        for i, jrow in enumerate(flat_rows, 1):
            jrow["qseq"] = i
            f.write(json.dumps(jrow, ensure_ascii=False) + "\n")

    for room, days in tables.items():
        path = os.path.join(agent.quotes_dir, f"{room}.md")
        parts = [f"# {titles[room]} 报价汇总\n"]
        for day in sorted(days):
            parts.append(f"\n## {day}\n\n{MD_HEADER}\n{MD_SEP}\n" + "\n".join(days[day]) + "\n")
        with open(path, "w", encoding="utf-8") as f:
            f.write("".join(parts))
        log(f"wrote {path} days={len(days)}")

    log(f"done: analyzed={len(records)} quotes={done} skipped={skipped}")
    return 0


def main():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根
    ap = argparse.ArgumentParser(description="群聊报价提取 Agent（回填模式）")
    ap.add_argument("--config", default=os.path.join(base, "agent.config.json"))
    ap.add_argument("--data-dir", default=os.path.join(base, "data"))
    ap.add_argument("--backfill", action="store_true", help="遍历 messages.jsonl 全量重建报价 md")
    args = ap.parse_args()
    if args.backfill:
        sys.exit(backfill(args.config, args.data_dir))
    ap.print_help()


if __name__ == "__main__":
    main()
