"""pi_bridge 测试：mock 后端会话、http 后端、配置选择与降级。"""
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
        session = PiBridge(backend, "http").begin_session("bounded", tools=[{
            "name": "test_tool", "handler": handler, "parameters": {"type": "object"}}])
        self.assertIn("上限", session.ask("run"))
        self.assertEqual(backend.complete_response.call_count, 6)
        handler.assert_not_called()
        self.assertTrue(any("tool arguments must be an object" in msg.get("content", "")
                            for msg in session.history if msg["role"] == "tool"))


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
