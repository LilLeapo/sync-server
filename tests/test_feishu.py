"""飞书集成测试：加签/验签/加解密、事件 webhook、指令分发、报价推送链路。

全部离线：FakeClient 拦截所有飞书 HTTP 调用，不触网。
"""
import base64
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import feishu, sync_server  # noqa: E402
from server.feishu import FeishuBot, FeishuClient, webhook_sign, verify_signature  # noqa: E402
from server.bot_plugins import PluginHost  # noqa: E402
from server.pi_bridge import PiBridge  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakeClient(FeishuClient):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.replies = []
        self.sends = []
        self.webhook_calls = []

    def reply_message(self, message_id, msg_type, content):
        self.replies.append((message_id, content))
        return {"code": 0}

    def send_message(self, receive_id, receive_id_type, msg_type, content):
        self.sends.append((receive_id, receive_id_type, content))
        return {"code": 0}

    def push_webhook(self, url, text, secret=""):
        self.webhook_calls.append((url, text))
        return {"code": 0}


class FakeStore:
    def __init__(self):
        self._rooms = []

    def stats(self):
        return {"count": 42, "images": 7}

    def rooms(self):
        return self._rooms


def make_quote_log(tmpdir, n=2):
    rows = []
    for i in range(n):
        rows.append({
            "qseq": i + 1, "roomId": "group@chatroom", "time": 1740000000000 + i,
            "sender": "张三", "source": "文字", "msgSeq": i + 1,
            "项目": f"苹果{i}", "价格": "¥5", "单位": "元/斤", "数量": str(i + 1), "备注": "包邮",
        })
    path = os.path.join(tmpdir, "quotes.jsonl")
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    from server.store import QuoteLog
    return QuoteLog(path)


def make_im_event(text, message_id="om_1", chat_id="oc_1", sender_type="user"):
    return {
        "schema": "2.0",
        "header": {"event_type": "im.message.receive_v1", "event_id": "evt-" + str(time.time_ns()),
                   "token": "vt", "app_id": "cli_x", "tenant_key": "t"},
        "event": {
            "sender": {"sender_type": sender_type, "sender_id": {"open_id": "ou_1"}},
            "message": {"message_id": message_id, "chat_id": chat_id,
                        "chat_type": "group", "message_type": "text",
                        "content": json.dumps({"text": text})},
        },
    }


