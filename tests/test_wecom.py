"""企业微信智能机器人离线测试：WebSocket 帧编解码、消息解析、指令/问答分发、报价推送。

全部离线：不建立真实网络连接，用 FakeConn 拦截发送，用临时插件目录隔离。
"""
import json
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.bot_plugins import PluginHost  # noqa: E402
from server.wecom import (  # noqa: E402
    CMD_RESPOND,
    CMD_RESPOND_WELCOME,
    CMD_SEND,
    CMD_MSG_CALLBACK,
    CMD_EVENT_CALLBACK,
    WeComBot,
    WeComConnection,
    build_ws_frame,
    load_wecom_config,
    parse_ws_frame,
)

PLUGIN_SRC = '''
def register(api):
    def ping(api, event, args):
        return "pong:" + (args or "")
    api.register_command("ping", ping, usage="/ping <文本>", description="测试")
    def on_text(api, event, text):
        if text.strip() == "你好":
            return "hi"
        return None
    api.register_message_hook(on_text)
'''


class FakeConn:
    """记录所有下行命令的假连接。"""

    def __init__(self):
        self.sent = []
        self.started = False
        self.disconnects = []

    def send_command(self, cmd, body, req_id=None):
        self.sent.append((cmd, body, req_id))
        return req_id or "req"

    def start(self):
        self.started = True

    @property
    def connected(self):
        return True

    def disconnect_current(self, peer=False):
        self.disconnects.append(peer)


def msg_frame(text, *, chatid="chat1", chattype="group", msgid="m1", msgtype="text"):
    body = {
        "msgid": msgid,
        "chatid": chatid,
        "chattype": chattype,
        "from": {"userid": "u1"},
        "msgtype": msgtype,
    }
    if msgtype == "text":
        body["text"] = {"content": text}
    elif msgtype == "mixed":
        body["mixed"] = {"msg_item": [{"msgtype": "text", "text": {"content": text}}]}
    elif msgtype == "voice":
        body["voice"] = {"content": text}
    return {"cmd": CMD_MSG_CALLBACK, "headers": {"req_id": "req-1"}, "body": body}


class TestWSFrames(unittest.TestCase):
    def test_build_and_parse_roundtrip(self):
        payload = "你好，企业微信".encode("utf-8")
        mask = b"\x01\x02\x03\x04"
        raw = build_ws_frame(0x1, payload, mask_key=mask)
        self.assertEqual(raw[0], 0x81)           # FIN + text
        self.assertTrue(raw[1] & 0x80)           # 客户端必须掩码
        parsed = parse_ws_frame(raw)
        self.assertIsNotNone(parsed)
        fin, opcode, out, consumed = parsed
        self.assertTrue(fin)
        self.assertEqual(opcode, 0x1)
        self.assertEqual(out, payload)
        self.assertEqual(consumed, len(raw))

    def test_extended_lengths(self):
        for n in (125, 126, 700, 70000):
            raw = build_ws_frame(0x1, b"x" * n, mask_key=b"abcd")
            parsed = parse_ws_frame(raw)
            self.assertIsNotNone(parsed, f"len={n}")
            self.assertEqual(parsed[2], b"x" * n)

    def test_incomplete_returns_none(self):
        raw = build_ws_frame(0x1, b"hello world", mask_key=b"abcd")
        self.assertIsNone(parse_ws_frame(raw[:3]))
        self.assertIsNone(parse_ws_frame(raw[:-1]))

    def test_length_prefixes(self):
        self.assertEqual(build_ws_frame(0x1, b"a" * 10, b"abcd")[1], 0x80 | 10)
        self.assertEqual(build_ws_frame(0x1, b"a" * 200, b"abcd")[1], 0x80 | 126)
        self.assertEqual(build_ws_frame(0x1, b"a" * 70000, b"abcd")[1], 0x80 | 127)
        self.assertEqual(struct.unpack("!H", build_ws_frame(0x1, b"a" * 200, b"abcd")[2:4])[0], 200)


