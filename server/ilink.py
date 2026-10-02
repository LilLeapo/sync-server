#!/usr/bin/env python3
"""微信 iLink Bot 私聊通道集成 —— 多账号（一进程多轮询）+ 智能问答 + 报价推送。

iLink Bot 是腾讯官方 `@tencent-weixin/openclaw-weixin`（微信个人号 1v1 助手）协议，
底层由第三方 Python SDK [`weixin-ilink`](https://pypi.org/project/weixin-ilink/) 实现
（扫码登录 + 长轮询 + 文本/图片/语音/文件/视频 + CDN AES 解密）。
**注意：iLink bot 不能进微信群，且一个微信号只对应一个能联系上的 BOT ID**，
所以它只作为 1v1 私聊通道，不替代 Android 模块抓群。

架构（对齐官方 openclaw：一个 gateway 进程管多个账号）：
- `ILinkBot`     —— 单个账号的工作线程：一份凭据 → 一个 `WeixinBot` → 一个轮询线程，
                    各自独立 cursor / context_token / 问答会话；
- `ILinkManager` —— 多账号管理：扫描 `accounts_dir` 下所有凭据，逐个起轮询线程；
                    网页扫码新增账号（不覆盖）；报价推送按「哪个账号能发给这个人」路由。

本模块做三件事（都不触发报价提取，即「不动现有抓群链路」）：
1. 收私聊消息：可选并入消息库（`store_messages`，前端以 `iLink 私聊 · <用户>` 分组展示）；
2. 智能问答：插件指令（/help、/quotes…）+ Agent 多轮问答（复用 pi_bridge + qa_tools），
              思考期间微信端显示「对方正在输入…」；
3. 报价推送：`notify_quote` 钩子 → Agent 格式化 → 通过能联系到目标的账号 `send_markdown` 主动推送。

登录：网页「微信扫码」按钮（`/api/ilink/login` + `/api/ilink/qr`），或
      `python -m server.ilink --login [--credentials data/ilink/accounts/<id>.json]`。

配置见 ilink.config.json.example。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import os
import queue
import re
import sys
import threading
import time
import uuid

DEFAULT_CREDS = os.path.join("data", "ilink", "creds.json")
TEXT_LIMIT = 20000


class ILinkError(RuntimeError):
    pass


def _load_sdk():
    """惰性导入 weixin-ilink，未安装时给出明确安装提示。"""
    try:
        import weixin_ilink  # type: ignore
        return weixin_ilink
    except Exception as e:  # pragma: no cover - 依赖缺失路径
        raise ILinkError(
            "iLink 通道需要第三方协议 SDK：pip install weixin-ilink") from e


def _now_label(ms) -> str:
    try:
        return dt.datetime.fromtimestamp(int(ms) / 1000).strftime("%m-%d %H:%M")
    except Exception:
        return ""


def _safe_name(raw: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(raw or "")) or "account"


def _read_creds(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


class _TypingIndicator:
    """在 Agent 思考期间保持微信「对方正在输入…」状态。

    进入上下文时立即发一次，其后每隔 interval 秒续发（微信客户端的输入状态
    会超时消失）；退出时停止续发，实际回复消息会自然清掉该状态。
    """

    def __init__(self, reply_typing, interval: float = 4.0, log=None):
        self._reply = reply_typing
        self._interval = max(float(interval or 4), 1.5)
        self._log = log or (lambda _m: None)
        self._stop = threading.Event()
        self._warned = False

    def __enter__(self):
        self._poke()
        threading.Thread(target=self._run, daemon=True, name="ilink-typing").start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        return False

    def _poke(self):
        try:
            self._reply()
        except Exception as e:  # 个别账号可能不支持，失败不影响问答
            if not self._warned:
                self._warned = True
                self._log(f"「正在输入」状态发送失败（忽略）: {e}")

    def _run(self):
        while not self._stop.wait(self._interval):
            self._poke()


# ---------------------------------------------------------------------------
# 单账号工作线程
# ---------------------------------------------------------------------------

class ILinkBot:
    """一个 iLink 账号的收发/问答工作线程（一份凭据 = 一个轮询线程）。"""

    def __init__(self, cfg: dict, *, store=None, quote_log=None, bridge=None,
                 plugin_host=None, data_dir: str = "data", bot_factory=None):
        self.cfg = cfg
        self.store = store
        self.quote_log = quote_log
        self.bridge = bridge
        self.plugin_host = plugin_host

        self.credentials_file = str(cfg.get("credentials_file") or
                                    os.path.join(data_dir, "ilink", "creds.json"))
        creds = _read_creds(self.credentials_file)
        self.account_id = str(creds.get("accountId") or "")
        self.user_id = str(creds.get("userId") or "")

        self.store_messages = bool(cfg.get("store_messages", True))
        self.allowed_users = {str(u) for u in (cfg.get("allowed_users") or []) if str(u).strip()}

        push = cfg.get("push") or {}
        self.push_enabled = bool(push.get("enabled", True))
        self.push_to = [str(u) for u in (push.get("to") or []) if str(u).strip()]
        self.push_fallback_last = bool(push.get("fallback_last", True))
        self.push_rooms = list(cfg.get("push_rooms") or ["*"])

        qna = cfg.get("qna") or {}
        self.qna_enabled = bool(qna.get("enabled", True))
        self.qna_system = str(qna.get("system_prompt") or "")
        self.qna_recent = int(qna.get("recent_quotes") or 20)
        self.qna_session_ttl = int(qna.get("session_ttl") or 1800)
        self.qna_typing = bool(qna.get("typing", True))
        self.qna_typing_interval = max(float(qna.get("typing_interval") or 4), 1.5)

        self.data_dir = data_dir
        ilink_dir = os.path.join(data_dir, "ilink")
        os.makedirs(ilink_dir, exist_ok=True)
        self.log_path = str(cfg.get("log_file") or os.path.join(ilink_dir, "bot.log"))
        self.state_path = str(cfg.get("state_file") or os.path.join(ilink_dir, "state.json"))

        self.connected = False
        self.last_error = ""
        self.pushed = 0
        self._bot = None
        self._bot_factory = bot_factory or self._default_bot_factory
        self._stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._seen_sigs: set = set()
        self._ctx_tokens: dict[str, str] = {}
        self._ctx_ts: dict[str, float] = {}
        self._last_user = ""
        self._last_activity = 0.0
        self._state_lock = threading.Lock()
        self._load_state()

        self._push_q: "queue.Queue[tuple]" = queue.Queue()
        self._sessions: dict[str, list] = {}
        self._sessions_lock = threading.Lock()
        self._warned_no_bridge = False

        if self.push_enabled:
            threading.Thread(target=self._push_loop, daemon=True,
                             name=f"ilink-push-{self.account_id or 'x'}").start()

    # -- 日志与状态 ------------------------------------------------

    def log(self, msg: str):
        line = f"{dt.datetime.now():%F %T} {msg}\n"
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass
        print(f"[ilink{':' + self.account_id if self.account_id else ''}] {msg}",
              file=sys.stderr)

    def _load_state(self):
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        self._seen_sigs = set(data.get("seen_sigs") or [])
        self._ctx_tokens = {str(k): str(v) for k, v in (data.get("ctx_tokens") or {}).items()}
        self._ctx_ts = {str(k): float(v) for k, v in (data.get("ctx_ts") or {}).items()}
        self._last_user = str(data.get("last_user") or "")

    def _save_state(self):
        try:
            with self._state_lock:
                sigs = list(self._seen_sigs)[-1000:]
                ctx = dict(list(self._ctx_tokens.items())[-500:])
                ctx_ts = {u: self._ctx_ts.get(u, 0.0) for u in ctx}
                payload = {"seen_sigs": sigs, "ctx_tokens": ctx, "ctx_ts": ctx_ts,
                           "last_user": self._last_user,
                           "account_id": self.account_id, "user_id": self.user_id}
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    def _remember_ctx(self, user: str, token: str):
        if not user or not token:
            return
        with self._state_lock:
            self._ctx_tokens[user] = token
            self._ctx_ts[user] = time.time()
            self._last_user = user
        self._save_state()

    def bot_status(self) -> dict:
        return {
            "enabled": True,
            "account_id": self.account_id,
            "user_id": self.user_id,
            "has_credentials": os.path.exists(self.credentials_file),
            "ws_connected": self.connected,
            "ws_error": self.last_error,
            "push_mode": "send" if self.push_enabled else "none",
            "push_configured": bool(self.push_to or self._last_user),
            "push_targets": self.push_to or ([self._last_user] if self._last_user else []),
            "qna_enabled": self.qna_enabled,
            "typing": self.qna_typing,
            "store_messages": self.store_messages,
            "pushed": self.pushed,
            "agent": self.bridge.backend_kind if self.bridge else None,
        }

    # -- 生命周期 --------------------------------------------------

    def _default_bot_factory(self):
        sdk = _load_sdk()
        if not os.path.exists(self.credentials_file):
            raise ILinkError(f"缺少 iLink 凭据 {self.credentials_file}")
        return sdk.WeixinBot(credentials_file=self.credentials_file)

    def start(self):
        if not os.path.exists(self.credentials_file):
            self.log(f"未找到凭据（{self.credentials_file}），跳过")
            return
        self._spawn_poll()

    def _spawn_poll(self):
        if self._poll_thread is not None and self._poll_thread.is_alive():
            return
        self._stop.clear()
        self._poll_thread = threading.Thread(target=self._run, daemon=True,
                                             name=f"ilink-poll-{self.account_id or 'x'}")
        self._poll_thread.start()

    def stop(self):
        self._stop.set()
        bot, self._bot = self._bot, None
        if bot is not None:
            try:
                bot.stop()
            except Exception:
                pass

    def wait_stopped(self, timeout: float = 40):
        t = self._poll_thread
        if t is not None:
            t.join(timeout)

    def _run(self):
        backoff = 5
        while not self._stop.is_set():
            try:
                bot = self._bot_factory()
                self._bot = bot
                self.account_id = str(getattr(bot, "account_id", "") or self.account_id)
                self.connected = True
                self.last_error = ""
                backoff = 5
                self.log(f"长轮询启动 account={self.account_id or '未知'}")
                for msg in bot.messages():
                    if self._stop.is_set():
                        break
                    try:
                        self._on_message(msg)
                    except Exception as e:
                        self.log(f"消息处理异常: {e}")
            except Exception as e:
                self.connected = False
                self.last_error = str(e)
                self.log(f"异常（{backoff}s 后重试）: {e}")
            finally:
                self.connected = False
                bot, self._bot = self._bot, None
                if bot is not None:
                    try:
                        bot.stop()
                    except Exception:
                        pass
            if self._stop.is_set():
                break
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 120)
        self.connected = False

    # -- 消息处理（入库 + 插件指令 + Agent 问答） -------------------

    def _on_message(self, msg):
        user = str(getattr(msg, "from_user", "") or "")
        if not user:
            return
        self._last_activity = time.time()
        if self.allowed_users and user not in self.allowed_users:
            self.log(f"忽略未授权用户 {user}")
            return
        token = str(getattr(msg, "context_token", "") or "")
        if token:
            self._remember_ctx(user, token)

        text = getattr(msg, "text", None)
        if text is None:
            self._reply(msg, "目前只支持文字消息，可以发文字试试～")
            return
        text = str(text).strip()
        if not text:
            return
        self.log(f"收到私聊 user={user} text={text!r}")
        self._store_inbound(msg, user, text)

        api = self.plugin_host.spawn_api() if self.plugin_host else None
        event = {"event": {"message": {"message_id": getattr(msg, "message_id", None),
                                        "chat_id": user, "chat_type": "p2p"},
                           "sender": {"sender_type": "user",
                                      "sender_id": {"open_id": user}}}}
        if api is not None:
            api._set_event(event, text)

        # 1) 插件消息钩子
        if self.plugin_host:
            verdict = self.plugin_host.run_message_hooks(api, event, text)
            if isinstance(verdict, str):
                self._reply(msg, verdict)
                return
            if verdict is False:
                return

        # 2) 斜杠命令
        if text.startswith("/"):
            name, _, args = text[1:].partition(" ")
            name = name.lower()
            entry = self.plugin_host.commands.get(name) if self.plugin_host else None
            if entry:
                self.log(f"command /{name} from {user}")
                out = self.plugin_host.run_command(name, args.strip(), api)
                if out:
                    self._reply(msg, out)
                return
            self._reply(msg, f"未知指令 /{name}，试试 /help")
            return

        # 3) Agent 问答
        if not self.qna_enabled:
            return
        if self.bridge is None:
            if not self._warned_no_bridge:
                self._warned_no_bridge = True
                self.log("问答未启用：未配置 Agent（agent.config.json）")
            self._reply(msg, "问答功能未启用：服务器没有配置 Agent 模型。")
            return
        threading.Thread(target=self._qa_job, args=(msg, user, text),
                         daemon=True, name="ilink-qa").start()

    def _store_inbound(self, msg, user: str, text: str):
        """把私聊消息并入消息库（仅入库展示，不触发报价提取）。"""
        if not self.store_messages or self.store is None:
            return
        now = int(time.time() * 1000)
        mid = getattr(msg, "message_id", None)
        svr = f"ilink-{mid}" if mid is not None else f"ilink-{user}-{uuid.uuid4().hex[:12]}"
        record = {
            "roomId": f"ilink:{user}",
            "roomName": f"iLink 私聊 · {user}",
            "sender": user,
            "senderName": user,
            "content": text,
            "type": 1,
            "createTime": now,
            "msgSvrId": svr,
        }
        try:
            self.store.add_message(record)
        except Exception as e:
            self.log(f"入库失败: {e}")

    # -- 问答 ------------------------------------------------------

    def _qa_job(self, msg, user: str, text: str):
        try:
            session = self._get_session(user)
            ctx = self._qa_context(user)
            if self.qna_typing and hasattr(msg, "reply_typing"):
                with _TypingIndicator(msg.reply_typing, self.qna_typing_interval, self.log):
                    answer = session.ask(text, context=ctx)
            else:
                answer = session.ask(text, context=ctx)
            self._reply(msg, answer or "我暂时回答不了这个问题（模型没有给出结果）。")
        except Exception as e:
            self.log(f"问答失败 user={user}: {e}")
            self._reply(msg, f"处理这个问题时出错了：{e}")

    def _get_session(self, user: str):
        from .pi_bridge import QNA_SYSTEM
        with self._sessions_lock:
            entry = self._sessions.get(user)
            if entry and time.time() - entry[1] < self.qna_session_ttl:
                entry[1] = time.time()
                return entry[0]
        system = self.qna_system or QNA_SYSTEM
        session = self.bridge.begin_session(
            f"ilink-{self.account_id}-{user}", system, self._qa_tools())
        with self._sessions_lock:
            self._sessions[user] = [session, time.time()]
        return session

    def _qa_tools(self) -> list[dict]:
        from .qa_tools import build_qa_tools
        return build_qa_tools(self.store, self.quote_log)

    def _qa_context(self, user: str) -> str | None:
        from .qa_tools import rows_text
        if not self.quote_log:
            return None
        rows, _ = self.quote_log.list(0, 500)
        if not rows:
            return None
        return ("最近报价（背景资料，回答中如引用请转述数据）:\n"
                + rows_text(rows[-self.qna_recent:]))

    # -- 回复 ------------------------------------------------------

    def _reply(self, msg, text: str):
        text = (text or "").strip()
        if not text:
            return
        try:
            msg.reply_markdown(text[:TEXT_LIMIT])
        except Exception as e:
            self.log(f"回复失败: {e}")

    # -- 报价推送（管理器会禁用单账号推送，这里保留以便管理器和测试复用） --

    def notify_quote(self, rows: list[dict]):
        if not self.push_enabled or not rows:
            return
        room = str(rows[0].get("roomId") or "unknown")
        if not self._room_allowed(room):
            return
        sig = self._quote_sig(rows)
        if sig in self._seen_sigs:
            return
        self._seen_sigs.add(sig)
        self._save_state()
        self._push_q.put((room, rows))

    def _room_allowed(self, room: str) -> bool:
        if "*" in self.push_rooms:
            return True
        return any(room == p or room.startswith(p) for p in self.push_rooms)

    @staticmethod
    def _quote_sig(rows: list[dict]) -> str:
        key = tuple(
            (r.get("roomId"), r.get("msgSeq"), r.get("供应商"), r.get("项目"),
             r.get("价格"), r.get("单位"), r.get("备注"))
            for r in rows)
        return hashlib.sha1(json.dumps(key, ensure_ascii=False).encode("utf-8")).hexdigest()

    def _push_loop(self):
        while True:
            try:
                room, rows = self._push_q.get(timeout=5)
            except queue.Empty:
                continue
            try:
                self._push_one(room, rows)
            except Exception as e:
                self.log(f"推送异常 room={room}: {e}")

    def _push_targets(self) -> list[str]:
        if self.push_to:
            return list(self.push_to)
        with self._state_lock:
            last = self._last_user
        if self.push_fallback_last and last:
            return [last]
        return []

    def _push_one(self, room: str, rows: list[dict]):
        if self.plugin_host:
            api = self.plugin_host.spawn_api()
            if self.plugin_host.run_quote_hooks(api, {"room": room, "rows": rows}) is False:
                return
        for r in rows:
            r.setdefault("timeLabel", _now_label(r.get("time")))
        text = self._format(room, rows)
        targets = self._push_targets()
        if not targets:
            self.log(f"未配置 push.to 且尚无对话用户，跳过推送 room={room}")
            return
        for user in targets:
            self._send_markdown(user, text)

    def _send_markdown(self, user: str, text: str):
        bot = self._bot
        if bot is None or not self.connected:
            self.log(f"未连接，跳过推送 user={user}")
            return
        token = self._ctx_tokens.get(user) or None
        try:
            bot.send_markdown(user, text[:TEXT_LIMIT], context_token=token)
            self.pushed += 1
            self.log(f"已推送 user={user}")
        except Exception as e:
            self.log(f"推送失败 user={user}: {e}")

    def _format(self, room: str, rows: list[dict]) -> str:
        if self.bridge is not None:
            try:
                out = self.bridge.format_quotes(room, rows)
                if out and out.strip():
                    return out.strip()
            except Exception as e:
                self.log(f"Agent 格式化失败，用模板兜底: {e}")
        from .pi_bridge import fallback_format
        return fallback_format(room, rows)


# ---------------------------------------------------------------------------
# 多账号管理器：一进程多轮询
# ---------------------------------------------------------------------------

class ILinkManager:
    """管理多个 iLink 账号（凭据）：每个账号一个 `ILinkBot` + 一个轮询线程。

    - 启动时扫描 `accounts_dir/*.json` 以及兼容的旧 `credentials_file`；
    - 网页扫码新增账号（不覆盖已有），登录成功后立即起轮询；
    - 报价推送按目标用户路由到「拥有其 context_token 的账号」。
    """

    def __init__(self, cfg: dict, *, store=None, quote_log=None, bridge=None,
                 plugin_host=None, data_dir: str = "data", bot_factory=None):
        self.cfg = cfg
        self.store = store
        self.quote_log = quote_log
        self.bridge = bridge
        self.plugin_host = plugin_host
        self.data_dir = data_dir
        self._bot_factory = bot_factory

        ilink_dir = os.path.join(data_dir, "ilink")
        os.makedirs(ilink_dir, exist_ok=True)
        self.accounts_dir = str(cfg.get("accounts_dir") or os.path.join(ilink_dir, "accounts"))
        self.legacy_creds = str(cfg.get("credentials_file") or os.path.join(ilink_dir, "creds.json"))
        self.auto_login = bool(cfg.get("auto_login", True))
        self.store_messages = bool(cfg.get("store_messages", True))
        self.allowed_users = [str(u) for u in (cfg.get("allowed_users") or []) if str(u).strip()]
        self.qna_cfg = dict(cfg.get("qna") or {})
        self.qna_enabled = bool(self.qna_cfg.get("enabled", True))
        self.qna_typing = bool(self.qna_cfg.get("typing", True))

        push = cfg.get("push") or {}
        self.push_enabled = bool(push.get("enabled", True))
        self.push_to = [str(u) for u in (push.get("to") or []) if str(u).strip()]
        self.push_fallback_last = bool(push.get("fallback_last", True))
        self.push_rooms = list(cfg.get("push_rooms") or ["*"])

        self.log_path = os.path.join(ilink_dir, "manager.log")
        self.push_state_path = os.path.join(ilink_dir, "push_state.json")
        self._seen_sigs = self._load_push_state()

        self.accounts: list[ILinkBot] = []
        self._accounts_lock = threading.Lock()
        self._stop = threading.Event()

        # 扫码登录会话状态
        self._login_lock = threading.Lock()
        self._login_status = "idle"
        self._login_qr = ""
        self._login_error = ""
        self._login_account = ""

        self._push_q: "queue.Queue[tuple]" = queue.Queue()
        if self.push_enabled:
            threading.Thread(target=self._push_loop, daemon=True,
                             name="ilink-push").start()

    # -- 日志与状态 ------------------------------------------------

    def log(self, msg: str):
        line = f"{dt.datetime.now():%F %T} {msg}\n"
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass
        print("[ilink] " + msg, file=sys.stderr)

    def _load_push_state(self) -> set:
        try:
            with open(self.push_state_path, "r", encoding="utf-8") as f:
                return set(json.load(f).get("seen_sigs") or [])
        except Exception:
            return set()

    def _save_push_state(self):
        try:
            tmp = self.push_state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"seen_sigs": list(self._seen_sigs)[-1000:]}, f)
            os.replace(tmp, self.push_state_path)
        except OSError:
            pass

    def bot_status(self) -> dict:
        accs = self._account_statuses()
        primary = self.accounts[0] if self.accounts else None
        last_user = self._last_user()
        return {
            "enabled": True,
            "accounts": accs,
            "account_count": len(accs),
            "account_id": primary.account_id if primary else "",
            "has_credentials": bool(self.accounts),
            "ws_connected": any(a["ws_connected"] for a in accs),
            "ws_error": next((a["ws_error"] for a in accs if a["ws_error"]), ""),
            "push_mode": "send" if self.push_enabled else "none",
            "push_configured": bool(self.push_to or last_user),
            "push_targets": self._push_targets(),
            "qna_enabled": self.qna_enabled,
            "typing": self.qna_typing,
            "store_messages": self.store_messages,
            "pushed": sum(a["pushed"] for a in accs),
            "agent": self.bridge.backend_kind if self.bridge else None,
        }

    def _account_statuses(self) -> list[dict]:
        with self._accounts_lock:
            accounts = list(self.accounts)
        return [{
            "account_id": a.account_id,
            "user_id": a.user_id,
            "ws_connected": a.connected,
            "ws_error": a.last_error,
            "last_user": a._last_user,
            "typing": a.qna_typing,
            "pushed": a.pushed,
        } for a in accounts]

    # -- 生命周期 --------------------------------------------------

    def start(self):
        self._load_accounts()
        with self._accounts_lock:
            count = len(self.accounts)
        if count == 0:
            self.log(f"未找到任何 iLink 凭据（{self.accounts_dir} / {self.legacy_creds}）")
            if self.auto_login:
                self.log("自动发起 Web 扫码登录，请在网页端扫描二维码")
                self.start_login()
        else:
            self.log(f"iLink 多账号启动：{count} 个账号")

    def stop(self):
        self._stop.set()
        with self._accounts_lock:
            accounts = list(self.accounts)
        for a in accounts:
            a.stop()

    def _load_accounts(self):
        paths: list[str] = []
        if os.path.isdir(self.accounts_dir):
            for fn in sorted(os.listdir(self.accounts_dir)):
                if not fn.endswith(".json") or fn.startswith("_"):
                    continue
                if fn.endswith(".state.json"):   # 跳过每账号状态文件
                    continue
                paths.append(os.path.join(self.accounts_dir, fn))
        if os.path.exists(self.legacy_creds):
            paths.append(self.legacy_creds)
        seen: set[str] = set()
        for p in paths:
            info = _read_creds(p)
            account_id = str(info.get("accountId") or "")
            if not account_id:
                # 不是有效凭据（无 accountId），跳过
                continue
            if account_id in seen:
                continue
            seen.add(account_id)
            self._add_account(p, account_id, replace=False)

    def _account_cfg(self, creds_path: str, account_id: str) -> dict:
        # 兼容旧单账号：旧凭据继续用旧 state.json，避免升级后丢失 context_token
        if os.path.abspath(creds_path) == os.path.abspath(self.legacy_creds):
            state_file = os.path.join(self.data_dir, "ilink", "state.json")
        else:
            state_file = os.path.join(self.accounts_dir,
                                      _safe_name(account_id) + ".state.json")
        return {
            "enabled": True,
            "credentials_file": creds_path,
            "state_file": state_file,
            "store_messages": self.store_messages,
            "allowed_users": self.allowed_users,
            "push": {"enabled": False},        # 推送统一由管理器路由
            "push_rooms": self.push_rooms,
            "qna": self.qna_cfg,
        }

    def _make_account(self, creds_path: str, account_id: str) -> ILinkBot:
        cfg = self._account_cfg(creds_path, account_id)
        factory = None
        if self._bot_factory is not None:
            # 管理器注入的工厂：账号 → 一个零参可调用，供该账号的 _run 使用
            factory = self._bot_factory(creds_path, account_id)
        return ILinkBot(cfg, store=self.store, quote_log=self.quote_log,
                        bridge=self.bridge, plugin_host=self.plugin_host,
                        data_dir=self.data_dir, bot_factory=factory)

    def _add_account(self, creds_path: str, account_id: str, replace: bool = True):
        existing = None
        if replace:
            with self._accounts_lock:
                for a in self.accounts:
                    if account_id and a.account_id == account_id:
                        existing = a
                        break
                if existing is not None:
                    self.accounts = [a for a in self.accounts if a is not existing]
        if existing is not None:
            self.log(f"账号 {account_id} 重新登录，重启其轮询")
            existing.stop()
            existing.wait_stopped(45)
        worker = self._make_account(creds_path, account_id)
        with self._accounts_lock:
            self.accounts.append(worker)
        worker.start()
        self.log(f"账号已加入：{worker.account_id or account_id or creds_path}")

    # -- Web 扫码登录 ---------------------------------------------

    def start_login(self) -> bool:
        """发起扫码登录（后台线程），成功后新增一个账号。"""
        with self._login_lock:
            if self._login_status in ("starting", "waiting", "scanned", "refreshing", "redirected"):
                return False
            self._login_status = "starting"
            self._login_qr = ""
            self._login_error = ""
        threading.Thread(target=self._login_run, daemon=True, name="ilink-login").start()
        return True

    def _set_login(self, **kw):
        with self._login_lock:
            for k, v in kw.items():
                setattr(self, "_" + k, v)

    def _on_qr(self, content: str):
        self._set_login(login_qr=str(content or ""))

    def _on_login_status(self, status: str):
        mapping = {"waiting": "waiting", "scanned": "scanned",
                   "refreshing": "refreshing", "redirected": "redirected"}
        if status in mapping:
            with self._login_lock:
                if self._login_status == "scanned" and mapping[status] == "waiting":
                    return
            self._set_login(login_status=mapping[status])

    def _login_run(self):
        try:
            sdk = _load_sdk()
            os.makedirs(self.accounts_dir, exist_ok=True)
            pending = os.path.join(self.accounts_dir, f"_pending-{uuid.uuid4().hex}.json")
            info = sdk.login(save_to=pending, on_qrcode=self._on_qr,
                             on_status_change=self._on_login_status)
            account_id = str((info or {}).get("accountId") or "")
            final = os.path.join(self.accounts_dir, _safe_name(account_id or uuid.uuid4().hex) + ".json")
            os.replace(pending, final)
            self._set_login(login_status="success", login_account=account_id, login_qr="")
            self.log(f"扫码登录成功 account={account_id} -> {final}")
            self._add_account(final, account_id)
        except Exception as e:
            self._set_login(login_status="error", login_error=str(e))
            self.log(f"扫码登录失败: {e}")

    def login_status(self) -> dict:
        with self._login_lock:
            state = {
                "status": self._login_status,
                "qr_content": self._login_qr,
                "error": self._login_error,
                "login_account": self._login_account,
            }
        with self._accounts_lock:
            accounts = list(self.accounts)
        state.update({
            "has_credentials": bool(accounts),
            "ws_connected": any(a.connected for a in accounts),
            "account_id": (accounts[0].account_id if accounts else "") or state.get("login_account") or "",
            "accounts_count": len(accounts),
        })
        return state

    def qr_svg(self) -> bytes | None:
        with self._login_lock:
            content = self._login_qr
        if not content:
            return None
        try:
            import qrcode
            from qrcode.image.svg import SvgPathImage
        except Exception as e:  # pragma: no cover - 依赖缺失
            raise ILinkError("Web 端显示二维码需要安装 qrcode：pip install qrcode") from e
        img = qrcode.make(content, image_factory=SvgPathImage, border=2)
        buf = io.BytesIO()
        img.save(buf)
        return buf.getvalue()

    # -- 报价推送（按目标用户路由到对应账号） ----------------------

    def notify_quote(self, rows: list[dict]):
        if not self.push_enabled or not rows:
            return
        room = str(rows[0].get("roomId") or "unknown")
        if not self._room_allowed(room):
            return
        sig = ILinkBot._quote_sig(rows)
        if sig in self._seen_sigs:
            return
        self._seen_sigs.add(sig)
        self._save_push_state()
        self._push_q.put((room, rows))

    def _room_allowed(self, room: str) -> bool:
        if "*" in self.push_rooms:
            return True
        return any(room == p or room.startswith(p) for p in self.push_rooms)

    def _push_loop(self):
        while True:
            try:
                room, rows = self._push_q.get(timeout=5)
            except queue.Empty:
                continue
            try:
                self._push_one(room, rows)
            except Exception as e:
                self.log(f"推送异常 room={room}: {e}")

    def _last_user(self) -> str:
        best, ts = "", 0.0
        with self._accounts_lock:
            accounts = list(self.accounts)
        for a in accounts:
            if a._last_user and a._last_activity >= ts:
                ts, best = a._last_activity, a._last_user
        return best

    def _push_targets(self) -> list[str]:
        if self.push_to:
            return list(self.push_to)
        last = self._last_user()
        if self.push_fallback_last and last:
            return [last]
        return []

    def _push_one(self, room: str, rows: list[dict]):
        if self.plugin_host:
            api = self.plugin_host.spawn_api()
            if self.plugin_host.run_quote_hooks(api, {"room": room, "rows": rows}) is False:
                return
        for r in rows:
            r.setdefault("timeLabel", _now_label(r.get("time")))
        text = self._format(room, rows)
        targets = self._push_targets()
        if not targets:
            self.log(f"未配置 push.to 且尚无对话用户，跳过推送 room={room}")
            return
        for user in targets:
            self._send_to_user(user, text)

    def _send_to_user(self, user: str, text: str):
        with self._accounts_lock:
            accounts = list(self.accounts)
        # 优先：拥有该用户 context_token 的账号中，token 最新的（避免用旧 BOT 的过期 token）
        candidates = [a for a in accounts if user in a._ctx_tokens and a.connected]
        if candidates:
            acc = max(candidates, key=lambda a: a._ctx_ts.get(user, 0.0))
        else:
            connected = [a for a in accounts if a.connected]
            if not connected:
                self.log(f"无已连接账号可推送给 {user}")
                return
            acc = max(connected, key=lambda a: a._last_activity)
        self.log(f"通过账号 {acc.account_id} 推送 user={user}")
        acc._send_markdown(user, text)

    def _format(self, room: str, rows: list[dict]) -> str:
        if self.bridge is not None:
            try:
                out = self.bridge.format_quotes(room, rows)
                if out and out.strip():
                    return out.strip()
            except Exception as e:
                self.log(f"Agent 格式化失败，用模板兜底: {e}")
        from .pi_bridge import fallback_format
        return fallback_format(room, rows)


# ---------------------------------------------------------------------------
# 配置与装配
# ---------------------------------------------------------------------------

def load_ilink_config(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"[ilink] 配置解析失败 {path}: {e}", file=sys.stderr)
        return None
    if not isinstance(cfg, dict) or not cfg.get("enabled"):
        return None
    return cfg


def create_ilink_bot(config_path: str, *, store=None, quote_log=None, bridge=None,
                     plugin_host=None, data_dir: str = "data") -> ILinkManager | None:
    cfg = load_ilink_config(config_path)
    if not cfg:
        return None
    bot = ILinkManager(cfg, store=store, quote_log=quote_log, bridge=bridge,
                       plugin_host=plugin_host, data_dir=data_dir)
    bot.log(f"iLink 多账号通道启用: dir={bot.accounts_dir} "
            f"store_messages={bot.store_messages} "
            f"push={bot.push_enabled}({bot.push_to or '最近用户'}) "
            f"qna={bot.qna_enabled} typing={bot.qna_typing} "
            f"agent={bridge.backend_kind if bridge else None}")
    if cfg.get("startup_notice") and bot.push_to:
        threading.Thread(target=_startup_notice, args=(bot,), daemon=True).start()
    return bot


def _startup_notice(bot: ILinkManager):
    try:
        time.sleep(2)
        bot._send_to_user(bot.push_to[0],
                          "🤖 报价机器人已上线。报价消息会自动推送；直接提问可查看报价数据（试试 /help）。")
    except Exception as e:
        bot.log(f"上线通知失败: {e}")


def _cli():
    ap = argparse.ArgumentParser(description="微信 iLink Bot 扫码登录（多账号）")
    ap.add_argument("--login", action="store_true", help="扫码登录并保存凭据")
    ap.add_argument("--credentials", default=DEFAULT_CREDS,
                    help=f"凭据保存路径（默认 {DEFAULT_CREDS}）")
    args = ap.parse_args()
    if not args.login:
        ap.print_help()
        return
    sdk = _load_sdk()
    os.makedirs(os.path.dirname(os.path.abspath(args.credentials)), exist_ok=True)
    sdk.login(save_to=args.credentials)
    print(f"[ilink] 登录凭据已保存到 {args.credentials}")


if __name__ == "__main__":
    _cli()