def poll_until(cond, timeout=6.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def make_bot(cfg, tmpdir, with_plugins=True, bridge=None):
    client = FakeClient(cfg)
    store = FakeStore()
    quote_log = make_quote_log(tmpdir)
    host = None
    if with_plugins:
        host = PluginHost(os.path.join(ROOT, "plugins"))
        host.load()
        host.bind(store=store, quote_log=quote_log, bridge=bridge,
                  feishu_client=client, feishu_cfg=cfg, bot=None)
    bot = FeishuBot(client, cfg, store=store, quote_log=quote_log, bridge=bridge,
                    plugin_host=host, data_dir=tmpdir)
    if with_plugins:
        host.api.bot = bot
    return bot, client


BASE_CFG = {
    "verification_token": "vt",
    "encrypt_key": "",
    "event_path": "/feishu/event",
    "push": {"mode": "none"},
    "push_rooms": ["*"],
    "qna": {"enabled": True, "recent_quotes": 5},
    "startup_notice": False,
}


class TestCrypto(unittest.TestCase):
    def test_webhook_sign_formula(self):
        # 与 Go 参考实现一致：HMAC(key=f"{ts}\n{secret}", msg="") → base64
        ts = 1234567890
        secret = "s3cr3t"
        import hmac
        expect = base64.b64encode(
            hmac.new(f"{ts}\n{secret}".encode(), b"", hashlib.sha256).digest()).decode()
        self.assertEqual(webhook_sign(secret, ts), expect)

    def test_event_signature(self):
        raw = b'{"encrypt":"x"}'
        good = hashlib.sha256(b"ts" + b"no" + b"key" + raw).hexdigest()
        self.assertTrue(verify_signature("ts", "no", "key", raw, good))
        self.assertFalse(verify_signature("ts", "no", "key", raw, "deadbeef"))

    def test_aes_roundtrip(self):
        try:
            from Crypto.Cipher import AES
            from Crypto.Util.Padding import pad
        except ImportError:
            self.skipTest("无 pycryptodome")
        key = hashlib.sha256(b"enc-key").digest()
        iv = os.urandom(16)
        plain = json.dumps({"type": "url_verification", "challenge": "CH123",
                            "token": "vt"}).encode()
        cipher = AES.new(key, AES.MODE_CBC, iv)
        enc = base64.b64encode(iv + cipher.encrypt(pad(plain, 16))).decode()
        self.assertEqual(feishu._aes_decrypt("enc-key", {"encrypt": enc}),
                         json.dumps({"type": "url_verification", "challenge": "CH123",
                                     "token": "vt"}))


class TestWebhookEndpoint(unittest.TestCase):
    def test_challenge_plain(self):
        with tempfile.TemporaryDirectory() as d:
            bot, _ = make_bot(dict(BASE_CFG), d)
            code, text = bot.handle_webhook(
                {}, json.dumps({"type": "url_verification", "challenge": "CH-1",
                                "token": "vt"}).encode())
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(text)["challenge"], "CH-1")

    def test_token_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            bot, _ = make_bot(dict(BASE_CFG), d)
            code, _ = bot.handle_webhook(
                {}, json.dumps({"header": {"token": "wrong", "event_type": "x",
                                           "event_id": "e1"}, "event": {}}).encode())
            self.assertEqual(code, 403)

    def test_encrypted_challenge(self):
        try:
            from Crypto.Cipher import AES
            from Crypto.Util.Padding import pad
        except ImportError:
            self.skipTest("无 pycryptodome")
        with tempfile.TemporaryDirectory() as d:
            cfg = dict(BASE_CFG, encrypt_key="enc-key")
            bot, _ = make_bot(cfg, d)
            key = hashlib.sha256(b"enc-key").digest()
            iv = os.urandom(16)
            plain = json.dumps({"type": "url_verification", "challenge": "CH-9"}).encode()
            cipher = AES.new(key, AES.MODE_CBC, iv)
            enc = json.dumps({"encrypt": base64.b64encode(
                iv + cipher.encrypt(pad(plain, 16))).decode()}).encode()
            sig = hashlib.sha256(b"123" + b"abc" + b"enc-key" + enc).hexdigest()
            code, text = bot.handle_webhook(
                {"X-Lark-Request-Timestamp": "123", "X-Lark-Request-Nonce": "abc",
                 "X-Lark-Signature": sig}, enc)
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(text)["challenge"], "CH-9")

    def test_real_http_route(self):
        """通过真实 HTTP Handler 走一遍 /feishu/event。"""
        with tempfile.TemporaryDirectory() as d:
            bot, _ = make_bot(dict(BASE_CFG), d)
            srv = ThreadingHTTPServer(("127.0.0.1", 0), sync_server.Handler)
            srv.feishu_bot = bot
            srv.token = ""
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                import urllib.request
                req = urllib.request.Request(
                    f"http://127.0.0.1:{srv.server_address[1]}/feishu/event",
                    data=json.dumps({"type": "url_verification", "challenge": "CH-H",
                                     "token": "vt"}).encode(),
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    out = json.loads(resp.read())
                self.assertEqual(out["challenge"], "CH-H")
            finally:
                srv.shutdown()
                srv.server_close()


class TestCommandDispatch(unittest.TestCase):
    def test_stats_command_via_event(self):
        with tempfile.TemporaryDirectory() as d:
            bot, client = make_bot(dict(BASE_CFG), d)
            bot.handle_webhook({}, json.dumps(make_im_event("/stats")).encode())
            self.assertTrue(poll_until(lambda: client.replies))
            _, content = client.replies[0]
            self.assertIn("消息 42 条", content["text"])
            self.assertIn("报价记录 2 条", content["text"])

    def test_unknown_command(self):
        with tempfile.TemporaryDirectory() as d:
            bot, client = make_bot(dict(BASE_CFG), d)
            bot.handle_webhook({}, json.dumps(make_im_event("/nope")).encode())
            self.assertTrue(poll_until(lambda: client.replies))
            self.assertIn("/help", client.replies[0][1]["text"])

    def test_natural_quotes_hook(self):
        with tempfile.TemporaryDirectory() as d:
            bot, client = make_bot(dict(BASE_CFG), d)
            bot.handle_webhook({}, json.dumps(make_im_event("@_user_1 最近报价")).encode())
            self.assertTrue(poll_until(lambda: client.replies))
            self.assertIn("苹果0", client.replies[0][1]["text"])

    def test_own_bot_message_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            bot, client = make_bot(dict(BASE_CFG), d)
            bot.handle_webhook({}, json.dumps(
                make_im_event("/stats", sender_type="bot")).encode())
            time.sleep(0.4)
            self.assertEqual(client.replies, [])

    def test_qa_without_bridge_answers_hint(self):
        with tempfile.TemporaryDirectory() as d:
            bot, client = make_bot(dict(BASE_CFG), d, bridge=None)
            bot.handle_webhook({}, json.dumps(make_im_event("苹果多少钱")).encode())
            self.assertTrue(poll_until(lambda: client.replies))
            self.assertIn("没有配置 Agent", client.replies[0][1]["text"])

    def test_qa_with_mock_bridge(self):
        try:
            import pi_agent  # noqa: F401
        except ImportError:
            self.skipTest("无 pi-agent SDK")
        with tempfile.TemporaryDirectory() as d:
            bridge = PiBridge.from_config({"enabled": True, "provider": "mock", "model": "m"})
            bot, client = make_bot(dict(BASE_CFG), d, bridge=bridge)
            bot.handle_webhook({}, json.dumps(make_im_event("苹果多少钱")).encode())
            self.assertTrue(poll_until(lambda: client.replies, timeout=15))
            self.assertIn("Echo:", client.replies[0][1]["text"])


class TestQuotePush(unittest.TestCase):
    def cfg(self, mode="webhook"):
        return dict(BASE_CFG, push={"mode": mode, "webhook_url": "http://127.0.0.1:1/hook/x",
                                    "webhook_secret": "", "chat_id": "oc_push"})

    def rows(self, msg_seq=1):
        return [{
            "roomId": "group@chatroom", "time": 1740000000000, "sender": "张三",
            "source": "文字", "msgSeq": msg_seq, "项目": "苹果", "价格": "¥5",
            "单位": "元/斤", "数量": "10", "备注": "包邮",
        }]

    def test_push_via_webhook_and_dedupe(self):
        with tempfile.TemporaryDirectory() as d:
            bot, client = make_bot(self.cfg(), d)
            bot.notify_quote(self.rows(1))
            bot.notify_quote(self.rows(1))  # 重复应被去重
            self.assertTrue(poll_until(lambda: len(client.webhook_calls) == 1))
            url, text = client.webhook_calls[0]
            self.assertIn("hook/x", url)
            self.assertIn("📊 新报价", text)
            self.assertIn("苹果", text)
            self.assertIn("¥5", text)

    def test_restart_dedup_state(self):
        with tempfile.TemporaryDirectory() as d:
            bot, client = make_bot(self.cfg(), d)
            bot.notify_quote(self.rows(1))
            self.assertTrue(poll_until(lambda: len(client.webhook_calls) == 1))
            # 模拟重启：同 data-dir 新建 bot，同一批报价不应再推
            bot2, client2 = make_bot(self.cfg(), d)
            bot2.notify_quote(self.rows(1))
            time.sleep(0.6)
            self.assertEqual(client2.webhook_calls, [])

    def test_quote_hook_suppression(self):
        with tempfile.TemporaryDirectory() as d:
            plugin_dir = os.path.join(d, "plugins")
            os.makedirs(plugin_dir)
            with open(os.path.join(plugin_dir, "suppress.py"), "w") as f:
                f.write("def register(api):\n"
                        "    api.register_quote_hook(lambda api, ctx: False)\n")
            cfg = self.cfg()
            client = FakeClient(cfg)
            host = PluginHost(plugin_dir)
            host.load()
            bot = FeishuBot(client, cfg, store=FakeStore(),
                            quote_log=make_quote_log(d), plugin_host=host, data_dir=d)
            bot.notify_quote(self.rows(1))
            time.sleep(0.6)
            self.assertEqual(client.webhook_calls, [])
            self.assertEqual(bot.pushed, 0)

    def test_app_mode_push(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = dict(BASE_CFG, push={"mode": "app", "chat_id": "oc_push"})
            bot, client = make_bot(cfg, d)
            bot.notify_quote(self.rows(1))
            self.assertTrue(poll_until(lambda: client.sends))
            rid, rtype, content = client.sends[0]
            self.assertEqual(rid, "oc_push")
            self.assertEqual(rtype, "chat_id")
            self.assertIn("苹果", content["text"])

    def test_room_filter(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = dict(self.cfg(), push_rooms=["other"])
            bot, client = make_bot(cfg, d)
            bot.notify_quote(self.rows(1))
            time.sleep(0.5)
            self.assertEqual(client.webhook_calls, [])


if __name__ == "__main__":
    unittest.main()