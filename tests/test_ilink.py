"""微信 iLink 私聊通道离线测试：单账号工作线程 + 多账号管理器 + 网页扫码登录。

全部离线：用 FakeMsg / FakeSdkBot / FakeSdk 替代真实 SDK，不扫码、不触网。
"""
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

from server.bot_plugins import PluginHost  # noqa: E402
from server.ilink import (  # noqa: E402
    ILinkBot,
    ILinkError,
    ILinkManager,
    _TypingIndicator,
    load_ilink_config,
)
from server.sync_server import Handler  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PLUGIN_SRC = '''
def register(api):
    def ping(api, event, args):
        return "pong:" + (args or "")
    api.register_command("ping", ping, usage="/ping <文本>")
    def on_text(api, event, text):
        if text.strip() == "你好":
            return "hi"
        return None
    api.register_message_hook(on_text)
'''


class FakeMsg:
    def __init__(self, text, *, user="u1", mid=1, ctx="ctx-1"):
        self._text = text
        self.from_user = user
        self.message_id = mid
        self.context_token = ctx
        self.replies = []
        self.typing = 0

    @property
    def text(self):
        return self._text

    def reply_text(self, t):
        self.replies.append(("text", t))

    def reply_markdown(self, t):
        self.replies.append(("md", t))

    def reply_typing(self):
        self.typing += 1


class FakeSdkBot:
    """一个（假）iLink 账号连接。messages() 阻塞直到 stop()。"""

    def __init__(self, account_id="acct-1"):
        self.account_id = account_id
        self.sent = []
        self.stopped = False

    def messages(self):
        while not self.stopped:
            time.sleep(0.02)
        return
        yield  # 使其成为生成器

    def send_markdown(self, to, md, context_token=None):
        self.sent.append((to, md, context_token))

    def stop(self):
        self.stopped = True


class FakeStore:
    def __init__(self):
        self.records = []

    def add_message(self, msg):
        self.records.append(msg)
        return {"seq": len(self.records), "msg": msg}

    def stats(self):
        return {"count": len(self.records), "images": 0}

    def rooms(self):
        return []


def make_worker(tmp, *, store=None, plugin_host=None, bridge=None, connection=True,
                bot_factory=None, **cfg):
    base = {"enabled": True, "credentials_file": os.path.join(tmp, "creds.json"),
            "push": {"enabled": False}, "qna": {"enabled": False}}
    base.update(cfg)
    bot = ILinkBot(base, store=store, plugin_host=plugin_host, bridge=bridge,
                   data_dir=tmp, bot_factory=bot_factory)
    if connection:
        bot._bot = FakeSdkBot()
        bot.connected = True
    return bot


def write_creds(path, account_id, user_id="u"):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"botToken": "t-" + account_id, "accountId": account_id,
                   "baseUrl": "https://example.invalid", "userId": user_id}, f)


def make_manager(tmp, *, accounts_dir, bot_factory, **cfg):
    base = {"enabled": True, "accounts_dir": accounts_dir,
            "credentials_file": os.path.join(tmp, "legacy.json"),
            "auto_login": False, "push": {"enabled": False}, "qna": {"enabled": False}}
    base.update(cfg)
    return ILinkManager(base, data_dir=tmp, bot_factory=bot_factory)


class TestConfig(unittest.TestCase):
    def test_disabled_returns_none(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"enabled": False}, f)
            path = f.name
        try:
            self.assertIsNone(load_ilink_config(path))
        finally:
            os.unlink(path)

    def test_missing_creds_raises(self):
        tmp = tempfile.mkdtemp()
        bot = make_worker(tmp, connection=False)
        with self.assertRaises(ILinkError):
            bot._default_bot_factory()


class TestDispatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        pdir = os.path.join(self.tmp, "plugins")
        os.makedirs(pdir)
        with open(os.path.join(pdir, "t.py"), "w", encoding="utf-8") as f:
            f.write(PLUGIN_SRC)
        self.plugins = PluginHost(pdir)
        self.plugins.load()

    def _bot(self, **cfg):
        return make_worker(self.tmp, plugin_host=self.plugins, **cfg)

    def test_command_reply(self):
        bot = self._bot()
        msg = FakeMsg("/ping hello")
        bot._on_message(msg)
        self.assertEqual(msg.replies[-1], ("md", "pong:hello"))

    def test_message_hook(self):
        bot = self._bot()
        msg = FakeMsg("你好")
        bot._on_message(msg)
        self.assertEqual(msg.replies[-1], ("md", "hi"))

    def test_unknown_command(self):
        bot = self._bot()
        msg = FakeMsg("/nope")
        bot._on_message(msg)
        self.assertIn("未知指令", msg.replies[-1][1])

    def test_qna_without_bridge(self):
        bot = self._bot(qna={"enabled": True})
        msg = FakeMsg("今天有什么报价")
        bot._on_message(msg)
        self.assertIn("未启用", msg.replies[-1][1])

    def test_qna_with_mock_pi_agent(self):
        """非命令文本走 Pi Agent 问答（pi-agent 内置 mock provider）。"""
        try:
            import pi_agent  # noqa: F401
        except ImportError:
            self.skipTest("无 pi-agent SDK")
        from server.pi_bridge import PiBridge
        bridge = PiBridge.from_config({"enabled": True, "provider": "mock", "model": "m"})
        self.assertIsNotNone(bridge)
        bot = make_worker(self.tmp, plugin_host=self.plugins, connection=False,
                          bridge=bridge, qna={"enabled": True})
        msg = FakeMsg("苹果多少钱")
        bot._on_message(msg)
        deadline = time.time() + 20
        while time.time() < deadline and not msg.replies:
            time.sleep(0.05)
        self.assertTrue(msg.replies, "Agent 未回复")
        self.assertIn("Echo:", msg.replies[0][1])
        self.assertGreaterEqual(msg.typing, 1, "未发送「正在输入」状态")

    def test_qna_typing_can_be_disabled(self):
        try:
            import pi_agent  # noqa: F401
        except ImportError:
            self.skipTest("无 pi-agent SDK")
        from server.pi_bridge import PiBridge
        bridge = PiBridge.from_config({"enabled": True, "provider": "mock", "model": "m"})
        bot = make_worker(self.tmp, plugin_host=self.plugins, connection=False,
                          bridge=bridge, qna={"enabled": True, "typing": False})
        msg = FakeMsg("苹果多少钱")
        bot._on_message(msg)
        deadline = time.time() + 20
        while time.time() < deadline and not msg.replies:
            time.sleep(0.05)
        self.assertTrue(msg.replies)
        self.assertEqual(msg.typing, 0)

    def test_allowlist_blocks(self):
        bot = self._bot(allowed_users=["someone_else"])
        msg = FakeMsg("/ping x")
        bot._on_message(msg)
        self.assertEqual(msg.replies, [])

    def test_non_text_hint(self):
        bot = self._bot()
        msg = FakeMsg(None)
        bot._on_message(msg)
        self.assertIn("文字", msg.replies[-1][1])


class TestStoreInbound(unittest.TestCase):
    def test_message_stored_without_quote_agent(self):
        tmp = tempfile.mkdtemp()
        store = FakeStore()
        bot = make_worker(tmp, store=store)
        bot._on_message(FakeMsg("无氧杆 100", user="u9", mid=123))
        self.assertEqual(len(store.records), 1)
        rec = store.records[0]
        self.assertEqual(rec["roomId"], "ilink:u9")
        self.assertEqual(rec["content"], "无氧杆 100")
        self.assertEqual(rec["type"], 1)
        self.assertEqual(rec["msgSvrId"], "ilink-123")
        self.assertIn("iLink 私聊", rec["roomName"])

    def test_store_disabled(self):
        tmp = tempfile.mkdtemp()
        store = FakeStore()
        bot = make_worker(tmp, store=store, store_messages=False)
        bot._on_message(FakeMsg("hi"))
        self.assertEqual(store.records, [])


