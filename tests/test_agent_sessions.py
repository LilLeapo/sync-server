"""会话管理：SessionManager 取/建/过期/淘汰/落盘恢复，/reset 指令，群 @ 回复的会话行为与 HTTP 接口。"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import agent_sessions, sync_server  # noqa: E402
from server.agent_sessions import SessionManager  # noqa: E402
from server.bot_plugins import PluginHost  # noqa: E402
from server.pi_bridge import Conversation, PiBridge  # noqa: E402
from server.store import Store  # noqa: E402
from server.wechat_reply import RESET_REPLY, WechatReplier  # noqa: E402

DEV = "wxid_a-ANDROID_A"


class _Backend:
    """记录请求的假 HTTP 后端：每次回复「答N」。"""

    def __init__(self):
        self.requests = []

    def complete_response(self, system, messages, json_mode=False, tools=None, timeout=None):
        self.requests.append({"system": system, "messages": list(messages)})
        return {"content": f"答{len(self.requests)}"}

    def complete(self, system, messages, json_mode=False):
        return "摘要"


def bridge():
    b = PiBridge(_Backend(), "http", session_cfg={"persist": True})
    b.log = lambda *a: None
    return b


class TestSessionManager(unittest.TestCase):
    def test_same_key_is_created_once_under_concurrency(self):
        mgr = SessionManager("t-once", ttl=60)
        created = []

        def factory():
            time.sleep(0.05)
            created.append(1)
            return object()

        out = []
        threads = [threading.Thread(target=lambda: out.append(mgr.get("k", factory))) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(created), 1)
        self.assertEqual(len({id(x) for x in out}), 1)

    def test_slow_creation_does_not_block_other_keys(self):
        mgr = SessionManager("t-block", ttl=60)
        gate = threading.Event()

        def slow():
            gate.wait(2)
            return "slow"

        t = threading.Thread(target=lambda: mgr.get("slow", slow))
        t.start()
        time.sleep(0.05)
        started = time.monotonic()
        self.assertEqual(mgr.get("fast", lambda: "fast"), "fast")
        self.assertLess(time.monotonic() - started, 0.5)
        gate.set()
        t.join()

    def test_ttl_lru_sig_and_broken(self):
        mgr = SessionManager("t-life", ttl=60, max_sessions=2)
        a = mgr.get("a", lambda: object())
        self.assertIs(mgr.get("a", lambda: object()), a)
        self.assertIsNot(mgr.get("a", lambda: object(), sig="new prompt"), a)  # sig 变化重建
        mgr._entries["a"].last_used -= 120
        self.assertEqual(mgr.sweep(), 1)
        mgr.get("x", lambda: 1)
        mgr.get("y", lambda: 2)
        mgr.get("z", lambda: 3)  # 超过上限淘汰最久未用的 x
        self.assertEqual(list(mgr._entries), ["y", "z"])

        class Broken:
            broken = True
        b = mgr.get("b", Broken)
        self.assertIsNot(mgr.get("b", Broken), b)

    def test_conversation_persists_and_restores_after_restart(self):
        with tempfile.TemporaryDirectory() as d:
            br = bridge()
            mgr = SessionManager("t-disk", ttl=600, persist_dir=d)
            conv = mgr.get("chat1", lambda: br.begin_session("s-chat1", "SYS", []))
            self.assertIsInstance(conv, Conversation)
            self.assertEqual(conv.ask("你好"), "答1")
            files = [f for f in os.listdir(d) if f.startswith("t-disk-")]
            self.assertEqual(len(files), 1)

            # 「重启」：新的 manager 与 bridge，同 key 首次访问时恢复历史
            br2 = bridge()
            mgr2 = SessionManager("t-disk", ttl=600, persist_dir=d)
            conv2 = mgr2.get("chat1", lambda: br2.begin_session("s-chat1", "SYS", []))
            self.assertEqual(conv2.history, [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "答1"}])
            conv2.ask("再问")
            self.assertIn({"role": "user", "content": "你好"}, br2.backend.requests[0]["messages"])

            # system prompt 变了不恢复；reset 删除文件
            mgr3 = SessionManager("t-disk", ttl=600, persist_dir=d)
            self.assertEqual(mgr3.get("chat1", lambda: bridge().begin_session("s", "新提示词", [])).history, [])
            self.assertTrue(mgr3.reset("chat1"))
            self.assertEqual([f for f in os.listdir(d) if f.startswith("t-disk-")], [])

    def test_expired_files_are_cleaned_on_start(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t-old-abc.json")
            with open(path, "w") as f:
                json.dump({"v": 1, "namespace": "t-old", "key": "k", "lastUsed": time.time() - 999}, f)
            SessionManager("t-old", ttl=60, persist_dir=d)
            self.assertFalse(os.path.exists(path))

    def test_module_level_stats_and_reset(self):
        mgr = SessionManager("t-api", ttl=60)
        conv = mgr.get("k1", lambda: bridge().begin_session("sid", "SYS", []))
        conv.ask("hi")
        rows = [r for r in agent_sessions.all_stats() if r["namespace"] == "t-api"]
        self.assertEqual((rows[0]["key"], rows[0]["turns"], rows[0]["sessionId"]), ("k1", 1, "sid"))
        self.assertTrue(agent_sessions.reset("t-api", "k1"))
        self.assertEqual(mgr.stats(), [])


class TestResetCommand(unittest.TestCase):
    def test_builtin_reset_uses_api_session_key(self):
        host = PluginHost("/nonexistent")
        self.assertIn("reset", host.commands)
        self.assertIn("/new", host.help_text())
        mgr = SessionManager("t-cmd", ttl=60)
        first = mgr.get("chat", lambda: object())
        api = host.spawn_api()
        api.sessions, api.session_key = mgr, "chat"
        self.assertIn("新对话", host.run_command("reset", "", api))
        self.assertIsNot(mgr.get("chat", lambda: object()), first)
        self.assertIn("不支持", host.run_command("new", "", host.spawn_api()))


class _RecordingBridge:
    def __init__(self):
        self.sessions = []

    def begin_session(self, session_id, system="", tools=None):
        bridge = self

        class _S:
            def __init__(self):
                self.contexts = []
                self.tools = {t["name"]: t for t in tools or []}
                self.last_turn = {"ok": True, "tools": ["list_rooms"], "usage": {"total": 9}, "error": ""}

            def ask(self, text, context=None):
                self.contexts.append(context)
                return "好的"
        sess = _S()
        bridge.sessions.append(sess)
        return sess


def mention(svr, content="@机器人 价格多少", at=2000, **over):
    msg = {"roomId": "1@chatroom", "roomName": "铜群", "sender": "wxid_x", "senderName": "张三",
           "content": content, "type": 1, "msgSvrId": svr, "createTime": at, "atMe": True, "deviceId": DEV}
    msg.update(over)
    return msg


class TestWechatReplierSessions(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(self._tmp.name)
        self.bridge = _RecordingBridge()
        self.sent = []
        self.replier = WechatReplier(self.bridge, lambda t, c, d="", **kw: self.sent.append((t, c, d)) or "sid",
                                     store=self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def test_room_context_is_recent_messages_before_the_mention(self):
        for i, text in enumerate(["早上好", "沪铜涨了", "无氧杆多少钱"]):
            self.store.add_message({"roomId": "1@chatroom", "sender": "wxid_y", "senderName": "李四", "content": text,
                                    "type": 1, "msgSvrId": 10 + i, "createTime": 1000 + i, "deviceId": DEV})
        self.store.add_message({**mention(99), "atMe": True})  # @ 本身已入库
        self.store.add_message({"roomId": "1@chatroom", "sender": "wxid_z", "content": "之后的消息", "type": 1,
                                "msgSvrId": 50, "createTime": 9000, "deviceId": DEV})
        self.store.add_message({"roomId": "1@chatroom", "sender": "wxid_z", "content": "别的设备", "type": 1,
                                "msgSvrId": 51, "createTime": 1500, "deviceId": "other"})
        self.assertEqual(self.replier.handle(mention(99)), "好的")
        ctx = self.bridge.sessions[0].contexts[0]
        self.assertIn("沪铜涨了", ctx)
        self.assertLess(ctx.index("早上好"), ctx.index("无氧杆多少钱"))
        for absent in ("价格多少", "之后的消息", "别的设备"):
            self.assertNotIn(absent, ctx)
        entry = self.replier.log.list()[0][-1]
        self.assertEqual((entry["tools"], entry["usage"]), (["list_rooms"], {"total": 9}))

    def test_reset_word_clears_device_room_session(self):
        self.replier.handle(mention(1))
        self.assertEqual(len(self.bridge.sessions), 1)
        self.assertEqual(self.replier.handle(mention(2, content="@机器人 /reset")), RESET_REPLY)
        self.assertEqual(self.sent[-1], ("1@chatroom", RESET_REPLY, DEV))
        self.replier.handle(mention(3))
        self.assertEqual(len(self.bridge.sessions), 2)

    def test_default_tools_exclude_opt_in_send_tools(self):
        self.replier.handle(mention(1))
        names = set(self.bridge.sessions[0].tools)
        self.assertIn("recent_messages", names)
        self.assertFalse(names & {"send_message", "send_image"})


class TestSessionsHttpApi(unittest.TestCase):
    def test_list_and_reset(self):
        with tempfile.TemporaryDirectory() as d:
            srv = ThreadingHTTPServer(("127.0.0.1", 0), sync_server.Handler)
            srv.store, srv.token, srv.web_dir = Store(d), "", d
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            base = "http://127.0.0.1:%d" % srv.server_address[1]
            try:
                mgr = SessionManager("t-http", ttl=60)
                mgr.get("chat9", lambda: bridge().begin_session("sid9", "SYS", [])).ask("hi")
                with urllib.request.urlopen(base + "/api/agent/sessions", timeout=5) as resp:
                    rows = [r for r in json.loads(resp.read())["sessions"] if r["namespace"] == "t-http"]
                self.assertEqual(rows[0]["key"], "chat9")
                req = urllib.request.Request(base + "/api/agent/sessions/reset",
                                             data=json.dumps({"namespace": "t-http", "key": "chat9"}).encode())
                with urllib.request.urlopen(req, timeout=5) as resp:
                    self.assertTrue(json.loads(resp.read())["reset"])
                self.assertEqual(mgr.stats(), [])
            finally:
                srv.shutdown()
                srv.server_close()


if __name__ == "__main__":
    unittest.main()
