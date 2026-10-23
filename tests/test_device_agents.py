"""多设备时每台设备一个独立 Agent：@ 回复会话、报价提取上下文/台账、问答工具数据互不相通。

同一条群消息被两台设备各报一次时，两台设备的 Agent 各自处理、各自留档；
同一台设备重复投递仍只处理一次。没有 deviceId 的旧数据归入 "" 一组，行为与升级前一致。
"""
import itertools
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.qa_tools import build_qa_tools  # noqa: E402
from server.quote_agent import QuoteAgent  # noqa: E402
from server.quote_rules import QuoteLedger, normalize_event  # noqa: E402
from server.store import QuoteLog, Store  # noqa: E402
from server.wechat_reply import WechatReplier  # noqa: E402
from tests.test_quote_pipeline import agent_at  # noqa: E402
from tests.test_quote_rules import NOW, quote, source  # noqa: E402

DEV_A = "wxid_aaa-ANDROID_A"
DEV_B = "wxid_bbb-ANDROID_B"
ROOM = "suppliers@chatroom"


def record(text, seq, device, svr=1, room=ROOM, at=NOW):
    return {"seq": seq, "receivedAt": at, "msg": {
        "type": 1, "roomId": room, "sender": "buyer", "senderName": "采购",
        "createTime": at, "msgSvrId": str(svr), "content": text, "deviceId": device}}


class _Session:
    def __init__(self, session_id, tools, system=""):
        self.session_id = session_id
        self.system = system
        self.tools = {t["name"]: t["handler"] for t in tools}
        self.asked = []

    def ask(self, text, context=None):
        self.asked.append(text)
        return "好的"


class _Bridge:
    def __init__(self):
        self.sessions = []

    def begin_session(self, session_id, system="", tools=None):
        sess = _Session(session_id, tools or [], system)
        self.sessions.append(sess)
        return sess


def mention(device, svr=1, room="1@chatroom", **over):
    msg = {"roomId": room, "sender": "wxid_x", "senderName": "张三", "content": "在吗",
           "type": 1, "msgSvrId": svr, "atMe": True, "deviceId": device}
    msg.update(over)
    return msg


