#!/usr/bin/env python3
"""Agent 会话管理 —— 各机器人与群 @ 回复共用的会话缓存。

    sessions = SessionManager("feishu", ttl=1800, persist_dir="data/agent_sessions")
    conv = sessions.get(chat_id, lambda: bridge.begin_session(f"feishu-{chat_id}", system, tools))
    conv.ask(text, context=...)
    sessions.reset(chat_id)          # /reset：清空这个会话的记忆

- get 原子地「取或建」：同一个 key 并发只建一次；不同 key 互不阻塞（建会话可能要构建工具）
- sig 变了（如设备的提示词/工具改了）就重建；执行器卡死（broken）的会话也会重建
- 闲置超过 ttl 的会话过期（ttl<=0 永不过期）；超过 max_sessions 按最久未用淘汰（只出内存，落盘保留）
- 落盘：每轮提交后写 <persist_dir>/<namespace>-<sha1(key)>.json；重启后同 key 首次访问时恢复，
  前提是没过期、且 system prompt 与工具没变；过期或 /reset 时删除文件
- 只有 pi_bridge.Conversation 会落盘；测试里的假会话照常缓存
"""
from __future__ import annotations

import collections
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import weakref

_MANAGERS: "weakref.WeakSet[SessionManager]" = weakref.WeakSet()
FILE_VERSION = 1


