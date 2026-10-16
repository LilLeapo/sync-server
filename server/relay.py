"""模块长连接监听：NDJSON 协议（每行一个 JSON），hello 握手可选 token 鉴权。

帧格式：
  {"hello":"wabsync/1","token":"..."}  首帧必须是握手；服务器配置了 token 时校验，
                                      失败回 {"ok":false,"error":"unauthorized"} 并断开
  {"ping":true}                        心跳，回 {"ok":true}
  <ChatMessage JSON>                   消息，回 {"ok":true,"seq":N}
                                      （60s 内重复投递回 {"ok":true,"dup":true}）

上行帧的 ack 可顺带携带一条下行发送指令：{"ok":true,"send":{"talker":"...","content":"..."}}。
服务端通过 /api/send 入队，模块下次上行/心跳时取走；带 deviceId 的指令只发给该设备的连接。
每条指令有服务端内部 id（不下发给模块），被取走/因队列溢出丢弃时经 on_send_status 通知。

设备身份：握手帧可带 "deviceId" / "deviceName"（模块按「微信 wxid + ANDROID_ID」派生）。
绑定后，该连接上报的消息都会打上 deviceId，落库与去重按设备分开。
"""
import collections
import hmac
import json
import socket
import threading
import time
import uuid

from .store import Store

MAX_SOCKET_LINE = 34 * 1024 * 1024  # 帧上限（容纳 base64 原图）
HELLO_TIMEOUT_S = 5
IDLE_TIMEOUT_S = 120  # 模块 30s 心跳，两倍余量判死
MAX_DEVICE_OUTBOX = 200  # 单设备待发指令上限，模块长期离线时丢弃最旧的


