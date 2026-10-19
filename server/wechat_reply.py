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

每条被 @ 的消息都记进回复记录（ReplyLog）：回了什么、用了哪些工具、为什么没回、是否已被手机取走。
Agent 经 send_message / send_image 工具代发的消息也各记一条（kind=agent_send）。

会话（SessionManager，命名空间 wechat）：闲置 session_ttl（默认 2 小时）后过期，可落盘恢复；
群里 @ 机器人说「/reset」或「重置对话」清空本设备在该群的记忆。每轮附带该群在这条 @ 之前的
最近几条消息作为背景（只用于本轮，不进会话历史）。
"""
from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time

from .agent_prompts import WECHAT_REPLY_SYSTEM  # noqa: F401  (sync_server 经此引用)
from .agent_sessions import SessionManager
from .device_settings import DEFAULTS as DEVICE_DEFAULTS

MAX_REPLY_CHARS = 1500
MAX_LOG = 500  # 回复记录保留条数
MAX_EARLY = 200  # 回执先于登记到达时暂存的条数上限
DEFAULT_SESSION_TTL = 7200  # 群会话闲置 2 小时后重开
CONTEXT_MESSAGES = 12  # 每轮附带的群内最近消息条数
MAX_TURN_SENDS = 3  # 每轮最多经发送工具代发的条数
RESET_WORDS = {"/reset", "/new", "重置对话", "新对话", "清空上下文"}
MENTION_PREFIX_RE = re.compile(r"^(?:@[^\s\u2005]+[\s\u2005]*)+")
RESET_REPLY = "已开启新对话，之前的上下文已清空。"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _norm(text) -> str:
    return re.sub(r"\s+", "", str(text or ""))


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

    def add(self, msg: dict, status: str, reason: str = "", kind: str = "reply") -> int:
        """kind：reply 被 @ 后的回复；agent_send Agent 经发送工具代发。"""
        entry = {
            "kind": kind,
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

    def sent_texts(self, device_id: str, room: str, limit: int = 100) -> set[str]:
        """本设备最近发到这个群的文字（回复与代发，已入队或已取走），用于从群聊背景里剔除自己说过的话。"""
        with self._lock:
            rows = [e for e in self._items[-limit * 5:] if e["deviceId"] == device_id and e["roomId"] == room
                    and e.get("reply") and e["status"] in ("queued", "delivered")]
        return {_norm(e["reply"]) for e in rows[-limit:]}

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
                 settings=None, log_path: str | None = None, session_ttl: float = DEFAULT_SESSION_TTL,
                 sessions_dir: str | None = None, max_sessions: int = 500, device_caps=None):
        """bridge: PiBridge（需有 begin_session）。
        send(talker, content, device_id, image_path=None, image_url=None) -> 指令 id（str）或 bool，
        假值表示没能入队（纯文字时只传前三个参数）。

        device_id 为触发本次回复的那台设备：下行只投给它，
        避免多设备同时监同一个群时由别的设备代发。
        state_path 记住网页的总开关；缺省文件时默认开启。
        settings: DeviceSettings，每台设备的回复开关/提示词/工具；缺省时全部开启（opt_in 工具除外）。
        sessions_dir: 会话落盘目录（None 不落盘）；device_caps(device_id) -> 握手声明的能力集合。
        """
        self.bridge = bridge
        self.send = send
        self.store = store
        self.quote_log = quote_log
        self.settings = settings
        self.device_caps = device_caps
        self.log = ReplyLog(log_path)
        self._state_path = state_path
        self._flag_lock = threading.Lock()
        self._enabled = True
        self._load()
        self._seen: set = set()
        self._seen_lock = threading.Lock()
        self.sessions = SessionManager("wechat", ttl=session_ttl, persist_dir=sessions_dir,
                                       max_sessions=max_sessions)
        # 正在处理的 @（按设备）：发送工具据此限流、记录来源
        self._active: dict[str, dict] = {}
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
        room = str(msg.get("roomId") or "")
        entry = self.log.add(msg, "answering")
        if self._strip_mention(msg.get("content")) in RESET_WORDS:
            self.sessions.reset(self._key(device_id, room))
            return self._deliver(entry, room, device_id, RESET_REPLY)
        text, err, turn = self._answer(msg, cfg)
        if turn:
            self.log.update(entry, tools=list(turn.get("tools") or []), usage=dict(turn.get("usage") or {}))
        if not text:
            self.log.update(entry, status="failed", reason=err)
            return None
        return self._deliver(entry, room, device_id, text)

    def _deliver(self, entry: int, room: str, device_id: str, text: str) -> str | None:
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

    @staticmethod
    def _strip_mention(content) -> str:
        return MENTION_PREFIX_RE.sub("", str(content or "").strip()).strip()

    @staticmethod
    def _key(device_id: str, room: str) -> str:
        return f"{device_id or '-'}|{room}"

    def _answer(self, msg: dict, cfg: dict) -> tuple[str | None, str, dict | None]:
        """返回 (回复文本, 失败原因, 本轮信息 last_turn)。"""
        room = str(msg.get("roomId") or "")
        device_id = str(msg.get("deviceId") or "")
        sender = str(msg.get("senderName") or msg.get("sender") or "").strip()
        content = str(msg.get("content") or "").strip()
        prompt = f"群成员 {sender} 说：{content}" if sender else content
        self._active[device_id] = {"msg": msg, "sends": 0}
        sess = None
        try:
            sess = self._session(room, device_id, cfg)
            out = sess.ask(prompt, context=self._room_context(msg))
        except Exception as e:
            print(f"[wechat_reply] agent failed room={room}: {e}", file=sys.stderr)
            return None, f"Agent 调用失败：{e}", None
        finally:
            self._active.pop(device_id, None)
        turn = getattr(sess, "last_turn", None)
        turn = turn if isinstance(turn, dict) else None
        text = (out or "").strip() if isinstance(out, str) else ""
        if not text:
            if turn and turn.get("error"):
                return None, f"Agent 调用失败：{turn['error']}", turn
            return None, "模型没有给出回复（调用失败或返回为空，详见服务日志）", turn
        return text[:MAX_REPLY_CHARS], "", turn

    def _room_context(self, msg: dict) -> str | None:
        """这条 @ 之前该群（同一设备）最近几条消息，只作本轮背景。"""
        if self.store is None or not hasattr(self.store, "query_messages"):
            return None
        from .qa_tools import message_line
        try:
            at = int(msg.get("createTime") or 0)
        except (TypeError, ValueError):
            at = 0
        room, device_id = str(msg.get("roomId") or ""), str(msg.get("deviceId") or "")
        rows = self.store.query_messages(room=room, device=device_id, before_ms=at, limit=CONTEXT_MESSAGES * 2,
                                         exclude_svr=msg.get("msgSvrId"))
        # 手机模块若把本机发出的回复也上报了，这里剔掉：它们已在会话历史里，重复出现只会让模型反复强调同一件事
        own = self.log.sent_texts(device_id, room)
        rows = [r for r in rows if _norm((r.get("msg") or {}).get("content")) not in own][:CONTEXT_MESSAGES]
        if not rows:
            return None
        return "本群在这条 @ 之前的最近消息（按时间顺序）：\n" + "\n".join(message_line(r) for r in reversed(rows))

    def _session(self, room: str, device_id: str = "", cfg: dict | None = None):
        """按「设备 + 群」复用会话；设备的提示词或工具改了就重开（旧会话记忆随之丢弃）。"""
        cfg = cfg or self.device_settings(device_id)
        sig = (cfg["prompt"], None if cfg["tools"] is None else tuple(cfg["tools"]))

        def factory():
            from .qa_tools import build_qa_tools
            tools = (build_qa_tools(self.store, self.quote_log, device=device_id,
                                    sender=self._sender(device_id), opt_in=True)
                     if self.quote_log is not None or self.store is not None else [])
            if cfg["tools"] is None:  # 「全部工具」不含对外发送类（opt_in）工具
                tools = [t for t in tools if not t.get("opt_in")]
            else:
                allowed = set(cfg["tools"])
                tools = [t for t in tools if t["name"] in allowed]
            system = WECHAT_REPLY_SYSTEM
            if cfg["prompt"]:
                system += "\n\n这台设备的职责说明：\n" + cfg["prompt"]
            system += f"\n当前微信群roomId：{room}。未指定其他群时查询使用这个room。"
            session_id = f"wechat-{device_id}-{room}" if device_id else f"wechat-{room}"
            return self.bridge.begin_session(session_id, system, tools)

        return self.sessions.get(self._key(device_id, room), factory, sig=sig)

    def _sender(self, device_id: str):
        """发送工具的执行端：只在处理 @ 的过程中可用，每轮最多 MAX_TURN_SENDS 条，逐条记进回复记录。"""
        def send(room_id: str, room_label: str, text: str = "", image_path: str = "", server_image: str = "") -> str:
            active = self._active.get(device_id)
            if active is None:
                return "只能在回复群消息的过程中发送，没有发送。"
            if active["sends"] >= MAX_TURN_SENDS:
                return f"本轮已代发 {MAX_TURN_SENDS} 条，达到上限，没有发送。"
            image_url = ""
            if server_image:
                caps = set(self.device_caps(device_id) or ()) if self.device_caps else set()
                if "imageUrl" not in caps:
                    return "这台设备的手机模块没有声明支持服务器图片下发（imageUrl），没有发送；可以改用手机上的图片路径。"
                image_url = "/" + server_image.lstrip("/")
            origin = active["msg"]
            label = text or f"[图片] {image_path or server_image}"
            entry = self.log.add({
                "deviceId": device_id, "roomId": room_id, "roomName": room_label,
                "sender": origin.get("sender"), "senderName": origin.get("senderName"),
                "content": f"（Agent 代发，来自「{origin.get('roomName') or origin.get('roomId')}」里的 @）"
                           + str(origin.get("content") or "")}, "answering", kind="agent_send")
            try:
                if image_path or image_url:
                    sent = self.send(room_id, text, device_id, image_path=image_path or None, image_url=image_url or None)
                else:
                    sent = self.send(room_id, text, device_id)
            except Exception as e:
                self.log.update(entry, status="failed", reply=label, reason=f"下发失败：{e}")
                return f"发送失败：{e}"
            if not sent:
                self.log.update(entry, status="failed", reply=label, reason="下发失败：手机长连接端口没有开启")
                return "发送失败：手机长连接没有开启。"
            active["sends"] += 1
            self.log.queued(entry, label, sent if isinstance(sent, str) else "")
            print(f"[wechat_reply] agent_send room={room_id} device={device_id or '-'} image={bool(image_path or image_url)}")
            return f"已排队发送到「{room_label}」（{room_id}），手机取走后发出。"
        return send

    def _loop(self, q: "queue.Queue[dict]"):
        while True:
            msg = q.get()
            try:
                self.handle(msg)
            except Exception as e:
                print(f"[wechat_reply] {e}", file=sys.stderr)