class TestWorkerPush(unittest.TestCase):
    def _rows(self, room, seq=1):
        return [{"roomId": room, "msgSeq": seq, "供应商": "甲", "项目": "无氧杆",
                 "价格": "100", "单位": "吨", "sender": "小王", "time": 1700000000000,
                 "roomName": "报价群"}]

    def test_push_to_configured_users(self):
        tmp = tempfile.mkdtemp()
        bot = make_worker(tmp, push={"enabled": True, "to": ["u1", "u2"]})
        bot._push_one("a@chatroom", self._rows("a@chatroom"))
        sent = bot._bot.sent
        self.assertEqual([s[0] for s in sent], ["u1", "u2"])
        self.assertIn("新报价", sent[0][1])
        self.assertEqual(bot.pushed, 2)

    def test_push_falls_back_to_last_user(self):
        tmp = tempfile.mkdtemp()
        bot = make_worker(tmp, push={"enabled": True, "to": [], "fallback_last": True})
        bot._remember_ctx("u9", "ctx-9")
        bot._push_one("a@chatroom", self._rows("a@chatroom"))
        self.assertEqual(bot._bot.sent[0][0], "u9")
        self.assertEqual(bot._bot.sent[0][2], "ctx-9")

    def test_no_target_skips(self):
        tmp = tempfile.mkdtemp()
        bot = make_worker(tmp, push={"enabled": True, "to": [], "fallback_last": False})
        bot._push_one("a@chatroom", self._rows("a@chatroom"))
        self.assertEqual(bot._bot.sent, [])


class TestTypingIndicator(unittest.TestCase):
    def test_sends_on_enter_and_stops_on_exit(self):
        calls = []
        ind = _TypingIndicator(lambda: calls.append(1), interval=4)
        with ind:
            self.assertEqual(len(calls), 1)
        self.assertTrue(ind._stop.is_set())

    def test_swallows_errors(self):
        def boom():
            raise RuntimeError("no typing ticket")
        with _TypingIndicator(boom, interval=4) as ind:
            pass
        self.assertTrue(ind._warned)


class TestStatePersistence(unittest.TestCase):
    def test_ctx_and_last_user_persist(self):
        tmp = tempfile.mkdtemp()
        bot = make_worker(tmp, connection=False)
        bot._remember_ctx("u1", "ctx-abc")
        bot2 = make_worker(tmp, connection=False)
        self.assertEqual(bot2._ctx_tokens.get("u1"), "ctx-abc")
        self.assertEqual(bot2._last_user, "u1")
        self.assertEqual(bot2.bot_status()["push_targets"], ["u1"])


# ---------------------------------------------------------------------------
# 多账号管理器
# ---------------------------------------------------------------------------

