"""群聊 @ 才触发 agent 回复，并通过下行 send 回发。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.wechat_reply import WechatReplier  # noqa: E402


class _Session:
    def __init__(self, reply):
        self.reply = reply
        self.asked = []

    def ask(self, text, context=None):
        self.asked.append(text)
        return self.reply


class _Bridge:
    def __init__(self, reply="收到"):
        self.reply = reply
        self.sessions = []

    def begin_session(self, session_id, system="", tools=None):
        sess = _Session(self.reply)
        self.sessions.append((session_id, system, sess))
        return sess


def _msg(**over):
    base = {
        "roomId": "49991213437@chatroom",
        "sender": "wxid_other",
        "senderName": "张三",
        "content": "在吗",
        "type": 1,
        "msgSvrId": 1001,
        "atMe": True,
        "atAll": False,
        "atUserList": "wxid_f8loychwun8h22",
    }
    base.update(over)
    return base


class TestWechatReply(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.bridge = _Bridge("好的，我在")
        self.replier = WechatReplier(self.bridge, lambda t, c, d="": self.sent.append((t, c)) or True)

    def test_at_me_in_group_replies(self):
        out = self.replier.handle(_msg())
        self.assertEqual(out, "好的，我在")
        self.assertEqual(self.sent, [("49991213437@chatroom", "好的，我在")])
        self.assertIn("张三", self.bridge.sessions[0][2].asked[0])

    def test_not_mentioned_is_silent(self):
        self.assertIsNone(self.replier.handle(_msg(atMe=False, msgSvrId=1002)))
        self.assertEqual(self.sent, [])

    def test_private_chat_is_silent_even_if_at_me(self):
        self.assertIsNone(self.replier.handle(_msg(roomId="wxid_other", msgSvrId=1003)))
        self.assertEqual(self.sent, [])

    def test_at_all_replies_when_flagged(self):
        out = self.replier.handle(_msg(atMe=True, atAll=True, atUserList=None, msgSvrId=1004, content="开会"))
        self.assertEqual(out, "好的，我在")
        self.assertEqual(len(self.sent), 1)

    def test_non_text_is_silent(self):
        self.assertIsNone(self.replier.handle(_msg(type=3, msgSvrId=1005)))
        self.assertEqual(self.sent, [])

    def test_empty_content_is_silent(self):
        self.assertIsNone(self.replier.handle(_msg(content="  ", msgSvrId=1006)))
        self.assertEqual(self.sent, [])

    def test_duplicate_msg_svr_id_once(self):
        self.assertIsNotNone(self.replier.handle(_msg(msgSvrId=1007)))
        self.assertIsNone(self.replier.handle(_msg(msgSvrId=1007, content="再说一次")))
        self.assertEqual(len(self.sent), 1)

    def test_empty_model_output_does_not_send(self):
        self.bridge.reply = "   "
        self.assertIsNone(self.replier.handle(_msg(msgSvrId=1008)))
        self.assertEqual(self.sent, [])

    def test_switch_off_skips_without_consuming(self):
        self.assertTrue(self.replier.enabled)
        self.replier.set_enabled(False)
        self.assertIsNone(self.replier.handle(_msg(msgSvrId=1009)))
        self.assertEqual(self.sent, [])
        self.replier.set_enabled(True)
        self.assertEqual(self.replier.handle(_msg(msgSvrId=1009)), "好的，我在")
        self.assertEqual(len(self.sent), 1)


if __name__ == "__main__":
    unittest.main()
