"""模块长连接监听：NDJSON 协议（每行一个 JSON），hello 握手可选 token 鉴权。

帧格式：
  {"hello":"wabsync/1","token":"..."}  首帧必须是握手；服务器配置了 token 时校验，
                                      失败回 {"ok":false,"error":"unauthorized"} 并断开
  {"ping":true}                        心跳，回 {"ok":true}
  <ChatMessage JSON>                   消息，回 {"ok":true,"seq":N}
                                      （60s 内重复投递回 {"ok":true,"dup":true}）

上行帧的 ack 可顺带携带一条下行发送指令：{"ok":true,"send":{"talker":"...","content":"..."}}。
服务端通过 /api/send 入队，模块下次上行/心跳时取走。
"""
import collections
import hmac
import json
import socket
import threading

from .store import Store

MAX_SOCKET_LINE = 34 * 1024 * 1024  # 帧上限（容纳 base64 原图）
HELLO_TIMEOUT_S = 5
IDLE_TIMEOUT_S = 120  # 模块 30s 心跳，两倍余量判死


class RelayListener:
    def __init__(self, host: str, port: int, store: Store, agent=None, token: str = "",
                 replier=None):
        self.host = host
        self.port = port
        self.store = store
        self.agent = agent
        self.replier = replier
        self.token = token or ""
        self._clients: set[socket.socket] = set()
        self._lock = threading.Lock()
        # 下行发送队列：服务端调用 send() 入队，每个已连接的模块在 ack 里取一条带走。
        self._outbox: "collections.deque[dict]" = collections.deque()
        self._outbox_lock = threading.Lock()

    def send(self, talker: str, content: str) -> bool:
        """下发一条发送指令；返回是否成功入队（后续由模块 ack 时取走）。"""
        if not talker or not content:
            return False
        with self._outbox_lock:
            self._outbox.append({"talker": talker, "content": content})
        return True

    def _next_send(self) -> dict | None:
        with self._outbox_lock:
            if not self._outbox:
                return None
            return self._outbox.popleft()

    def start(self):
        threading.Thread(target=self._accept_loop, daemon=True, name="relay-socket").start()

    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def _accept_loop(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(8)
        while True:
            conn, addr = srv.accept()
            threading.Thread(target=self._serve, args=(conn, addr), daemon=True).start()

    def _check_hello(self, line: bytes) -> bool:
        try:
            msg = json.loads(line.decode("utf-8", "replace"))
        except Exception:
            return False
        if not isinstance(msg, dict) or "hello" not in msg:
            return False
        if self.token:
            return hmac.compare_digest(str(msg.get("token") or ""), self.token)
        return True

    def _serve(self, conn: socket.socket, addr):
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            conn.settimeout(HELLO_TIMEOUT_S)
            rbuf = conn.makefile("rb")

            first = rbuf.readline(MAX_SOCKET_LINE)
            if not first or not first.endswith(b"\n") or not self._check_hello(first.strip()):
                try:
                    conn.sendall(b'{"ok":false,"error":"unauthorized"}\n')
                except OSError:
                    pass
                return
            conn.sendall(b'{"ok":true}\n')
            conn.settimeout(IDLE_TIMEOUT_S)

            with self._lock:
                self._clients.add(conn)
            try:
                while True:
                    line = rbuf.readline(MAX_SOCKET_LINE)
                    if not line:
                        break
                    if not line.endswith(b"\n"):
                        break  # 超长帧，直接断开
                    line = line.strip()
                    if not line:
                        continue
                    conn.sendall(self._handle_frame(line) + b"\n")
            finally:
                with self._lock:
                    self._clients.discard(conn)
        except (OSError, socket.timeout):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _handle_frame(self, line: bytes) -> bytes:
        try:
            msg = json.loads(line.decode("utf-8", "replace"))
        except Exception:
            return b'{"ok":false,"error":"invalid json"}'
        if not isinstance(msg, dict):
            return b'{"ok":false,"error":"invalid frame"}'
        if "ping" in msg or "hello" in msg:
            return self._ack(downlink=True)
        if not msg.get("roomId"):
            return b'{"ok":false,"error":"missing roomId"}'
        rec = self.store.add_message(msg)
        if rec is None:
            return self._ack({"dup": True}, downlink=True)
        if self.agent is not None:
            try:
                self.agent.enqueue(rec)
            except Exception:
                pass
        if self.replier is not None:
            try:
                self.replier.enqueue(rec.get("msg") or msg)
            except Exception:
                pass
        return self._ack({"seq": rec["seq"]}, downlink=True)

    def _ack(self, extra: dict | None = None, downlink: bool = False) -> bytes:
        """构造 ack；模块上行帧的 ack 可顺带携带一条下行发送指令。"""
        obj = {"ok": True}
        if extra:
            obj.update(extra)
        if downlink:
            send = self._next_send()
            if send is not None:
                obj["send"] = send
        # Compact separators so naive clients matching the literal "ok":true still work.
        return json.dumps(obj, separators=(",", ":")).encode()
