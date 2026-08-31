"""pi_bridge 测试：mock 后端会话、http 后端、配置选择与降级。"""
import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import pi_bridge  # noqa: E402
from server.pi_bridge import PiBridge, fallback_format  # noqa: E402

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