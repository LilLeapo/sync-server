#!/usr/bin/env python3
"""Pi Agent SDK 桥接层 —— 服务器端接入 Agent 的统一入口。

用 [pi-agent](https://pypi.org/project/pi-agent/)（Pi 的 Python 版本，
<https://github.com/aniketmaurya/pi-py>）跑 Agent 会话与工具调用；
SDK 未安装时自动降级到「纯标准库 OpenAI 兼容 HTTP」后端，代码零改动。

对外只暴露同步 API（服务器是线程模型），内部用专用 asyncio 线程驱动 SDK：

    bridge = PiBridge.from_config({"provider": "pi", "base_url": "...", "api_key": "...", "model": "..."})
    text = bridge.complete_chat(SYSTEM, "用户的自然语言问题")      # 单轮补全
    text = bridge.format_quotes(roomId, rows)                     # 报价格式化（LLM，失败自动降级模板）
    sess = bridge.begin_session(SYSTEM, tools)                    # 多轮问答会话
    reply = sess.ask("昨天这批货最低价多少？", context="...")       # 会话 ask

provider 取值：
    auto（默认）  优先 pi-agent SDK，import 失败降级 http
    pi            强制 pi-agent SDK
    http          纯标准库 OpenAI 兼容 HTTP（/chat/completions）
    mock          pi-agent SDK 内置 mock provider（无需 api_key，测试用）
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import threading
import time
import urllib.request

DEFAULT_TIMEOUT_S = 90
SESSION_HISTORY = 10  # 会话保留的问答对数（system 之外）
QNA_SYSTEM = """你是绑在飞书群里的报价助理。只根据给定的资料回答，资料不足就直说没有查到，
不要编造。回答使用简体中文，简明扼要；涉及多行数据时逐行列出。"""

FORMAT_SYSTEM = """你是报价推送格式化器。用户给出一个群聊中提取到的报价记录（JSON 数组），
请整理成适合发到飞书群的纯文本消息，要求：
- 不输出 JSON、代码块或任何解释，只输出最终文本；
- 每一条报价单独一行，格式：「项目」价格 单位 ×数量 —— 备注（发送人，时间）；
- 没有数量的省略 ×数量，没有备注的省略 —— 备注；
- 开头一行写「📊 新报价 · <房间名>」，结尾不要总结、不要客套。"""


def _extract_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _fmt_row(r: dict) -> str:
    """把一条报价记录压成一行，缺字段自动省略。"""
    parts = [str(r.get("项目") or ""), str(r.get("价格") or "")]
    delim = " "
    for key, prefix, suffix in (("单位", "", ""), ("数量", "×", "")):
        v = str(r.get(key) or "").strip()
        if not v:
            continue
        parts.append(prefix + v + suffix)
    note = str(r.get("备注") or "").strip()
    sender = str(r.get("sender") or "").strip()
    when = str(r.get("timeLabel") or "")
    tail = " —— ".join(x for x in (note, sender, when) if x)
    return delim.join(x for x in parts if x) + ((" —— " + tail) if tail else "")


def fallback_format(room: str, rows: list[dict]) -> str:
    lines = [f"📊 新报价 · {room}", ""]
    for r in rows:
        lines.append(_fmt_row(r))
    return "\n".join(lines)


class BridgeError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 纯标准库 HTTP 后端（OpenAI 兼容 /chat/completions）
# ---------------------------------------------------------------------------

class HttpBackend:
    """无第三方依赖的兜底后端。多轮对话通过调用方维护的历史实现。"""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: int = DEFAULT_TIMEOUT_S):
        self.base_url = str(base_url).rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def complete(self, system: str | None, messages: list[dict], json_mode: bool = False) -> str | None:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}] if system else [],
            "temperature": 0,
        }
        body["messages"] += [{"role": m["role"], "content": m["content"]} for m in messages]
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.api_key,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]
        except Exception as e:
            raise BridgeError(f"http backend call failed: {e}") from e


# ---------------------------------------------------------------------------
# pi-agent SDK 后端
# ---------------------------------------------------------------------------

class PiSdkBackend:
    """真正的 pi-agent SDK 后端：Agent 会话 + 工具调用 + 多轮记忆。"""

    def __init__(self, base_url: str, api_key: str, model: str, mock: bool = False,
                 timeout: int = DEFAULT_TIMEOUT_S):
        try:
            from pi_agent.agent_core import Agent, Model  # noqa: F401
            from pi_agent.pi_ai import create_default_registry
        except ImportError as e:  # pragma: no cover
            raise BridgeError(
                "pi-agent SDK 未安装：pip install pi-agent（或换 provider=http）") from e
        self._Agent = Agent
        self._Model = Model
        self.timeout = timeout
        if mock:
            self.registry = create_default_registry()
            self.model_args = dict(id="mock-model", provider="mock", api="mock")
            self.api_key = None
        else:
            self.registry = create_default_registry()
            self.model_args = dict(
                id=model,
                provider="custom",
                api="openai-completions",
                base_url=str(base_url).rstrip("/"),
            )
            self.api_key = api_key

    # -- 单轮补全 ------------------------------------------------

    def complete(self, system: str | None, messages: list[dict], json_mode: bool = False) -> str | None:
        from pi_agent.agent_core import AssistantMessage, TextContent
        from pi_agent.pi_ai import complete_simple

        prompt = "\n\n".join(m["content"] for m in messages if m.get("role") == "user")
        if not prompt and messages:
            prompt = messages[-1].get("content") or ""
        fut = asyncio.run_coroutine_threadsafe(
            complete_simple(
                prompt,
                model=self._Model(**self.model_args),
                registry=self.registry,
                system_prompt=system,
                api_key=self.api_key,
                session_id=f"complete-{time.time_ns()}",
            ), _get_loop())
        msg = fut.result(self.timeout)
        if not isinstance(msg, AssistantMessage):
            return None
        return " ".join(
            b.text for b in msg.content if isinstance(b, TextContent)).strip() or None

    # -- 会话 ----------------------------------------------------

    def begin_session(self, session_id: str, system: str, tools: list[dict]) -> "PiSdkSession":
        return PiSdkSession(self, session_id, system, tools)


class PiSdkSession:
    """一次 pi-agent Agent 会话（有记忆、可带工具）。线程安全：同一会话串行。"""

    def __init__(self, backend: PiSdkBackend, session_id: str, system: str, tools: list[dict]):
        from pi_agent.agent_core import Agent, AgentTool
        from pi_agent.pi_ai import create_agent_stream_fn

        self.backend = backend
        self.session_id = session_id
        self._lock = threading.Lock()
        self._agent = Agent(
            stream_fn=create_agent_stream_fn(backend.registry),
            session_id=session_id,
            get_api_key=(lambda: backend.api_key) if backend.api_key else None,
        )
        self._agent.set_model(backend._Model(**backend.model_args))
        self._agent.set_system_prompt(system)
        if tools:
            self._agent.set_tools([AgentTool(
                name=t["name"],
                label=t.get("label") or t["name"],
                description=t.get("description") or t["name"],
                parameters=t.get("parameters"),
                execute=self._make_execute(t["handler"]),
            ) for t in tools])

    @staticmethod
    def _make_execute(handler):
        async def _execute(tool_call_id, params, abort_event=None, on_update=None):
            loop = asyncio.get_running_loop()
            text = await loop.run_in_executor(None, handler, dict(params or {}))
            from pi_agent.agent_core import AgentToolResult, TextContent
            return AgentToolResult(content=[TextContent(text=str(text))], details={})
        return _execute

    def ask(self, text: str, context: str | None = None) -> str:
        user_text = (context + "\n\n" + text) if context else text
        with self._lock:
            fut = asyncio.run_coroutine_threadsafe(self._run(user_text), _get_loop())
            out = fut.result(self.backend.timeout)
        self._prune()
        return out

    async def _run(self, text: str) -> str:
        from pi_agent.agent_core import AssistantMessage, TextContent
        agent = self._agent
        if agent.state.is_streaming:
            await agent.wait_for_idle()
        await agent.prompt(text)
        for msg in reversed(agent.state.messages):
            if isinstance(msg, AssistantMessage) and any(
                isinstance(b, TextContent) for b in msg.content):
                return " ".join(b.text for b in msg.content if isinstance(b, TextContent)).strip()
        return ""

    def _prune(self):
        """只保留最近 N 对问答，防止上下文无限膨胀。"""
        msgs = self._agent.state.messages
        keep = SESSION_HISTORY * 2
        if len(msgs) > keep:
            del msgs[:-keep]


# ---------------------------------------------------------------------------
# 桥：统一外观
# ---------------------------------------------------------------------------

_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()


def _get_loop() -> asyncio.AbstractEventLoop:
    global _loop
    with _loop_lock:
        if _loop is None or _loop.is_closed():
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, daemon=True,
                             name="pi-agent-loop").start()
        return _loop


class SessionBridge:
    """http 后端的会话壳：自己维护历史。pi 后端则是 PiSdkSession 的薄包装。"""

    def __init__(self, owner: "PiBridge", session_id: str, system: str, tools: list[dict]):
        self.owner = owner
        self.session_id = session_id
        self.system = system
        self.tools = tools
        self.history: list[dict] = []
        if hasattr(owner.backend, "begin_session"):
            self._pi = owner.backend.begin_session(session_id, system, tools)
        else:
            self._pi = None

    def ask(self, text: str, context: str | None = None) -> str | None:
        if self._pi is not None:
            return self._pi.ask(text, context)
        prompt = text
        if context:
            prompt = context + "\n\n" + text
        self.history.append({"role": "user", "content": prompt})
        if len(self.history) > SESSION_HISTORY * 2:
            del self.history[:-SESSION_HISTORY * 2]
        try:
            out = self.owner.backend.complete(self.system, self.history)
        except BridgeError as e:
            self.owner.log(f"session {self.session_id} llm failed: {e}")
            out = None
        if out:
            self.history.append({"role": "assistant", "content": out})
        return out


class PiBridge:
    def __init__(self, backend, backend_kind: str, warn: str = ""):
        self.backend = backend
        self.backend_kind = backend_kind
        self.warn = warn
        self.log = print

    # -- 工厂 -- 从 agent.config.json 的 dict 构建 -------------------------

    @staticmethod
    def from_config(cfg: dict | None) -> "PiBridge | None":
        """cfg 形如 {"provider":"auto","base_url":...,"api_key":...,"model":...}。
        配置缺失 / enabled=False 时返回 None。"""
        if not cfg or not cfg.get("enabled", True):
            return None
        if not cfg.get("model"):
            return None
        provider = str(cfg.get("provider") or "auto").lower()
        base_url = str(cfg.get("base_url") or "").rstrip("/")
        api_key = str(cfg.get("api_key") or "")
        model = str(cfg["model"])
        timeout = int(cfg.get("timeout") or DEFAULT_TIMEOUT_S)

        if provider in ("http", "pi", "auto"):
            if not base_url or not api_key:
                print("[pi_bridge] 配置缺少 base_url/api_key，跳过 Agent", file=sys.stderr)
                return None
        if provider in ("pi", "auto"):
            try:
                backend = PiSdkBackend(base_url, api_key, model, timeout=timeout)
                return PiBridge(backend, "pi")
            except BridgeError as e:
                if provider == "pi":
                    print(f"[pi_bridge] {e}", file=sys.stderr)
                    return None
                print(f"[pi_bridge] {e}，降级 http 后端", file=sys.stderr)
                backend = HttpBackend(base_url, api_key, model, timeout)
                return PiBridge(backend, "http")
        if provider == "http":
            return PiBridge(HttpBackend(base_url, api_key, model, timeout), "http")
        if provider == "mock":
            try:
                backend = PiSdkBackend("", "", model or "mock-model", mock=True, timeout=timeout)
                return PiBridge(backend, "mock")
            except BridgeError as e:
                print(f"[pi_bridge] {e}", file=sys.stderr)
                return None
        print(f"[pi_bridge] 未知 provider={provider}（可选 auto/pi/http/mock）", file=sys.stderr)
        return None

    # -- 单轮 ---------------------------------------------------

    def complete_messages(self, system: str | None, messages: list[dict],
                          json_mode: bool = False) -> str | None:
        try:
            return self.backend.complete(system, messages, json_mode)
        except BridgeError as e:
            self.log(f"[pi_bridge] complete failed: {e}")
            return None

    def complete_chat(self, system: str, text: str) -> str | None:
        return self.complete_messages(system, [{"role": "user", "content": text}])

    def complete_json(self, system: str, text: str) -> dict | None:
        raw = self.complete_messages(system, [{"role": "user", "content": text}],
                                     json_mode=True)
        return _extract_json(raw)

    # -- 会话 ---------------------------------------------------

    def begin_session(self, session_id: str, system: str = QNA_SYSTEM,
                      tools: list[dict] | None = None) -> SessionBridge:
        return SessionBridge(self, session_id, system, tools or [])

    # -- 报价格式化（LLM，失败自动降级模板） -------------------------

    def format_quotes(self, room: str, rows: list[dict]) -> str:
        payload = [{
            "项目": r.get("项目", ""), "价格": r.get("价格", ""),
            "单位": r.get("单位", ""), "数量": r.get("数量", ""),
            "备注": r.get("备注", ""), "发送人": r.get("sender", ""),
            "时间": r.get("timeLabel", ""),
        } for r in rows]
        try:
            out = self.complete_chat(
                FORMAT_SYSTEM,
                f"房间：{room}\n" + json.dumps(payload, ensure_ascii=False))
            if out and out.strip():
                return out.strip()
        except Exception as e:
            self.log(f"[pi_bridge] format failed, fallback template: {e}")
        return fallback_format(room, rows)