class RelayListener:
    def __init__(self, host: str, port: int, store: Store, agent=None, token: str = "",
                 replier=None):
        self.host = host
        self.port = port
        self.store = store
        self.agent = agent
        self.replier = replier
        self.token = token or ""
        # 已连接模块：socket -> {device_id, device_name, remote, connectedAt, count}
        self._clients: dict[socket.socket, dict] = {}
        self._lock = threading.Lock()
        # 下行发送队列：服务端调用 send() 入队，每个已连接的模块在 ack 里取一条带走。
        # 无 deviceId 的进全局队列（谁先 ack 谁取），带 deviceId 的进该设备专属队列。
        self._outbox: "collections.deque[dict]" = collections.deque()
        self._device_outbox: dict[str, "collections.deque[dict]"] = {}
        self._dropped: dict[str, int] = {}  # deviceId -> 因队列溢出丢弃的条数
        self._outbox_lock = threading.Lock()
        # on_send_status(send_id, status, device_id)：status 为 delivered（被模块取走）/ dropped
        self.on_send_status = None

    def send(self, talker: str, content: str, device_id: str | None = None,
             image_path: str | None = None) -> str:
        """下发一条发送指令；入队成功返回指令 id，失败返回空串（后续由模块 ack 时取走）。

        image_path 非空时下发图片发送指令（模块走 SendImgProxyUI 相册链路），content 可为空。
        device_id 非空时只投给该设备（指令带 deviceId，模块端也会二次校验）。
        以 "_" 开头的字段是服务端内部记录，下发前去掉。
        """
        image_path = (image_path or "").strip()
        if not talker or (not content and not image_path):
            return ""
        send_id = uuid.uuid4().hex[:12]
        item = {"talker": talker, "content": content, "_id": send_id, "_at": int(time.time() * 1000)}
        if image_path:
            item["imagePath"] = image_path
        did = str(device_id or "").strip()
        dropped = []
        with self._outbox_lock:
            if did:
                item["deviceId"] = did
                q = self._device_outbox.setdefault(did, collections.deque())
                q.append(item)
                while len(q) > MAX_DEVICE_OUTBOX:
                    dropped.append(q.popleft())
                    self._dropped[did] = self._dropped.get(did, 0) + 1
            else:
                self._outbox.append(item)
        for old in dropped:
            self._notify(old, "dropped", did)
        return send_id

    def _next_send(self, device_id: str = "") -> dict | None:
        with self._outbox_lock:
            item = None
            if device_id:
                q = self._device_outbox.get(device_id)
                if q:
                    item = q.popleft()
            if item is None and self._outbox:
                item = self._outbox.popleft()
        if item is None:
            return None
        self._notify(item, "delivered", device_id)
        return {k: v for k, v in item.items() if not k.startswith("_")}

    def _notify(self, item: dict, status: str, device_id: str):
        hook = self.on_send_status
        if hook is None or not item.get("_id"):
            return
        try:
            hook(item["_id"], status, device_id)
        except Exception:
            pass

    def pending(self) -> dict:
        """待取走的下行指令：全局队列与每台设备的条数、最早入队时间、累计丢弃数。"""
        with self._outbox_lock:
            devices = {}
            for did in set(self._device_outbox) | set(self._dropped):
                q = self._device_outbox.get(did) or ()
                devices[did] = {"count": len(q), "oldestAt": q[0]["_at"] if q else 0,
                                "dropped": self._dropped.get(did, 0)}
            return {"global": {"count": len(self._outbox),
                               "oldestAt": self._outbox[0]["_at"] if self._outbox else 0},
                    "devices": devices}

    def start(self):
        threading.Thread(target=self._accept_loop, daemon=True, name="relay-socket").start()

    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def devices(self) -> list[dict]:
        """当前已连接设备（按 deviceId 合并同一设备的重复连接）。"""
        with self._lock:
            infos = list(self._clients.values())
        merged: dict[str, dict] = {}
        for info in infos:
            item = merged.setdefault(info.get("device_id") or "", {
                "deviceId": info.get("device_id") or "",
                "deviceName": info.get("device_name") or "",
                "remote": info.get("remote") or "",
                "connectedAt": info.get("connectedAt") or 0,
                "count": 0,
            })
            if not item["deviceName"] and info.get("device_name"):
                item["deviceName"] = info["device_name"]
            if info.get("connectedAt") and (not item["connectedAt"] or info["connectedAt"] < item["connectedAt"]):
                item["connectedAt"] = info["connectedAt"]
            item["count"] += int(info.get("count") or 0)
        return sorted(merged.values(), key=lambda e: e["connectedAt"])

    def _accept_loop(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(8)
        while True:
            conn, addr = srv.accept()
            threading.Thread(target=self._serve, args=(conn, addr), daemon=True).start()

    def _parse_hello(self, line: bytes) -> dict | None:
        """校验首帧握手；通过时返回连接信息（含设备标识），失败返回 None。"""
        try:
            msg = json.loads(line.decode("utf-8", "replace"))
        except Exception:
            return None
        if not isinstance(msg, dict) or "hello" not in msg:
            return None
        if self.token and not hmac.compare_digest(str(msg.get("token") or ""), self.token):
            return None
        return {
            "device_id": str(msg.get("deviceId") or "").strip(),
            "device_name": str(msg.get("deviceName") or "").strip(),
        }

    def _serve(self, conn: socket.socket, addr):
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            conn.settimeout(HELLO_TIMEOUT_S)
            rbuf = conn.makefile("rb")

            first = rbuf.readline(MAX_SOCKET_LINE)
            info = self._parse_hello(first.strip()) if (first and first.endswith(b"\n")) else None
            if info is None:
                try:
                    conn.sendall(b'{"ok":false,"error":"unauthorized"}\n')
                except OSError:
                    pass
                return
            conn.settimeout(IDLE_TIMEOUT_S)

            info["remote"] = f"{addr[0]}:{addr[1]}" if isinstance(addr, tuple) else str(addr)
            info["connectedAt"] = int(time.time() * 1000)
            info["count"] = 0
            if info["device_id"]:
                # 记住设备名，网页与 /api/devices 重启后仍能显示
                try:
                    self.store.remember_device(info["device_id"], info["device_name"])
                except Exception:
                    pass
            # 先登记再回 ack：客户端收到 ok 时设备已经在 /api/devices 里
            with self._lock:
                self._clients[conn] = info
            conn.sendall(b'{"ok":true}\n')
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
                    conn.sendall(self._handle_frame(line, info) + b"\n")
            finally:
                with self._lock:
                    self._clients.pop(conn, None)
        except (OSError, socket.timeout):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _handle_frame(self, line: bytes, conn: dict | None = None) -> bytes:
        device_id = str((conn or {}).get("device_id") or "")
        try:
            msg = json.loads(line.decode("utf-8", "replace"))
        except Exception:
            return b'{"ok":false,"error":"invalid json"}'
        if not isinstance(msg, dict):
            return b'{"ok":false,"error":"invalid frame"}'
        if "ping" in msg or "hello" in msg:
            return self._ack(downlink=True, device_id=device_id)
        if not msg.get("roomId"):
            return b'{"ok":false,"error":"missing roomId"}'
        if device_id:
            # 握手身份优先：同一进程内 payload 可能缺失或过期
            msg["deviceId"] = device_id
        rec = self.store.add_message(msg)
        if rec is None:
            return self._ack({"dup": True}, downlink=True, device_id=device_id)
        if conn is not None:
            conn["count"] = int(conn.get("count") or 0) + 1
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
        return self._ack({"seq": rec["seq"]}, downlink=True, device_id=device_id)

    def _ack(self, extra: dict | None = None, downlink: bool = False,
             device_id: str = "") -> bytes:
        """构造 ack；模块上行帧的 ack 可顺带携带一条本设备的下行发送指令。"""
        obj = {"ok": True}
        if extra:
            obj.update(extra)
        if downlink:
            send = self._next_send(device_id)
            if send is not None:
                obj["send"] = send
        # Compact separators so naive clients matching the literal "ok":true still work.
        return json.dumps(obj, separators=(",", ":")).encode()
