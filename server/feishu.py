#!/usr/bin/env python3
"""飞书机器人集成：应用 API 客户端 + 事件接收（官方 SDK 长连接 / webhook）+ 报价推送 + 问答机器人。

纯标准库实现（urllib），不需要引入官方 SDK：

- FeishuClient  — tenant_access_token 缓存、发消息、回复消息、自定义机器人 webhook 推送（加签）
- FeishuBot     — 事件 webhook（url_verification / 签名校验 / AES-256-CBC 解密 / 去重）、
                  im.message.receive_v1 分发（指令 → 插件；其余 → Agent 问答）、
                  报价推送队列（Agent 格式化 → 群机器人）
- 事件安全      — 配置 encrypt_key 时做 X-Lark-Signature 校验并解密（需要 pycryptodome 或
                  cryptography）；未配置时校验 Verification Token。

配置见 feishu.config.json.example。
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.request

FEISHU_BASE = "https://open.feishu.cn/open-apis"
TEXT_LIMIT = 8000          # 单条 text 消息截断长度（飞书限制远大于此，留余量）
DEFAULT_QA_SYSTEM = "你是绑在飞书群里的报价助理。"  # 缺省时用 pi_bridge.QNA_SYSTEM

MENTION_RE = re.compile(r"@_user_\d+|@_all")


class FeishuError(RuntimeError):
    def __init__(self, code, msg):
        super().__init__(f"feishu api error {code}: {msg}")
        self.code = code


def _aes_decrypt(encrypt_key: str, payload: dict) -> str:
    """解密事件体 {"encrypt": base64}：AES-256-CBC，key=sha256(encrypt_key)，iv=前 16 字节。"""
    raw = base64.b64decode(payload["encrypt"])
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    iv, data = raw[:16], raw[16:]
    try:
        from Crypto.Cipher import AES  # pycryptodome
        plain = AES.new(key, AES.MODE_CBC, iv).decrypt(data)
    except ImportError:
        try:
            from cryptography.hazmat.backends import default_backend
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        except ImportError:
            raise FeishuError(-1, "事件配置了 encrypt_key，需要 pip install pycryptodome（或 cryptography）")
        dec = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
        plain = dec.decryptor().update(data) + dec.decryptor().finalize()
    return _pkcs7_unpad(plain).decode("utf-8")


def _pkcs7_unpad(data: bytes) -> bytes:
    n = data[-1]
    if n <= 0 or n > 16:
        return data
    return data[:-n]


def verify_signature(timestamp: str, nonce: str, encrypt_key: str, raw_body: bytes, signature: str) -> bool:
    """X-Lark-Signature = sha256(timestamp + nonce + encrypt_key + 原始 body) 的 hex。"""
    digest = hashlib.sha256(
        (timestamp + nonce + encrypt_key).encode("utf-8") + raw_body).hexdigest()
    return hmac.compare_digest(digest, signature or "")


def webhook_sign(secret: str, timestamp: int) -> str:
    """自定义机器人加签：HMAC-SHA256(key = f"{timestamp}\\n{secret}", msg = "") → base64。"""
    string_to_sign = f"{timestamp}\n{secret}"
    return base64.b64encode(hmac.new(string_to_sign.encode("utf-8"), b"", hashlib.sha256).digest()).decode()


def now_label(ms) -> str:
    try:
        return dt.datetime.fromtimestamp(int(ms) / 1000).strftime("%m-%d %H:%M")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------

class FeishuClient:
    def __init__(self, cfg: dict):
        self.app_id = str(cfg.get("app_id") or "")
        self.app_secret = str(cfg.get("app_secret") or "")
        self._token = ""
        self._token_expire = 0.0
        self._lock = threading.Lock()
        self.log = print

    # -- 令牌 ---------------------------------------------------

    def _get_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_expire - 120:
                return self._token
            body = json.dumps({"app_id": self.app_id, "app_secret": self.app_secret}).encode()
            req = urllib.request.Request(
                FEISHU_BASE + "/auth/v3/tenant_access_token/internal",
                data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("code") != 0:
                raise FeishuError(data.get("code"), data.get("msg"))
            self._token = data["tenant_access_token"]
            self._token_expire = time.time() + int(data.get("expire") or 7200)
            return self._token

    # -- 应用 API -------------------------------------------------

    def _api(self, method: str, path: str, payload: dict | None = None, params: str = ""):
        url = FEISHU_BASE + path + (("?" + params) if params else "")
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": "Bearer " + self._get_token(),
            })
        with urllib.request.urlopen(req, timeout=15) as resp:
            out = json.loads(resp.read().decode("utf-8"))
        if out.get("code") != 0:
            raise FeishuError(out.get("code"), out.get("msg"))
        return out

    def send_message(self, receive_id: str, receive_id_type: str, msg_type: str, content: dict) -> dict:
        return self._api(
            "POST", "/im/v1/messages", {"receive_id": receive_id, "msg_type": msg_type,
                                        "content": json.dumps(content, ensure_ascii=False)},
            params="receive_id_type=" + receive_id_type)

    def reply_message(self, message_id: str, msg_type: str, content: dict) -> dict:
        return self._api(
            "POST", f"/im/v1/messages/{message_id}/reply",
            {"msg_type": msg_type, "content": json.dumps(content, ensure_ascii=False)})

    def send_text(self, receive_id: str, receive_id_type: str, text: str) -> dict:
        return self.send_message(receive_id, receive_id_type, "text", {"text": text})

    # -- 自定义机器人 webhook 推送 ---------------------------------

    def push_webhook(self, url: str, text: str, secret: str = "") -> dict:
        payload: dict = {"msg_type": "text", "content": {"text": text}}
        if secret:
            ts = int(time.time())
            payload["timestamp"] = str(ts)
            payload["sign"] = webhook_sign(secret, ts)
        req = urllib.request.Request(
            url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            out = json.loads(resp.read().decode("utf-8"))
        code = out.get("code", out.get("StatusCode", 0))
        if code not in (0, "0"):
            raise FeishuError(code, out.get("msg") or out.get("StatusMessage"))
        return out


# ---------------------------------------------------------------------------
# 机器人
# ---------------------------------------------------------------------------

class FeishuBot:
    """报价推送 + 问答机器人。events 处理全部异步化（worker 线程），webhook 立即返回 200。"""

    def __init__(self, client: FeishuClient, cfg: dict, *,
                 store=None, quote_log=None, bridge=None, plugin_host=None,
                 data_dir: str = "data"):
        self.client = client
        self.cfg = cfg
        self.store = store
        self.quote_log = quote_log
        self.bridge = bridge
        self.plugin_host = plugin_host
        self.event_path = str(cfg.get("event_path") or "/feishu/event")

        self.verification_token = str(cfg.get("verification_token") or "")
        self.encrypt_key = str(cfg.get("encrypt_key") or "")
        self.event_mode = str(cfg.get("event_mode") or "webhook")
        self.ws_connected = False
        self.ws_error = ""

        push = cfg.get("push") or {}
        self.push_mode = str(push.get("mode") or "none")
        self.webhook_url = str(push.get("webhook_url") or "")
        self.webhook_secret = str(push.get("webhook_secret") or "")
        self.push_chat_id = str(push.get("chat_id") or "")
        self.push_rooms = list(cfg.get("push_rooms") or ["*"])
        self.chat_room_map = dict(cfg.get("chat_room_map") or {})

        qna = cfg.get("qna") or {}
        self.qna_enabled = bool(qna.get("enabled", True))
        self.qna_system = str(qna.get("system_prompt") or "")
        self.qna_recent = int(qna.get("recent_quotes") or 20)
        self.qna_session_ttl = int(qna.get("session_ttl") or 1800)

        self.data_dir = data_dir
        os.makedirs(os.path.join(data_dir, "feishu"), exist_ok=True)
        self.log_path = os.path.join(data_dir, "feishu", "bot.log")
        self.state_path = os.path.join(data_dir, "feishu", "push_state.json")

        self._seen_sigs = self._load_state()
        self._seen_events: set[str] = set()
        self._event_q: "queue.Queue[dict]" = queue.Queue()
        self._push_q: "queue.Queue[tuple]" = queue.Queue()
        self._sessions: dict[str, list] = {}   # chat_id -> [session, last_ts]
        self._sessions_lock = threading.Lock()
        self._warned_no_bridge = False
        self.pushed = 0

        self._worker = threading.Thread(target=self._event_loop, daemon=True, name="feishu-events")
        self._worker.start()
        self._pusher = threading.Thread(target=self._push_loop, daemon=True, name="feishu-push")
        self._pusher.start()

    # -- 日志与状态 ------------------------------------------------

    def log(self, msg: str):
        line = f"{dt.datetime.now():%F %T} {msg}\n"
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass
        print("[feishu] " + msg, file=sys.stderr)

    def _load_state(self) -> set:
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return set(data.get("seen_sigs") or [])
        except Exception:
            return set()

    def _save_state(self):
        try:
            tmp = self.state_path + ".tmp"
            sigs = list(self._seen_sigs)[-1000:]
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"seen_sigs": sigs}, f)
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    def bot_status(self) -> dict:
        return {
            "enabled": True,
            "event_mode": self.event_mode,
            "ws_connected": self.ws_connected,
            "ws_error": self.ws_error,
            "push_mode": self.push_mode,
            "qna_enabled": self.qna_enabled,
            "pushed": self.pushed,
            "agent": self.bridge.backend_kind if self.bridge else None,
            "event_path": self.event_path,
        }

    # -- webhook 入口（由 HTTP Handler 调用，必须快速返回） ---------

    def handle_webhook(self, headers, raw_body: bytes) -> tuple[int, str]:
        try:
            body = json.loads(raw_body.decode("utf-8", "replace"))
        except Exception:
            return 400, json.dumps({"code": 400, "msg": "invalid json"}, ensure_ascii=False)

        # 加密模式：先验签再解密（公网部署务必配置 encrypt_key）
        if self.encrypt_key:
            try:
                ok = verify_signature(
                    headers.get("X-Lark-Request-Timestamp") or "",
                    headers.get("X-Lark-Request-Nonce") or "",
                    self.encrypt_key, raw_body,
                    headers.get("X-Lark-Signature") or "")
                if not ok:
                    return 403, json.dumps({"code": 403, "msg": "bad signature"}, ensure_ascii=False)
                body = json.loads(_aes_decrypt(self.encrypt_key, body))
            except FeishuError as e:
                self.log(f"事件解密失败: {e}")
                return 403, json.dumps({"code": 403, "msg": "decrypt failed"}, ensure_ascii=False)
            except Exception as e:
                self.log(f"事件处理异常: {e}")
                return 400, json.dumps({"code": 400, "msg": "bad body"}, ensure_ascii=False)

        # URL 校验：原样回 challenge
        if isinstance(body, dict) and body.get("type") == "url_verification":
            return 200, json.dumps({"challenge": body.get("challenge", "")}, ensure_ascii=False)

        # 明文模式校验 Verification Token
        if not self.encrypt_key:
            token = ((body.get("header") or {}).get("token") or ""
                     or body.get("token") or "")
            if self.verification_token and not hmac.compare_digest(token, self.verification_token):
                return 403, json.dumps({"code": 403, "msg": "token mismatch"}, ensure_ascii=False)

        # 幂等去重 + 投递 worker，立即返回成功（飞书要求 3s 内响应）
        self.enqueue_event(body)
        return 200, json.dumps({"code": 0})

    def enqueue_event(self, body: dict) -> bool:
        """幂等去重后投递到事件 worker（webhook 与长连接共用）。"""
        event_id = ((body.get("header") or {}).get("event_id") or "")
        if event_id:
            if event_id in self._seen_events:
                return False
            self._seen_events.add(event_id)
            if len(self._seen_events) > 2000:
                self._seen_events = set(list(self._seen_events)[-1000:])
        self._event_q.put(body)
        return True

    # -- 官方 SDK 长连接事件接收 -------------------------------------

    @staticmethod
    def _lark_sdk():
        try:
            import lark_oapi  # 飞书官方 Python SDK
            return lark_oapi
        except Exception as e:
            raise FeishuError(-1, f"长连接需要安装官方 SDK：pip install lark-oapi（{e}）")

    def start_event_source(self):
        """按 event_mode 启动事件接收源；long_conn 用官方 SDK WebSocket。"""
        if self.event_mode == "long_conn":
            threading.Thread(target=self._ws_loop, daemon=True, name="feishu-ws").start()
        elif self.event_mode not in ("webhook", "none"):
            self.log(f"未知事件模式 {self.event_mode}，回退 webhook")

    def _ws_loop(self):
        retry = 0
        while True:
            try:
                lark = self._lark_sdk()
                builder = lark.EventDispatcherHandler.builder("", "")
                handler = builder.register_p2_im_message_receive_v1(
                    self._ws_message_handler).build()
                ws_client = lark.ws.Client(
                    self.client.app_id, self.client.app_secret, event_handler=handler)
                self.ws_error = ""
                self.ws_connected = True
                retry = 0
                self.log("长连接事件接收启动 (lark-oapi ws)")
                ws_client.start()  # 阻塞运行，SDK 内部自动重连
            except Exception as e:
                self.ws_connected = False
                self.ws_error = str(e)
                self.log(f"长连接异常（第 {retry + 1} 次）: {e}")
                retry += 1
            time.sleep(min(5 + retry * 5, 60))

    def _ws_message_handler(self, data):
        """lark-oapi P2ImMessageReceiveV1 → 与 webhook 相同的事件结构。"""
        try:
            ev = data.event
            sender = ev.sender if ev else None
            message = ev.message if ev else None
            payload = {
                "header": {
                    "event_type": "im.message.receive_v1",
                    "event_id": getattr(data, "uuid", "") or "",
                },
                "event": {
                    "message": {
                        "message_id": getattr(message, "message_id", None),
                        "chat_id": getattr(message, "chat_id", None),
                        "chat_type": getattr(message, "chat_type", None) or "p2p",
                        "message_type": getattr(message, "message_type", None),
                        "content": getattr(message, "content", None),
                    },
                    "sender": {
                        "sender_type": getattr(sender, "sender_type", None) or "user",
                        "sender_id": {
                            "open_id": getattr(
                                getattr(sender, "sender_id", None), "open_id", "") or "",
                        },
                    },
                },
            }
            self.enqueue_event(payload)
        except Exception as e:
            self.log(f"长连接事件解析失败: {e}")

    # -- 事件处理线程 ---------------------------------------------

    def _event_loop(self):
        last_gc = time.time()
        while True:
            try:
                ev = self._event_q.get(timeout=5)
                try:
                    self._dispatch(ev)
                except Exception as e:
                    self.log(f"事件处理异常: {e}")
            except queue.Empty:
                pass
            except Exception as e:
                self.log(f"事件循环异常: {e}")
            if time.time() - last_gc > 600:
                last_gc = time.time()
                self._gc_sessions()

    def _dispatch(self, ev: dict):
        header = ev.get("header") or {}
        etype = header.get("event_type") or ev.get("type") or ""
        if etype == "im.message.receive_v1":
            self._on_im_message(ev)

    # -- 消息事件 ------------------------------------------------

    def _on_im_message(self, ev: dict):
        event = ev.get("event") or {}
        message = event.get("message") or {}
        sender = event.get("sender") or {}
        if sender.get("sender_type") == "bot":
            return  # 不回其他机器人，也避免自触发
        if self.cfg.get("bot_open_id") and sender.get("sender_id", {}).get("open_id") == self.cfg["bot_open_id"]:
            return

        message_id = message.get("message_id") or ""
        chat_id = message.get("chat_id") or ""
        chat_type = message.get("chat_type") or "p2p"
        text = self._extract_text(message)
        if text is None:
            self.reply(message_id, "目前只支持文字消息，可以发文字试试～")
            return
        text = MENTION_RE.sub("", text).strip()
        if not text or not message_id:
            return

        api = self.plugin_host.spawn_api() if self.plugin_host else None
        if api is not None:
            api._set_event(ev, text)

        # 1) 插件消息钩子（先于命令与问答）
        if self.plugin_host:
            verdict = self.plugin_host.run_message_hooks(api, ev, text)
            if isinstance(verdict, str):
                self.reply(message_id, verdict)
                return
            if verdict is False:
                return

        # 2) 斜杠命令 → 插件命令表
        if text.startswith("/"):
            name, _, args = text[1:].partition(" ")
            name = name.lower()
            entry = self.plugin_host.commands.get(name) if self.plugin_host else None
            if entry:
                self.log(f"command /{name} from {chat_id}")
                out = self.plugin_host.run_command(name, args.strip(), api)
                if out:
                    self.reply(message_id, out)
                return
            self.reply(message_id, f"未知指令 /{name}，试试 /help")
            return

        # 3) 其余 → Agent 问答
        if not self.qna_enabled:
            return
        if self.bridge is None:
            if not self._warned_no_bridge:
                self._warned_no_bridge = True
                self.log("问答未启用：未配置 Agent（agent.config.json）")
                self.reply(message_id, "问答功能未启用：服务器没有配置 Agent 模型。")
            return
        threading.Thread(target=self._qa_job, args=(chat_id, message_id, text, chat_type),
                         daemon=True, name="feishu-qa").start()

    @staticmethod
    def _extract_text(message: dict) -> str | None:
        mtype = message.get("message_type")
        content = message.get("content") or "{}"
        try:
            data = json.loads(content)
        except Exception:
            return None
        if mtype == "text":
            return (data.get("text") or "").strip() or None
        if mtype == "post":
            parts = []
            for para in (data.get("content") or []):
                if not isinstance(para, list):
                    continue
                for seg in para:
                    if isinstance(seg, dict) and seg.get("tag") == "text":
                        parts.append(seg.get("text") or "")
                    elif isinstance(seg, str):
                        parts.append(seg)
            return "".join(parts).strip() or None
        return None

    # -- 问答 ----------------------------------------------------

    def _qa_job(self, chat_id: str, message_id: str, text: str, chat_type: str):
        try:
            session = self._get_session(chat_id)
            ctx = self._qa_context(chat_id)
            answer = session.ask(text, context=ctx)
            if not answer:
                answer = "我暂时回答不了这个问题（模型没有给出结果）。"
            self.reply(message_id, answer)
        except Exception as e:
            self.log(f"问答失败 chat={chat_id}: {e}")
            self.reply(message_id, f"处理这个问题时出错了：{e}")

    def _get_session(self, chat_id: str):
        from .pi_bridge import QNA_SYSTEM
        with self._sessions_lock:
            entry = self._sessions.get(chat_id)
            if entry and time.time() - entry[1] < self.qna_session_ttl:
                entry[1] = time.time()
                return entry[0]
        system = self.qna_system or QNA_SYSTEM
        session = self.bridge.begin_session(f"feishu-{chat_id}", system, self._qa_tools())
        with self._sessions_lock:
            self._sessions[chat_id] = [session, time.time()]
        return session

    def _gc_sessions(self):
        with self._sessions_lock:
            now = time.time()
            for k in [k for k, v in self._sessions.items() if now - v[1] > self.qna_session_ttl]:
                del self._sessions[k]

    def _qa_tools(self) -> list[dict]:
        """给 Agent 的工具：检索报价 / 查群聊统计。handler 同步返回 str。"""
        return [
            {
                "name": "search_quotes",
                "label": "查报价",
                "description": "按关键词检索已提取的报价记录（项目/备注/发送人/单位），返回匹配列表。参数 keyword 关键词、limit 1-30 条数。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keyword": {"type": "string", "description": "检索关键词"},
                        "limit": {"type": "integer", "description": "最多返回条数，默认 10"},
                    },
                },
                "handler": self._tool_search_quotes,
            },
            {
                "name": "latest_quotes",
                "label": "最近报价",
                "description": "返回最近的报价记录。参数 limit 1-30 条数，默认 10。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "最多返回条数，默认 10"},
                    },
                },
                "handler": self._tool_latest_quotes,
            },
            {
                "name": "room_stats",
                "label": "群聊统计",
                "description": "查询群聊捕获统计：消息总数、图片数、活跃群列表。无参数。",
                "parameters": {"type": "object", "properties": {}},
                "handler": self._tool_room_stats,
            },
        ]

    def _tool_search_quotes(self, params: dict) -> str:
        kw = str(params.get("keyword") or "").strip().lower()
        try:
            limit = min(max(int(params.get("limit") or 10), 1), 30)
        except (TypeError, ValueError):
            limit = 10
        rows, _ = self.quote_log.list(0, 2000) if self.quote_log else ([], 0)
        picked = [r for r in rows if (
            not kw or kw in " ".join(str(r.get(k) or "") for k in
                                     ("项目", "备注", "sender", "单位", "价格")).lower())]
        return self._rows_text(picked[:limit]) or "没有找到匹配的报价。"

    def _tool_latest_quotes(self, params: dict) -> str:
        try:
            limit = min(max(int(params.get("limit") or 10), 1), 30)
        except (TypeError, ValueError):
            limit = 10
        rows, _ = self.quote_log.list(0, 2000) if self.quote_log else ([], 0)
        return self._rows_text(rows[-limit:]) or "还没有报价记录。"

    def _tool_room_stats(self, params: dict) -> str:
        if not self.store:
            return "存储不可用。"
        stats = self.store.stats()
        rooms = self.store.rooms()[:8]
        lines = [f"累计消息 {stats.get('count', 0)} 条，图片 {stats.get('images', 0)} 张。"]
        if rooms:
            lines.append("活跃群：" + "、".join(
                str(r.get("roomId") or "").replace("@chatroom", "") for r in rooms))
        return "\n".join(lines)

    @staticmethod
    def _rows_text(rows: list[dict]) -> str:
        from .pi_bridge import _fmt_row
        lines = []
        for r in rows:
            row = dict(r)
            row["timeLabel"] = now_label(row.get("time"))
            lines.append(_fmt_row(row))
        return "\n".join(lines)

    def _qa_context(self, chat_id: str) -> str | None:
        """给问答注入的背景：该群对应的微信房间的最近报价。"""
        parts = []
        room = self._room_for_chat(chat_id)
        if self.quote_log:
            rows, _ = self.quote_log.list(0, 500, room)
            if rows:
                parts.append("最近报价（背景资料，回答中如引用请转述数据）:\n"
                             + self._rows_text(rows[-self.qna_recent:]))
        if room is None and self.store:
            rooms = self.store.rooms()
            if rooms:
                parts.append("已接入的微信群：" + "、".join(
                    str(r.get("roomId") or "").replace("@chatroom", "") for r in rooms[:10]))
        return "\n".join(parts) or None

    def _room_for_chat(self, chat_id: str) -> str | None:
        mapped = self.chat_room_map.get(chat_id)
        if not mapped:
            return None
        if self.store:
            for r in self.store.rooms():
                rid = r.get("roomId") or ""
                if rid == mapped or rid.endswith(mapped) or mapped in rid:
                    return rid
        return mapped if "@chatroom" in mapped or "/" in mapped else None

    # -- 回复 ----------------------------------------------------

    def reply(self, message_id: str, text: str):
        try:
            self.client.reply_message(message_id, "text", {"text": text[:TEXT_LIMIT]})
        except Exception as e:
            self.log(f"回复失败: {e}")

    # -- 报价推送 -------------------------------------------------

    def notify_quote(self, rows: list[dict]):
        """quote_agent 的 on_quote 钩子入口：过滤 → 去重 → 入推送队列。"""
        if self.push_mode == "none" or not rows:
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
            (r.get("roomId"), r.get("msgSeq"), r.get("项目"), r.get("价格"),
             r.get("单位"), r.get("备注"))
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

    def _push_one(self, room: str, rows: list[dict]):
        # 出错重试 3 次
        err = None
        text = ""
        for attempt in range(3):
            try:
                # 1) 插件报价钩子：返回 False 抑制默认推送（插件可自行处理）
                ctx = {"room": room, "rows": rows}
                if self.plugin_host:
                    api = self.plugin_host.spawn_api()
                    if self.plugin_host.run_quote_hooks(api, ctx) is False:
                        return
                # 2) Agent 格式化（LLM 失败自动用模板兜底）
                for r in rows:
                    r.setdefault("timeLabel", now_label(r.get("time")))
                text = self._format(room, rows)
                # 3) 推送
                if self.push_mode == "webhook":
                    if not self.webhook_url:
                        self.log("push.mode=webhook 但未配置 webhook_url")
                        return
                    self.client.push_webhook(self.webhook_url, text, self.webhook_secret)
                elif self.push_mode == "app":
                    if not self.push_chat_id:
                        self.log("push.mode=app 但未配置 chat_id")
                        return
                    self.client.send_text(self.push_chat_id, "chat_id", text)
                else:
                    return
                self.pushed += 1
                self.log(f"已推送报价 room={room} items={len(rows)}")
                return
            except Exception as e:
                err = e
                if attempt < 2:
                    time.sleep(3 * (attempt + 1))
        self.log(f"推送失败 room={room}: {err}")

    def _format(self, room: str, rows: list[dict]) -> str:
        if self.bridge is not None:
            try:
                out = self.bridge.format_quotes(room, rows)
                if out and out.strip():
                    return out.strip()[:TEXT_LIMIT]
            except Exception as e:
                self.log(f"Agent 格式化失败，用模板兜底: {e}")
        from .pi_bridge import fallback_format
        return fallback_format(room, rows)[:TEXT_LIMIT]


# ---------------------------------------------------------------------------
# 配置与装配
# ---------------------------------------------------------------------------

def load_feishu_config(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"[feishu] 配置解析失败 {path}: {e}", file=sys.stderr)
        return None
    if not cfg.get("enabled"):
        return None
    return cfg


def create_feishu_bot(config_path: str, *, store=None, quote_log=None, bridge=None,
                      plugin_host=None, data_dir: str = "data") -> FeishuBot | None:
    cfg = load_feishu_config(config_path)
    if not cfg:
        return None
    client = FeishuClient(cfg)
    bot = FeishuBot(client, cfg, store=store, quote_log=quote_log, bridge=bridge,
                    plugin_host=plugin_host, data_dir=data_dir)
    bot.log(f"飞书机器人启用: push={bot.push_mode} qna={bot.qna_enabled} "
            f"agent={bridge.backend_kind if bridge else None} event={bot.event_path}")
    if cfg.get("startup_notice"):
        threading.Thread(target=_startup_notice, args=(bot,), daemon=True).start()
    return bot


def _startup_notice(bot: FeishuBot):
    try:
        time.sleep(2)
        text = ("🤖 报价机器人已上线。报价消息会自动推送；@我 或私聊我可以提问（试试 /help）。")
        if bot.push_mode == "webhook" and bot.webhook_url:
            bot.client.push_webhook(bot.webhook_url, text, bot.webhook_secret)
        elif bot.push_mode == "app" and bot.push_chat_id:
            bot.client.send_text(bot.push_chat_id, "chat_id", text)
    except Exception as e:
        bot.log(f"上线通知失败: {e}")