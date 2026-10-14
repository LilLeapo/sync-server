#!/usr/bin/env python3
"""群聊被 @ 时，用 Agent 生成回复，经下行队列发回该群。

只处理同时满足以下条件的消息：
  - roomId 以 @chatroom 结尾（群聊）
  - atMe 为真（模块用 msgSource 的 atuserlist / notify@all 判定）
  - type == 1（文本）
  - content 非空

私聊、没被 @ 的群消息、图片消息都不回复。

多设备时每台设备（每个微信号）各是一个独立 Agent：会话记忆按「设备 + 群」分开，
问答工具只查本设备的数据，去重按「设备 + msgSvrId」——同一台设备重复投递只回一次，
两台设备都被 @（如 @所有人）则各自回复。每台设备的回复开关、提示词、可用工具
来自 DeviceSettings（设备的 Agent 不一定做报价）。

每条被 @ 的消息都记进回复记录（ReplyLog）：回了什么、为什么没回、是否已被手机取走。
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time

from .device_settings import DEFAULTS as DEVICE_DEFAULTS

WECHAT_REPLY_SYSTEM = """你是微信群里的助手，只有在被 @ 时才会看到这条消息。
用简体中文直接回答，控制在一两段。不要使用 Markdown 标题、代码块或表格。
不要 @ 任何人，也不要复述这段说明。不知道就说不知道，不要编造。"""

MAX_REPLY_CHARS = 1500
MAX_LOG = 500  # 回复记录保留条数
MAX_EARLY = 200  # 回执先于登记到达时暂存的条数上限


def _now_ms() -> int:
    return int(time.time() * 1000)


class ReplyLog:
    """被 @ 后每次处理的记录，落盘 data/replies.json（只保留最近 MAX_LOG 条）。

    status：answering 生成中 / queued 待手机取走 / delivered 已被手机取走 /
    skipped 开关关闭未回复 / failed 模型或下发失败 / dropped 待发队列溢出被丢弃 /
    lost 服务重启时还没确认下发。每次变更递增 rev，网页按 rev 增量拉取，
    状态变化（如待取走 → 已取走）也能拉到。
    """

    def __init__(self, path: str | None = None):
        self.path = path
        self._lock = threading.Lock()
        self._items: list[dict] = []
        self._by_id: dict[int, dict] = {}
        self._by_send: dict[str, dict] = {}
        self._early: dict[str, tuple] = {}  # 手机取走得比登记还快时，先记下回执
        self._next_id = 1
        self._rev = 0
        self._load()

    def add(self, msg: dict, status: str, reason: str = "") -> int:
        entry = {
            "at": _now_ms(),
            "deviceId": str(msg.get("deviceId") or ""),
            "roomId": str(msg.get("roomId") or ""),
            "roomName": msg.get("roomName") or "",
            "sender": str(msg.get("sender") or ""),
            "senderName": msg.get("senderName") or "",
            "msgSvrId": str(msg.get("msgSvrId") or ""),
            "question": str(msg.get("content") or "").strip(),
            "reply": "",
            "status": status,
            "reason": reason,
            "deliveredAt": 0,
        }
        with self._lock:
            entry["id"] = self._next_id
            self._next_id += 1
            self._items.append(entry)
            self._by_id[entry["id"]] = entry
            self._touch(entry)
            while len(self._items) > MAX_LOG:
                old = self._items.pop(0)
                self._by_id.pop(old["id"], None)
                self._by_send.pop(old.get("sendId") or "", None)
            self._save_locked()
            return entry["id"]

    def update(self, entry_id: int, **fields):
        with self._lock:
            entry = self._by_id.get(entry_id)
            if entry is None:
                return
            entry.update(fields)
            self._touch(entry)
            self._save_locked()

    def queued(self, entry_id: int, reply: str, send_id: str = ""):
        """回复已进下行队列；send_id 用来对上之后的「已取走 / 被丢弃」回执。"""
        with self._lock:
            entry = self._by_id.get(entry_id)
            if entry is None:
                return
            entry.update(status="queued", reply=reply, sendId=send_id)
            if send_id:
                early = self._early.pop(send_id, None)
                if early is not None:
                    self._apply_send(entry, *early)
                else:
                    self._by_send[send_id] = entry
            self._touch(entry)
            self._save_locked()

    def mark_send(self, send_id: str, status: str, device_id: str = ""):
        """RelayListener.on_send_status 回调：下行指令被手机取走或因队列溢出丢弃。"""
        with self._lock:
            entry = self._by_send.pop(send_id, None)
            if entry is None:
                if len(self._early) >= MAX_EARLY:
                    self._early.pop(next(iter(self._early)))
                self._early[send_id] = (status, device_id, _now_ms())
                return
            self._apply_send(entry, status, device_id, _now_ms())
            self._touch(entry)
            self._save_locked()

    @staticmethod
    def _apply_send(entry: dict, status: str, device_id: str, at: int):
        if status == "delivered":
            entry.update(status="delivered", deliveredAt=at, deliveredTo=device_id)
        else:
            entry.update(status="dropped", reason="设备离线太久，待发队列溢出，这条回复被丢弃")

    def list(self, after_rev: int = 0, device: str | None = None, limit: int = 200) -> tuple[list[dict], int]:
        with self._lock:
            rows = [dict(e) for e in self._items
                    if e["rev"] > after_rev and (device is None or e["deviceId"] == device)]
            return rows[-limit:], self._rev

    def _touch(self, entry: dict):
        self._rev += 1
        entry["rev"] = self._rev

    def _load(self):
        if not self.path or not os.path.isfile(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            items = [e for e in data.get("items") or [] if isinstance(e, dict) and "id" in e]
        except Exception as e:
            print(f"[wechat_reply] 读取回复记录失败: {e}", file=sys.stderr)
            return
        self._items = items[-MAX_LOG:]
        self._by_id = {e["id"]: e for e in self._items}
        self._next_id = max([int(data.get("nextId") or 1)] + [e["id"] + 1 for e in self._items])
        self._rev = max([int(data.get("rev") or 0)] + [int(e.get("rev") or 0) for e in self._items])
        # 下行队列只在内存里，重启前还没确认的回复已经无从追踪
        for e in self._items:
            if e.get("status") in ("answering", "queued"):
                e.update(status="lost", reason="服务重启，没能确认这条回复是否发出")
                self._touch(e)

    def _save_locked(self):
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"nextId": self._next_id, "rev": self._rev, "items": self._items},
                          f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError as e:
            print(f"[wechat_reply] 保存回复记录失败: {e}", file=sys.stderr)


class WechatReplier:
    def __init__(self, bridge, send, state_path: str | None = None, *, store=None, quote_log=None,
                 settings=None, log_path: str | None = None):
        """bridge: PiBridge（需有 begin_session）。
        send(talker, content, device_id) -> 指令 id（str）或 bool，假值表示没能入队。

        device_id 为触发本次回复的那台设备：下行只投给它，
        避免多设备同时监同一个群时由别的设备代发。
        state_path 记住网页的总开关；缺省文件时默认开启。
        settings: DeviceSettings，每台设备的回复开关/提示词/工具；缺省时全部开启。
        """
        self.bridge = bridge
        self.send = send
        self.store = store
        self.quote_log = quote_log
        self.settings = settings
        self.log = ReplyLog(log_path)
        self._state_path = state_path
        self._flag_lock = threading.Lock()
        self._enabled = True
        self._load()
        self._seen: set = set()
        self._seen_lock = threading.Lock()
        self._sessions: dict = {}
        self._sessions_lock = threading.Lock()
        # 每台设备一条队列 + 一个工作线程：某台设备的慢回复不会堵住其他设备
        self._queues: "dict[str, queue.Queue[dict]]" = {}
        self._queues_lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        with self._flag_lock:
            return self._enabled

    def set_enabled(self, on: bool) -> bool:
        on = bool(on)
        with self._flag_lock:
            self._enabled = on
            self._save()
        print(f"[wechat_reply] enabled={on}")
        return on

    def _load(self):
        if not self._state_path or not os.path.isfile(self._state_path):
            return
        try:
            with open(self._state_path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "enabled" in data:
                self._enabled = bool(data["enabled"])
        except Exception as e:
            print(f"[wechat_reply] 读取开关失败: {e}", file=sys.stderr)

    def _save(self):
        if not self._state_path:
            return
        try:
            os.makedirs(os.path.dirname(self._state_path) or ".", exist_ok=True)
            tmp = self._state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"enabled": self._enabled}, f)
            os.replace(tmp, self._state_path)
        except Exception as e:
            print(f"[wechat_reply] 保存开关失败: {e}", file=sys.stderr)

    def enqueue(self, msg: dict):
        # 先筛掉不该回的消息，只为真正被 @ 过的设备起工作线程
        if not isinstance(msg, dict) or not self._eligible(msg):
            return
        device_id = str(msg.get("deviceId") or "")
        with self._queues_lock:
            q = self._queues.get(device_id)
            if q is None:
                q = self._queues[device_id] = queue.Queue()
                threading.Thread(target=self._loop, args=(q,), daemon=True,
                                 name=f"wechat-reply-{device_id or '-'}").start()
        q.put(msg)

    def device_settings(self, device_id: str) -> dict:
        if self.settings is None:
            return dict(DEVICE_DEFAULTS)
        return self.settings.get(device_id)

    def on_send_status(self, send_id: str, status: str, device_id: str = ""):
        self.log.mark_send(send_id, status, device_id)

    def handle(self, msg: dict) -> str | None:
        """同步处理。回复进了下行队列时返回回复文本，否则返回 None。

        被 @ 却没回复（开关关闭、模型失败、下发失败）也记进回复记录，方便在网页上排查。
        """
        if not self._eligible(msg):
            return None
        device_id = str(msg.get("deviceId") or "")
        if not self.enabled:
            self.log.add(msg, "skipped", "自动回复总开关已关闭")
            return None
        cfg = self.device_settings(device_id)
        if not cfg["reply"]:
            self.log.add(msg, "skipped", "这台设备的自动回复已关闭")
            return None
        if not self._mark(msg):
            return None
        entry = self.log.add(msg, "answering")
        text, err = self._answer(msg, cfg)
        if not text:
            self.log.update(entry, status="failed", reason=err)
            return None
        room = str(msg.get("roomId") or "")
        try:
            sent = self.send(room, text, device_id)
        except Exception as e:
            print(f"[wechat_reply] send failed room={room}: {e}", file=sys.stderr)
            self.log.update(entry, status="failed", reply=text, reason=f"下发失败：{e}")
            return None
        if not sent:
            self.log.update(entry, status="failed", reply=text, reason="下发失败：手机长连接端口没有开启")
            return None
        self.log.queued(entry, text, sent if isinstance(sent, str) else "")
        print(f"[wechat_reply] queued room={room} device={device_id or '-'} chars={len(text)}")
        return text

    @staticmethod
    def _eligible(msg: dict) -> bool:
        room = str(msg.get("roomId") or "")
        if not room.endswith("@chatroom"):
            return False
        if not msg.get("atMe"):
            return False
        try:
            if int(msg.get("type") or 0) != 1:
                return False
        except (TypeError, ValueError):
            return False
        return bool(str(msg.get("content") or "").strip())

    def _mark(self, msg: dict) -> bool:
        device_id = str(msg.get("deviceId") or "")
        svr = msg.get("msgSvrId")
        if svr in (None, "", 0, "0"):
            key = (device_id, msg.get("roomId"), msg.get("createTime"), msg.get("content"))
        else:
            key = (device_id, str(svr))
        with self._seen_lock:
            if key in self._seen:
                return False
            if len(self._seen) > 2000:
                self._seen.clear()
            self._seen.add(key)
        return True

    def _answer(self, msg: dict, cfg: dict) -> tuple[str | None, str]:
        """返回 (回复文本, 失败原因)。"""
        room = str(msg.get("roomId") or "")
        device_id = str(msg.get("deviceId") or "")
        sender = str(msg.get("senderName") or msg.get("sender") or "").strip()
        content = str(msg.get("content") or "").strip()
        prompt = f"群成员 {sender} 说：{content}" if sender else content
        try:
            out = self._session(room, device_id, cfg).ask(prompt)
        except Exception as e:
            print(f"[wechat_reply] agent failed room={room}: {e}", file=sys.stderr)
            return None, f"Agent 调用失败：{e}"
        text = (out or "").strip()
        if not text:
            return None, "模型没有给出回复（调用失败或返回为空，详见服务日志）"
        return text[:MAX_REPLY_CHARS], ""

    def _session(self, room: str, device_id: str = "", cfg: dict | None = None):
        """按「设备 + 群」复用会话；设备的提示词或工具改了就重开（旧会话记忆随之丢弃）。"""
        cfg = cfg or self.device_settings(device_id)
        sig = (cfg["prompt"], None if cfg["tools"] is None else tuple(cfg["tools"]))
        key = (device_id, room)
        with self._sessions_lock:
            cur = self._sessions.get(key)
            if cur is not None and cur[0] == sig:
                return cur[1]
            from .qa_tools import build_qa_tools
            tools = (build_qa_tools(self.store, self.quote_log, device=device_id)
                     if self.quote_log is not None else [])
            if cfg["tools"] is not None:
                allowed = set(cfg["tools"])
                tools = [t for t in tools if t["name"] in allowed]
            system = WECHAT_REPLY_SYSTEM
            if cfg["prompt"]:
                system += "\n\n这台设备的职责说明：\n" + cfg["prompt"]
            system += f"\n当前微信群roomId：{room}。未指定其他群时查询使用这个room。"
            session_id = f"wechat-{device_id}-{room}" if device_id else f"wechat-{room}"
            sess = self.bridge.begin_session(session_id, system, tools)
            self._sessions[key] = (sig, sess)
            return sess

    def _loop(self, q: "queue.Queue[dict]"):
        while True:
            msg = q.get()
            try:
                self.handle(msg)
            except Exception as e:
                print(f"[wechat_reply] {e}", file=sys.stderr)