class TestMultiAccount(unittest.TestCase):
    def _factory(self):
        return lambda creds, aid: (lambda: FakeSdkBot(aid))

    def test_loads_multiple_accounts_and_polls(self):
        tmp = tempfile.mkdtemp()
        adir = os.path.join(tmp, "accounts")
        write_creds(os.path.join(adir, "a.json"), "A@im.bot", "uA")
        write_creds(os.path.join(adir, "b.json"), "B@im.bot", "uB")
        mgr = make_manager(tmp, accounts_dir=adir, bot_factory=self._factory())
        try:
            mgr.start()
            deadline = time.time() + 5
            while time.time() < deadline and len(mgr.accounts) < 2:
                time.sleep(0.02)
            self.assertEqual(len(mgr.accounts), 2)
            self.assertEqual({a.account_id for a in mgr.accounts},
                             {"A@im.bot", "B@im.bot"})
            st = mgr.bot_status()
            self.assertEqual(st["account_count"], 2)
            self.assertTrue(st["ws_connected"])
            self.assertEqual(len(st["accounts"]), 2)
        finally:
            mgr.stop()

    def test_push_routes_to_correct_account(self):
        tmp = tempfile.mkdtemp()
        adir = os.path.join(tmp, "accounts")
        write_creds(os.path.join(adir, "a.json"), "A@im.bot", "uA")
        write_creds(os.path.join(adir, "b.json"), "B@im.bot", "uB")
        mgr = make_manager(tmp, accounts_dir=adir, bot_factory=self._factory(),
                           push={"enabled": True, "to": [], "fallback_last": False})
        try:
            mgr.start()
            deadline = time.time() + 5
            while time.time() < deadline and len(mgr.accounts) < 2:
                time.sleep(0.02)
            a, b = mgr.accounts
            a._ctx_tokens["userA"] = "tokenA"
            a._last_activity = 1.0
            b._ctx_tokens["userB"] = "tokenB"
            b._last_activity = 2.0
            mgr._send_to_user("userA", "hi")
            mgr._send_to_user("userB", "yo")
            self.assertEqual(a._bot.sent, [("userA", "hi", "tokenA")])
            self.assertEqual(b._bot.sent, [("userB", "yo", "tokenB")])
            # 未绑定任何账号的用户：回退到最近活跃账号
            mgr._send_to_user("stranger", "zzz")
            self.assertEqual(b._bot.sent[-1][0], "stranger")
        finally:
            mgr.stop()

    def test_state_files_not_loaded_as_accounts(self):
        tmp = tempfile.mkdtemp()
        adir = os.path.join(tmp, "accounts")
        write_creds(os.path.join(adir, "a.json"), "A@im.bot", "uA")
        with open(os.path.join(adir, "A_im.bot.state.json"), "w", encoding="utf-8") as f:
            json.dump({"account_id": "A@im.bot", "ctx_tokens": {}}, f)
        with open(os.path.join(adir, "a.json.sync"), "w", encoding="utf-8") as f:
            f.write("cursor")
        mgr = make_manager(tmp, accounts_dir=adir, bot_factory=self._factory())
        try:
            mgr.start()
            time.sleep(0.15)
            self.assertEqual(len(mgr.accounts), 1)
            self.assertEqual(mgr.accounts[0].account_id, "A@im.bot")
        finally:
            mgr.stop()

    def test_push_prefers_newest_token_for_same_user(self):
        tmp = tempfile.mkdtemp()
        adir = os.path.join(tmp, "accounts")
        write_creds(os.path.join(adir, "a.json"), "A@im.bot", "uA")
        write_creds(os.path.join(adir, "b.json"), "B@im.bot", "uB")
        mgr = make_manager(tmp, accounts_dir=adir, bot_factory=self._factory())
        try:
            mgr.start()
            deadline = time.time() + 5
            while time.time() < deadline and len(mgr.accounts) < 2:
                time.sleep(0.02)
            a, b = mgr.accounts
            # 两个账号都留有同一 user 的 token（模拟升级遗留），B 的更新 → 应选 B
            a._ctx_tokens["dupe"] = "stale"
            a._ctx_ts["dupe"] = 1.0
            b._ctx_tokens["dupe"] = "fresh"
            b._ctx_ts["dupe"] = 2.0
            mgr._send_to_user("dupe", "hi")
            self.assertEqual(b._bot.sent[-1], ("dupe", "hi", "fresh"))
            self.assertEqual(a._bot.sent, [])
        finally:
            mgr.stop()

    def test_manager_dedupe(self):
        tmp = tempfile.mkdtemp()
        adir = os.path.join(tmp, "accounts")
        write_creds(os.path.join(adir, "a.json"), "A@im.bot", "uA")
        mgr = make_manager(tmp, accounts_dir=adir, bot_factory=self._factory(),
                           push={"enabled": True, "to": ["x"]})
        try:
            rows = [{"roomId": "g@chatroom", "msgSeq": 1, "项目": "苹果", "价格": "5"}]
            mgr.notify_quote(rows)
            self.assertEqual(mgr._push_q.qsize(), 1)
            mgr.notify_quote(rows)
            self.assertEqual(mgr._push_q.qsize(), 1)
        finally:
            mgr.stop()


