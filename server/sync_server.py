#!/usr/bin/env python3
"""sync-server 主入口：手机模块长连接接收 + HTTP API + 网页前端托管。

用法：
  python3 -m server.sync_server                 # 本机（adb reverse）模式
  python3 -m server.sync_server --bind 0.0.0.0 --token <secret>   # 公网模式

依赖：Python 标准库 + ffmpeg（仅用于解码微信 wxgf 私有图片格式）。
"""
import argparse
import hmac
import json
import mimetypes
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

try:
    from .relay import RelayListener
    from .store import Store, QuoteLog
    from . import quote_agent, pi_bridge, bot_plugins, wechat_reply
    from . import feishu as feishu_mod
    from . import dingtalk as dingtalk_mod
except ImportError:  # 允许 python3 server/sync_server.py 直接运行
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from server.relay import RelayListener
    from server.store import Store, QuoteLog
    from server import quote_agent, pi_bridge, bot_plugins, wechat_reply
    from server import feishu as feishu_mod
    from server import dingtalk as dingtalk_mod


class Handler(BaseHTTPRequestHandler):
    server_version = "SyncServer/1.0"
    protocol_version = "HTTP/1.1"

    # ---------- 基础响应 ----------

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _text(self, code: int, text: str, ctype: str = "text/plain; charset=utf-8"):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------- 鉴权 ----------

    def _authorized(self) -> bool:
        token = self.server.token
        if not token:
            return True
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:], token):
            return True
        params = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        q = params.get("token", [None])[0]
        return bool(q) and hmac.compare_digest(q, token)

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self._json(401, {"ok": False, "error": "unauthorized"})
        return False

    # ---------- 上报与 API ----------

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 0:
            return self.rfile.read(length)
        return self.rfile.read()

    def _params(self) -> dict:
        return {k: v[0] for k, v in parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "").items()}

    def _handle_post(self):
        store = self.server.store
        raw = self._read_body()
        try:
            msg = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            self._json(400, {"ok": False, "error": "invalid json"})
            return
        if not isinstance(msg, dict) or not msg.get("roomId"):
            self._json(400, {"ok": False, "error": "missing roomId"})
            return
        rec = store.add_message(msg)
        if rec is None:
            self._json(200, {"ok": True, "dup": True})
            return
        self._notify_agent(rec)
        self._notify_replier(rec)
        self._json(200, {"ok": True, "seq": rec["seq"]})

    def _notify_agent(self, rec: dict):
        agent = getattr(self.server, "agent", None)
        if agent is not None:
            try:
                agent.enqueue(rec)
            except Exception:
                pass

    def _notify_replier(self, rec: dict):
        replier = getattr(self.server, "wechat_replier", None)
        if replier is not None:
            try:
                replier.enqueue(rec.get("msg") or {})
            except Exception:
                pass

    def _handle_messages(self):
        store = self.server.store
        params = self._params()
        try:
            after = int(params.get("after", "0"))
        except ValueError:
            after = 0
        try:
            limit = min(int(params.get("limit", "1000")), 5000)
        except ValueError:
            limit = 1000
        room = params.get("room") or None
        qtext = params.get("q") or ""
        msgs, total, start = store.list_messages(after, limit)
        if room:
            msgs = [r for r in msgs if r["msg"].get("roomId") == room]
        if qtext:
            needle = qtext.lower()
            msgs = [r for r in msgs if needle in (r["msg"].get("content") or "").lower()]
        # imageBase64 落盘时已存为 imgFile，响应里剔除以控制体积（存储不变）
        now_ms = int(time.time() * 1000)
        original_ids = {
            (r.get("msg") or {}).get("msgSvrId")
            for r in store.messages
            if isinstance(r.get("msg"), dict)
            and r["msg"].get("type") == 3
            and r["msg"].get("imageKind") == "original"
        }
        PENDING_ORIGINAL_MS = 120_000  # 手机下载/上传原图通常几十秒内完成
        slim = []
        for r in msgs:
            m = r.get("msg")
            if isinstance(m, dict) and m.get("imageBase64"):
                m = dict(m)
                m.pop("imageBase64", None)
                r = dict(r)
                r["msg"] = m
            m = r.get("msg")
            if (
                isinstance(m, dict)
                and m.get("type") == 3
                and m.get("imageKind") != "original"
            ):
                svr = m.get("msgSvrId")
                recv_at = r.get("receivedAt") or now_ms
                if (
                    svr not in original_ids
                    and now_ms - recv_at < PENDING_ORIGINAL_MS
                ):
                    r["pendingOriginal"] = True
            slim.append(r)
        self._json(200, {"ok": True, "total": total, "start": start, "messages": slim})

    def _handle_rooms(self):
        self._json(200, {"ok": True, "rooms": self.server.store.rooms()})

    def _handle_stats(self):
        listener = getattr(self.server, "relay_listener", None)
        clients = listener.client_count() if listener else 0
        self._json(200, {"ok": True, **self.server.store.stats(), "socketClients": clients})

    def _handle_quotes(self):
        params = self._params()
        try:
            after = int(params.get("after", "0"))
        except ValueError:
            after = 0
        try:
            limit = min(int(params.get("limit", "500")), 2000)
        except ValueError:
            limit = 500
        room = params.get("room") or None
        rows, total = self.server.quote_log.list(after, limit, room)
        self._json(200, {"ok": True, "total": total, "quotes": rows})

    def _handle_clear(self):
        params = self._params()
        count = self.server.store.clear(params.get("room") or None)
        agent = getattr(self.server, "agent", None)
        if agent is not None:
            # 消息清空后 seq 重新计数，重置去重状态防止新消息被误判已处理
            agent.reset_state()
        self._json(200, {"ok": True, "count": count})

    def _handle_feishu_status(self):
        bot = getattr(self.server, "feishu_bot", None)
        if bot is None:
            self._json(200, {"ok": True, "feishu": {"enabled": False}})
            return
        self._json(200, {"ok": True, "feishu": bot.bot_status()})

    def _handle_dingtalk_status(self):
        bot = getattr(self.server, "dingtalk_bot", None)
        if bot is None:
            self._json(200, {"ok": True, "dingtalk": {"enabled": False}})
            return
        self._json(200, {"ok": True, "dingtalk": bot.bot_status()})

    # ---------- 静态与图片 ----------

    def _handle_static(self, path: str):
        web_dir = self.server.web_dir
        rel = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
        full = os.path.realpath(os.path.join(web_dir, rel))
        root = os.path.realpath(web_dir)
        if not full.startswith(root + os.sep) or not os.path.isfile(full):
            self._text(404, "not found")
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _handle_image(self, path: str):
        rel = path.lstrip("/")
        full = self.server.store.image_path(rel)
        if full is None:
            self._text(404, "not found")
            return
        ctype = mimetypes.guess_type(full)[0] or "image/jpeg"
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    # ---------- 路由 ----------

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/ping":
            self._json(200, {"ok": True})
        elif path == "/api/messages":
            if self._require_auth():
                self._handle_messages()
        elif path == "/api/rooms":
            if self._require_auth():
                self._handle_rooms()
        elif path == "/api/stats":
            if self._require_auth():
                self._handle_stats()
        elif path == "/api/quotes":
            if self._require_auth():
                self._handle_quotes()
        elif path == "/api/feishu":
            if self._require_auth():
                self._handle_feishu_status()
        elif path == "/api/dingtalk":
            if self._require_auth():
                self._handle_dingtalk_status()
        elif path.startswith("/images/"):
            if self._require_auth():
                self._handle_image(path)
        else:
            self._handle_static(path)  # 页面本身不鉴权，数据接口统一 401

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        # 飞书事件 webhook：走飞书自己的验证（token/签名/加密），不套本服务鉴权
        feishu_bot = getattr(self.server, "feishu_bot", None)
        if feishu_bot is not None and path == feishu_bot.event_path:
            raw = self._read_body()
            code, text = feishu_bot.handle_webhook(self.headers, raw)
            self._text(code, text, ctype="application/json; charset=utf-8")
            return
        if not self._require_auth():
            return
        if path == "/api/clear":
            self._handle_clear()
        elif path == "/api/send":
            self._handle_send()
        else:
            self._handle_post()

    def _handle_send(self):
        """让服务端安排一条消息发送：{"talker":"xxx@chatroom","content":"..."}。
        指令入队，等模块下次上行/心跳的 ack 取走并调 SendMessage 发送。"""
        listener = getattr(self.server, "relay_listener", None)
        if listener is None:
            self._json(503, {"ok": False, "error": "relay socket disabled"})
            return
        raw = self._read_body()
        try:
            msg = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            self._json(400, {"ok": False, "error": "invalid json"})
            return
        if not isinstance(msg, dict):
            self._json(400, {"ok": False, "error": "invalid frame"})
            return
        talker = str(msg.get("talker") or "").strip()
        content = str(msg.get("content") or "")
        if not talker or not content:
            self._json(400, {"ok": False, "error": "missing talker/content"})
            return
        listener.send(talker, content)
        self._json(200, {"ok": True, "queued": True, "clients": listener.client_count()})

    def log_message(self, fmt, *args):
        pass