class TestReplyAgentPerDevice(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(self._tmp.name)
        self.quote_log = QuoteLog(os.path.join(self._tmp.name, "quotes", "quotes.jsonl"))
        self.bridge = _Bridge()
        self.sent = []
        self.replier = WechatReplier(
            self.bridge, lambda t, c, d="": self.sent.append((t, c, d)) or True,
            store=self.store, quote_log=self.quote_log)

    def tearDown(self):
        self._tmp.cleanup()

    def test_same_room_on_two_devices_has_two_sessions(self):
        self.replier.handle(mention(DEV_A, svr=1))
        self.replier.handle(mention(DEV_B, svr=2))
        self.replier.handle(mention(DEV_A, svr=3))
        self.assertEqual(len(self.bridge.sessions), 2)
        a, b = self.bridge.sessions
        self.assertNotEqual(a.session_id, b.session_id)
        self.assertEqual(len(a.asked), 2)
        self.assertEqual(len(b.asked), 1)

    def test_group_reply_looks_at_quotes_from_all_groups_by_default(self):
        self.replier.handle(mention(DEV_A, svr=1, room="1@chatroom"))
        system = self.bridge.sessions[0].system
        self.assertIn("当前微信群roomId：1@chatroom", system, "the model still knows which group it answers in")
        self.assertNotIn("未指定其他群时查询使用这个room", system, "quote lookups are no longer pinned to this group")
        self.assertIn("查报价、比价、供应商时默认看本设备所有群（不传 room）", system)

    def test_legacy_session_id_is_unchanged(self):
        self.replier.handle(mention("", svr=1))
        self.assertEqual(self.bridge.sessions[0].session_id, "wechat-1@chatroom")

    def test_mention_seen_by_two_devices_is_answered_by_each(self):
        self.assertEqual(self.replier.handle(mention(DEV_A)), "好的")
        self.assertEqual(self.replier.handle(mention(DEV_B)), "好的")
        self.assertIsNone(self.replier.handle(mention(DEV_A)))  # 同一台设备重复投递仍只回一次
        self.assertEqual([d for _, _, d in self.sent], [DEV_A, DEV_B])

    def test_slow_device_does_not_block_other_device(self):
        gate = threading.Event()
        b_replied = threading.Event()

        class _SlowSession:
            def __init__(self, session_id):
                self.session_id = session_id

            def ask(self, text, context=None):
                if DEV_A in self.session_id:
                    gate.wait(5)
                return "好的"

        class _SlowBridge:
            def begin_session(self, session_id, system="", tools=None):
                return _SlowSession(session_id)

        sent = []

        def send(talker, content, device=""):
            sent.append(device)
            if device == DEV_B:
                b_replied.set()
            return True

        replier = WechatReplier(_SlowBridge(), send)
        replier.enqueue(mention(DEV_A, svr=1))
        replier.enqueue(mention(DEV_B, svr=2))
        try:
            self.assertTrue(b_replied.wait(2), "设备 B 的回复被设备 A 的慢调用堵住")
            self.assertEqual(sent, [DEV_B])
        finally:
            gate.set()

    def test_tools_only_see_own_device(self):
        self.store.add_message(mention(DEV_A, svr=1))
        self.store.add_message(mention(DEV_B, svr=2, room="2@chatroom"))
        self.store.add_message(mention(DEV_B, svr=3, room="2@chatroom"))
        self.replier.handle(mention(DEV_A, svr=9))
        self.replier.handle(mention(DEV_B, svr=9))
        a, b = self.bridge.sessions
        self.assertIn("累计消息 1 条", a.tools["room_stats"]({}))
        self.assertIn("累计消息 2 条", b.tools["room_stats"]({}))


class TestQuoteAgentPerDevice(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.agent = agent_at(self._tmp.name)
        self.item = quote()

    def tearDown(self):
        self._tmp.cleanup()

    def _process(self, rec, items=None):
        reply = json.dumps({"events": [self.item] if items is None else items}, ensure_ascii=False)
        with patch.object(self.agent, "_call_llm", return_value=reply):
            self.agent._process(rec)

    def test_same_message_on_two_devices_is_recorded_for_each(self):
        self._process(record(self.item["evidence"], 1, DEV_A))
        self._process(record(self.item["evidence"], 2, DEV_B))
        log = QuoteLog(self.agent.quotes_jsonl)
        self.assertEqual(len(log.list(0, 100, None, DEV_A)[0]), 1)
        self.assertEqual(len(log.list(0, 100, None, DEV_B)[0]), 1)
        # 两台设备的报价互不替代
        current = self.agent.ledger.current(ROOM)
        self.assertEqual(sorted(r["deviceId"] for r in current), [DEV_A, DEV_B])
        self.assertTrue(all(r["status"] == "active" for r in current))

    def test_context_and_known_quotes_do_not_cross_devices(self):
        payloads = []

        def fake(content):
            payloads.append(json.loads(content))
            return json.dumps({"events": [self.item]}, ensure_ascii=False)

        with patch.object(self.agent, "_call_llm", side_effect=fake):
            self.agent._process(record(self.item["evidence"], 1, DEV_A, svr=1))
            self.agent._process(record("调价+50", 2, DEV_B, svr=2, at=NOW + 1000))
            self.agent._process(record("调价+50", 3, DEV_A, svr=2, at=NOW + 1000))
        self.assertEqual(payloads[1]["相邻消息"], [])
        self.assertEqual(payloads[1]["已有报价"], [])
        self.assertEqual(len(payloads[2]["相邻消息"]), 1)
        self.assertEqual(len(payloads[2]["已有报价"]), 1)

    def test_markdown_is_split_per_device(self):
        self._process(record(self.item["evidence"], 1, DEV_A))
        self._process(record(self.item["evidence"], 2, DEV_B))
        self._process(record(self.item["evidence"], 3, ""))
        names = sorted(n for n in os.listdir(self.agent.quotes_dir) if n.endswith(".md"))
        self.assertEqual(names, sorted([f"{DEV_A}_{ROOM}.md", f"{DEV_B}_{ROOM}.md", f"{ROOM}.md"]))

    def test_larger_image_only_replaces_same_device(self):
        def image(seq, device, size):
            return {"seq": seq, "receivedAt": NOW, "imgFile": f"images/{seq}.jpg", "imgSize": size,
                    "msg": {"type": 3, "roomId": ROOM, "msgSvrId": "77", "deviceId": device}}

        self.agent._heap = []
        self.agent._cv = threading.Condition()
        self.agent._counter = itertools.count()
        self.agent.enqueue(image(1, DEV_A, 10))
        self.agent.enqueue(image(2, DEV_B, 20))  # B 的图各自排队，不顶掉 A 的
        self.agent.enqueue(image(3, DEV_A, 30))  # 同设备更大的版本才替换
        self.assertEqual(sorted(entry[2]["seq"] for entry in self.agent._heap), [2, 3])

    def test_reset_for_one_device_keeps_other_context(self):
        self._process(record(self.item["evidence"], 1, DEV_A))
        self._process(record(self.item["evidence"], 2, DEV_B))
        self.agent.reset_state(DEV_A)
        scopes = {scope[0] for scope, items in self.agent._context.items() if items}
        self.assertEqual(scopes, {DEV_B})

    def test_qa_tools_scoped_to_device(self):
        self._process(record(self.item["evidence"], 1, DEV_A))
        self._process(record(self.item["evidence"], 2, DEV_B))
        log = QuoteLog(self.agent.quotes_jsonl)
        scoped = {t["name"]: t["handler"] for t in build_qa_tools(None, log, device=DEV_A)}
        everyone = {t["name"]: t["handler"] for t in build_qa_tools(None, log)}
        rows = json.loads(scoped["current_quotes"]({}))["quotes"]
        self.assertEqual({r["deviceId"] for r in rows}, {DEV_A})
        self.assertEqual(len(json.loads(everyone["current_quotes"]({}))["quotes"]), 2)
        self.assertEqual(len(json.loads(scoped["quote_history"]({}))["events"]), 1)
        self.assertEqual(scoped["latest_quotes"]({}).count("\n"), 0)
        other = next(r for r in json.loads(everyone["current_quotes"]({}))["quotes"] if r["deviceId"] == DEV_B)
        self.assertEqual(scoped["quote_evidence"]({"quote_id": other["quoteId"]}), "没有找到该quoteId的证据。")


class TestLedgerPerDevice(unittest.TestCase):
    def test_adjustment_only_touches_its_own_device(self):
        ledger = QuoteLedger()
        item = quote()
        ledger.ingest([item], source(item["evidence"], 1, deviceId=DEV_A))
        ledger.ingest([item], source(item["evidence"], 1, deviceId=DEV_B))
        change = {"event_type": "adjustment", "供应商": "湖北桦昌", "certainty": "explicit",
                  "evidence": "湖北桦昌下调50", "details": {"delta": -50}}
        ledger.ingest([change], source(change["evidence"], 2, at=NOW + 1000, deviceId=DEV_A))
        amounts = {r["deviceId"]: r["details"]["amount"] for r in ledger.current()}
        self.assertEqual(amounts, {DEV_A: 110750, DEV_B: 110800})
        self.assertEqual(len(ledger.current(device=DEV_B)), 1)
        self.assertEqual(len(ledger.history(device=DEV_A)), 2)

    def test_legacy_event_ids_are_unchanged(self):
        item = quote()
        legacy = normalize_event(item, source(item["evidence"]), 0)["eventId"]
        self.assertEqual(legacy, "78b1f70a3633b0b7dde13179")  # 升级前已落盘的 events.jsonl 仍幂等
        self.assertEqual(normalize_event(item, source(item["evidence"], deviceId=""), 0)["eventId"], legacy)
        self.assertNotEqual(normalize_event(item, source(item["evidence"], deviceId=DEV_A), 0)["eventId"], legacy)


if __name__ == "__main__":
    unittest.main()
