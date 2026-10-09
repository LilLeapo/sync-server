"""引用消息：解析出回复正文和被引用内容，并进入报价分析队列。"""
import itertools
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.quote_agent import QuoteAgent, message_source, message_summary  # noqa: E402
from server.wechat_quote import is_quote_message, parse_quote, quote_analysis_text  # noqa: E402

QUOTE_TYPE = 822083633  # 低 16 位是 49

TEXT_QUOTE = """<?xml version="1.0"?>
<msg><appmsg>
<title>这个也报 12000</title>
<des></des>
<type>57</type>
<content></content>
<refermsg>
<type>1</type>
<chatusr>wxid_abc</chatusr>
<displayname>陈工</displayname>
<content>wxid_abc:\n无氧杆 2609合约+500</content>
</refermsg>
</appmsg></msg>"""

IMAGE_QUOTE = """<msg><appmsg>
<title>看下这张</title>
<type>57</type>
<content></content>
<refermsg>
<type>3</type>
<displayname>李四</displayname>
<content><![CDATA[<?xml version="1.0"?><msg><img aeskey="secret" /></msg>]]></content>
</refermsg>
</appmsg></msg>"""

LINK = """<msg><appmsg><title>官网</title><des>说明</des><type>5</type><url>https://example.com</url></appmsg></msg>"""


def _msg(content, mtype=QUOTE_TYPE):
    return {"type": mtype, "content": content, "roomId": "1@chatroom", "sender": "wxid_me"}


class TestParseQuote(unittest.TestCase):
    def test_text_quote_keeps_reply_and_refer(self):
        msg = _msg(TEXT_QUOTE)
        self.assertTrue(is_quote_message(msg))
        parsed = parse_quote(TEXT_QUOTE)
        self.assertEqual(parsed["reply"], "这个也报 12000")
        self.assertEqual(parsed["refer"]["type"], 1)
        self.assertEqual(parsed["refer"]["sender"], "陈工")
        self.assertEqual(parsed["refer"]["text"], "无氧杆 2609合约+500")

        text = quote_analysis_text(msg)
        self.assertIn("发送的消息：\n这个也报 12000", text)
        self.assertIn("被引用的消息（陈工，文本）：\n无氧杆 2609合约+500", text)
        self.assertNotIn("<refermsg", text)
        self.assertEqual(message_source(msg), "引用")
        self.assertIn("12000", message_summary(msg))
        self.assertIn("无氧杆", message_summary(msg))

    def test_empty_app_content_does_not_hide_refer(self):
        parsed = parse_quote(TEXT_QUOTE)
        self.assertNotEqual(parsed["refer"]["text"], "")

    def test_image_refer_is_placeholder_not_xml(self):
        msg = _msg(IMAGE_QUOTE)
        text = quote_analysis_text(msg)
        self.assertIn("发送的消息：\n看下这张", text)
        self.assertIn("被引用的消息（李四，图片）：\n[图片]", text)
        self.assertNotIn("aeskey", text)

    def test_link_is_not_quote(self):
        msg = _msg(LINK, 49)
        self.assertFalse(is_quote_message(msg))
        self.assertIsNone(quote_analysis_text(msg))
        self.assertEqual(message_source(msg), "文字")

    def test_plain_text_unchanged(self):
        msg = {"type": 1, "content": "无氧杆 2609合约+500"}
        self.assertFalse(is_quote_message(msg))
        self.assertIsNone(quote_analysis_text(msg))
        self.assertEqual(message_source(msg), "文字")
        self.assertEqual(message_summary(msg), "无氧杆 2609合约+500")


class TestEnqueueQuote(unittest.TestCase):
    def _agent(self):
        agent = QuoteAgent.__new__(QuoteAgent)
        agent._processed = set()
        agent._heap = []
        agent._cv = threading.Condition()
        agent._counter = itertools.count()
        return agent

    def test_quote_is_queued(self):
        agent = self._agent()
        agent.enqueue({"seq": 7, "msg": _msg(TEXT_QUOTE)})
        self.assertEqual(len(agent._heap), 1)
        self.assertEqual(agent._heap[0][2]["seq"], 7)

    def test_link_is_not_queued(self):
        agent = self._agent()
        agent.enqueue({"seq": 8, "msg": _msg(LINK, 49)})
        self.assertEqual(agent._heap, [])

    def test_text_still_queued(self):
        agent = self._agent()
        agent.enqueue({"seq": 1, "msg": {"type": 1, "content": "hi", "roomId": "1@chatroom"}})
        self.assertEqual(len(agent._heap), 1)
