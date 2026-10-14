"""按设备分开收取：设备识别、分别落库/去重、设备过滤与定向下行。

设备身份来自模块（ChatConfig.deviceId，按「微信 wxid + ANDROID_ID」派生）：
socket 握手帧带 deviceId/deviceName，HTTP 兜底上报带 payload.deviceId。
"""
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import sync_server  # noqa: E402
from server.quote_agent import QuoteAgent  # noqa: E402
from server.relay import RelayListener  # noqa: E402
from server.store import QuoteLog, Store  # noqa: E402
from server.wechat_reply import WechatReplier  # noqa: E402

DEV_A = "wxid_aaa-ANDROID_A"
DEV_B = "wxid_bbb-ANDROID_B"


def _msg(**over):
    base = {
        "roomId": "1@chatroom",
        "roomName": "报价群",
        "sender": "wxid_x",
        "senderName": "张三",
        "content": "无氧杆 100",
        "type": 1,
        "createTime": 1700000000000,
        "msgSvrId": 100,
    }
    base.update(over)
    return base


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestPerDeviceStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_same_message_from_two_devices_is_kept_twice(self):
        first = self.store.add_message(_msg(deviceId=DEV_A))
        second = self.store.add_message(_msg(deviceId=DEV_B))
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(len(self.store.messages), 2)
        self.assertEqual({r["msg"]["deviceId"] for r in self.store.messages}, {DEV_A, DEV_B})

    def test_same_device_redelivery_is_deduped(self):
        self.assertIsNotNone(self.store.add_message(_msg(deviceId=DEV_A)))
        self.assertIsNone(self.store.add_message(_msg(deviceId=DEV_A)))
        self.assertEqual(len(self.store.messages), 1)

    def test_legacy_messages_without_device_share_one_bucket(self):
        self.assertIsNotNone(self.store.add_message(_msg()))
        self.assertIsNone(self.store.add_message(_msg()))  # 旧数据仍按 (svr, kind, size) 去重
        devices = self.store.devices()
        self.assertEqual([d["deviceId"] for d in devices], [""])
        self.assertEqual(devices[0]["count"], 1)

    def test_devices_aggregates_counts_and_rooms(self):
        self.store.add_message(_msg(deviceId=DEV_A, msgSvrId=1))
        self.store.add_message(_msg(deviceId=DEV_A, msgSvrId=2, roomId="2@chatroom"))
        self.store.add_message(_msg(deviceId=DEV_B, msgSvrId=1))
        devices = {d["deviceId"]: d for d in self.store.devices()}
        self.assertEqual(devices[DEV_A]["count"], 2)
        self.assertEqual(devices[DEV_A]["rooms"], 2)
        self.assertEqual(devices[DEV_B]["count"], 1)

    def test_remember_device_name_persists_across_restart(self):
        self.store.remember_device(DEV_A, "小米 14")
        self.assertEqual(self.store.devices()[0]["deviceName"], "小米 14")
        # 空名字不清掉已有名字（HTTP 兜底只有 id）
        self.store.remember_device(DEV_A, "")
        self.assertEqual(self.store.devices()[0]["deviceName"], "小米 14")
        reopened = Store(self._tmp.name)
        self.assertEqual(reopened.devices()[0]["deviceName"], "小米 14")

    def test_rooms_filtered_by_device(self):
        self.store.add_message(_msg(deviceId=DEV_A, msgSvrId=1))
        self.store.add_message(_msg(deviceId=DEV_A, msgSvrId=2))
        self.store.add_message(_msg(deviceId=DEV_B, msgSvrId=1))
        self.assertEqual(self.store.rooms(DEV_B)[0]["count"], 1)
        self.assertEqual(self.store.rooms(DEV_A)[0]["count"], 2)
        self.assertEqual(self.store.rooms(None)[0]["count"], 3)

    def test_list_messages_and_stats_filtered_by_device(self):
        self.store.add_message(_msg(deviceId=DEV_A, msgSvrId=1))
        self.store.add_message(_msg(deviceId=DEV_B, msgSvrId=1))
        rows, total, start = self.store.list_messages(0, 100, DEV_A)
        self.assertEqual((total, start, len(rows)), (1, 0, 1))
        self.assertEqual(self.store.stats(DEV_B)["count"], 1)
        self.assertEqual(self.store.stats()["count"], 2)

    def test_clear_scoped_by_device(self):
        self.store.add_message(_msg(deviceId=DEV_A, msgSvrId=1))
        self.store.add_message(_msg(deviceId=DEV_B, msgSvrId=1))
        self.store.clear(None, DEV_B)
        self.assertEqual([r["msg"]["deviceId"] for r in self.store.messages], [DEV_A])
        self.store.clear("1@chatroom", DEV_A)
        self.assertEqual(self.store.messages, [])

    def test_clear_all_still_works(self):
        self.store.add_message(_msg(deviceId=DEV_A, msgSvrId=1))
        self.store.add_message(_msg(deviceId=DEV_B, msgSvrId=1))
        self.store.clear()
        self.assertEqual(self.store.messages, [])


