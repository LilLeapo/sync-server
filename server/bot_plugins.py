#!/usr/bin/env python3
"""机器人插件框架 —— 「保留插件开发空间」的落点。

插件就是一个 plugins/ 目录下的 Python 文件，暴露 register(api)：

    # plugins/hello.py
    def register(api):
        def run(api, event, args):
            return "你好，" + args + "！"
        api.register_command("hello", run, usage="/hello <名字>", description="打招呼示例")

可用注册点（PluginApi）：
  register_command(name, handler, usage="", description="", admin_only=False)
      handler(api, event, args) -> str | None  返回文本则由机器人回复
  register_message_hook(fn)      fn(api, event, text) -> str | None | False
      str  → 机器人回复该文本；False → 插件已自行处理；None → 继续走命令/问答
  register_quote_hook(fn)        fn(api, ctx) -> None | False
      ctx = {"room": str, "rows": [报价行...]}；返回 False 抑制默认群推送（插件自行推）
  运行时对象：api.store / api.quotes / api.bridge / api.feishu / api.config / api.reply() / api.log()

加载策略：importlib 逐个加载，单个插件失败只告警不拖垮主体。
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys

MODULE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


class PluginApi:
    """插件与宿主之间的界面。bind() 之后才有运行时资源。"""

    def __init__(self, host: "PluginHost"):
        self.host = host
        self.store = None
        self.quotes = None
        self.bridge = None
        self.feishu = None
        self.config = None
        self.bot = None
        self.event = None
        self.text = ""

    def _set_event(self, event: dict, text: str):
        self.event = event
        self.text = text

    # -- 插件在 register() 里调用的注册 API -------------------------

    def register_command(self, name, handler, usage: str = "", description: str = "",
                         admin_only: bool = False):
        self.host.commands[name.lower()] = {
            "handler": handler, "usage": usage, "description": description,
            "admin_only": admin_only, "plugin": self.host.current_plugin,
        }

    def register_message_hook(self, fn):
        self.host.message_hooks.append(fn)

    def register_quote_hook(self, fn):
        self.host.quote_hooks.append(fn)

    # -- 运行期便捷方法 -------------------------------------------

    def reply(self, text: str):
        if self.bot is not None and self.event is not None:
            message_id = ((self.event.get("event") or {}).get("message") or {}).get("message_id")
            if message_id:
                self.bot.reply(message_id, text)

    def log(self, msg: str):
        if self.bot is not None:
            self.bot.log(f"[plugin:{self.host.current_plugin or '-'}] {msg}")
        else:
            print(f"[plugin:{self.host.current_plugin or '-'}] {msg}", file=sys.stderr)


class PluginHost:
    def __init__(self, plugins_dir: str):
        self.plugins_dir = plugins_dir
        self.commands: dict[str, dict] = {}
        self.message_hooks: list = []
        self.quote_hooks: list = []
        self.current_plugin = ""
        self.api = PluginApi(self)  # 注册模板；运行期事件用 spawn_api() 派生

    # -- 装配运行时资源 -------------------------------------------

    def bind(self, *, store=None, quote_log=None, bridge=None, feishu_client=None,
             feishu_cfg=None, bot=None):
        self.api.store = store
        self.api.quotes = quote_log
        self.api.bridge = bridge
        self.api.feishu = feishu_client
        self.api.config = feishu_cfg
        self.api.bot = bot

    def spawn_api(self) -> "PluginApi":
        """为单个事件派生一个带运行时资源、但独立 event/text 的 PluginApi。"""
        api = PluginApi(self)
        api.store = self.api.store
        api.quotes = self.api.quotes
        api.bridge = self.api.bridge
        api.feishu = self.api.feishu
        api.config = self.api.config
        api.bot = self.api.bot
        return api

    # -- 加载 ---------------------------------------------------

    def load(self) -> int:
        if not os.path.isdir(self.plugins_dir):
            return 0
        loaded = 0
        for fname in sorted(os.listdir(self.plugins_dir)):
            if not fname.endswith(".py") or fname.startswith("_"):
                continue
            modname = fname[:-3]
            if not MODULE_NAME_RE.match(modname):
                continue
            self.current_plugin = modname
            try:
                spec = importlib.util.spec_from_file_location(
                    f"syncserver_plugins.{modname}",
                    os.path.join(self.plugins_dir, fname))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)  # type: ignore[union-attr]
                register = getattr(module, "register", None)
                if not callable(register):
                    raise RuntimeError(f"缺少 register(api) 函数")
                register(self.api)
                loaded += 1
                print(f"[plugins] 已加载 {modname}")
            except Exception as e:
                print(f"[plugins] 加载失败 {fname}: {e}", file=sys.stderr)
        self.current_plugin = ""
        return loaded

    # -- 分发 ---------------------------------------------------

    def run_command(self, name: str, args: str, api: PluginApi) -> str | None:
        entry = self.commands.get(name.lower())
        if not entry:
            return None
        return entry["handler"](api, api.event, args)

    def run_message_hooks(self, api: PluginApi, event: dict, text: str):
        for fn in list(self.message_hooks):
            try:
                out = fn(api, event, text)
            except Exception as e:
                print(f"[plugins] message hook 异常: {e}", file=sys.stderr)
                continue
            if isinstance(out, str) or out is False:
                return out
        return None

    def run_quote_hooks(self, api: PluginApi, ctx: dict):
        for fn in list(self.quote_hooks):
            try:
                out = fn(api, ctx)
            except Exception as e:
                print(f"[plugins] quote hook 异常: {e}", file=sys.stderr)
                continue
            if out is False:
                return False
        return None

    def help_text(self) -> str:
        lines = ["可用指令："]
        for name, entry in sorted(self.commands.items()):
            usage = entry.get("usage") or ("/" + name)
            desc = entry.get("description") or ""
            lines.append(f"  {usage}" + (f" —— {desc}" if desc else ""))
        return "\n".join(lines)