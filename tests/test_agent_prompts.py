"""问答提示词分层组装：领域知识不进 system prompt、按工具生成要点、Skill 按需读取、群背景剔除自己的回复。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.agent_prompts import ANSWER_RULES, QNA_SYSTEM, compose_system, wechat_base_prompt  # noqa: E402
from server.pi_bridge import PiBridge  # noqa: E402
from server.qa_tools import build_qa_tools  # noqa: E402
from server.quote_skill import quote_guidance  # noqa: E402
from server.skills import list_skills, read_skill  # noqa: E402
from server.store import Store  # noqa: E402
from server.wechat_reply import WechatReplier  # noqa: E402
from tests.test_agent_tools import run  # noqa: E402

SKILL = "supplier-quote-analysis"


class TestComposeSystem(unittest.TestCase):
    def test_default_session_prompt_is_short_and_not_quote_centric(self):
        tools = build_qa_tools(None, None)
        system = compose_system(QNA_SYSTEM, tools)
        self.assertTrue(system.startswith(QNA_SYSTEM))
        self.assertIn(ANSWER_RULES, system)
        self.assertIn(SKILL, system)  # 只有索引
        self.assertNotIn("87款", system)
        self.assertNotIn("飞书", QNA_SYSTEM)
        # 领域知识全文远大于整个 system prompt：以前是整段拼进去的
        self.assertLess(len(system), len(quote_guidance()) / 3)

    def test_hints_follow_actual_tools(self):
        self.assertNotIn("工具使用要点", compose_system("角色", []))
        self.assertNotIn("read_skill", compose_system("角色", []))
        only_stats = compose_system("角色", [{"name": "room_stats"}])
        self.assertNotIn("current_quotes", only_stats)
        full = compose_system("角色", [{"name": n} for n in ("compare_quotes", "futures_quote", "send_message")])
        self.assertIn("futures_quote", full)
        self.assertIn("明确要求发送", full)

    def test_bridge_composes_on_begin_session(self):
        bridge = PiBridge(object(), "http")
        conv = bridge.begin_session("s", "你是物流售后。", [{"name": "read_skill", "handler": str}])
        self.assertTrue(conv.system.startswith("你是物流售后。"))
        self.assertIn(SKILL, conv.system)
        self.assertIn("回答规则", wechat_base_prompt())


class TestSkills(unittest.TestCase):
    def test_list_and_read_on_demand(self):
        skill = next(s for s in list_skills() if s["name"] == SKILL)
        self.assertIn("references/phrases-and-cases.md", skill["files"])
        body = read_skill(SKILL)
        self.assertIn("# 供应商报价解读", body)
        self.assertNotIn("description:", body)
        self.assertIn("references/phrases-and-cases.md", body)
        self.assertIn("87款", read_skill(SKILL, "references/phrases-and-cases.md"))
        self.assertIn("没有", read_skill(SKILL, "../../../server/pi_bridge.py"))
        self.assertIn("没有名为", read_skill("nope"))

    def test_read_skill_tool_fits_result_cap(self):
        tools = build_qa_tools(None, None)
        self.assertIn("供应商报价解读", run(tools, "read_skill", name=SKILL))
        ref = run(tools, "read_skill", name=SKILL, file="references/phrases-and-cases.md")
        self.assertNotIn("已截断", ref)
        self.assertIn("参数错误", run(tools, "read_skill", name="other"))


class _Bridge:
    def __init__(self):
        self.contexts = []

    def begin_session(self, session_id, system="", tools=None):
        bridge = self

        class _S:
            def ask(self, text, context=None):
                bridge.contexts.append(context)
                return "沪铜今天还没有报价。"
        return _S()


class TestOwnRepliesLeftOutOfGroupContext(unittest.TestCase):
    def test_bot_reply_echoed_by_module_is_not_background(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(d)
            bridge = _Bridge()
            replier = WechatReplier(bridge, lambda t, c, dev="": "sid", store=store)

            def mention(svr, at):
                msg = {"roomId": "1@chatroom", "sender": "wxid_u", "senderName": "张三", "content": "@你好 在吗",
                       "type": 1, "msgSvrId": svr, "createTime": at, "atMe": True, "deviceId": "dev"}
                store.add_message(msg)
                return msg

            replier.handle(mention(1, 1000))
            # 模块把机器人发出去的回复也上报了（空白略有不同）
            store.add_message({"roomId": "1@chatroom", "sender": "wxid_bot", "content": "沪铜今天 还没有报价。",
                               "type": 1, "msgSvrId": 2, "createTime": 1100, "deviceId": "dev"})
            store.add_message({"roomId": "1@chatroom", "sender": "wxid_v", "senderName": "李四", "content": "那铝呢",
                               "type": 1, "msgSvrId": 3, "createTime": 1200, "deviceId": "dev"})
            replier.handle(mention(4, 1300))
            ctx = bridge.contexts[-1]
            self.assertIn("那铝呢", ctx)
            self.assertNotIn("还没有报价", ctx)


if __name__ == "__main__":
    unittest.main()
