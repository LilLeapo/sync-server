#!/usr/bin/env python3
"""群聊被 @ 时，用 Agent 生成回复，经下行队列发回该群。

只处理同时满足以下条件的消息：
  - roomId 以 @chatroom 结尾（群聊）
  - atMe 为真（模块用 msgSource 的 atuserlist / notify@all 判定）
  - type == 1（文本）
  - content 非空

私聊、没被 @ 的群消息、图片消息都不回复。同一 msgSvrId 只回复一次。
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading

WECHAT_REPLY_SYSTEM = """你是微信群里的助手，只有在被 @ 时才会看到这条消息。
用简体中文直接回答，控制在一两段。不要使用 Markdown 标题、代码块或表格。
不要 @ 任何人，也不要复述这段说明。不知道就说不知道，不要编造。"""

MAX_REPLY_CHARS = 1500


class WechatReplier:
    def __init__(self, bridge, send, state_path: str | None = None, *, store=None, quote_log=None):
        """bridge: PiBridge（需有 begin_session）。send(talker, content) -> bool。

        state_path 记住网页开关；缺省文件时默认开启。
        """
        self.bridge = bridge
        self.send = send
        self.store = store
        self.quote_log = quote_log
        self._state_path = state_path
        self._flag_lock = threading.Lock()
        self._enabled = True
        self._load()
        self._seen: set = set()
        self._seen_lock = threading.Lock()
        self._sessions: dict = {}
        self._sessions_lock = threading.Lock()
        self._q: "queue.Queue[dict]" = queue.Queue()
        threading.Thread(target=self._loop, daemon=True, name="wechat-reply").start()

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
        if isinstance(msg, dict):
            self._q.put(msg)

    def handle(self, msg: dict) -> str | None:
        """同步处理。开关关闭、不该回、或模型没给出文本时返回 None。"""
        if not self.enabled:
            return None
        if not self._eligible(msg) or not self._mark(msg):
            return None
        text = self._answer(msg)
        if not text:
            return None
        room = str(msg.get("roomId") or "")
        try:
            ok = self.send(room, text)
        except Exception as e:
            print(f"[wechat_reply] send failed room={room}: {e}", file=sys.stderr)
            return None
        if not ok:
            return None
        print(f"[wechat_reply] queued room={room} chars={len(text)}")
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
        svr = msg.get("msgSvrId")
        if svr in (None, "", 0, "0"):
            key = (msg.get("roomId"), msg.get("createTime"), msg.get("content"))
        else:
            key = str(svr)
        with self._seen_lock:
            if key in self._seen:
                return False
            if len(self._seen) > 2000:
                self._seen.clear()
            self._seen.add(key)
        return True

    def _answer(self, msg: dict) -> str | None:
        room = str(msg.get("roomId") or "")
        sender = str(msg.get("senderName") or msg.get("sender") or "").strip()
        content = str(msg.get("content") or "").strip()
        prompt = f"群成员 {sender} 说：{content}" if sender else content
        try:
            out = self._session(room).ask(prompt)
        except Exception as e:
            print(f"[wechat_reply] agent failed room={room}: {e}", file=sys.stderr)
            return None
        text = (out or "").strip()
        if not text:
            return None
        return text[:MAX_REPLY_CHARS]

    def _session(self, room: str):
        with self._sessions_lock:
            sess = self._sessions.get(room)
            if sess is None:
                from .qa_tools import build_qa_tools
                tools = build_qa_tools(self.store, self.quote_log) if self.quote_log is not None else []
                system = WECHAT_REPLY_SYSTEM + f"\n当前微信群roomId：{room}。未指定其他群时查询使用这个room。"
                sess = self.bridge.begin_session(f"wechat-{room}", system, tools)
                self._sessions[room] = sess
            return sess

    def _loop(self):
        while True:
            msg = self._q.get()
            try:
                self.handle(msg)
            except Exception as e:
                print(f"[wechat_reply] {e}", file=sys.stderr)