class FakeSdk:
    """假 SDK：模拟扫码登录（先出二维码，等到事件后再返回凭据）。"""
    qr_event = threading.Event()

    @classmethod
    def login(cls, *, save_to=None, on_qrcode=None, on_status_change=None):
        on_qrcode("https://login.example/qr?ticket=abc")
        on_status_change("waiting")
        cls.qr_event.wait(3)
        info = {"botToken": "t", "accountId": "acct-9",
                "baseUrl": "https://example.invalid", "userId": "u9"}
        with open(save_to, "w", encoding="utf-8") as f:
            json.dump(info, f)
        return info


class TestWebLogin(unittest.TestCase):
    def test_login_flow_creates_account(self):
        import server.ilink as ilink_mod
        tmp = tempfile.mkdtemp()
        adir = os.path.join(tmp, "accounts")
        FakeSdk.qr_event.clear()
        mgr = make_manager(tmp, accounts_dir=adir,
                           bot_factory=lambda creds, aid: (lambda: FakeSdkBot(aid)))
        orig = ilink_mod._load_sdk
        ilink_mod._load_sdk = lambda: FakeSdk
        try:
            self.assertTrue(mgr.start_login())
            for _ in range(100):
                if mgr.login_status()["status"] in ("waiting", "scanned"):
                    break
                time.sleep(0.02)
            self.assertEqual(mgr.login_status()["status"], "waiting")
            svg = mgr.qr_svg()
            self.assertIsNotNone(svg)
            self.assertIn(b"<svg", svg)
            FakeSdk.qr_event.set()
            for _ in range(150):
                if mgr.login_status()["status"] == "success":
                    break
                time.sleep(0.02)
            st = mgr.login_status()
            self.assertEqual(st["status"], "success")
            self.assertEqual(st["account_id"], "acct-9")
            self.assertEqual(st["accounts_count"], 1)
            # 凭据已落到 accounts 目录，且账号轮询已启动
            self.assertTrue(os.path.exists(os.path.join(adir, "acct-9.json")))
            self.assertIsNone(mgr.qr_svg())
        finally:
            ilink_mod._load_sdk = orig
            FakeSdk.qr_event.set()
            mgr.stop()

    def test_qr_svg_without_login_returns_none(self):
        tmp = tempfile.mkdtemp()
        mgr = make_manager(tmp, accounts_dir=os.path.join(tmp, "accounts"),
                           bot_factory=lambda creds, aid: (lambda: FakeSdkBot(aid)))
        self.assertIsNone(mgr.qr_svg())


class FakeIlinkHttpBot:
    def __init__(self):
        self.started = 0

    def login_status(self):
        return {"status": "waiting", "qr_content": "https://login.example/qr",
                "error": "", "account_id": "", "has_credentials": False,
                "ws_connected": False}

    def start_login(self):
        self.started += 1
        return True

    def qr_svg(self):
        return b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"

    def bot_status(self):
        return {"enabled": True}


class TestIlinkHttp(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.token = ""
        self.server.store = None
        self.server.web_dir = os.path.join(ROOT, "web")
        self.server.feishu_bot = None
        self.server.ilink_bot = FakeIlinkHttpBot()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_login_status(self):
        with urllib.request.urlopen(self.base + "/api/ilink/login", timeout=5) as r:
            data = json.loads(r.read())
        self.assertEqual(data["status"], "waiting")
        self.assertTrue(data["ok"])

    def test_qr_svg(self):
        with urllib.request.urlopen(self.base + "/api/ilink/qr", timeout=5) as r:
            self.assertTrue(r.headers["Content-Type"].startswith("image/svg"))
            self.assertIn(b"<svg", r.read())

    def test_login_start(self):
        req = urllib.request.Request(self.base + "/api/ilink/login", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        self.assertTrue(data["started"])
        self.assertEqual(self.server.ilink_bot.started, 1)


if __name__ == "__main__":
    unittest.main()
