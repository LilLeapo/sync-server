"""插件框架与内置插件测试。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.bot_plugins import PluginHost  # noqa: E402


class TestBuiltinPlugins(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cls.host = PluginHost(os.path.join(cls.root, "plugins"))
        cls.loaded = cls.host.load()

    def test_all_builtin_loaded(self):
        self.assertEqual(self.loaded, 5)
        for name in ("help", "quotes", "rooms", "stats", "echo"):
            self.assertIn(name, self.host.commands, f"缺少插件命令 {name}")

    def test_echo_command(self):
        api = self.host.spawn_api()
        out = self.host.run_command("echo", "hello world", api)
        self.assertEqual(out, "hello world")

    def test_help_message_hook(self):
        api = self.host.spawn_api()
        out = self.host.run_message_hooks(api, {}, "帮助")
        self.assertIn("echo", out)
        self.assertIn("/quotes", out)


class TestBadPluginIsolation(unittest.TestCase):
    def test_bad_plugin_does_not_kill_others(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "bad.py"), "w") as f:
                f.write("raise RuntimeError('boom')\n")
            with open(os.path.join(d, "good.py"), "w") as f:
                f.write("def register(api):\n"
                        "    api.register_command('good', lambda a, e, x: 'ok')\n")
            host = PluginHost(d)
            self.assertEqual(host.load(), 1)  # bad 失败只告警
            self.assertIn("good", host.commands)
            self.assertNotIn("bad", host.commands)


if __name__ == "__main__":
    unittest.main()