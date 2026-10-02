"""下行发送队列与 ack 携带发送指令的协议测试。"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.relay import RelayListener  # noqa: E402


class _FakeStore:
    def __init__(self):
        self.seq = 0

    def add_message(self, msg):
        self.seq += 1
        return {"seq": self.seq}


def _decode(raw: bytes) -> dict:
    return json.loads(raw.decode("utf-8"))


class TestDownlinkQueue(unittest.TestCase):
    def setUp(self):
        self.listener = RelayListener("127.0.0.1", 0, _FakeStore())

    def test_send_rejects_empty(self):
        self.assertFalse(self.listener.send("", "hi"))
        self.assertFalse(self.listener.send("room", ""))

    def test_ping_ack_carries_send(self):
        self.assertTrue(self.listener.send("123@chatroom", "hello"))
        ack = _decode(self.listener._handle_frame(b'{"ping":true}'))
        self.assertTrue(ack["ok"])
        self.assertEqual(ack["send"], {"talker": "123@chatroom", "content": "hello"})

    def test_send_is_consumed_once(self):
        self.listener.send("123@chatroom", "one")
        first = _decode(self.listener._handle_frame(b'{"ping":true}'))
        second = _decode(self.listener._handle_frame(b'{"ping":true}'))
        self.assertIn("send", first)
        self.assertNotIn("send", second)

    def test_message_ack_carries_seq_and_send(self):
        self.listener.send("123@chatroom", "reply")
        raw = json.dumps({"roomId": "123@chatroom", "content": "x"}).encode()
        ack = _decode(self.listener._handle_frame(raw))
        self.assertEqual(ack["seq"], 1)
        self.assertEqual(ack["send"]["content"], "reply")

    def test_order_preserved(self):
        self.listener.send("r", "1")
        self.listener.send("r", "2")
        a = _decode(self.listener._handle_frame(b'{"ping":true}'))["send"]["content"]
        b = _decode(self.listener._handle_frame(b'{"ping":true}'))["send"]["content"]
        self.assertEqual((a, b), ("1", "2"))


if __name__ == "__main__":
    unittest.main()