class TestRelayDeviceBinding(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(self._tmp.name)
        self.listener = RelayListener("127.0.0.1", 0, self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def test_parse_hello_reads_device_fields(self):
        info = self.listener._parse_hello(
            json.dumps({"hello": "wabsync/1", "deviceId": DEV_A,
                        "deviceName": "小米 14"}).encode())
        self.assertEqual(info, {"device_id": DEV_A, "device_name": "小米 14"})

    def test_parse_hello_rejects_bad_token(self):
        listener = RelayListener("127.0.0.1", 0, self.store, token="secret")
        ok = listener._parse_hello(
            json.dumps({"hello": "wabsync/1", "token": "secret",
                        "deviceId": DEV_A}).encode())
        self.assertEqual(ok["device_id"], DEV_A)
        self.assertIsNone(listener._parse_hello(
            json.dumps({"hello": "wabsync/1", "token": "wrong"}).encode()))

    def test_handshake_identity_tags_messages(self):
        info = {"device_id": DEV_A, "device_name": "小米 14", "count": 0}
        raw = json.dumps(_msg()).encode()
        ack = json.loads(self.listener._handle_frame(raw, info))
        self.assertTrue(ack["ok"])
        self.assertEqual(self.store.messages[0]["msg"]["deviceId"], DEV_A)
        self.assertEqual(info["count"], 1)

    def test_connection_identity_overrides_payload(self):
        info = {"device_id": DEV_A, "count": 0}
        raw = json.dumps(_msg(deviceId=DEV_B)).encode()
        self.listener._handle_frame(raw, info)
        self.assertEqual(self.store.messages[0]["msg"]["deviceId"], DEV_A)

    def test_device_outbox_is_targeted(self):
        self.listener.send("1@chatroom", "for-a", DEV_A)
        self.listener.send("1@chatroom", "for-b", DEV_B)
        ack_a = json.loads(self.listener._handle_frame(b'{"ping":true}',
                                                       {"device_id": DEV_A, "count": 0}))
        self.assertEqual(ack_a["send"], {"talker": "1@chatroom", "content": "for-a",
                                         "deviceId": DEV_A})
        # A 取不到 B 的指令
        self.assertNotIn("send", json.loads(self.listener._handle_frame(
            b'{"ping":true}', {"device_id": DEV_A, "count": 0})))
        ack_b = json.loads(self.listener._handle_frame(b'{"ping":true}',
                                                       {"device_id": DEV_B, "count": 0}))
        self.assertEqual(ack_b["send"]["content"], "for-b")

    def test_broadcast_send_still_has_no_device_id(self):
        self.listener.send("1@chatroom", "any")
        ack = json.loads(self.listener._handle_frame(b'{"ping":true}'))
        self.assertEqual(ack["send"], {"talker": "1@chatroom", "content": "any"})

    def test_devices_reports_connected_device(self):
        self.listener._clients[object()] = {
            "device_id": DEV_A, "device_name": "小米 14", "remote": "1.2.3.4:5",
            "connectedAt": 10, "count": 3,
        }
        self.listener._clients[object()] = {
            "device_id": DEV_A, "device_name": "", "remote": "1.2.3.4:6",
            "connectedAt": 20, "count": 1,
        }
        devices = self.listener.devices()
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["deviceName"], "小米 14")
        self.assertEqual(devices[0]["count"], 4)
        self.assertEqual(devices[0]["connectedAt"], 10)


class TestRelaySocketHandshake(unittest.TestCase):
    """真 socket 走一遍握手：deviceId 绑定 + 消息落库 + 定向下行。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(self._tmp.name)
        self.listener = RelayListener("127.0.0.1", free_port(), self.store)
        self.listener.start()
        self.sock = socket.create_connection(("127.0.0.1", self.listener.port), timeout=5)
        self.rfile = self.sock.makefile("rb")

    def tearDown(self):
        try:
            self.sock.close()
        except OSError:
            pass
        self._tmp.cleanup()

    def _readline(self) -> dict:
        line = self.rfile.readline()
        self.assertTrue(line, "socket closed")
        return json.loads(line.decode())

    def _hello(self, **extra):
        frame = {"hello": "wabsync/1", "deviceId": DEV_A, "deviceName": "小米 14"}
        frame.update(extra)
        self.sock.sendall((json.dumps(frame) + "\n").encode())
        self.assertTrue(self._readline()["ok"])

    def test_handshake_binds_device_and_delivers_targeted_send(self):
        self._hello()
        self.assertEqual(self.listener.devices()[0]["deviceId"], DEV_A)
        self.assertEqual(self.store.devices()[0]["deviceName"], "小米 14")
        self.listener.send("1@chatroom", "hello-device", DEV_A)
        self.sock.sendall((json.dumps(_msg()) + "\n").encode())
        ack = self._readline()
        self.assertTrue(ack["ok"])
        self.assertEqual(ack["send"]["deviceId"], DEV_A)
        self.assertEqual(self.store.messages[0]["msg"]["deviceId"], DEV_A)


class TestWechatReplyDeviceTarget(unittest.TestCase):
    def test_reply_is_sent_to_triggering_device(self):
        sent = []

        class _Session:
            def ask(self, text, context=None):
                return "好的"

        class _Bridge:
            def begin_session(self, session_id, system="", tools=None):
                return _Session()

        replier = WechatReplier(
            _Bridge(), lambda t, c, d="": sent.append((t, c, d)) or True)
        out = replier.handle(_msg(atMe=True, deviceId=DEV_B, roomId="1@chatroom"))
        self.assertEqual(out, "好的")
        self.assertEqual(sent, [("1@chatroom", "好的", DEV_B)])


class TestQuoteRowDevice(unittest.TestCase):
    def test_quote_rows_carry_device_id(self):
        rec = {"seq": 1, "msg": _msg(deviceId=DEV_A)}
        rows = QuoteAgent._quote_rows(None, rec, [{"供应商": "某铜业", "价格": "100"}])
        self.assertEqual(rows[0]["deviceId"], DEV_A)
        # 模型输出不能覆盖设备归属
        rows = QuoteAgent._quote_rows(
            None, rec, [{"schemaVersion": 2, "供应商": "某铜业", "价格": "100",
                         "deviceId": "spoofed"}])
        self.assertEqual(rows[0]["deviceId"], DEV_A)

    def test_quote_log_filters_by_device(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "quotes.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps({"qseq": 1, "roomId": "1@chatroom", "deviceId": DEV_A}) + "\n")
                f.write(json.dumps({"qseq": 2, "roomId": "1@chatroom", "deviceId": DEV_B}) + "\n")
                f.write(json.dumps({"qseq": 3, "roomId": "1@chatroom"}) + "\n")
            log = QuoteLog(path)
            rows, _ = log.list(0, 10, None, DEV_A)
            self.assertEqual([r["qseq"] for r in rows], [1])
            rows, _ = log.list(0, 10, None, "")
            self.assertEqual([r["qseq"] for r in rows], [3])
            rows, _ = log.list(0, 10, None, None)
            self.assertEqual([r["qseq"] for r in rows], [1, 2, 3])


class TestDeviceHttpApi(unittest.TestCase):
    """真实 HTTP 路由：/api/devices、?device= 过滤、定向 /api/send。"""

    _next_svr = 10000

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        d = cls._tmp.name
        cls.store = Store(d)
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), sync_server.Handler)
        cls.srv.store = cls.store
        cls.srv.token = ""
        cls.srv.web_dir = d
        cls.srv.quote_log = QuoteLog(os.path.join(d, "quotes", "quotes.jsonl"))
        cls.srv.relay_listener = RelayListener("127.0.0.1", 0, cls.store)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls._tmp.cleanup()

    def setUp(self):
        # 每个用例各自播一条（同一条消息由两台设备各报一次）
        TestDeviceHttpApi._next_svr += 1
        self.svr_id = TestDeviceHttpApi._next_svr
        self._post("/", _msg(deviceId=DEV_A, msgSvrId=self.svr_id))
        self._post("/", _msg(deviceId=DEV_B, msgSvrId=self.svr_id))

    def _get(self, path: str) -> dict:
        with urllib.request.urlopen(self.base + path, timeout=5) as resp:
            return json.loads(resp.read())

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())

    def test_http_fallback_payload_keeps_device(self):
        data = self._get("/api/messages?after=0&limit=500")
        rows = [r for r in data["messages"] if r["msg"].get("msgSvrId") == self.svr_id]
        self.assertEqual(sorted(r["msg"]["deviceId"] for r in rows), sorted([DEV_A, DEV_B]))

    def test_messages_and_rooms_filtered_by_device(self):
        rows = self._get("/api/messages?after=0&limit=500&device=" + DEV_A)["messages"]
        self.assertTrue(rows)
        self.assertTrue(all(r["msg"].get("deviceId") == DEV_A for r in rows))
        rooms_b = self._get("/api/rooms?device=" + DEV_B)["rooms"]
        rooms_all = {r["roomId"]: r for r in self._get("/api/rooms")["rooms"]}
        self.assertTrue(all(r["roomId"] == "1@chatroom" for r in rooms_b))
        self.assertGreater(rooms_all["1@chatroom"]["count"], rooms_b[0]["count"])

    def test_devices_endpoint_lists_both_devices(self):
        data = self._get("/api/devices")
        ids = {d["deviceId"] for d in data["devices"]}
        self.assertIn(DEV_A, ids)
        self.assertIn(DEV_B, ids)
        self.assertIn("online", data)

    def test_send_can_target_device(self):
        out = self._post("/api/send", {"talker": "1@chatroom", "content": "hi", "device": DEV_A})
        self.assertTrue(out["queued"])
        self.assertEqual(out["device"], DEV_A)
        queued = self.srv.relay_listener._device_outbox[DEV_A].popleft()
        self.assertEqual(queued["deviceId"], DEV_A)

    def test_clear_scoped_by_device(self):
        left_b = len([r for r in self.store.messages if r["msg"].get("deviceId") == DEV_B])
        self._post("/api/clear?device=" + DEV_A, {})
        self.assertFalse([r for r in self.store.messages if r["msg"].get("deviceId") == DEV_A])
        self.assertEqual(
            len([r for r in self.store.messages if r["msg"].get("deviceId") == DEV_B]), left_b)


if __name__ == "__main__":
    unittest.main()
