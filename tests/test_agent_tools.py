"""扩充的 Agent 工具：群名解析、聊天记录、供应商、计算器、联网工具（离线假传输）、发送工具、数据卡片与插件工具。"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import qa_tools  # noqa: E402
from server.bot_plugins import PluginHost  # noqa: E402
from server.net_tools import FRESH_MS, NetToolError, Tavily, build_net_tools, futures_quote, parse_symbol  # noqa: E402
from server.pi_bridge import ToolGate  # noqa: E402
from server.agent_context import ContextPolicy  # noqa: E402
from server.agent_prompts import compose_system  # noqa: E402
from server.cards import CardMaker  # noqa: E402
from server.qa_tools import build_qa_tools, calculate, resolve_room, tool_catalog  # noqa: E402
from server.quote_rules import QuoteLedger, compare_quotes, contract_key  # noqa: E402
from server.relay import RelayListener  # noqa: E402
from server.store import QuoteLog, Store  # noqa: E402
from server.wechat_reply import MAX_TURN_SENDS, WechatReplier  # noqa: E402
from tests.test_quote_rules import NOW, ingest, quote  # noqa: E402

DEV = "wxid_a-ANDROID_A"
SINA_CU = ('var hq_str_nf_CU2611="铜2611,113000,110000.000,110150.000,109400.000,0.000,109890.000,109920.000,'
           '109900.000,0.000,109450.000,8,1,169694.000,50621,沪,铜,2026-10-19,1,,,";')
SINA_LME = 'var hq_str_hf_CAD="14448.380,,14443.500,14447.500,14477.500,14371.000,11:52:22,14407.500,14372.500,0,1,2,2026-10-19,伦铜,1395";'


def tools_by_name(tools):
    return {t["name"]: t for t in tools}


def run(tools, tool_name, /, **params):
    """经 ToolGate 调用：与 Agent 真实路径一致（schema 规范化 + 截断）。"""
    text, _ = ToolGate(tools, ContextPolicy()).run(tool_name, params)
    return text


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.store = Store(self.root)
        self.quote_log = QuoteLog(os.path.join(self.root, "quotes", "quotes.jsonl"))
        for i, (room, name, sender, text) in enumerate([
            ("1@chatroom", "富利铜业报价群", "陈工", "无氧杆 2611合约+500"),
            ("1@chatroom", "富利铜业报价群", "王总", "今天出货吗"),
            ("2@chatroom", "铝锭交流群", "陈工", "铝锭现货 23000"),
            ("3@chatroom", "富利铜业二群", "李四", "收到"),
        ]):
            self.store.add_message({"roomId": room, "roomName": name, "sender": "wxid_" + str(i), "senderName": sender,
                                    "content": text, "type": 1, "msgSvrId": 100 + i, "createTime": 4_000_000_000_000 + i,
                                    "deviceId": DEV})

    def tearDown(self):
        self._tmp.cleanup()


class TestRoomsAndMessages(_StoreCase):
    def test_resolve_room_by_id_name_fuzzy_and_ambiguity(self):
        self.assertEqual(resolve_room(self.store, "2@chatroom", DEV), ("2@chatroom", None))
        self.assertEqual(resolve_room(self.store, "铝锭交流群", DEV), ("2@chatroom", None))
        self.assertEqual(resolve_room(self.store, "富利铜业报价群", DEV), ("1@chatroom", None))
        room, err = resolve_room(self.store, "富利铜业", DEV)
        self.assertIsNone(room)
        self.assertIn("多个群", err)
        self.assertIn("没有找到", resolve_room(self.store, "不存在的群", DEV)[1])
        self.assertEqual(resolve_room(self.store, "9@chatroom", DEV), ("9@chatroom", None))  # 报价库可能有、消息库没有
        self.assertIsNotNone(resolve_room(self.store, "9@chatroom", DEV, must_exist=True)[1])
        self.assertEqual(resolve_room(None, "铝锭交流群"), ("铝锭交流群", None))

    def test_list_recent_and_search_messages_are_device_scoped(self):
        tools = build_qa_tools(self.store, self.quote_log, device=DEV)
        self.assertIn("富利铜业报价群｜1@chatroom", run(tools, "list_rooms"))
        recent = run(tools, "recent_messages", room="富利铜业报价群", limit="5")
        self.assertLess(recent.index("无氧杆"), recent.index("今天出货吗"))
        found = run(tools, "search_messages", keyword="现货", days=3650)
        self.assertIn("【铝锭交流群】", found)
        self.assertIn("铝锭现货", found)
        other = build_qa_tools(self.store, self.quote_log, device="other")
        self.assertIn("没有", run(other, "search_messages", keyword="现货", days=3650))
        self.assertIn("必填", run(tools, "recent_messages"))

    def test_quote_tools_accept_room_names(self):
        ingest(QuoteLedger(os.path.join(self.root, "quotes", "events.jsonl")), quote())
        tools = build_qa_tools(self.store, self.quote_log, device=None)
        out = run(tools, "current_quotes", room="铝锭交流群")
        self.assertEqual(json.loads(out)["quotes"], [])
        self.assertIn("多个群", run(tools, "current_quotes", room="富利铜业"))


class TestSuppliersAndCalculator(unittest.TestCase):
    def test_list_suppliers_groups_current_quotes(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = QuoteLedger(os.path.join(root, "events.jsonl"))
            ingest(ledger, quote())
            tools = build_qa_tools(None, QuoteLog(os.path.join(root, "quotes.jsonl")))
            data = json.loads(run(tools, "list_suppliers"))
            self.assertEqual(len(data["suppliers"]), 1)
            row = data["suppliers"][0]
            self.assertGreaterEqual(row["active"] + row["needs_confirmation"], 1)
            self.assertTrue(row["products"])

    def test_calculate(self):
        self.assertEqual(calculate("(109900-300)*25.5"), "(109900-300)*25.5 = 2794800")
        self.assertTrue(calculate("1,000 × 5%").endswith("= 50"))
        self.assertTrue(calculate("1/3").endswith("0.33333333"))
        for bad in ("2**100", "1/0", "__import__('os')", "a+1", "[1]"):
            self.assertIn("无法计算", calculate(bad))


class TestNetTools(unittest.TestCase):
    def test_parse_symbol(self):
        self.assertEqual(parse_symbol("沪铜2611"), {"code": "nf_CU2611", "name": "沪铜", "contract": "2611"})
        self.assertEqual(parse_symbol("cu0")["contract"], "主力连续")
        self.assertEqual(parse_symbol("沪铝主力")["code"], "nf_AL0")
        self.assertEqual(parse_symbol("LME铜")["code"], "hf_CAD")
        with self.assertRaises(NetToolError):
            parse_symbol("比特币")

    def test_futures_quote_benchmark_and_freshness(self):
        at = 1792380600000  # 2026-10-19 11:30:00+08:00
        out = futures_quote("CU2611", fetch=lambda code: SINA_CU, now_ms=at + 60_000)
        self.assertEqual((out["price"], out["at"], out["fresh"]), (109900.0, "2026-10-19T11:30:00+08:00", True))
        self.assertEqual(out["benchmark"], {"name": "沪铜", "contract": "2611", "price": 109900.0,
                                            "at": out["at"], "source": "新浪财经 hq.sinajs.cn nf_CU2611"})
        self.assertEqual(out["change"], 450.0)
        stale = futures_quote("CU2611", fetch=lambda code: SINA_CU, now_ms=at + FRESH_MS + 1)
        self.assertFalse(stale["fresh"])
        lme = futures_quote("伦铜", fetch=lambda code: SINA_LME, now_ms=at)
        self.assertNotIn("benchmark", lme)
        self.assertEqual(lme["price"], 14448.38)
        with self.assertRaises(NetToolError):
            futures_quote("CU2512", fetch=lambda code: 'var hq_str_nf_CU2512="";')

    def test_futures_benchmark_feeds_compare_quotes(self):
        """futures_quote 的 benchmark（合约 2611）与提取出的 contract（CU2611 写法也行）能匹配。"""
        self.assertEqual(contract_key("CU2611"), contract_key("沪铜 2611"))
        self.assertEqual(contract_key("2611"), "2611")
        ledger = QuoteLedger()
        rows = ingest(ledger, quote(pricing="futures", amount=None, premium=-300, benchmark="沪铜", contract="CU2610"))
        params = {"destination": "宜兴", "as_of": NOW,
                  "benchmark": {"name": "沪铜", "contract": "2610", "price": 111000, "at": NOW - 60000, "source": "新浪"}}
        result = compare_quotes(rows, params)
        prices = [q["price"] for g in result["groups"] for q in g["quotes"]]
        self.assertTrue(prices and all(p is not None for p in prices), result)

    def test_tavily_search_and_extract_tools(self):
        calls = []

        def post(path, body):
            calls.append((path, body))
            if path == "/search":
                return {"results": [{"title": "沪铜收涨", "url": "https://x.test/a", "content": "铜价" * 600,
                                     "published_date": "2026-10-19"}]}
            return {"results": [{"url": body["urls"][0], "raw_content": "正文" * 3000}]}

        tools = tools_by_name(build_net_tools({"futures": {"enabled": False}}, tavily=Tavily("k", post=post)))
        self.assertEqual(set(tools), {"web_search", "web_extract"})
        data = json.loads(run(list(tools.values()), "web_search", query="沪铜", max_results=20, days=2))
        self.assertEqual(calls[0][1]["max_results"], 8)
        self.assertEqual(calls[0][1]["topic"], "news")
        self.assertEqual(len(data["results"][0]["content"]), 800)
        page = json.loads(run(list(tools.values()), "web_extract", url="https://x.test/a"))
        self.assertEqual(page["truncated"], 6000)
        self.assertEqual(build_net_tools({}, fetch=lambda c: SINA_CU)[0]["name"], "futures_quote")  # 无 key 不给搜索

    def test_configure_injects_net_and_plugin_tools(self):
        host = PluginHost("/nonexistent")
        from server.tool_schema import S
        host.api.register_agent_tool("double", lambda p, ctx: str(p["n"] * 2 + (ctx["device"] == DEV)),
                                     parameters=S.object({"n": S.integer().required()}))
        host.api.register_agent_tool("secret_op", lambda p, ctx: "x", opt_in=True)
        host.api.register_agent_tool("search_quotes", lambda p, ctx: "hijack")  # 不能覆盖内置工具
        try:
            qa_tools.configure(plugin_tools=list(host.agent_tools.values()),
                               net_tools=build_net_tools({}, fetch=lambda c: SINA_CU))
            tools = build_qa_tools(None, None, device=DEV)
            names = [t["name"] for t in tools]
            self.assertIn("futures_quote", names)
            self.assertNotIn("secret_op", names)
            self.assertEqual(names.count("search_quotes"), 1)
            self.assertEqual(run(tools, "double", n="4"), "9")
            self.assertIn("secret_op", {t["name"]: t["optIn"] for t in tool_catalog() if t["optIn"]})
        finally:
            qa_tools.configure(net_tools=[], plugin_tools=[])


class _SendCase(_StoreCase):
    def setUp(self):
        super().setUp()
        self.sent = []
        self.caps = set()

        self.svr = 900

        def send(talker, content, device_id="", image_path=None, image_url=None):
            if talker != "1@chatroom":  # 只记代发；对 @ 本身的回复发回 1@chatroom
                self.sent.append((talker, content, device_id, image_path, image_url))
            return f"send-{len(self.sent)}"

        self.replier = WechatReplier(_ToolBridge(self), send, store=self.store, device_caps=lambda d: self.caps)
        self.replier.settings = _Settings(["send_message", "send_image", "list_rooms"])

    def ask_with(self, calls):
        self.calls = calls
        self.svr += 1
        return self.replier.handle({"roomId": "1@chatroom", "roomName": "富利铜业报价群", "sender": "wxid_boss",
                                    "senderName": "老板", "content": "@机器人 把报价转到铝锭群", "type": 1,
                                    "msgSvrId": self.svr, "atMe": True, "deviceId": DEV})


class TestSendTools(_SendCase):
    def test_send_message_and_image_with_limits_and_log(self):
        self.ask_with([("send_message", {"room": "铝锭交流群", "text": "今日报价见群公告"}),
                       ("send_image", {"room": "2@chatroom", "image_path": "/sdcard/DCIM/a.jpg"}),
                       ("send_image", {"room": "2@chatroom", "image_path": "/etc/passwd"}),
                       ("send_message", {"room": "不存在的群", "text": "x"})])
        self.assertEqual(self.sent[0], ("2@chatroom", "今日报价见群公告", DEV, None, None))
        self.assertEqual(self.sent[1], ("2@chatroom", "", DEV, "/sdcard/DCIM/a.jpg", None))
        self.assertIn("/sdcard/", self.results[2])
        self.assertIn("没有找到", self.results[3])
        rows = self.replier.log.list()[0]
        sends = [r for r in rows if r["kind"] == "agent_send"]
        self.assertEqual([(r["roomId"], r["status"]) for r in sends], [("2@chatroom", "queued")] * 2)
        self.assertIn("Agent 代发", sends[0]["question"])

        self.sent.clear()
        self.ask_with([("send_message", {"room": "2@chatroom", "text": str(i)}) for i in range(MAX_TURN_SENDS + 1)])
        self.assertEqual(len(self.sent), MAX_TURN_SENDS)
        self.assertIn("上限", self.results[-1])

    def test_server_image_requires_device_caps(self):
        with open(os.path.join(self.root, "images", "q.jpg"), "wb") as f:
            f.write(b"\xff\xd8\xff")
        self.ask_with([("send_image", {"room": "2@chatroom", "server_image": "images/q.jpg"})])
        self.assertEqual(self.sent, [])
        self.assertIn("imageUrl", self.results[0])
        self.caps = {"imageUrl"}
        self.ask_with([("send_image", {"room": "2@chatroom", "server_image": "images/q.jpg"}),
                       ("send_image", {"room": "2@chatroom", "server_image": "images/none.jpg"})])
        self.assertEqual(self.sent[0][4], "/images/q.jpg")
        self.assertIn("没有图片", self.results[1])

    def test_send_tools_only_when_explicitly_enabled(self):
        self.replier.settings = _Settings(None)
        self.ask_with([("send_message", {"room": "2@chatroom", "text": "x"})])
        self.assertIn("没有名为 send_message", self.results[0])
        self.assertEqual(self.sent, [])


CARD = {"room": "铝锭交流群", "title": "铝锭报价",
        "sections": [{"columns": [{"name": "供应商"}, {"name": "价格", "kind": "number", "unit": "元/吨"}],
                      "rows": [["陈工", 23000], ["李四", "电议"]]}]}


class TestSendCard(_SendCase):
    def setUp(self):
        super().setUp()
        self.renders = 0
        qa_tools.configure(net_tools=[], card_maker=CardMaker(self.render))
        self.addCleanup(qa_tools.configure, net_tools=[], plugin_tools=[])
        self.replier.settings = _Settings(["send_card", "list_rooms"])
        self.caps = {"imageUrl"}

    def render(self, html, element_id):
        self.renders += 1
        if "制图会失败" in html:
            raise RuntimeError("超过 20 秒没有完成")
        return b"\x89PNG\r\n\x1a\nfake"

    def agent_sends(self):
        return [r for r in self.replier.log.list()[0] if r["kind"] == "agent_send"]

    def test_card_reaches_downlink_as_server_image(self):
        self.ask_with([("send_card", CARD)])
        [(room, text, device, image_path, image_url)] = self.sent
        self.assertEqual((room, text, device, image_path), ("2@chatroom", "", DEV, None))
        self.assertRegex(image_url, r"^/images/cards/\d{8}/[0-9a-f]{20}\.png$")
        self.assertTrue(self.store.image_path(image_url.lstrip("/")), "png file exists under the data dir")
        self.assertIn("已排队发送", self.results[0])
        self.assertEqual([(r["roomId"], r["status"]) for r in self.agent_sends()], [("2@chatroom", "queued")])

    def test_bad_card_is_neither_drawn_nor_sent(self):
        bad = {**CARD, "sections": [{**CARD["sections"][0], "rows": [["陈工"]]}]}
        self.ask_with([("send_card", bad)])
        self.assertEqual((self.sent, self.renders), ([], 0))
        self.assertIn("卡片格式不对：第1节第1行有 1 格，但 columns 有 2 列。没有制图，也没有发送。", self.results[0])

    def test_device_without_image_url_caps_is_refused_before_drawing(self):
        self.caps = set()
        self.ask_with([("send_card", CARD)])
        self.assertEqual((self.sent, self.renders), ([], 0))
        self.assertIn("imageUrl", self.results[0])
        self.assertNotIn("images/cards", self.results[0])

    def test_turn_cap_applies_to_cards_before_drawing(self):
        self.ask_with([("send_card", {**CARD, "title": f"铝锭报价{i}"}) for i in range(MAX_TURN_SENDS + 1)])
        self.assertEqual((len(self.sent), self.renders), (MAX_TURN_SENDS, MAX_TURN_SENDS))
        self.assertIn("上限", self.results[-1])
        self.assertNotIn("images/cards", self.results[-1])

    def test_failed_drawing_uses_no_quota_and_leaves_no_log(self):
        self.replier.settings = _Settings(["send_card", "send_message", "list_rooms"])
        self.ask_with([("send_card", {**CARD, "title": "制图会失败"})]
                      + [("send_message", {"room": "2@chatroom", "text": str(i)}) for i in range(MAX_TURN_SENDS)])
        self.assertIn("制图失败：超过 20 秒没有完成", self.results[0])
        self.assertEqual([text for _, text, *_ in self.sent], [str(i) for i in range(MAX_TURN_SENDS)])
        self.assertEqual(sorted(r["reply"] for r in self.agent_sends()), [str(i) for i in range(MAX_TURN_SENDS)])

    def test_only_when_ticked_and_configured(self):
        self.replier.settings = _Settings(None)
        self.ask_with([("send_card", CARD)])
        self.assertIn("没有名为 send_card", self.results[0])
        self.assertEqual({t["name"]: t["optIn"] for t in tool_catalog()}.get("send_card"), True)
        tools = build_qa_tools(self.store, self.quote_log, device=DEV, sender=lambda *a, **k: "", opt_in=True)
        self.assertIn("要把报价、比较结果或行情做成图时用 send_card", compose_system("角色", tools))

        qa_tools.configure(net_tools=[])
        self.replier.settings = _Settings(["send_card", "list_rooms"])
        self.ask_with([("send_card", CARD)])
        self.assertIn("没有名为 send_card", self.results[0], "a ticked send_card disappears without a maker")
        self.assertNotIn("send_card", {t["name"] for t in tool_catalog()})
        self.assertEqual(self.sent, [])


class _Settings:
    def __init__(self, tools):
        self.tools = tools

    def get(self, device_id):
        return {"reply": True, "quotes": True, "prompt": "", "tools": self.tools}


class _ToolBridge:
    """会话 ask 时依次执行测试给定的工具调用（经 ToolGate），结果存到 case.results。"""

    def __init__(self, case):
        self.case = case

    def begin_session(self, session_id, system="", tools=None):
        case = self.case

        class _S:
            def ask(self, text, context=None):
                gate = ToolGate(tools or [], ContextPolicy())
                case.results = [gate.run(name, params)[0] for name, params in case.calls]
                return "处理好了"
        return _S()


class TestRelayImageUrl(unittest.TestCase):
    def test_image_url_only_to_devices_declaring_caps(self):
        with tempfile.TemporaryDirectory() as d:
            listener = RelayListener("127.0.0.1", 0, Store(d))
            self.assertEqual(listener.send("1@chatroom", "", DEV, image_url="/images/a.jpg"), "")
            listener._clients[object()] = {"device_id": DEV, "caps": ["imageUrl"]}
            self.assertEqual(listener.caps(DEV), {"imageUrl"})
            self.assertTrue(listener.send("1@chatroom", "", DEV, image_url="/images/a.jpg"))
            item = listener._next_send(DEV)
            self.assertEqual(item["imageUrl"], "/images/a.jpg")
            self.assertEqual(listener.send("1@chatroom", "", None, image_url="/images/a.jpg"), "")


if __name__ == "__main__":
    unittest.main()
