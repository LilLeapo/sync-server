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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

try:
    from .relay import RelayListener
    from .store import Store, QuoteLog
    from . import quote_agent
except ImportError:  # 允许 python3 server/sync_server.py 直接运行
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from server.relay import RelayListener
    from server.store import Store, QuoteLog
    from server import quote_agent


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
        self._json(200, {"ok": True, "seq": rec["seq"]})

    def _notify_agent(self, rec: dict):
        agent = getattr(self.server, "agent", None)
        if agent is not None:
            try:
                agent.enqueue(rec)
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
        slim = []
        for r in msgs:
            m = r.get("msg")
            if isinstance(m, dict) and m.get("imageBase64"):
                m = dict(m)
                m.pop("imageBase64", None)
                r = dict(r)
                r["msg"] = m
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
        self._json(200, {"ok": True, "count": count})

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
        elif path.startswith("/images/"):
            if self._require_auth():
                self._handle_image(path)
        else:
            self._handle_static(path)  # 页面本身不鉴权，数据接口统一 401

    def do_POST(self):
        if not self._require_auth():
            return
        path = self.path.split("?", 1)[0]
        if path == "/api/clear":
            self._handle_clear()
        else:
            self._handle_post()

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
    ap.add_argument("--token", default=os.environ.get("SYNC_TOKEN", ""),
                    help="鉴权 token（也可用环境变量 SYNC_TOKEN）；不传则无鉴权")
    args = ap.parse_args()

    os.makedirs(args.data_dir, exist_ok=True)
    store = Store(args.data_dir)
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    server.store = store
    server.web_dir = os.path.abspath(args.web_dir)
    server.token = args.token
    server.agent = quote_agent.create_agent(args.agent_config, args.data_dir)
    server.quote_log = QuoteLog(os.path.join(args.data_dir, "quotes", "quotes.jsonl"))

    if args.socket_port > 0:
        server.relay_listener = RelayListener(
            args.bind, args.socket_port, store, server.agent, token=args.token)
        server.relay_listener.start()
    else:
        server.relay_listener = None

    print(f"[sync_server] http on http://{args.bind}:{args.port}  "
          f"socket={'off' if args.socket_port <= 0 else f'{args.bind}:{args.socket_port}'}  "
          f"auth={'token' if args.token else 'off'}  messages={len(store.messages)}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
