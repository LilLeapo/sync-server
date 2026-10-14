"""每台设备的 Agent 设置（回复/报价开关、提示词、工具）、回复记录与待发队列状态。

设备的 Agent 不一定做报价：关掉报价的设备不提取报价，提示词与可用工具按设备单独配置。
"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import sync_server  # noqa: E402
from server.device_settings import DeviceSettings  # noqa: E402
from server.qa_tools import tool_catalog  # noqa: E402
from server.quote_agent import QuoteAgent, backfill  # noqa: E402
from server.relay import MAX_DEVICE_OUTBOX, RelayListener  # noqa: E402
from server.store import QuoteLog, Store  # noqa: E402
from server.wechat_reply import ReplyLog, WechatReplier  # noqa: E402
from tests.test_quote_rules import NOW, quote  # noqa: E402

DEV_A = "wxid_aaa-ANDROID_A"
DEV_B = "wxid_bbb-ANDROID_B"
TOOL_NAMES = {t["name"] for t in tool_catalog()}


def mention(device, svr=1, room="1@chatroom", **over):
    msg = {"roomId": room, "roomName": "测试群", "sender": "wxid_x", "senderName": "张三",
           "content": "在吗", "type": 1, "msgSvrId": svr, "atMe": True, "deviceId": device}
    msg.update(over)
    return msg


class _Bridge:
    def __init__(self, reply="好的"):
        self.reply = reply
        self.sessions = []  # (session_id, system, tool_names)

    def begin_session(self, session_id, system="", tools=None):
        self.sessions.append((session_id, system, [t["name"] for t in tools or []]))
        bridge = self

        class _Session:
            def ask(self, text, context=None):
                return bridge.reply
        return _Session()


class TestDeviceSettings(unittest.TestCase):
    def test_defaults_are_all_on(self):
        cfg = DeviceSettings().get(DEV_A)
        self.assertEqual(cfg, {"reply": True, "quotes": True, "prompt": "", "tools": None})

    def test_update_persists_and_only_touches_given_fields(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "device_settings.json")
            settings = DeviceSettings(path)
            settings.update(DEV_A, {"quotes": False, "prompt": "  你是客服  "})
            settings.update(DEV_A, {"tools": ["room_stats", "room_stats"]}, TOOL_NAMES)
            reloaded = DeviceSettings(path)
            self.assertEqual(reloaded.get(DEV_A), {"reply": True, "quotes": False,
                                                    "prompt": "你是客服", "tools": ["room_stats"]})
            self.assertEqual(reloaded.get(DEV_B)["quotes"], True)
            self.assertEqual(set(reloaded.all()), {DEV_A})

    def test_invalid_patch_is_rejected_without_partial_write(self):
        settings = DeviceSettings()
        for patch_ in ({"reply": "yes"}, {"tools": "room_stats"}, {"color": 1},
                       {"prompt": "x" * 5000}, {"quotes": False, "tools": ["rm_rf"]}):
            with self.assertRaises(ValueError):
                settings.update(DEV_A, patch_, TOOL_NAMES)
        self.assertEqual(settings.all(), {})


class TestReplierDeviceSettings(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = DeviceSettings()
        self.bridge = _Bridge()
        self.sent = []
        quote_log = QuoteLog(os.path.join(self._tmp.name, "quotes", "quotes.jsonl"))
        self.replier = WechatReplier(
            self.bridge, lambda t, c, d="": self.sent.append(d) or f"send-{len(self.sent)}",
            store=Store(self._tmp.name), quote_log=quote_log, settings=self.settings)

    def tearDown(self):
        self._tmp.cleanup()

    def test_device_reply_switch_only_affects_that_device(self):
        self.settings.update(DEV_A, {"reply": False})
        self.assertIsNone(self.replier.handle(mention(DEV_A)))
        self.assertEqual(self.replier.handle(mention(DEV_B)), "好的")
        self.assertEqual(self.sent, [DEV_B])
        skipped = [r for r in self.replier.log.list()[0] if r["status"] == "skipped"]
        self.assertEqual([r["deviceId"] for r in skipped], [DEV_A])
        # 重新打开后同一条消息仍可回复（关闭时不占用去重）
        self.settings.update(DEV_A, {"reply": True})
        self.assertEqual(self.replier.handle(mention(DEV_A)), "好的")

    def test_global_switch_off_is_logged(self):
        self.replier.set_enabled(False)
        self.assertIsNone(self.replier.handle(mention(DEV_A)))
        row = self.replier.log.list()[0][0]
        self.assertEqual((row["status"], row["reason"]), ("skipped", "自动回复总开关已关闭"))

    def test_prompt_and_tools_follow_device_settings(self):
        self.settings.update(DEV_A, {"prompt": "你是售后客服，只回答物流问题", "tools": ["room_stats"]},
                             TOOL_NAMES)
        self.replier.handle(mention(DEV_A, svr=1))
        self.replier.handle(mention(DEV_B, svr=2))
        (_, sys_a, tools_a), (_, sys_b, tools_b) = self.bridge.sessions
        self.assertIn("你是售后客服", sys_a)
        self.assertEqual(tools_a, ["room_stats"])
        self.assertNotIn("售后客服", sys_b)
        self.assertIn("compare_quotes", tools_b)  # 没改过设置的设备默认全部工具

    def test_changing_settings_restarts_session(self):
        self.replier.handle(mention(DEV_A, svr=1))
        self.replier.handle(mention(DEV_A, svr=2))
        self.assertEqual(len(self.bridge.sessions), 1)
        self.settings.update(DEV_A, {"tools": []}, TOOL_NAMES)
        self.replier.handle(mention(DEV_A, svr=3))
        self.assertEqual(len(self.bridge.sessions), 2)
        self.assertEqual(self.bridge.sessions[1][2], [])

    def test_failures_are_logged_with_reason(self):
        self.bridge.reply = "  "
        self.assertIsNone(self.replier.handle(mention(DEV_A, svr=1)))
        self.bridge.reply = "好的"
        replier = WechatReplier(self.bridge, lambda t, c, d="": "", settings=self.settings)
        self.assertIsNone(replier.handle(mention(DEV_A, svr=2)))
        model_fail = self.replier.log.list()[0][0]
        send_fail = replier.log.list()[0][0]
        self.assertEqual(model_fail["status"], "failed")
        self.assertIn("模型没有给出回复", model_fail["reason"])
        self.assertEqual((send_fail["status"], send_fail["reply"]), ("failed", "好的"))


class TestReplyLog(unittest.TestCase):
    def test_delivery_receipt_updates_status_and_rev(self):
        log = ReplyLog()
        rid = log.add(mention(DEV_A), "answering")
        log.queued(rid, "好的", "s1")
        _, rev = log.list()
        log.mark_send("s1", "delivered", DEV_A)
        changed, _ = log.list(rev)
        self.assertEqual([(r["id"], r["status"], r["deliveredTo"]) for r in changed], [(rid, "delivered", DEV_A)])

    def test_receipt_before_registration_is_applied(self):
        log = ReplyLog()
        rid = log.add(mention(DEV_A), "answering")
        log.mark_send("s1", "delivered", DEV_A)  # 手机比登记还快
        log.queued(rid, "好的", "s1")
        self.assertEqual(log.list()[0][0]["status"], "delivered")

    def test_dropped_and_device_filter(self):
        log = ReplyLog()
        log.queued(log.add(mention(DEV_A), "answering"), "a", "s1")
        log.queued(log.add(mention(DEV_B), "answering"), "b", "s2")
        log.mark_send("s2", "dropped")
        rows, _ = log.list(device=DEV_B)
        self.assertEqual([r["status"] for r in rows], ["dropped"])

    def test_restart_marks_unconfirmed_as_lost(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "replies.json")
            log = ReplyLog(path)
            log.queued(log.add(mention(DEV_A, svr=1), "answering"), "a", "s1")
            done = log.add(mention(DEV_A, svr=2), "answering")
            log.queued(done, "b", "s2")
            log.mark_send("s2", "delivered", DEV_A)
            _, rev = log.list()
            reloaded = ReplyLog(path)
            self.assertEqual([r["status"] for r in reloaded.list()[0]], ["lost", "delivered"])
            self.assertEqual([r["status"] for r in reloaded.list(rev)[0]], ["lost"])
            self.assertGreater(reloaded.add(mention(DEV_A, svr=3), "skipped"), done)


class TestRelayPending(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.listener = RelayListener("127.0.0.1", 0, Store(self._tmp.name))
        self.events = []
        self.listener.on_send_status = lambda sid, status, dev: self.events.append((sid, status, dev))

    def tearDown(self):
        self._tmp.cleanup()

    def test_pending_counts_and_delivery_receipt(self):
        sid = self.listener.send("1@chatroom", "hi", DEV_A)
        self.listener.send("1@chatroom", "all")
        pending = self.listener.pending()
        self.assertEqual(pending["devices"][DEV_A]["count"], 1)
        self.assertGreater(pending["devices"][DEV_A]["oldestAt"], 0)
        self.assertEqual(pending["global"]["count"], 1)
        ack = json.loads(self.listener._handle_frame(b'{"ping":true}', {"device_id": DEV_A, "count": 0}))
        self.assertEqual(ack["send"], {"talker": "1@chatroom", "content": "hi", "deviceId": DEV_A})
        self.assertEqual(self.events, [(sid, "delivered", DEV_A)])
        self.assertEqual(self.listener.pending()["devices"][DEV_A]["count"], 0)

    def test_overflow_reports_dropped(self):
        first = self.listener.send("1@chatroom", "0", DEV_A)
        for i in range(MAX_DEVICE_OUTBOX):
            self.listener.send("1@chatroom", str(i + 1), DEV_A)
        self.assertEqual(self.events, [(first, "dropped", DEV_A)])
        info = self.listener.pending()["devices"][DEV_A]
        self.assertEqual((info["count"], info["dropped"]), (MAX_DEVICE_OUTBOX, 1))


class TestQuoteSwitch(unittest.TestCase):
    def test_device_with_quotes_off_is_not_extracted(self):
        settings = DeviceSettings()
        settings.update(DEV_B, {"quotes": False})
        agent = QuoteAgent.__new__(QuoteAgent)
        agent.settings = settings
        agent._processed = set()
        pushed = []
        agent._push = lambda rec, nb: pushed.append(rec["msg"]["deviceId"])
        for device in (DEV_A, DEV_B):
            agent.enqueue({"seq": 1, "msg": {"type": 1, "roomId": "1@chatroom", "deviceId": device}})
        self.assertEqual(pushed, [DEV_A])

    def test_backfill_skips_devices_with_quotes_off(self):
        with tempfile.TemporaryDirectory() as root:
            item = quote()
            with open(os.path.join(root, "messages.jsonl"), "w") as stream:
                for seq, device in ((1, DEV_A), (2, DEV_B)):
                    stream.write(json.dumps({"seq": seq, "receivedAt": NOW, "msg": {
                        "type": 1, "roomId": "s@chatroom", "createTime": NOW, "msgSvrId": "9",
                        "content": item["evidence"], "deviceId": device}}, ensure_ascii=False) + "\n")
            os.makedirs(os.path.join(root, "quotes"))
            DeviceSettings(os.path.join(root, "device_settings.json")).update(DEV_B, {"quotes": False})
            config = {"enabled": True, "base_url": "unused", "api_key": "unused", "model": "unused"}
            reply = json.dumps({"events": [item]}, ensure_ascii=False)
            with patch("server.quote_agent.load_config", return_value=config), \
                    patch.object(QuoteAgent, "_call_llm", return_value=reply):
                self.assertEqual(backfill("unused", root), 0)
            rows, _ = QuoteLog(os.path.join(root, "quotes", "quotes.jsonl")).list()
            self.assertEqual({r["deviceId"] for r in rows}, {DEV_A})


class TestDeviceSettingsHttpApi(unittest.TestCase):
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
        cls.srv.device_settings = DeviceSettings(os.path.join(d, "device_settings.json"))
        cls.srv.relay_listener = RelayListener("127.0.0.1", 0, cls.store)
        cls.srv.wechat_replier = WechatReplier(
            _Bridge(), lambda t, c, dev="": cls.srv.relay_listener.send(t, c, dev or None),
            settings=cls.srv.device_settings)
        cls.srv.relay_listener.on_send_status = cls.srv.wechat_replier.on_send_status
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls._tmp.cleanup()

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as resp:
            return json.loads(resp.read())

    def _post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_settings_roundtrip_and_validation(self):
        data = self._get("/api/device-settings")
        self.assertEqual(data["defaults"]["reply"], True)
        self.assertIn("compare_quotes", {t["name"] for t in data["tools"]})
        self.assertTrue(data["basePrompt"])
        code, out = self._post("/api/device-settings", {"deviceId": DEV_A, "quotes": False, "tools": ["room_stats"]})
        self.assertEqual((code, out["settings"]["quotes"], out["settings"]["tools"]), (200, False, ["room_stats"]))
        self.assertEqual(self._get("/api/device-settings")["devices"][DEV_A]["quotes"], False)
        self.assertEqual(self._post("/api/device-settings", {"deviceId": DEV_A, "tools": ["nope"]})[0], 400)
        self.assertEqual(self._post("/api/device-settings", {"quotes": False})[0], 400)

    def test_replies_and_pending_follow_delivery(self):
        self.store.add_message(mention(DEV_B, svr=77))  # 设备有消息，待发清空后仍在列表里
        self.srv.wechat_replier.handle(mention(DEV_B, svr=77))
        devices = {d["deviceId"]: d for d in self._get("/api/devices")["devices"]}
        self.assertEqual(devices[DEV_B]["pending"], 1)
        self.assertIn("reply", devices[DEV_B])
        first = self._get("/api/replies?device=" + DEV_B)
        self.assertEqual([r["status"] for r in first["replies"]], ["queued"])
        self.srv.relay_listener._handle_frame(b'{"ping":true}', {"device_id": DEV_B, "count": 0})
        changed = self._get("/api/replies?after=%d" % first["rev"])["replies"]
        self.assertEqual([r["status"] for r in changed], ["delivered"])
        self.assertEqual({d["deviceId"]: d for d in self._get("/api/devices")["devices"]}[DEV_B]["pending"], 0)

    def test_send_returns_id(self):
        _, out = self._post("/api/send", {"talker": "1@chatroom", "content": "hi", "device": DEV_A})
        self.assertTrue(out["sendId"])


if __name__ == "__main__":
    unittest.main()