def main():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根
    ap = argparse.ArgumentParser(description="sync-server：模块长连接接收端 + 消息/报价查看服务")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--data-dir", default=os.path.join(base, "data"))
    ap.add_argument("--web-dir", default=os.path.join(base, "web"))
    ap.add_argument("--socket-port", type=int, default=8081,
                    help="模块长连接监听端口，0 关闭")
    ap.add_argument("--agent-config", default=os.path.join(base, "agent.config.json"),
                    help="报价 Agent 配置（缺失或未启用则跳过）")
    ap.add_argument("--feishu-config", default=os.path.join(base, "feishu.config.json"),
                    help="飞书机器人配置（缺失或未启用则跳过）")
    ap.add_argument("--dingtalk-config", default=os.path.join(base, "dingtalk.config.json"),
                    help="钉钉机器人配置（缺失或未启用则跳过）")
    ap.add_argument("--plugins-dir", default=os.path.join(base, "plugins"),
                    help="机器人插件目录")
    ap.add_argument("--token", default=os.environ.get("SYNC_TOKEN", ""),
                    help="鉴权 token（也可用环境变量 SYNC_TOKEN）；不传则无鉴权")
    args = ap.parse_args()

    os.makedirs(args.data_dir, exist_ok=True)
    store = Store(args.data_dir)
    quote_log = QuoteLog(os.path.join(args.data_dir, "quotes", "quotes.jsonl"))

    # Agent 桥（pi-agent SDK；agent.config.json 未启用时返回 None）
    agent_cfg = quote_agent.load_config(args.agent_config)
    bridge = pi_bridge.PiBridge.from_config(agent_cfg)
    if bridge is not None:
        print(f"[sync_server] agent bridge: {bridge.backend_kind}")

    # 插件主机（可扩展性落点）
    plugins = bot_plugins.PluginHost(args.plugins_dir)
    plugins.load()

    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    server.store = store
    server.web_dir = os.path.abspath(args.web_dir)
    server.token = args.token
    server.bridge = bridge
    server.quote_log = quote_log

    # 飞书机器人：报价推送 + 问答 + 长连接/Webhook 事件
    server.feishu_bot = feishu_mod.create_feishu_bot(
        args.feishu_config, store=store, quote_log=quote_log, bridge=bridge,
        plugin_host=plugins, data_dir=args.data_dir)
    on_quote = []
    if server.feishu_bot is not None:
        plugins.bind(store=store, quote_log=quote_log, bridge=bridge,
                     feishu_client=server.feishu_bot.client,
                     feishu_cfg=server.feishu_bot.cfg, bot=server.feishu_bot)
        server.feishu_bot.start_event_source()
        on_quote.append(server.feishu_bot.notify_quote)
    # 钉钉机器人：报价推送（自定义机器人 webhook）+ Stream 长连接问答
    server.dingtalk_bot = dingtalk_mod.create_dingtalk_bot(
        args.dingtalk_config, store=store, quote_log=quote_log, bridge=bridge,
        plugin_host=plugins, data_dir=args.data_dir)
    if server.dingtalk_bot is not None:
        server.dingtalk_bot.start()
        on_quote.append(server.dingtalk_bot.notify_quote)
    server.agent = quote_agent.create_agent(
        args.agent_config, args.data_dir,
        on_quote=on_quote or None, bridge=bridge)

    def _downlink(talker: str, content: str) -> bool:
        listener = getattr(server, "relay_listener", None)
        if listener is None:
            return False
        return listener.send(talker, content)

    server.wechat_replier = (
        wechat_reply.WechatReplier(bridge, _downlink) if bridge is not None else None)

    if args.socket_port > 0:
        server.relay_listener = RelayListener(
            args.bind, args.socket_port, store, server.agent, token=args.token,
            replier=server.wechat_replier)
        server.relay_listener.start()
    else:
        server.relay_listener = None

    print(f"[sync_server] http on http://{args.bind}:{args.port}  "
          f"socket={'off' if args.socket_port <= 0 else f'{args.bind}:{args.socket_port}'}  "
          f"auth={'token' if args.token else 'off'}  messages={len(store.messages)}  "
          f"feishu={'on' if server.feishu_bot else 'off'} "
          f"dingtalk={'on' if server.dingtalk_bot else 'off'} "
          f"wechat_reply={'on' if server.wechat_replier else 'off'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
