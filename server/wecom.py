#!/usr/bin/env python3
"""企业微信智能机器人集成 —— 纯标准库 WebSocket 长连接（API 模式）。

参考：https://work.weixin.qq.com/nl/index/aicli
协议：https://developer.work.weixin.qq.com/document/path/101805
      （使用长连接 / 接收消息 / 回复消息 / 主动推送消息）

模块组成：
- WSClient        —— 极简 RFC6455 WebSocket 客户端（socket + ssl + 掩码帧），零第三方依赖
- WeComConnection —— 长连接生命周期：握手 → aibot_subscribe 认证 → 30s 心跳 → 断线指数退避重连
- WeComBot        —— 机器人业务：消息分发（插件指令 → Agent 问答）、进入会话欢迎语、
                     报价主动推送（aibot_send_msg，Markdown）

与钉钉/飞书机器人共用同一套 Agent 桥（server/pi_bridge.py）与插件框架（server/bot_plugins.py）。

配置见 wecom.config.json.example。
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import queue
import re
import socket
import ssl
import struct
import sys
import threading
import time
import uuid

from .agent_sessions import SessionManager, session_settings, sessions_dir

DEFAULT_WS_URL = "wss://openws.work.weixin.qq.com"
TEXT_LIMIT = 20000          # Markdown 内容上限 20480 字节，留余量
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MENTION_RE = re.compile(r"^(?:@[^\s@]+\s*)+")   # 去掉群聊里开头的 @机器人名

# WebSocket 命令（与官方协议一致）
CMD_SUBSCRIBE = "aibot_subscribe"
CMD_HEARTBEAT = "ping"
CMD_RESPOND = "aibot_respond_msg"
CMD_RESPOND_WELCOME = "aibot_respond_welcome_msg"
CMD_RESPOND_UPDATE = "aibot_respond_update_msg"
CMD_SEND = "aibot_send_msg"
CMD_MSG_CALLBACK = "aibot_msg_callback"
CMD_EVENT_CALLBACK = "aibot_event_callback"


class WeComError(RuntimeError):
    pass


class WebSocketClosed(ConnectionError):
    pass


# ---------------------------------------------------------------------------
# 极简 WebSocket 客户端（RFC6455，客户端掩码）
# ---------------------------------------------------------------------------

def build_ws_frame(opcode: int, payload: bytes, mask_key: bytes | None = None) -> bytes:
    """构造一个客户端帧（FIN=1，必带掩码）。mask_key 可注入以便测试。"""
    if mask_key is None:
        mask_key = os.urandom(4)
    header = bytearray([0x80 | (opcode & 0x0F)])
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack("!H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack("!Q", n)
    header += mask_key
    masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return bytes(header) + masked


def parse_ws_frame(buf: bytes):
    """尝试解析一帧，数据不足返回 None；否则返回 (fin, opcode, payload, consumed)。"""
    if len(buf) < 2:
        return None
    b0, b1 = buf[0], buf[1]
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    idx = 2
    if length == 126:
        if len(buf) < idx + 2:
            return None
        length = struct.unpack("!H", buf[idx:idx + 2])[0]
        idx += 2
    elif length == 127:
        if len(buf) < idx + 8:
            return None
        length = struct.unpack("!Q", buf[idx:idx + 8])[0]
        idx += 8
    mask = None
    if masked:
        if len(buf) < idx + 4:
            return None
        mask = buf[idx:idx + 4]
        idx += 4
    if len(buf) < idx + length:
        return None
    payload = buf[idx:idx + length]
    idx += length
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return fin, opcode, payload, idx


class WSClient:
    """同步阻塞式 WebSocket 客户端，仅覆盖本项目需要的文本帧收发。"""

    def __init__(self, url: str, *, timeout: int = 15, log=print):
        self.url = url
        self.timeout = timeout
        self.log = log
        self.sock: ssl.SSLSocket | socket.socket | None = None
        self._buf = b""
        self._send_lock = threading.Lock()

    # -- 握手 -----------------------------------------------------

    def connect(self):
        from urllib.parse import urlsplit
        u = urlsplit(self.url)
        if u.scheme not in ("ws", "wss"):
            raise WeComError(f"不支持的 websocket 协议: {self.url}")
        host = u.hostname or ""
        port = u.port or (443 if u.scheme == "wss" else 80)
        path = u.path or "/"
        if u.query:
            path += "?" + u.query

        raw = socket.create_connection((host, port), timeout=self.timeout)
        if u.scheme == "wss":
            ctx = ssl.create_default_context()
            raw = ctx.wrap_socket(raw, server_hostname=host)
        raw.settimeout(self.timeout)

        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        raw.sendall(request.encode("ascii"))

        header = b""
        while b"\r\n\r\n" not in header:
            chunk = raw.recv(4096)
            if not chunk:
                raise WebSocketClosed("握手时连接被关闭")
            header += chunk
        head, _, rest = header.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0].decode("latin1")
        if " 101 " not in status_line:
            raise WeComError(f"WebSocket 握手失败: {status_line}")

        expect = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode()
        if expect.lower() not in head.decode("latin1").lower():
            raise WeComError("WebSocket 握手响应缺少正确的 Sec-WebSocket-Accept")

        raw.settimeout(None)
        self.sock = raw
        self._buf = rest

    # -- 收发 -----------------------------------------------------

    def _send(self, opcode: int, payload: bytes):
        if self.sock is None:
            raise WebSocketClosed("连接未建立")
        with self._send_lock:
            self.sock.sendall(build_ws_frame(opcode, payload))

    def send_text(self, text: str):
        self._send(0x1, text.encode("utf-8"))

    def send_pong(self, payload: bytes = b""):
        self._send(0xA, payload)

    def recv_message(self, timeout: float | None = None):
        """读取一条完整消息（自动处理 ping/pong 与分片），返回 (opcode, payload)。

        超时抛 socket.timeout，连接关闭抛 WebSocketClosed。
        """
        if self.sock is None:
            raise WebSocketClosed("连接未建立")
        self.sock.settimeout(timeout)
        data = bytearray()
        op: int | None = None
        while True:
            parsed = parse_ws_frame(self._buf)
            if parsed is None:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise WebSocketClosed("连接被服务端关闭")
                self._buf += chunk
                continue
            fin, opcode, payload, consumed = parsed
            self._buf = self._buf[consumed:]
            if opcode == 0x9:          # ping → pong
                try:
                    self.send_pong(payload)
                except Exception:
                    pass
                continue
            if opcode == 0xA:          # pong
                continue
            if opcode == 0x8:          # close
                raise WebSocketClosed("服务端发送 close 帧")
            if opcode in (0x1, 0x2):
                data = bytearray(payload)
                op = opcode
                if fin:
                    return op, bytes(data)
            elif opcode == 0x0:        # continuation
                data += payload
                if fin:
                    return op, bytes(data)
            # 其它保留操作码忽略

    def close(self):
        sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 长连接管理：认证、心跳、重连、收发
# ---------------------------------------------------------------------------

class WeComConnection:
    """企业微信智能机器人长连接。

    on_callback(frame) 接收 aibot_msg_callback / aibot_event_callback；
    on_state(connected, error) 汇报连接状态（供 /api/wecom 展示）。
    """

    def __init__(self, bot_id: str, secret: str, ws_url: str = DEFAULT_WS_URL, *,
                 on_callback=None, on_state=None, log=print,
                 heartbeat: int = 30, max_backoff: int = 60, connect_timeout: int = 15):
        self.bot_id = bot_id
        self.secret = secret
        self.ws_url = ws_url or DEFAULT_WS_URL
        self.on_callback = on_callback
        self.on_state = on_state
        self.log = log
        self.heartbeat = max(int(heartbeat or 30), 5)
        self.max_backoff = max(int(max_backoff or 60), 5)
        self.connect_timeout = connect_timeout

        self._ws: WSClient | None = None
        self._ws_lock = threading.Lock()
        self._out_q: "queue.Queue[str]" = queue.Queue()
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._peer_disconnected = False   # 收到 disconnected_event：被新连接接管
        self._thread: threading.Thread | None = None
        self._seq = 0
        self._seq_lock = threading.Lock()

    # -- 生命周期 -------------------------------------------------

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="wecom-ws")
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._close()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def disconnect_current(self, peer: bool = False):
        """主动断开当前连接（触发重连流程）。peer=True 表示被新连接接管，延迟久一点再重连。"""
        if peer:
            self._peer_disconnected = True
        self._close()

    # -- 发送 -----------------------------------------------------

    def _req_id(self, cmd: str) -> str:
        with self._seq_lock:
            self._seq += 1
            n = self._seq
        return f"{cmd}-{int(time.time() * 1000)}-{n}"

    def send_command(self, cmd: str, body: dict, req_id: str | None = None) -> str:
        """发送一条命令；未连接时进队列，重连后补发。返回 req_id。"""
        req = req_id or self._req_id(cmd)
        payload = json.dumps(
            {"cmd": cmd, "headers": {"req_id": req}, "body": body},
            ensure_ascii=False)
        ws = self._current_ws()
        sent = False
        if ws is not None and self._connected.is_set():
            try:
                ws.send_text(payload)
                sent = True
            except Exception as e:  # 断线瞬间：转入队列
                self.log(f"发送失败，转待发队列: {e}")
        if not sent:
            self._out_q.put(payload)
        return req

    def _current_ws(self) -> WSClient | None:
        with self._ws_lock:
            return self._ws

    def _close(self):
        with self._ws_lock:
            ws, self._ws = self._ws, None
        self._connected.clear()
        if ws is not None:
            ws.close()

    # -- 连接主循环 ----------------------------------------------

    def _run(self):
        backoff = 1
        while not self._stop.is_set():
            try:
                ws = WSClient(self.ws_url, timeout=self.connect_timeout, log=self.log)
                ws.connect()
                with self._ws_lock:
                    self._ws = ws
                self._subscribe(ws)
                self._connected.set()
                self._state(True, "")
                self.log("企业微信长连接已认证 (aibot_subscribe)")
                backoff = 1
                self._pump(ws)
            except Exception as e:
                if self._stop.is_set():
                    break
                self._connected.clear()
                self._state(False, str(e))
                # 被新连接接管时多等一会，避免两个实例反复抢连接
                delay = max(backoff, 15) if self._peer_disconnected else backoff
                self._peer_disconnected = False
                self.log(f"长连接异常（{delay}s 后重连）: {e}")
                self._close()
                self._stop.wait(delay)
                backoff = min(backoff * 2, self.max_backoff)
        self._connected.clear()

    def _subscribe(self, ws: WSClient):
        req = self._req_id(CMD_SUBSCRIBE)
        ws.send_text(json.dumps({
            "cmd": CMD_SUBSCRIBE,
            "headers": {"req_id": req},
            "body": {"bot_id": self.bot_id, "secret": self.secret},
        }, ensure_ascii=False))
        resp = self._await(ws, req, self.connect_timeout)
        if not resp or int(resp.get("errcode") or 0) != 0:
            raise WeComError(f"订阅失败: {resp}")

    def _await(self, ws: WSClient, req_id: str, timeout: float):
        deadline = time.time() + timeout
        while not self._stop.is_set():
            remain = deadline - time.time()
            if remain <= 0:
                return None
            try:
                op, data = ws.recv_message(timeout=remain)
            except socket.timeout:
                return None
            if op != 0x1:
                continue
            frame = _loads(data)
            if frame is None:
                continue
            if (frame.get("headers") or {}).get("req_id") == req_id and "errcode" in frame:
                return frame
            self._dispatch(frame)
        return None

    def _pump(self, ws: WSClient):
        last_ping = time.time()
        while not self._stop.is_set():
            self._flush(ws)
            wait = max(0.2, min(1.0, self.heartbeat - (time.time() - last_ping)))
            try:
                op, data = ws.recv_message(timeout=wait)
            except socket.timeout:
                op, data = None, b""
            if op == 0x1:
                frame = _loads(data)
                if frame is not None:
                    self._dispatch(frame)
            if time.time() - last_ping >= self.heartbeat:
                last_ping = time.time()
                ws.send_text(json.dumps({
                    "cmd": CMD_HEARTBEAT,
                    "headers": {"req_id": self._req_id("ping")},
                }, ensure_ascii=False))

    def _flush(self, ws: WSClient):
        while True:
            try:
                payload = self._out_q.get_nowait()
            except queue.Empty:
                return
            try:
                ws.send_text(payload)
            except Exception as e:
                self._out_q.put(payload)
                raise WeComError(f"待发队列发送失败: {e}") from e

    def _dispatch(self, frame: dict):
        errcode = frame.get("errcode")
        if errcode not in (None, 0):
            self.log(f"服务端返回错误 cmd={frame.get('cmd')} "
                     f"errcode={errcode} errmsg={frame.get('errmsg')}")
            return
        if frame.get("cmd") in (CMD_MSG_CALLBACK, CMD_EVENT_CALLBACK):
            if self.on_callback is None:
                return
            try:
                self.on_callback(frame)
            except Exception as e:
                self.log(f"回调处理异常: {e}")

    def _state(self, connected: bool, error: str):
        if self.on_state is None:
            return
        try:
            self.on_state(connected, error)
        except Exception:
            pass


def _loads(data: bytes) -> dict | None:
    try:
        frame = json.loads(data.decode("utf-8"))
    except Exception:
        return None
    return frame if isinstance(frame, dict) else None


# ---------------------------------------------------------------------------
# 机器人：消息分发（插件 + 问答）+ 报价主动推送
# ---------------------------------------------------------------------------

class WeComBot:
    def __init__(self, cfg: dict, *, store=None, quote_log=None, bridge=None,
                 plugin_host=None, data_dir: str = "data", connection=None):
        self.cfg = cfg
        self.store = store
        self.quote_log = quote_log
        self.bridge = bridge
        self.plugin_host = plugin_host

        self.bot_id = str(cfg.get("bot_id") or "")
        self.secret = str(cfg.get("secret") or "")
        self.ws_url = str(cfg.get("ws_url") or DEFAULT_WS_URL)

        push = cfg.get("push") or {}
        self.push_enabled = bool(push.get("enabled", True))
        self.push_chat_id = str(push.get("chat_id") or "")
        self.push_chat_type = int(push.get("chat_type") or 2)
        self.push_rooms = list(cfg.get("push_rooms") or ["*"])
        self.chat_room_map = dict(cfg.get("chat_room_map") or {})

        qna = cfg.get("qna") or {}
        self.qna_enabled = bool(qna.get("enabled", True))
        self.qna_system = str(qna.get("system_prompt") or "")
        self.qna_recent = int(qna.get("recent_quotes") or 20)
        self.qna_session_ttl = int(qna.get("session_ttl") or 1800)
        self.welcome = str(cfg.get("welcome") or
                           "您好！我是报价助理，发送 /help 查看可用指令。")

        self.data_dir = data_dir
        os.makedirs(os.path.join(data_dir, "wecom"), exist_ok=True)
        self.log_path = os.path.join(data_dir, "wecom", "bot.log")
        self.state_path = os.path.join(data_dir, "wecom", "push_state.json")

        self.connected = False
        self.last_error = ""
        self.pushed = 0
        self._seen_sigs = self._load_state()
        self._seen_msgs: set[str] = set()
        self._last_chat = ""
        self._last_chat_type = 2
        self._chat_lock = threading.Lock()
        self._push_q: "queue.Queue[tuple]" = queue.Queue()
        self.sessions = SessionManager(
            "wecom", ttl=self.qna_session_ttl, persist_dir=sessions_dir(data_dir, bridge),
            max_sessions=int(session_settings(bridge).get("max_sessions") or 500), log=lambda m: self.log(m))
        self._warned_no_bridge = False

        self.conn = connection or WeComConnection(
            self.bot_id, self.secret, self.ws_url,
            on_callback=self._on_frame, on_state=self._on_state, log=self.log,
            heartbeat=int(cfg.get("heartbeat_interval") or 30),
            max_backoff=int(cfg.get("reconnect_max_interval") or 60))

        if self.push_enabled:
            threading.Thread(target=self._push_loop, daemon=True,
                             name="wecom-push").start()

    # -- 日志与状态 ------------------------------------------------

    def log(self, msg: str):
        line = f"{dt.datetime.now():%F %T} {msg}\n"
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass
        print("[wecom] " + msg, file=sys.stderr)

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
            "bot_id": self.bot_id,
            "ws_connected": self.connected,
            "ws_error": self.last_error,
            "push_mode": "send" if self.push_enabled else "none",
            "push_configured": bool(self.push_chat_id or self._last_chat),
            "push_target": self.push_chat_id or self._last_chat or None,
            "qna_enabled": self.qna_enabled,
            "pushed": self.pushed,
            "agent": self.bridge.backend_kind if self.bridge else None,
        }

    def start(self):
        if not (self.bot_id and self.secret):
            self.log("未配置 bot_id/secret，长连接未启动")
            return
        self.conn.start()

    def _on_state(self, connected: bool, error: str):
        self.connected = connected
        self.last_error = error or ""

    # -- 连接回调入口 ----------------------------------------------

    def _on_frame(self, frame: dict):
        cmd = frame.get("cmd")
        if cmd == CMD_MSG_CALLBACK:
            self._handle_msg(frame)
        elif cmd == CMD_EVENT_CALLBACK:
            self._handle_event(frame)

    # -- 消息处理（插件指令 + Agent 问答） -------------------------

    def _handle_msg(self, frame: dict):
        body = frame.get("body") or {}
        req_id = (frame.get("headers") or {}).get("req_id") or ""

        msgid = str(body.get("msgid") or "")
        if msgid:
            if msgid in self._seen_msgs:
                return
            self._seen_msgs.add(msgid)
            if len(self._seen_msgs) > 2000:
                self._seen_msgs = set(list(self._seen_msgs)[-1000:])

        from_user = body.get("from") or {}
        userid = str(from_user.get("userid") or "")
        chatid = str(body.get("chatid") or userid)
        chattype = str(body.get("chattype") or "single")
        if chatid:
            self._remember_chat(chatid, 1 if chattype == "single" else 2)

        text = self._extract_text(body)
        if text is None:
            self._reply_markdown(req_id, "目前只支持文字消息，可以发文字试试～")
            return
        text = MENTION_RE.sub("", text).strip()
        if not text:
            return
        self.log(f"收到消息 chat={chatid} type={chattype} user={userid} text={text!r}")

        api = self.plugin_host.spawn_api() if self.plugin_host else None
        event = {"event": {"message": {"message_id": msgid, "chat_id": chatid,
                                        "chat_type": chattype},
                           "sender": {"sender_type": "user",
                                      "sender_id": {"open_id": userid}}}}
        if api is not None:
            api._set_event(event, text)
            api.sessions, api.session_key = self.sessions, chatid

        # 1) 插件消息钩子
        if self.plugin_host:
            verdict = self.plugin_host.run_message_hooks(api, event, text)
            if isinstance(verdict, str):
                self._reply_markdown(req_id, verdict)
                return
            if verdict is False:
                return

        # 2) 斜杠命令
        if text.startswith("/"):
            name, _, args = text[1:].partition(" ")
            name = name.lower()
            entry = self.plugin_host.commands.get(name) if self.plugin_host else None
            if entry:
                self.log(f"command /{name} from {chatid}")
                out = self.plugin_host.run_command(name, args.strip(), api)
                if out:
                    self._reply_markdown(req_id, out)
                return
            self._reply_markdown(req_id, f"未知指令 /{name}，试试 /help")
            return

        # 3) Agent 问答
        if not self.qna_enabled:
            return
        if self.bridge is None:
            if not self._warned_no_bridge:
                self._warned_no_bridge = True
                self.log("问答未启用：未配置 Agent（agent.config.json）")
            self._reply_markdown(req_id, "问答功能未启用：服务器没有配置 Agent 模型。")
            return
        threading.Thread(target=self._qa_job, args=(req_id, chatid, text),
                         daemon=True, name="wecom-qa").start()

    @staticmethod
    def _extract_text(body: dict) -> str | None:
        mtype = body.get("msgtype")
        if mtype == "text":
            return ((body.get("text") or {}).get("content") or "").strip() or None
        if mtype == "mixed":
            parts = []
            for item in ((body.get("mixed") or {}).get("msg_item") or []):
                if isinstance(item, dict) and item.get("msgtype") == "text":
                    parts.append((item.get("text") or {}).get("content") or "")
            return "".join(parts).strip() or None
        if mtype == "voice":
            return ((body.get("voice") or {}).get("content") or "").strip() or None
        return None

    # -- 事件处理 --------------------------------------------------

    def _handle_event(self, frame: dict):
        body = frame.get("body") or {}
        req_id = (frame.get("headers") or {}).get("req_id") or ""
        event = body.get("event") or {}
        etype = event.get("eventtype") or ""
        if etype == "enter_chat":
            if self.welcome:
                self.conn.send_command(
                    CMD_RESPOND_WELCOME,
                    {"msgtype": "text", "text": {"content": self.welcome[:TEXT_LIMIT]}},
                    req_id=req_id)
            return
        if etype == "template_card_event":
            self.log(f"模板卡片事件 key={((event.get('template_card_event') or {}).get('event_key'))}")
            return
        if etype == "feedback_event":
            fb = event.get("feedback_event") or {}
            self.log(f"用户反馈 type={fb.get('type')} content={fb.get('content')!r}")
            return
        if etype == "disconnected_event":
            self.log("收到 disconnected_event：已有新连接接管，断开当前连接并延迟重连")
            self.connected = False
            self.conn.disconnect_current(peer=True)
            return

    # -- 回复 ------------------------------------------------------

    def _reply_markdown(self, req_id: str, text: str):
        self.conn.send_command(
            CMD_RESPOND,
            {"msgtype": "markdown", "markdown": {"content": text[:TEXT_LIMIT]}},
            req_id=req_id)

    def _reply_stream(self, req_id: str, stream_id: str, content: str, finish: bool):
        self.conn.send_command(
            CMD_RESPOND,
            {"msgtype": "stream",
             "stream": {"id": stream_id, "finish": finish,
                        "content": content[:TEXT_LIMIT]}},
            req_id=req_id)

    # -- 问答 ------------------------------------------------------

    def _qa_job(self, req_id: str, chatid: str, text: str):
        stream_id = f"stream-{uuid.uuid4().hex}"
        try:
            self._reply_stream(req_id, stream_id, "⏳ 正在思考…", finish=False)
            session = self._get_session(chatid)
            ctx = self._qa_context(chatid)
            answer = session.ask(text, context=ctx)
            if not answer:
                answer = "我暂时回答不了这个问题（模型没有给出结果）。"
            self._reply_stream(req_id, stream_id, answer, finish=True)
        except Exception as e:
            self.log(f"问答失败 chat={chatid}: {e}")
            self._reply_stream(req_id, stream_id, f"处理这个问题时出错了：{e}", finish=True)

    def _get_session(self, chatid: str):
        from .pi_bridge import QNA_SYSTEM
        return self.sessions.get(chatid, lambda: self.bridge.begin_session(
            f"wecom-{chatid}", self.qna_system or QNA_SYSTEM, self._qa_tools()))

    def _qa_tools(self) -> list[dict]:
        from .qa_tools import build_qa_tools
        return build_qa_tools(self.store, self.quote_log)

    def _qa_context(self, chatid: str) -> str | None:
        """给问答注入背景：优先该会话映射到的微信群，否则最近全部报价。"""
        from .qa_tools import rows_text
        parts = []
        room = self._room_for_chat(chatid)
        if self.quote_log:
            rows, _ = self.quote_log.list(0, 500, room)
            if rows:
                parts.append("最近报价（背景资料，回答中如引用请转述数据）:\n"
                             + rows_text(rows[-self.qna_recent:]))
        if room is None and self.store:
            rooms = self.store.rooms()
            if rooms:
                from .qa_tools import room_name
                parts.append("已接入的微信群：" + "、".join(
                    room_name(r) for r in rooms[:10]))
        return "\n".join(parts) or None

    def _room_for_chat(self, chatid: str) -> str | None:
        mapped = self.chat_room_map.get(chatid)
        if not mapped:
            return None
        if self.store:
            for r in self.store.rooms():
                rid = r.get("roomId") or ""
                if rid == mapped or rid.endswith(mapped) or mapped in rid:
                    return rid
        return mapped if "@chatroom" in mapped or "/" in mapped else None

    def _remember_chat(self, chatid: str, chat_type: int):
        with self._chat_lock:
            self._last_chat = chatid
            self._last_chat_type = chat_type

    def _target_chat(self) -> tuple[str, int]:
        with self._chat_lock:
            if self.push_chat_id:
                return self.push_chat_id, self.push_chat_type
            return self._last_chat, self._last_chat_type

    # -- 报价推送 --------------------------------------------------

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

    def _push_one(self, room: str, rows: list[dict]):
        # 1) 插件报价钩子：返回 False 则插件自行推送
        if self.plugin_host:
            api = self.plugin_host.spawn_api()
            if self.plugin_host.run_quote_hooks(api, {"room": room, "rows": rows}) is False:
                return
        # 2) Agent 格式化（失败自动模板兜底）
        for r in rows:
            r.setdefault("timeLabel", _now_label(r.get("time")))
        text = self._format(room, rows)
        # 3) 主动推送（Markdown）
        chatid, chat_type = self._target_chat()
        if not chatid:
            self.log(f"未配置 push.chat_id 且机器人尚未收到过消息，跳过推送 room={room}")
            return
        self.conn.send_command(
            CMD_SEND,
            {"chatid": chatid, "chat_type": chat_type,
             "msgtype": "markdown", "markdown": {"content": text[:TEXT_LIMIT]}})
        self.pushed += 1
        self.log(f"已推送报价 room={room} items={len(rows)} -> {chatid}")

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


def _now_label(ms) -> str:
    try:
        return dt.datetime.fromtimestamp(int(ms) / 1000).strftime("%m-%d %H:%M")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 配置与装配
# ---------------------------------------------------------------------------

def load_wecom_config(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"[wecom] 配置解析失败 {path}: {e}", file=sys.stderr)
        return None
    if not isinstance(cfg, dict) or not cfg.get("enabled"):
        return None
    return cfg


def create_wecom_bot(config_path: str, *, store=None, quote_log=None, bridge=None,
                     plugin_host=None, data_dir: str = "data") -> WeComBot | None:
    cfg = load_wecom_config(config_path)
    if not cfg:
        return None
    bot = WeComBot(cfg, store=store, quote_log=quote_log, bridge=bridge,
                   plugin_host=plugin_host, data_dir=data_dir)
    bot.log(f"企业微信机器人启用: bot_id={'已配置' if bot.bot_id else '缺失'} "
            f"push={bot.push_enabled}({bot.push_chat_id or '最近会话'}) "
            f"qna={bot.qna_enabled} agent={bridge.backend_kind if bridge else None}")
    return bot
