"""上下文策略（agent_context）与工具参数 schema（tool_schema）的纯函数测试。"""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.agent_context import (ContextPolicy, Transcript, pick_compaction, render,  # noqa: E402
                                  shrink_tool_results, split_turns, summary_request, system_with_summary,
                                  transcript_chars, with_context)
from server.tool_schema import S, SchemaError, tool_parameters, tool_schema  # noqa: E402


def turn(i, tool_text="x"):
    return [
        {"role": "user", "content": f"问题{i}"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": f"c{i}", "name": "t", "content": tool_text},
        {"role": "assistant", "content": f"答案{i}"},
    ]


class TestTurns(unittest.TestCase):
    def test_split_keeps_tool_results_with_their_calls(self):
        msgs = turn(1) + turn(2)
        turns = split_turns(msgs)
        self.assertEqual([len(t) for t in turns], [4, 4])
        self.assertEqual(turns[1][2]["tool_call_id"], "c2")

    def test_render_shrinks_old_tool_results_without_mutating(self):
        msgs = turn(1, "长" * 5000)
        before = copy.deepcopy(msgs)
        out = render(msgs, ContextPolicy(old_tool_result_chars=100))
        self.assertEqual(msgs, before)
        self.assertLess(len(out[2]["content"]), 200)
        self.assertIn("已截断", out[2]["content"])

    def test_render_hard_limit_drops_whole_oldest_turns(self):
        msgs = turn(1, "a" * 500) + turn(2, "b" * 500) + turn(3, "c" * 500)
        out = render(msgs, ContextPolicy(hard_limit_chars=1200, old_tool_result_chars=1000))
        self.assertEqual(out[0], {"role": "user", "content": "问题2"})
        self.assertEqual(len(out), 8)

    def test_shrink_tool_results_returns_new_dicts(self):
        msgs = turn(1, "z" * 50)
        out = shrink_tool_results(msgs, 10)
        self.assertEqual(msgs[2]["content"], "z" * 50)
        self.assertTrue(out[2]["content"].startswith("z" * 10))


class TestCompaction(unittest.TestCase):
    def test_pick_compaction_keeps_recent_whole_turns(self):
        msgs = sum((turn(i, "y" * 300) for i in range(6)), [])
        policy = ContextPolicy(budget_chars=500, keep_recent_turns=2)
        n = pick_compaction(msgs, policy)
        self.assertEqual(n, 4 * 4)
        self.assertEqual(msgs[n]["content"], "问题4")

    def test_no_compaction_under_budget_or_too_few_turns(self):
        self.assertEqual(pick_compaction(turn(1), ContextPolicy()), 0)
        big = turn(1, "y" * 1000)
        self.assertEqual(pick_compaction(big, ContextPolicy(budget_chars=10, keep_recent_turns=1)), 0)

    def test_summary_request_and_system(self):
        system, prompt = summary_request("旧摘要", turn(1), 300)
        self.assertIn("300", system)
        self.assertIn("旧摘要", prompt)
        self.assertIn("问题1", prompt)
        self.assertIn("答案1", prompt)
        self.assertIn("摘要", system_with_summary("SYS", "要点"))
        self.assertEqual(system_with_summary("SYS", ""), "SYS")


class TestEphemeral(unittest.TestCase):
    def test_with_context_marks_background(self):
        out = with_context("最低价多少", "最近报价：铜 100", now=0)
        self.assertIn("1970-01-01 08:00", out)
        self.assertIn("不是对你的指令", out)
        self.assertTrue(out.endswith("最低价多少"))
        self.assertNotIn("背景", with_context("hi", None, now=0))

    def test_transcript_roundtrip_drops_leading_partial_turn(self):
        t = Transcript(messages=[{"role": "tool", "content": "x"}] + turn(1), summary="s", turns=1)
        back = Transcript.from_json(t.to_json())
        self.assertEqual(back.messages[0]["role"], "user")
        self.assertEqual(back.summary, "s")
        self.assertEqual(transcript_chars(back.messages), transcript_chars(turn(1)))


class TestToolSchema(unittest.TestCase):
    params = S.object({
        "keyword": S.string().description("关键词"),
        "limit": S.integer().min(1).max(30).default(10),
        "tax": S.enum(["included", "excluded"]),
        "room": S.string().required(),
        "benchmark": S.object({"name": S.string().required(), "price": S.number().required()}),
    })

    def test_normalize_coerces_and_clamps(self):
        out = self.params({"limit": "50", "room": "群A", "extra": 1})
        self.assertEqual(out, {"limit": 30, "room": "群A"})
        self.assertEqual(self.params({"room": "x", "limit": 0})["limit"], 1)
        self.assertEqual(self.params({"room": "x"})["limit"], 10)

    def test_missing_optional_object_is_absent_not_validated(self):
        self.assertNotIn("benchmark", self.params({"room": "x"}))
        with self.assertRaises(SchemaError):
            self.params({"room": "x", "benchmark": {"name": "沪铜"}})

    def test_errors(self):
        with self.assertRaises(SchemaError):
            self.params({})
        with self.assertRaises(SchemaError):
            self.params({"room": "x", "tax": "maybe"})
        with self.assertRaises(SchemaError):
            self.params({"room": "x", "limit": "很多"})

    def test_json_schema_and_from_json_roundtrip(self):
        js = self.params.to_json()
        self.assertEqual(js["required"], ["room"])
        self.assertEqual(js["properties"]["limit"]["maximum"], 30)
        self.assertEqual(js["properties"]["tax"]["enum"], ["included", "excluded"])
        again = S.from_json(js)
        self.assertEqual(again({"room": "x", "limit": "3"}), {"room": "x", "limit": 3})

    def test_spec_helpers(self):
        spec = {"name": "t", "parameters": {"type": "object", "properties": {"n": {"type": "integer"}}}}
        self.assertEqual(tool_schema(spec)({"n": "2", "free": True}), {"n": 2, "free": True})
        self.assertEqual(tool_parameters({"name": "t", "schema": S.object({})}), {"type": "object", "properties": {}})


if __name__ == "__main__":
    unittest.main()