def _fingerprint(session, sig) -> str:
    tools = sorted(str(t.get("name")) for t in getattr(session, "tools", None) or [] if isinstance(t, dict))
    raw = json.dumps([str(getattr(session, "system", "")), tools, repr(sig)], ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


class _Entry:
    __slots__ = ("session", "sig", "last_used", "created")

    def __init__(self, session, sig):
        self.session = session
        self.sig = sig
        self.last_used = self.created = time.time()


class SessionManager:
    def __init__(self, namespace: str, *, ttl: float = 1800, max_sessions: int = 500,
                 persist_dir: str | None = None, log=None):
        self.namespace = namespace
        self.ttl = float(ttl or 0)
        self.max_sessions = max(int(max_sessions or 0), 1)
        self.persist_dir = persist_dir
        self.log = log or (lambda msg: print(msg, file=sys.stderr))
        self._lock = threading.Lock()
        self._entries: "collections.OrderedDict[str, _Entry]" = collections.OrderedDict()
        self._key_locks: dict[str, threading.Lock] = {}
        _MANAGERS.add(self)
        self._clean_disk()

    # -- 取/建 ----------------------------------------------------

    def get(self, key, factory, sig=None):
        key = str(key)
        with self._lock:
            self._sweep_locked(time.time())
            hit = self._hit_locked(key, sig)
            if hit is not None:
                return hit
            klock = self._key_locks.setdefault(key, threading.Lock())
        with klock:
            with self._lock:
                hit = self._hit_locked(key, sig)
                if hit is not None:
                    return hit
                stale = self._entries.pop(key, None)
            if stale is not None and stale.sig != sig:
                self._delete_file(key)  # 提示词/工具变了：旧记忆不再适用
            session = factory()
            self._attach(key, session, sig)
            with self._lock:
                self._entries[key] = _Entry(session, sig)
                while len(self._entries) > self.max_sessions:
                    self._entries.popitem(last=False)
            return session

    def _hit_locked(self, key, sig):
        entry = self._entries.get(key)
        if entry is None or entry.sig != sig or getattr(entry.session, "broken", False) is True:
            return None
        entry.last_used = time.time()
        self._entries.move_to_end(key)
        return entry.session

    def reset(self, key) -> bool:
        key = str(key)
        with self._lock:
            entry = self._entries.pop(key, None)
        deleted = self._delete_file(key)
        return entry is not None or deleted

    def sweep(self) -> int:
        with self._lock:
            return self._sweep_locked(time.time())

    def _sweep_locked(self, now: float) -> int:
        if self.ttl <= 0:
            return 0
        expired = [k for k, e in self._entries.items() if now - e.last_used > self.ttl]
        for key in expired:
            del self._entries[key]
            self._delete_file(key)
        return len(expired)

    # -- 观测 -----------------------------------------------------

    def stats(self) -> list[dict]:
        with self._lock:
            items = list(self._entries.items())
        out = []
        for key, entry in items:
            row = {"namespace": self.namespace, "key": key, "lastAccessAt": int(entry.last_used * 1000),
                   "expiresAt": int((entry.last_used + self.ttl) * 1000) if self.ttl > 0 else None}
            fn = getattr(entry.session, "stats", None)
            if callable(fn) and type(entry.session).__name__ == "Conversation":
                row.update(fn())
            out.append(row)
        return out

    # -- 落盘 -----------------------------------------------------

    def _path(self, key: str) -> str | None:
        if not self.persist_dir:
            return None
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        return os.path.join(self.persist_dir, f"{self.namespace}-{digest}.json")

    def _attach(self, key: str, session, sig):
        if type(session).__name__ != "Conversation" or not self.persist_dir:
            return
        fp = _fingerprint(session, sig)
        path = self._path(key)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            fresh = self.ttl <= 0 or time.time() - float(data.get("lastUsed") or 0) <= self.ttl
            if data.get("v") == FILE_VERSION and data.get("key") == key and data.get("fingerprint") == fp and fresh:
                session.restore(data)
        except FileNotFoundError:
            pass
        except Exception as e:
            self.log(f"[agent_sessions] 恢复会话失败 {self.namespace}/{key}: {e}")
        session.on_commit = lambda conv, k=key, f=fp: self._persist(k, conv, f)

    def _persist(self, key: str, session, fp: str):
        path = self._path(key)
        if not path:
            return
        data = {"v": FILE_VERSION, "namespace": self.namespace, "key": key, "fingerprint": fp,
                "sessionId": getattr(session, "session_id", ""), **session.export()}
        try:
            os.makedirs(self.persist_dir, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.persist_dir, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError as e:
            self.log(f"[agent_sessions] 保存会话失败 {self.namespace}/{key}: {e}")

    def _delete_file(self, key: str) -> bool:
        path = self._path(key)
        if not path:
            return False
        try:
            os.remove(path)
            return True
        except OSError:
            return False

    def _clean_disk(self):
        """启动时删掉本命名空间里已过期的会话文件。"""
        if not self.persist_dir or self.ttl <= 0 or not os.path.isdir(self.persist_dir):
            return
        now = time.time()
        for name in os.listdir(self.persist_dir):
            path = os.path.join(self.persist_dir, name)
            if name.endswith(".tmp"):
                try:
                    if now - os.path.getmtime(path) > 3600:
                        os.remove(path)
                except OSError:
                    pass
                continue
            if not name.startswith(self.namespace + "-") or not name.endswith(".json"):
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("namespace") == self.namespace and now - float(data.get("lastUsed") or 0) > self.ttl:
                    os.remove(path)
            except (OSError, ValueError):
                continue


def all_stats() -> list[dict]:
    rows = []
    for manager in list(_MANAGERS):
        rows.extend(manager.stats())
    rows.sort(key=lambda r: r.get("lastAccessAt") or 0, reverse=True)
    return rows


def reset(namespace: str, key: str) -> bool:
    return any(m.reset(key) for m in list(_MANAGERS) if m.namespace == namespace)


def session_settings(bridge) -> dict:
    """agent.config.json 的 session 段（bridge.session_cfg）；假 bridge 时返回空。"""
    cfg = getattr(bridge, "session_cfg", None)
    return cfg if isinstance(cfg, dict) else {}


def sessions_dir(data_dir: str | None, bridge) -> str | None:
    if not data_dir or session_settings(bridge).get("persist", True) is False:
        return None
    return os.path.join(data_dir, "agent_sessions")
