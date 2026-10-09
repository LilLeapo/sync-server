"""Exercise Skill loading, extraction, persistence, QA, and staged backfill offline."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from server.quote_agent import QuoteAgent, backfill
from server.qa_tools import build_qa_tools
from server.store import QuoteLog
from tests.test_quote_rules import NOW, quote


def agent_at(root):
    agent = QuoteAgent.__new__(QuoteAgent)
    agent.data_dir = root
    agent.quotes_dir = os.path.join(root, "quotes")
    os.makedirs(agent.quotes_dir, exist_ok=True)
    agent.quotes_jsonl = os.path.join(agent.quotes_dir, "quotes.jsonl")
    agent.state_path = os.path.join(agent.quotes_dir, "state.json")
    agent._qseq = agent._max_qseq(agent.quotes_jsonl)
    agent._processed = set()
    agent._svr_state = {}
    agent.on_quote = []
    agent._log = lambda message: None
    agent._setup_analysis({})
    return agent


def record(text, seq=1, room="suppliers@chatroom"):
    return {"seq": seq, "receivedAt": NOW + seq * 1000, "msg": {"type": 1, "roomId": room,
            "sender": "buyer", "senderName": "采购", "createTime": NOW + seq * 1000,
            "msgSvrId": str(seq), "content": text}}


class TestPipeline(unittest.TestCase):
    def test_skill_events_conditions_survive_persistence_and_qa(self):
        with tempfile.TemporaryDirectory() as root:
            agent = agent_at(root)
            self.assertIn("87款", agent.system_prompt)
            item = quote(pickup_discount=150, pickup_region="湖北")
            item["evidence"] += "送到，自提减150"
            rec = record(item["evidence"])
            with patch.object(agent, "_call_llm", return_value=json.dumps({"events": [item]}, ensure_ascii=False)):
                agent._process(rec)
            log = QuoteLog(agent.quotes_jsonl)
            rows, _ = log.list()
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1]["details"]["amount"], 110650)
            tools = {tool["name"]: tool["handler"] for tool in build_qa_tools(None, log)}
            result = json.loads(tools["compare_quotes"]({"pickup_region": "湖北", "as_of": NOW + 2000}))
            self.assertEqual(result["groups"][0]["quotes"][0]["price"], 110650)
            proof = json.loads(tools["quote_evidence"]({"quote_id": rows[1]["quoteId"]}))
            self.assertEqual(proof["calculation"][0]["rule"], "pickup_discount")
            resumed = agent_at(root)
            with patch.object(resumed, "_call_llm", return_value=json.dumps({"events": [item]}, ensure_ascii=False)):
                resumed._process(rec)
            self.assertEqual(len(log.list()[0]), 2)
            with open(os.path.join(agent.quotes_dir, "suppliers@chatroom.md")) as stream:
                markdown = stream.read()
            self.assertEqual(markdown.count("<!-- quoteId:"), 2)

    def test_context_is_bounded_by_room_and_time(self):
        with tempfile.TemporaryDirectory() as root:
            agent = agent_at(root)
            captured = []
            def fake(content):
                captured.append(json.loads(content))
                return '{"events": []}'
            with patch.object(agent, "_call_llm", side_effect=fake):
                agent._process(record("存安无氧杆", 1))
                agent._process(record("调价+50", 2))
                agent._process(record("另一群", 3, "other@chatroom"))
                agent._process(record("很久以后", 3600))
            self.assertEqual(len(captured[1]["相邻消息"]), 1)
            self.assertEqual(captured[2]["相邻消息"], [])
            self.assertEqual(captured[3]["相邻消息"], [])

    def test_no_price_event_persisted_without_false_quote_push(self):
        with tempfile.TemporaryDirectory() as root:
            agent = agent_at(root)
            calls = []
            agent.on_quote = [calls.append]
            item = {"event_type": "stock", "供应商": "劲帆", "certainty": "explicit",
                    "evidence": "劲帆没货", "details": {"availability": "unavailable"}}
            with patch.object(agent, "_call_llm", return_value=json.dumps({"events": [item]}, ensure_ascii=False)):
                agent._process(record(item["evidence"]))
            self.assertEqual(len(agent.ledger.events), 1)
            self.assertEqual(calls, [])

    def test_failed_extraction_is_not_marked_processed(self):
        with tempfile.TemporaryDirectory() as root:
            agent = agent_at(root)
            with patch.object(agent, "_call_llm", return_value="invalid"):
                with self.assertRaises(ValueError):
                    agent._process(record("铜杆"))
            self.assertNotIn(1, agent._processed)

    def test_non_finite_model_output_is_rejected(self):
        with self.assertRaises(ValueError):
            QuoteAgent._parse_items('{"events": [{"details": {"amount": NaN}}]}')

    def test_failed_json_write_retries_markdown_without_duplicate_rows(self):
        with tempfile.TemporaryDirectory() as root:
            agent = agent_at(root)
            item = quote()
            rec = record(item["evidence"])
            response = json.dumps({"events": [item]}, ensure_ascii=False)
            with patch.object(agent, "_call_llm", return_value=response), patch.object(agent, "_append_jsonl", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    agent._process(rec)
            self.assertNotIn(1, agent._processed)
            with patch.object(agent, "_call_llm", return_value=response):
                agent._process(rec)
            with open(os.path.join(agent.quotes_dir, "suppliers@chatroom.md")) as stream:
                self.assertEqual(stream.read().count("<!-- quoteId:"), 1)
            self.assertEqual(len(QuoteLog(agent.quotes_jsonl).list()[0]), 1)

    def test_unknown_original_time_is_not_shown_as_forward_time_in_markdown(self):
        with tempfile.TemporaryDirectory() as root:
            agent = agent_at(root)
            item = quote()
            rec = record("<appmsg><type>40</type><des>" + item["evidence"] + "</des></appmsg>")
            rec["msg"]["type"] = 49
            with patch.object(agent, "_call_llm", return_value=json.dumps({"events": [item]}, ensure_ascii=False)):
                agent._process(rec)
            with open(os.path.join(agent.quotes_dir, "suppliers@chatroom.md")) as stream:
                self.assertIn("原始时间未知", stream.read())

    def test_quote_reader_resets_on_larger_atomic_replacement(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "quotes.jsonl")
            with open(path, "w") as stream:
                stream.write('{"qseq": 1, "价格": "110800"}\n')
            reader = QuoteLog(path)
            self.assertEqual(len(reader.list()[0]), 1)
            with open(path + ".tmp", "w") as stream:
                stream.write('{"qseq": 1, "价格": "110900"}\n{"qseq": 2, "价格": "110950"}\n')
            os.replace(path + ".tmp", path)
            self.assertEqual([row["价格"] for row in reader.list()[0]], ["110900", "110950"])

    def test_comparison_tool_replays_history_at_requested_time(self):
        with tempfile.TemporaryDirectory() as root:
            agent = agent_at(root)
            for seq, amount in ((1, 110800), (2, 110900)):
                item = quote(amount)
                with patch.object(agent, "_call_llm", return_value=json.dumps({"events": [item]}, ensure_ascii=False)):
                    agent._process(record(item["evidence"], seq))
            tools = {tool["name"]: tool["handler"] for tool in build_qa_tools(None, QuoteLog(agent.quotes_jsonl))}
            result = json.loads(tools["compare_quotes"]({"destination": "宜兴", "as_of": NOW + 1000}))
            self.assertEqual(result["groups"][0]["quotes"][0]["price"], 110800)

    def test_wechat_and_dingtalk_sessions_have_quote_tools(self):
        from server.dingtalk import DingTalkBot
        from server.wechat_reply import WechatReplier
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as root:
            bridge = Mock()
            log = QuoteLog(os.path.join(root, "quotes.jsonl"))
            wechat = WechatReplier(bridge, Mock(), quote_log=log)
            wechat._session("suppliers@chatroom")
            self.assertIn("compare_quotes", [tool["name"] for tool in bridge.begin_session.call_args.args[2]])
            dingtalk = DingTalkBot({}, bridge=bridge, quote_log=log, data_dir=root)
            dingtalk._get_session("conversation")
            self.assertIn("quote_evidence", [tool["name"] for tool in bridge.begin_session.call_args.args[2]])

    def test_backfill_uses_live_schema_and_aborts_without_replacing_existing_data(self):
        with tempfile.TemporaryDirectory() as root:
            item = quote()
            rec = record(item["evidence"])
            with open(os.path.join(root, "messages.jsonl"), "w") as stream:
                stream.write(json.dumps(rec, ensure_ascii=False) + "\n")
            os.makedirs(os.path.join(root, "quotes"))
            path = os.path.join(root, "quotes", "quotes.jsonl")
            with open(path, "w") as stream:
                stream.write("existing\n")
            config = {"enabled": True, "base_url": "unused", "api_key": "unused", "model": "unused"}
            with patch("server.quote_agent.load_config", return_value=config), patch.object(QuoteAgent, "_call_llm", return_value="invalid"):
                self.assertEqual(backfill("unused", root), 1)
            with open(path) as stream:
                self.assertEqual(stream.read(), "existing\n")
            with patch("server.quote_agent.load_config", return_value=config), patch.object(QuoteAgent, "_call_llm", return_value=json.dumps({"events": [item]}, ensure_ascii=False)):
                self.assertEqual(backfill("unused", root), 0)
            rows, _ = QuoteLog(path).list()
            self.assertEqual(rows[0]["schemaVersion"], 2)
            self.assertEqual(rows[0]["details"]["amount"], 110800)


if __name__ == "__main__":
    unittest.main()
