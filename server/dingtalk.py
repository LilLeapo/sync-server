#!/usr/bin/env python3
"""钉钉机器人集成：自定义机器人 webhook 报价推送 + 企业内部应用机器人（Stream 长连接）问答。

- DingTalkClient — 自定义机器人 webhook（可选加签）推送纯文本
- DingTalkBot    — 报价推送队列（webhook）+ 企业内部应用机器人 Stream 长连接收消息：
                  /help 等指令（复用插件命令表）→ Agent 问答

配置见 dingtalk.config.json.example：
  自定义机器人（群推送）：https://open.dingtalk.com/document/robots/custom-robot-access
  企业内部应用机器人 + Stream 模式：https://open.dingtalk.com/document/orgapp/stream-mode
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
import urllib.parse
import urllib.request

DINGTALK_ROBOT_BASE = "https://oapi.dingtalk.com/robot/send"
TEXT_LIMIT = 8000
MENTION_RE = re.compile(r"@_user_\d+|@_all")

_STREAM_HANDLER = None  # 由 _build_stream_handler 创建，供诊断


def webhook_sign(secret: str, timestamp_ms: int) -> str:
    """钉钉自定义机器人加签：HMAC-SHA256(key = f"{ts}\\n{secret}", msg = "") → base64。"""
    string_to_sign = f"{timestamp_ms}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), b"", hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def now_label(ms) -> str:
    try:
        return dt.datetime.fromtimestamp(int(ms) / 1000).strftime("%m-%d %H:%M")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 自定义机器人客户端
# ---------------------------------------------------------------------------

class DingTalkClient:
    def __init__(self, cfg: dict):
        wh = cfg.get("webhook") or {}
        self.access_token = str(wh.get("access_token") or "")
        self.secret = str(wh.get("secret") or "")

    def configured(self) -> bool:
        return bool(self.access_token)

    def push_text(self, text: str) -> dict:
        if not self.access_token:
            raise RuntimeError("钉钉自定义机器人未配置 access_token")
        url = DINGTALK_ROBOT_BASE + "?" + urllib.parse.urlencode(
            {"access_token": self.access_token})
        payload: dict = {"msgtype": "text", "text": {"content": text}}
        if self.secret:
            ts = int(time.time() * 1000)
            payload["timestamp"] = str(ts)
            payload["sign"] = webhook_sign(self.secret, ts)
        req = urllib.request.Request(
            url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            out = json.loads(resp.read().decode("utf-8"))
        if out.get("errcode") not in (0, None):
            raise RuntimeError(f"钉钉推送失败 {out.get('errcode')}: {out.get('errmsg')}")
        return out


# ---------------------------------------------------------------------------
# 机器人：推送 + Stream 长连接问答
# ---------------------------------------------------------------------------

class DingTalkBot:
    def __init__(self, cfg: dict, *, store=None, quote_log=None, bridge=None,
                 plugin_host=None, data_dir: str = "data"):
        self.cfg = cfg
        self.store = store
        self.quote_log = quote_log
        self.bridge = bridge
        self.plugin_host = plugin_host
        self.mode = str(cfg.get("mode") or "webhook")
        self.client = DingTalkClient(cfg)
        app = cfg.get("app") or {}
        self.client_id = str(app.get("client_id") or "")
        self.client_secret = str(app.get("client_secret") or "")
        self.active_push = self.client.configured()
        self.active_stream = bool(self.client_id and self.client_secret)
        self.push_rooms = list(cfg.get("push_rooms") or ["*"])
        qna = cfg.get("qna") or {}
        self.qna_enabled = bool(qna.get("enabled", True))
        self.qna_system = str(qna.get("system_prompt") or "")
        self.qna_session_ttl = int(qna.get("session_ttl") or 1800)

        self.data_dir = data_dir
        os.makedirs(os.path.join(data_dir, "dingtalk"), exist_ok=True)
        self.log_path = os.path.join(data_dir, "dingtalk", "bot.log")
        self.state_path = os.path.join(data_dir, "dingtalk", "push_state.json")

        self._seen_sigs = self._load_state()
        self._push_q: "queue.Queue[tuple]" = queue.Queue()
        self._sessions: dict[str, list] = {}
        self._sessions_lock = threading.Lock()
        self.pushed = 0
        self.ws_connected = False
        self.ws_error = ""
        self._warned_no_bridge = False

        if self.active_push:
            self._pusher = threading.Thread(target=self._push_loop, daemon=True,
                                            name="dingtalk-push")
            self._pusher.start()

    # -- 日志与状态 ------------------------------------------------

    def log(self, msg: str):
        line = f"{dt.datetime.now():%F %T} {msg}\n"
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass
        print("[dingtalk] " + msg, file=sys.stderr)

    def _load_state(self) -> set:
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                return set(json.load(f).get("seen_sigs") or [])
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
            "mode": self.mode,
            "push_configured": self.active_push,
            "ws_connected": self.ws_connected,
            "ws_error": self.ws_error,
            "qna_enabled": self.qna_enabled,
            "pushed": self.pushed,
            "agent": self.bridge.backend_kind if self.bridge else None,
        }

    # -- 报价推送 ------------------------------------------------

    def notify_quote(self, rows: list[dict]):
        if not self.active_push or not rows:
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
            (r.get("roomId"), r.get("msgSeq"), r.get("供应商"), r.get("项目"), r.get("价格"),
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
        for r in rows:
            r.setdefault("timeLabel", now_label(r.get("time")))
        text = self._format(room, rows)
        self.client.push_text(text)
        self.pushed += 1
        self.log(f"已推送报价 room={room} items={len(rows)}")

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

    # -- Stream 长连接收消息 --------------------------------------

    def start(self):
        if self.mode == "stream" and self.active_stream:
            threading.Thread(target=self._stream_loop, daemon=True,
                             name="dingtalk-stream").start()
        elif self.mode not in ("webhook", "none"):
            self.log(f"未知模式 {self.mode}，回退 webhook")

    def _stream_loop(self):
        retry = 0
        while True:
            try:
                import dingtalk_stream  # 钉钉官方 Stream 模式 SDK
                client = dingtalk_stream.DingTalkStreamClient(
                    dingtalk_stream.Credential(self.client_id, self.client_secret))
                handler = _build_stream_handler(self)
                client.register_callback_handler(
                    dingtalk_stream.ChatbotMessage.TOPIC, handler)
                self.ws_error = ""
                self.ws_connected = True
                retry = 0
                self.log("Stream 长连接启动 (dingtalk-stream)")
                client.start_forever()
            except Exception as e:
                self.ws_connected = False
                self.ws_error = str(e)
                self.log(f"Stream 异常（第 {retry + 1} 次）: {e}")
                retry += 1
            time.sleep(min(5 + retry * 5, 60))

    # -- 消息处理（插件命令 + 问答） --------------------------------

    def _on_text(self, text: str, reply, conv_id: str):
        if not text or not conv_id:
            return
        if text.startswith("/"):
            name, _, args = text[1:].partition(" ")
            name = name.lower()
            entry = self.plugin_host.commands.get(name) if self.plugin_host else None
            if entry:
                self.log(f"command /{name} from {conv_id}")
                api = self.plugin_host.spawn_api() if self.plugin_host else None
                out = self.plugin_host.run_command(name, args.strip(), api)
                if out:
                    reply(out)
                return
            reply(f"未知指令 /{name}，试试 /help")
            return

        if not self.qna_enabled:
            return
        if self.bridge is None:
            if not self._warned_no_bridge:
                self._warned_no_bridge = True
                self.log("问答未启用：未配置 Agent（agent.config.json）")
            reply("问答功能未启用：服务器没有配置 Agent 模型。")
            return
        threading.Thread(target=self._qa_job, args=(conv_id, text, reply),
                         daemon=True, name="dingtalk-qa").start()

    def _qa_job(self, conv_id: str, text: str, reply):
        try:
            session = self._get_session(conv_id)
            ctx = self._qa_context(conv_id)
            answer = session.ask(text, context=ctx)
            reply(answer or "我暂时回答不了这个问题（模型没有给出结果）。")
        except Exception as e:
            self.log(f"问答失败 conv={conv_id}: {e}")
            reply(f"处理这个问题时出错了：{e}")

    def _get_session(self, conv_id: str):
        from .pi_bridge import QNA_SYSTEM
        with self._sessions_lock:
            entry = self._sessions.get(conv_id)
            if entry and time.time() - entry[1] < self.qna_session_ttl:
                entry[1] = time.time()
                return entry[0]
        system = self.qna_system or QNA_SYSTEM
        session = self.bridge.begin_session(f"dingtalk-{conv_id}", system)
        with self._sessions_lock:
            self._sessions[conv_id] = [session, time.time()]
        return session

    def _qa_context(self, conv_id: str) -> str:
        """把最近的报价摘录给模型当上下文，保持与飞书问答接近的能力。"""
        try:
            rows = self.quote_log.list(0, 20, None)[0] if self.quote_log else []
        except Exception:
            return ""
        lines = []
        for r in rows[-10:]:
            supplier = r.get("供应商") or ""
            head = f"[{supplier}]" if supplier else ""
            line = f"{head}{r.get('项目')} {r.get('价格')} {r.get('单位')}".strip()
            if r.get("数量"):
                line += f" ×{r.get('数量')}"
            if r.get("备注"):
                line += f" {r.get('备注')}"
            line += f"（{r.get('sender')}，{now_label(r.get('time'))}）"
            lines.append(line)
        return "最近报价：\n" + "\n".join(lines) if lines else ""


# ---------------------------------------------------------------------------
# Stream SDK handler
# ---------------------------------------------------------------------------

def _build_stream_handler(bot: DingTalkBot):
    """返回官方 SDK 的 ChatbotHandler 子类实例（回复走 SDK 的 session webhook）。"""
    import dingtalk_stream

    class _Handler(dingtalk_stream.ChatbotHandler):
        async def process(self, callback_message):
            try:
                message = dingtalk_stream.ChatbotMessage.from_dict(callback_message.data)
                texts = _extract_texts(message)
                text = MENTION_RE.sub("", texts[0]).strip() if texts else ""
                if text:
                    if getattr(message, "sender_id", None):
                        bot.log(f"stream msg from "
                                f"{message.sender_nick or message.sender_id}")
                    bot._on_text(
                        text,
                        lambda out: _safe_reply(self, message, out, bot),
                        str(getattr(message, "conversation_id", "") or ""))
            except Exception as e:
                bot.log(f"stream 消息处理失败: {e}")
            return dingtalk_stream.AckMessage.STATUS_OK, "OK"

    global _STREAM_HANDLER
    _STREAM_HANDLER = _Handler()
    return _STREAM_HANDLER


def _safe_reply(handler, message, text: str, bot: DingTalkBot):
    try:
        handler.reply_text(text, message)
    except Exception as e:
        bot.log(f"回复失败: {e}")


def _extract_texts(message) -> list[str]:
    try:
        if getattr(message, "message_type", None) == "text" and getattr(message, "text", None):
            content = getattr(message.text, "content", None)
            if content:
                return [str(content)]
    except Exception:
        pass
    return []


# ---------------------------------------------------------------------------
# 配置与装配
# ---------------------------------------------------------------------------

def load_dingtalk_config(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"[dingtalk] 配置解析失败 {path}: {e}", file=sys.stderr)
        return None
    if not isinstance(cfg, dict) or not cfg.get("enabled"):
        return None
    return cfg


def create_dingtalk_bot(config_path: str, *, store=None, quote_log=None, bridge=None,
                        plugin_host=None, data_dir: str = "data") -> DingTalkBot | None:
    cfg = load_dingtalk_config(config_path)
    if not cfg:
        return None
    bot = DingTalkBot(cfg, store=store, quote_log=quote_log, bridge=bridge,
                      plugin_host=plugin_host, data_dir=data_dir)
    bot.log(f"钉钉机器人启用: push={bot.active_push} stream={bot.active_stream} "
            f"qna={bot.qna_enabled} agent={bridge.backend_kind if bridge else None}")
    return bot
