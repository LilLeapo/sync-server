"""pi_bridge 测试：mock 后端会话、http 后端、配置选择与降级。"""
import copy
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import pi_bridge  # noqa: E402
from server.pi_bridge import PiBridge, fallback_format  # noqa: E402
from server.qa_tools import build_qa_tools
from server.quote_rules import QuoteLedger
from server.store import QuoteLog
from tests.test_quote_rules import NOW, ingest, quote

HAS_PI = False
try:
    import pi_agent  # noqa: F401
    HAS_PI = True
except ImportError:
    pass


class _LLMHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        req = json.loads(raw)
        want_json = req.get("response_format", {}).get("type") == "json_object"
        text = '{"has_quote": true, "items": [{"项目": "苹果"}]}' if want_json else "你好"
        body = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": text}}]
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class _ToolLLMHandler(_LLMHandler):
    requests = []

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append(req)
        tool_results = [msg for msg in req["messages"] if msg["role"] == "tool"]
        if tool_results:
            result = json.loads(tool_results[-1]["content"])
            message = {"role": "assistant", "content": str(result["groups"][0]["quotes"][0]["price"])}
        else:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "compare-1", "type": "function", "function": {
                    "name": "compare_quotes", "arguments": json.dumps({"destination": "宜兴", "as_of": NOW})}}]}
        body = json.dumps({"choices": [{"message": message}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TestFallbackFormat(unittest.TestCase):
    def test_format(self):
        rows = [{"项目": "苹果", "价格": "¥5", "单位": "元/斤", "数量": "10",
                 "备注": "包邮", "sender": "张三", "timeLabel": "03-01 10:00"}]
        out = fallback_format("供销群", rows)
        self.assertIn("📊 新报价 · 供销群", out)
        self.assertIn("苹果", out)
        self.assertIn("¥5", out)
        self.assertIn("×10", out)
        self.assertIn("张三", out)


class TestHttpBackend(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _LLMHandler)
        cls.url = f"http://127.0.0.1:{cls.httpd.server_address[1]}/v1"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_complete_and_json(self):
        bridge = PiBridge.from_config({
            "enabled": True, "provider": "http", "base_url": self.url,
            "api_key": "k", "model": "m"})
        self.assertIsNotNone(bridge)
        self.assertEqual(bridge.backend_kind, "http")
        self.assertEqual(bridge.complete_chat("sys", "hi"), "你好")
        data = bridge.complete_json("sys", "提取")
        self.assertEqual(data["has_quote"], True)

    def test_auto_prefers_http_when_sdk_absent(self):
        bridge = PiBridge.from_config({
            "enabled": True, "provider": "auto", "base_url": self.url,
            "api_key": "k", "model": "m"})
        self.assertIsNotNone(bridge)
        # SDK 已装则 pi，未装则 http —— 两种情况都可用
        self.assertIn(bridge.backend_kind, ("pi", "http"))

    def test_http_session_executes_real_comparison_tool_and_returns_result_to_model(self):
        _ToolLLMHandler.requests = []
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _ToolLLMHandler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as root:
                ingest(QuoteLedger(os.path.join(root, "events.jsonl")), quote())
                bridge = PiBridge.from_config({"provider": "http", "base_url": f"http://127.0.0.1:{httpd.server_address[1]}",
                                               "api_key": "test", "model": "test"})
                session = bridge.begin_session("compare", "用户自定义提示词", build_qa_tools(None, QuoteLog(os.path.join(root, "quotes.jsonl"))))
                self.assertEqual(session.ask("比较到宜兴的价格"), "110800.0")
                first, second = _ToolLLMHandler.requests
                self.assertIn("87款", first["messages"][0]["content"])
                self.assertIn("用户自定义提示词", first["messages"][0]["content"])
                self.assertIn("compare_quotes", [tool["function"]["name"] for tool in first["tools"]])
                tool_message = next(msg for msg in second["messages"] if msg["role"] == "tool")
                self.assertEqual(tool_message["tool_call_id"], "compare-1")
                self.assertEqual(json.loads(tool_message["content"])["groups"][0]["quotes"][0]["price"], 110800)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_http_tool_argument_errors_and_round_limit_are_bounded(self):
        from unittest.mock import Mock
        backend = Mock(spec=["complete_response"])
        backend.complete_response.return_value = {"content": None, "tool_calls": [{
            "id": "bad-1", "function": {"name": "test_tool", "arguments": "[]"}}]}
        handler = Mock()
        bridge = PiBridge(backend, "http")
        session = bridge.begin_session("bounded", tools=[{
            "name": "test_tool", "handler": handler, "parameters": {"type": "object"}}])
        self.assertIn("上限", session.ask("run"))
        # 每次调用都算进上限；用尽后再发一次不带工具的请求收尾
        limit = bridge.policy.max_tool_calls
        self.assertEqual(backend.complete_response.call_count, limit + 1)
        self.assertIsNone(backend.complete_response.call_args_list[-1].kwargs["tools"])
        self.assertTrue(all(c.kwargs["tools"] for c in backend.complete_response.call_args_list[:-1]))
        handler.assert_not_called()
        self.assertTrue(any("tool arguments must be an object" in msg.get("content", "")
                            for msg in session.history if msg["role"] == "tool"))
        # 收尾的回复不带悬空的 tool_calls
        self.assertEqual(session.history[-1], {"role": "assistant", "content": session.history[-1]["content"]})


class _ScriptLLM(BaseHTTPRequestHandler):
    """可编排的假模型：script(req) 返回 assistant message dict；记录每次请求。"""
    script = None
    requests: list = []
    delay = 0

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append(req)
        if type(self).delay:
            time.sleep(type(self).delay)
        message = type(self).script(req)
        if message is None:
            body = b'{"error": {"message": "boom"}}'
            self.send_response(500)
        else:
            body = json.dumps({"choices": [{"message": message, "finish_reason": "stop"}],
                               "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}).encode()
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _call(name, args, cid="call-1"):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


@unittest.skipUnless(HAS_PI, "pi-agent SDK 未安装")
class TestPiRunnerOverHttp(unittest.TestCase):
    """provider=pi：pi 的 Agent 循环 + 标准库传输（不需要 openai 包）。"""

    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _ScriptLLM)
        cls.url = f"http://127.0.0.1:{cls.httpd.server_address[1]}/v1"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        _ScriptLLM.requests = []
        _ScriptLLM.delay = 0

    def bridge(self, **context):
        bridge = PiBridge.from_config({"provider": "pi", "base_url": self.url, "api_key": "k", "model": "m",
                                       "context": context})
        bridge.log = lambda *a: None
        self.assertEqual(bridge.backend_kind, "pi")
        return bridge

    def test_tool_loop_and_ephemeral_context_not_persisted(self):
        def script(req):
            if req["messages"][-1]["role"] == "tool":
                return {"role": "assistant", "content": "结果：" + req["messages"][-1]["content"]}
            return _call("echo", {"n": "3"})
        _ScriptLLM.script = staticmethod(script)
        seen = []
        conv = self.bridge().begin_session("s1", "SYS", tools=[{
            "name": "echo", "description": "回显", "handler": lambda p: seen.append(p) or f"n={p['n']}",
            "parameters": {"type": "object", "properties": {"n": {"type": "integer"}}}}])
        self.assertEqual(conv.ask("第一问", context="背景甲"), "结果：n=3")
        self.assertEqual(seen, [{"n": 3}])  # "3" 经 schema 规范化成整数
        first = _ScriptLLM.requests[0]
        self.assertEqual(first["temperature"], 0)
        self.assertFalse(first["stream"])
        self.assertEqual(first["tools"][0]["function"]["parameters"]["properties"]["n"]["type"], "integer")
        self.assertIn("背景甲", first["messages"][-1]["content"])
        self.assertEqual(conv.history[0], {"role": "user", "content": "第一问"})
        self.assertEqual(conv.last_turn["tools"], ["echo"])
        self.assertEqual(conv.transcript.usage["total"], 24)

        _ScriptLLM.requests = []
        _ScriptLLM.script = staticmethod(lambda req: {"role": "assistant", "content": "好"})
        self.assertEqual(conv.ask("第二问"), "好")
        sent = json.dumps(_ScriptLLM.requests[0]["messages"], ensure_ascii=False)
        self.assertNotIn("背景甲", sent)
        self.assertIn("第一问", sent)
        tool_msg = next(m for m in _ScriptLLM.requests[0]["messages"] if m["role"] == "tool")
        self.assertEqual(tool_msg["tool_call_id"], "call-1")

    def test_failed_turn_returns_none_and_keeps_history(self):
        _ScriptLLM.script = staticmethod(lambda req: {"role": "assistant", "content": "答一"})
        conv = self.bridge().begin_session("s2", "SYS")
        self.assertEqual(conv.ask("一"), "答一")
        before = copy.deepcopy(conv.history)
        _ScriptLLM.script = staticmethod(lambda req: None)
        self.assertIsNone(conv.ask("二"))  # 不能把上一轮的「答一」当答案
        self.assertEqual(conv.history, before)
        self.assertIn("500", conv.last_turn["error"])

    def test_timeout_aborts_without_committing(self):
        _ScriptLLM.script = staticmethod(lambda req: {"role": "assistant", "content": "迟到"})
        _ScriptLLM.delay = 2
        conv = self.bridge(turn_timeout=1).begin_session("s3", "SYS")
        started = time.monotonic()
        self.assertIsNone(conv.ask("慢"))
        self.assertLess(time.monotonic() - started, 6)
        self.assertEqual(conv.history, [])
        self.assertIn("已中止", conv.last_turn["error"])

    def test_tool_limit_withdraws_tools_then_model_answers(self):
        def script(req):
            if not req.get("tools"):
                return {"role": "assistant", "content": "基于已有结果作答"}
            return _call("echo", {}, cid=f"c{len(_ScriptLLM.requests)}")
        _ScriptLLM.script = staticmethod(script)
        calls = []
        conv = self.bridge(max_tool_calls=2).begin_session("s4", "SYS", tools=[{
            "name": "echo", "handler": lambda p: calls.append(1) or "ok"}])
        self.assertEqual(conv.ask("循环"), "基于已有结果作答")
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(_ScriptLLM.requests), 3)
        self.assertNotIn("tools", _ScriptLLM.requests[-1])

    def test_compaction_summarizes_into_system_prompt(self):
        def script(req):
            if "压缩" in req["messages"][0]["content"]:
                return {"role": "assistant", "content": "摘要：用户问过铜价"}
            return {"role": "assistant", "content": "答" * 50}
        _ScriptLLM.script = staticmethod(script)
        conv = self.bridge(budget_chars=150, keep_recent_turns=1).begin_session("s5", "SYS")
        conv.on_commit = lambda c: None
        for q in ("问一", "问二", "问三"):
            with conv._lock:  # 等后台压缩结束，保持测试确定
                pass
            conv.ask(q)
        deadline = time.monotonic() + 5
        while conv.transcript.compactions == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        with conv._lock:
            self.assertGreaterEqual(conv.transcript.compactions, 1)
            self.assertEqual(conv.transcript.summary, "摘要：用户问过铜价")
            self.assertEqual(conv.history[0]["role"], "user")
        _ScriptLLM.requests = []
        conv.ask("问四")
        self.assertIn("摘要：用户问过铜价", _ScriptLLM.requests[-1]["messages"][0]["content"])

    def test_compaction_falls_back_to_dropping_when_summary_fails(self):
        _ScriptLLM.script = staticmethod(lambda req: {"role": "assistant", "content": "答" * 80})
        conv = self.bridge(budget_chars=150, keep_recent_turns=1).begin_session("s6", "SYS")
        conv._compact_later = lambda: None  # 手动触发
        conv.ask("问一")
        conv.ask("问二")
        _ScriptLLM.script = staticmethod(lambda req: None)
        self.assertTrue(conv.compact_now())
        self.assertEqual(conv.transcript.summary, "")
        self.assertEqual(conv.history[0], {"role": "user", "content": "问二"})


@unittest.skipUnless(HAS_PI, "pi-agent SDK 未安装")
class TestPiSdkBackend(unittest.TestCase):
    def test_session_echo(self):
        bridge = PiBridge.from_config({"enabled": True, "provider": "mock", "model": "m"})
        self.assertIsNotNone(bridge)
        self.assertEqual(bridge.backend_kind, "mock")
        sess = bridge.begin_session("t1")
        out = sess.ask("你好")
        self.assertIsNotNone(out)
        self.assertIn("Echo:", out)

    def test_session_tool_calling(self):
        bridge = PiBridge.from_config({"enabled": True, "provider": "mock", "model": "m"})

        def weather(params):
            return f"Sunny 22C in {params.get('city')}"

        sess = bridge.begin_session("t2", tools=[{
            "name": "get_weather", "label": "天气", "description": "查天气",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            "handler": weather,
        }])
        out = sess.ask("What's the weather in San Francisco?")
        self.assertIn("Sunny 22C in San Francisco", out or "")

    def test_complete_chat(self):
        bridge = PiBridge.from_config({"enabled": True, "provider": "mock", "model": "m"})
        out = bridge.complete_chat("sys", "hello")
        self.assertEqual(out, "Echo: hello")


if __name__ == "__main__":
    unittest.main()
