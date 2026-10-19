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
  register_agent_tool(name, handler, description="", parameters=None, label="", opt_in=False)
      给问答 Agent 加工具：handler(params, ctx) -> str，ctx = {"store", "quote_log", "device", "ledger"}；
      parameters 为 tool_schema 的 S.object(...) 或 JSON Schema；opt_in=True 的工具需在设备设置里手动勾选
  运行时对象：api.store / api.quotes / api.bridge / api.feishu / api.wecom / api.ilink / api.config / api.reply() / api.log()
  api.sessions / api.session_key：当前会话所在的 SessionManager 与 key（机器人分发事件时设置）

内置指令（不依赖 plugins 目录）：/reset、/new —— 清空当前会话的 Agent 记忆。

加载策略：importlib 逐个加载，单个插件失败只告警不拖垮主体。
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys

MODULE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
TOOL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


def _reset_session(api, event, args):
    sessions, key = getattr(api, "sessions", None), getattr(api, "session_key", None)
    if sessions is None or key is None:
        return "这个渠道不支持重置对话。"
    sessions.reset(key)
    return "已开启新对话，之前的上下文已清空。"


class PluginApi:
    """插件与宿主之间的界面。bind() 之后才有运行时资源。"""

    def __init__(self, host: "PluginHost"):
        self.host = host
        self.store = None
        self.quotes = None
        self.bridge = None
        self.feishu = None
        self.wecom = None
        self.ilink = None
        self.config = None
        self.bot = None
        self.event = None
        self.text = ""
        self.sessions = None
        self.session_key = None

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

    def register_agent_tool(self, name, handler, description: str = "", parameters=None,
                            label: str = "", opt_in: bool = False):
        if not TOOL_NAME_RE.match(str(name)):
            raise ValueError(f"工具名不合法：{name}（字母开头，只含字母数字下划线）")
        from .tool_schema import S, Schema
        schema = parameters if isinstance(parameters, Schema) else S.from_json(
            parameters or {"type": "object", "properties": {}})
        self.host.agent_tools[name] = {
            "name": name, "label": label or name, "description": description or name, "schema": schema,
            "parameters": schema.to_json(), "handler": handler, "opt_in": bool(opt_in),
            "plugin": self.host.current_plugin,
        }

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
        self.agent_tools: dict[str, dict] = {}
        self.current_plugin = ""
        self.api = PluginApi(self)  # 注册模板；运行期事件用 spawn_api() 派生
        for name in ("reset", "new"):
            self.commands[name] = {"handler": _reset_session, "usage": "/" + name,
                                   "description": "清空当前对话的 Agent 记忆，重新开始", "admin_only": False,
                                   "plugin": "builtin"}

    # -- 装配运行时资源 -------------------------------------------

    def bind(self, *, store=None, quote_log=None, bridge=None, feishu_client=None,
             feishu_cfg=None, bot=None, wecom_client=None, ilink_client=None):
        self.api.store = store
        self.api.quotes = quote_log
        self.api.bridge = bridge
        self.api.feishu = feishu_client
        self.api.wecom = wecom_client
        self.api.ilink = ilink_client
        self.api.config = feishu_cfg
        self.api.bot = bot

    def spawn_api(self) -> "PluginApi":
        """为单个事件派生一个带运行时资源、但独立 event/text 的 PluginApi。"""
        api = PluginApi(self)
        api.store = self.api.store
        api.quotes = self.api.quotes
        api.bridge = self.api.bridge
        api.feishu = self.api.feishu
        api.wecom = self.api.wecom
        api.ilink = self.api.ilink
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