class TestConnectionSend(unittest.TestCase):
    def test_send_command_frame(self):
        class StubWS:
            def __init__(self):
                self.sent = []
            def send_text(self, text):
                self.sent.append(text)

        conn = WeComConnection("bot", "secret", on_callback=None)
        stub = StubWS()
        conn._ws = stub
        conn._connected.set()
        req = conn.send_command("aibot_respond_msg", {"msgtype": "markdown"})
        self.assertTrue(req)
        frame = json.loads(stub.sent[0])
        self.assertEqual(frame["cmd"], "aibot_respond_msg")
        self.assertEqual(frame["headers"]["req_id"], req)
        self.assertEqual(frame["body"], {"msgtype": "markdown"})

    def test_send_before_connect_is_queued(self):
        conn = WeComConnection("bot", "secret")
        conn.send_command("ping", {})
        self.assertEqual(conn._out_q.qsize(), 1)


class TestTextExtraction(unittest.TestCase):
    def test_text(self):
        self.assertEqual(WeComBot._extract_text({"msgtype": "text", "text": {"content": " hi "}}), "hi")
    def test_mixed(self):
        body = {"msgtype": "mixed", "mixed": {"msg_item": [
            {"msgtype": "text", "text": {"content": "a"}},
            {"msgtype": "image", "image": {"url": "u"}},
            {"msgtype": "text", "text": {"content": "b"}},
        ]}}
        self.assertEqual(WeComBot._extract_text(body), "ab")
    def test_voice(self):
        self.assertEqual(WeComBot._extract_text(
            {"msgtype": "voice", "voice": {"content": "转写文本"}}), "转写文本")
    def test_unsupported(self):
        self.assertIsNone(WeComBot._extract_text({"msgtype": "image", "image": {}}))


class TestBotDispatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # 临时插件目录：一个命令 + 一个消息钩子
        pdir = os.path.join(self.tmp, "plugins")
        os.makedirs(pdir)
        with open(os.path.join(pdir, "t.py"), "w", encoding="utf-8") as f:
            f.write(PLUGIN_SRC)
        self.plugins = PluginHost(pdir)
        self.plugins.load()

    def _bot(self, **cfg):
        base = {"enabled": True, "bot_id": "b", "secret": "s", "push": {"enabled": False}}
        base.update(cfg)
        conn = FakeConn()
        bot = WeComBot(base, plugin_host=self.plugins, data_dir=self.tmp, connection=conn)
        return bot, conn

    def test_command_reply(self):
        bot, conn = self._bot()
        bot._handle_msg(msg_frame("/ping hello"))
        cmd, body, req_id = conn.sent[-1]
        self.assertEqual(cmd, CMD_RESPOND)
        self.assertEqual(body["msgtype"], "markdown")
        self.assertIn("pong:hello", body["markdown"]["content"])
        self.assertEqual(req_id, "req-1")

    def test_message_hook_reply(self):
        bot, conn = self._bot()
        bot._handle_msg(msg_frame("你好"))
        self.assertIn("hi", conn.sent[-1][1]["markdown"]["content"])

    def test_unknown_command(self):
        bot, conn = self._bot()
        bot._handle_msg(msg_frame("/nope"))
        self.assertIn("未知指令", conn.sent[-1][1]["markdown"]["content"])

    def test_mention_stripped(self):
        bot, conn = self._bot()
        bot._handle_msg(msg_frame("@报告机器人 /ping x"))
        self.assertIn("pong:x", conn.sent[-1][1]["markdown"]["content"])

    def test_qna_without_bridge(self):
        bot, conn = self._bot(qna={"enabled": True})
        bot._handle_msg(msg_frame("今天天气如何"))
        self.assertIn("未启用", conn.sent[-1][1]["markdown"]["content"])

    def test_duplicate_msgid_ignored(self):
        bot, conn = self._bot()
        bot._handle_msg(msg_frame("/ping a", msgid="dup"))
        n = len(conn.sent)
        bot._handle_msg(msg_frame("/ping b", msgid="dup"))
        self.assertEqual(len(conn.sent), n)

    def test_unsupported_message_hint(self):
        bot, conn = self._bot()
        bot._handle_msg(msg_frame("", msgtype="image", msgid="img1"))
        self.assertIn("文字", conn.sent[-1][1]["markdown"]["content"])


class TestEvents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _bot(self):
        conn = FakeConn()
        bot = WeComBot({"enabled": True, "bot_id": "b", "secret": "s",
                        "push": {"enabled": False}, "welcome": "欢迎光临"},
                       data_dir=self.tmp, connection=conn)
        return bot, conn

    def test_enter_chat_welcome(self):
        bot, conn = self._bot()
        frame = {"cmd": CMD_EVENT_CALLBACK, "headers": {"req_id": "e1"},
                 "body": {"from": {"userid": "u1"}, "msgtype": "event",
                          "event": {"eventtype": "enter_chat"}}}
        bot._handle_event(frame)
        cmd, body, req_id = conn.sent[-1]
        self.assertEqual(cmd, CMD_RESPOND_WELCOME)
        self.assertEqual(body["text"]["content"], "欢迎光临")
        self.assertEqual(req_id, "e1")

    def test_feedback_logged(self):
        bot, conn = self._bot()
        frame = {"cmd": CMD_EVENT_CALLBACK, "headers": {"req_id": "e2"},
                 "body": {"event": {"eventtype": "feedback_event",
                                    "feedback_event": {"id": "f", "type": 1}}}}
        bot._handle_event(frame)
        self.assertEqual(conn.sent, [])

    def test_disconnected_event_triggers_peer_disconnect(self):
        bot, conn = self._bot()
        frame = {"cmd": CMD_EVENT_CALLBACK, "headers": {"req_id": "e3"},
                 "body": {"event": {"eventtype": "disconnected_event"}}}
        bot._handle_event(frame)
        self.assertEqual(conn.disconnects, [True])
        self.assertFalse(bot.connected)


class TestQuotePush(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _bot(self, **cfg):
        base = {"enabled": True, "bot_id": "b", "secret": "s",
                "push": {"enabled": True, "chat_id": "", "chat_type": 2},
                "push_rooms": ["*"]}
        base.update(cfg)
        conn = FakeConn()
        bot = WeComBot(base, data_dir=self.tmp, connection=conn)
        return bot, conn

    def _rows(self, room, seq=1):
        return [{"roomId": room, "msgSeq": seq, "供应商": "甲", "项目": "无氧杆",
                 "价格": "100", "单位": "吨", "sender": "小王", "time": 1700000000000,
                 "roomName": "报价群"}]

    def test_push_to_configured_chat(self):
        bot, conn = self._bot(push={"enabled": True, "chat_id": "groupX", "chat_type": 2})
        bot._push_one("a@chatroom", self._rows("a@chatroom"))
        cmd, body, _ = conn.sent[-1]
        self.assertEqual(cmd, CMD_SEND)
        self.assertEqual(body["chatid"], "groupX")
        self.assertEqual(body["chat_type"], 2)
        self.assertEqual(body["msgtype"], "markdown")
        self.assertIn("新报价", body["markdown"]["content"])
        self.assertEqual(bot.pushed, 1)

    def test_push_falls_back_to_last_chat(self):
        bot, conn = self._bot()
        bot._remember_chat("u9", 1)
        bot._push_one("a@chatroom", self._rows("a@chatroom"))
        body = conn.sent[-1][1]
        self.assertEqual(body["chatid"], "u9")
        self.assertEqual(body["chat_type"], 1)

    def test_no_target_skips(self):
        bot, conn = self._bot()
        bot._push_one("a@chatroom", self._rows("a@chatroom"))
        self.assertEqual(conn.sent, [])

    def test_room_filter_and_dedupe(self):
        bot, conn = self._bot(push_rooms=["group_a"], push={"enabled": True, "chat_id": "x"})
        rows_a = self._rows("group_a@chatroom")
        rows_b = self._rows("group_b@chatroom")
        bot.notify_quote(rows_b)
        self.assertTrue(bot._push_q.empty())
        bot.notify_quote(rows_a)
        self.assertEqual(bot._push_q.qsize(), 1)
        bot.notify_quote(rows_a)
        self.assertEqual(bot._push_q.qsize(), 1)


class TestConfig(unittest.TestCase):
    def test_disabled_returns_none(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"enabled": False}, f)
            path = f.name
        try:
            self.assertIsNone(load_wecom_config(path))
        finally:
            os.unlink(path)

    def test_enabled_returns_dict(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"enabled": True, "bot_id": "b"}, f)
            path = f.name
        try:
            cfg = load_wecom_config(path)
            self.assertEqual(cfg["bot_id"], "b")
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
