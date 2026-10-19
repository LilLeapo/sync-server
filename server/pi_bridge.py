#!/usr/bin/env python3
"""Pi Agent SDK 桥接层 —— 服务器端接入 Agent 的统一入口。

用 [pi-agent](https://pypi.org/project/pi-agent/)（Pi 的 Python 版本，
<https://github.com/aniketmaurya/pi-py>）跑 Agent 回合与工具调用；
SDK 未安装时自动降级到「纯标准库 OpenAI 兼容 HTTP」后端，代码零改动。
两种后端的模型请求都走本模块的 post_chat（标准库 urllib），不依赖 openai 包。

对外只暴露同步 API（服务器是线程模型），内部用专用 asyncio 线程驱动 SDK：

    bridge = PiBridge.from_config({"provider": "pi", "base_url": "...", "api_key": "...", "model": "..."})
    text = bridge.complete_chat(SYSTEM, "用户的自然语言问题")      # 单轮补全
    text = bridge.format_quotes(roomId, rows)                     # 报价格式化（LLM，失败自动降级模板）
    conv = bridge.begin_session("feishu-oc_x", SYSTEM, tools)     # 多轮问答会话（Conversation）
    reply = conv.ask("昨天这批货最低价多少？", context="...")       # context 只用于本轮，不进历史

会话设计（上下文策略见 agent_context.py）：
  - Conversation 自己持有规范化的历史（OpenAI 格式 dict，可直接落盘），pi 的 Agent
    只当「一轮的执行器」：每轮新建、灌入渲染后的历史、跑完把新消息转回 dict 再提交。
  - 本轮失败 / 超时 = 不提交，历史保持原样，返回 None（不会把上一轮的答案当成这一轮的）。
  - 每轮工具调用有上限；超限后不再给模型工具，逼它基于已有结果作答。

provider 取值：
    auto（默认）  优先 pi-agent SDK，import 失败降级 http
    pi            强制 pi-agent SDK
    http          纯标准库 OpenAI 兼容 HTTP（/chat/completions）
    mock          pi-agent SDK 内置 mock provider（无需 api_key，测试用）
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request

from .agent_context import (LIMIT_ANSWER, TOOL_LIMIT_TEXT, ContextPolicy, Transcript, clip, content_text,
                            dumps_args, pick_compaction, render, shrink_tool_results, summary_request,
                            system_with_summary, transcript_chars, with_context)
from .quote_skill import quote_guidance
from .tool_schema import SchemaError, tool_parameters, tool_schema

DEFAULT_TIMEOUT_S = 90
QNA_SYSTEM = """你是绑在飞书群里的报价助理。只根据给定的资料回答，资料不足就直说没有查到，
不要编造。回答使用简体中文，简明扼要；涉及多行数据时逐行列出。"""

FORMAT_SYSTEM = """你是报价推送格式化器。用户给出一个群聊中提取到的报价记录（JSON 数组），
请整理成适合发到飞书群的纯文本消息，要求：
- 不输出 JSON、代码块或任何解释，只输出最终文本；
- 每一条报价单独一行，格式：[供应商]「项目」价格 单位 ×数量 —— 备注（发送人，时间）；
- 供应商为空时省略 [供应商] 前缀；没有数量的省略 ×数量，没有备注的省略 —— 备注；
- 开头一行写「📊 新报价 · <房间名>」，结尾不要总结、不要客套。"""

# 模型请求与工具 handler 各用一个线程池：超时被放弃的请求不会占满默认线程池
_HTTP_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="llm-http")
_TOOL_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="agent-tool")


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
    supplier = str(r.get("供应商") or "").strip()
    head = f"[{supplier}] " if supplier else ""
    parts = [head + str(r.get("项目") or ""), str(r.get("价格") or "")]
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


def room_label(room: str, rows: list[dict]) -> str:
    """推送展示用群名：模块回传的群名称优先，回退到 roomId（去 @chatroom）。"""
    name = str((rows[0] if rows else {}).get("roomName") or "").strip()
    return name or str(room or "unknown").replace("@chatroom", "")


def fallback_format(room: str, rows: list[dict]) -> str:
    lines = [f"📊 新报价 · {room_label(room, rows)}", ""]
    for r in rows:
        lines.append(_fmt_row(r))
    return "\n".join(lines)


class BridgeError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 传输：标准库 OpenAI 兼容 /chat/completions
# ---------------------------------------------------------------------------

def post_chat(base_url: str, api_key: str | None, payload: dict, timeout: float,
              temperature: float | None = 0) -> dict:
    """POST {base_url}/chat/completions，返回响应 JSON。失败抛 BridgeError（带 HTTP 状态与报错片段）。"""
    body = dict(payload)
    body.pop("stream_options", None)
    body["stream"] = False
    if temperature is not None:
        body.setdefault("temperature", temperature)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    req = urllib.request.Request(str(base_url).rstrip("/") + "/chat/completions",
                                 data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=max(float(timeout), 1.0)) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read()[:400].decode("utf-8", "replace") if e.fp else ""
        finally:
            e.close()
        raise BridgeError(f"HTTP {e.code}: {detail}".strip()) from e
    except Exception as e:
        raise BridgeError(f"模型请求失败: {type(e).__name__}: {e}") from e


def _add_usage(total: dict, usage: dict | None):
    for key, value in (usage or {}).items():
        if isinstance(value, (int, float)):
            total[key] = total.get(key, 0) + value


def _openai_usage(raw) -> dict:
    if not isinstance(raw, dict):
        return {}
    return {"input": int(raw.get("prompt_tokens") or 0), "output": int(raw.get("completion_tokens") or 0),
            "total": int(raw.get("total_tokens") or 0)}


# ---------------------------------------------------------------------------
# 一轮内的工具执行：计数、上限、参数校验、结果截断
# ---------------------------------------------------------------------------

class ToolGate:
    def __init__(self, tools: list[dict], policy: ContextPolicy):
        self.specs = {t["name"]: t for t in tools}
        self.policy = policy
        self.calls = 0
        self.used: list[str] = []
        self.aborted = False
        self._lock = threading.Lock()

    @property
    def exhausted(self) -> bool:
        return self.calls >= self.policy.max_tool_calls

    def run(self, name: str, arguments) -> tuple[str, bool]:
        """执行一次工具调用，返回 (结果文本, 是否出错)。出错文本会回给模型。"""
        with self._lock:
            if self.aborted:
                return "本轮已中止，工具没有执行。", True
            if self.exhausted:
                return TOOL_LIMIT_TEXT, True
            self.calls += 1
            self.used.append(name)
        spec = self.specs.get(name)
        if spec is None:
            return f"没有名为 {name} 的工具。", True
        try:
            if isinstance(arguments, str):
                arguments = json.loads(arguments or "{}")
            if not isinstance(arguments, dict):
                raise SchemaError("tool arguments must be an object")
            params = tool_schema(spec)(arguments)
        except (ValueError, SchemaError) as exc:
            return f"参数错误：{exc}。请修正参数后重试。", True
        try:
            out = spec["handler"](params)
        except Exception as exc:
            return f"工具执行失败：{type(exc).__name__}: {exc}", True
        return clip(str(out), self.policy.tool_result_chars, "请缩小范围再查"), False


@dataclasses.dataclass
class TurnResult:
    ok: bool
    answer: str | None = None
    messages: list = dataclasses.field(default_factory=list)  # 本轮新增（不含 user 消息），OpenAI dict
    usage: dict = dataclasses.field(default_factory=dict)
    tools: list = dataclasses.field(default_factory=list)
    error: str = ""
    broken: bool = False  # 执行器卡死，会话不宜再用


def _tool_spec_json(tools: list[dict]) -> list[dict]:
    return [{"type": "function", "function": {
        "name": t["name"], "description": t.get("description", ""), "parameters": tool_parameters(t)}}
        for t in tools]


# ---------------------------------------------------------------------------
# 纯标准库 HTTP 后端（OpenAI 兼容 /chat/completions）
# ---------------------------------------------------------------------------

class HttpBackend:
    """无第三方依赖的兜底后端。"""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: int = DEFAULT_TIMEOUT_S,
                 temperature: float | None = 0):
        self.base_url = str(base_url).rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.temperature = temperature

    def complete(self, system: str | None, messages: list[dict], json_mode: bool = False) -> str | None:
        return self.complete_response(system, messages, json_mode).get("content")

    def complete_response(self, system: str | None, messages: list[dict], json_mode: bool = False,
                          tools: list[dict] | None = None, timeout: float | None = None) -> dict:
        body = {"model": self.model,
                "messages": ([{"role": "system", "content": system}] if system else []) + list(messages)}
        if tools:
            body["tools"] = _tool_spec_json(tools)
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        data = post_chat(self.base_url, self.api_key, body, timeout or self.timeout, self.temperature)
        try:
            message = dict(data["choices"][0]["message"])
        except Exception as e:
            raise BridgeError(f"模型响应格式不对: {str(data)[:200]}") from e
        message["_usage"] = _openai_usage(data.get("usage"))
        return message

    def runner(self) -> "HttpTurnRunner":
        return HttpTurnRunner(self)


def _wire(msg: dict) -> dict:
    """存储格式 → 请求格式：去掉内部字段；tool 消息不带 name（部分兼容服务不认）。"""
    out = {k: v for k, v in msg.items() if not k.startswith("_")}
    if out.get("role") == "tool":
        out.pop("name", None)
    return out


class HttpTurnRunner:
    """标准库后端的一轮：模型 ↔ 工具循环，工具用尽后再发一次不带工具的请求收尾。"""

    def __init__(self, backend):
        self.backend = backend

    def run_turn(self, system: str, history: list[dict], prompt: str, tools: list[dict],
                 policy: ContextPolicy, session_id: str = "") -> TurnResult:
        gate = ToolGate(tools, policy)
        base = [_wire(m) for m in history] + [{"role": "user", "content": prompt}]
        new: list[dict] = []
        usage: dict = {}
        deadline = time.monotonic() + policy.turn_timeout
        for _ in range(policy.max_tool_calls + 2):
            offer = tools if tools and not gate.exhausted else None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return TurnResult(False, error=f"超过 {policy.turn_timeout} 秒没有完成", usage=usage, tools=gate.used)
            try:
                resp = self.backend.complete_response(system, base + [_wire(m) for m in new], tools=offer,
                                                      timeout=remaining)
            except BridgeError as e:
                return TurnResult(False, error=str(e), usage=usage, tools=gate.used)
            _add_usage(usage, resp.get("_usage") if isinstance(resp.get("_usage"), dict) else None)
            usage["calls"] = usage.get("calls", 0) + 1
            calls = [c for c in resp.get("tool_calls") or [] if isinstance(c, dict)]
            content = resp.get("content")
            if calls and offer is None:  # 没给工具还要调工具：按上限收尾
                calls, content = [], content_text(content).strip() or LIMIT_ANSWER
            msg = {"role": "assistant", "content": content}
            if calls:
                msg["tool_calls"] = [{"id": str(c.get("id") or f"call-{i}"), "type": "function",
                                      "function": {"name": (c.get("function") or {}).get("name"),
                                                   "arguments": dumps_args((c.get("function") or {}).get("arguments"))}}
                                     for i, c in enumerate(calls)]
            new.append(msg)
            if not calls:
                answer = content_text(content).strip()
                if not answer:
                    return TurnResult(False, error="模型返回为空", usage=usage, tools=gate.used)
                return TurnResult(True, answer, new, usage, gate.used)
            for call in msg["tool_calls"]:
                fn = call["function"]
                text, _ = gate.run(fn["name"], fn["arguments"])
                new.append({"role": "tool", "tool_call_id": call["id"], "name": fn["name"], "content": text})
        new.append({"role": "assistant", "content": LIMIT_ANSWER})
        return TurnResult(True, LIMIT_ANSWER, new, usage, gate.used)


# ---------------------------------------------------------------------------
# pi-agent SDK 后端
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


class PiSdkBackend:
    """pi-agent SDK 后端：pi 的 Agent 循环 + 工具执行；模型请求经 post_chat，无需 openai 包。"""

    def __init__(self, base_url: str, api_key: str, model: str, mock: bool = False,
                 timeout: int = DEFAULT_TIMEOUT_S, temperature: float | None = 0):
        try:
            from pi_agent.agent_core import Model
            from pi_agent.pi_ai import MockProvider, OpenAICompletionsProvider, ProviderRegistry
        except ImportError as e:  # pragma: no cover
            raise BridgeError(
                "pi-agent SDK 未安装：pip install pi-agent（或换 provider=http）") from e
        self.timeout = timeout
        self.temperature = temperature
        self.base_url = str(base_url).rstrip("/")
        self.registry = ProviderRegistry()
        if mock:
            self.registry.register("mock", MockProvider())
            self.model = Model(id=model or "mock-model", provider="mock", api="mock")
            self.api_key = None
        else:
            self.registry.register("openai-completions", OpenAICompletionsProvider(request_fn=self._request))
            self.model = Model(id=model, provider="custom", api="openai-completions", base_url=self.base_url)
            self.api_key = api_key

    async def _request(self, payload: dict, api_key: str, base_url: str | None) -> dict:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_HTTP_POOL, post_chat, base_url or self.base_url, api_key,
                                          payload, self.timeout, self.temperature)

    # -- 单轮补全 ------------------------------------------------

    def complete(self, system: str | None, messages: list[dict], json_mode: bool = False) -> str | None:
        from pi_agent.agent_core import TextContent
        from pi_agent.pi_ai import complete_simple

        prompt = "\n\n".join(content_text(m.get("content")) for m in messages if m.get("role") == "user")
        if not prompt and messages:
            prompt = content_text(messages[-1].get("content"))
        fut = asyncio.run_coroutine_threadsafe(
            complete_simple(prompt, model=self.model, registry=self.registry, system_prompt=system,
                            api_key=self.api_key, session_id=f"complete-{time.time_ns()}"), _get_loop())
        try:
            msg = fut.result(self.timeout + 5)
        except concurrent.futures.TimeoutError as e:
            fut.cancel()
            raise BridgeError(f"超过 {self.timeout} 秒没有返回") from e
        if msg.stop_reason in ("error", "aborted"):
            raise BridgeError(msg.error_message or msg.stop_reason)
        return "".join(b.text for b in msg.content if isinstance(b, TextContent)).strip() or None

    def runner(self) -> "PiTurnRunner":
        return PiTurnRunner(self)


def _to_pi(messages: list[dict], model):
    from pi_agent.agent_core import AssistantMessage, TextContent, ToolCall, ToolResultMessage, UserMessage
    out = []
    for msg in messages:
        role = msg.get("role")
        text = content_text(msg.get("content"))
        if role == "user":
            out.append(UserMessage(content=text))
        elif role == "assistant":
            blocks = [TextContent(text=text)] if text else []
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                blocks.append(ToolCall(id=str(call.get("id")), name=str(fn.get("name") or ""),
                                       arguments=args if isinstance(args, dict) else {}))
            if blocks:
                out.append(AssistantMessage(content=blocks, api=model.api, provider=model.provider, model=model.id,
                                            stop_reason="toolUse" if msg.get("tool_calls") else "stop"))
        elif role == "tool":
            out.append(ToolResultMessage(tool_call_id=str(msg.get("tool_call_id")), tool_name=str(msg.get("name") or ""),
                                         content=[TextContent(text=text)], is_error=False))
    return out


def _from_pi(messages) -> list[dict]:
    """pi 消息 → 存储 dict。丢掉 thinking（推理模型的思考不回放进历史）。"""
    from pi_agent.agent_core import AssistantMessage, TextContent, ToolCall, ToolResultMessage, UserMessage
    out = []
    for msg in messages:
        if isinstance(msg, AssistantMessage):
            text = "".join(b.text for b in msg.content if isinstance(b, TextContent)).strip()
            calls = [{"id": b.id, "type": "function", "function": {"name": b.name, "arguments": dumps_args(b.arguments)}}
                     for b in msg.content if isinstance(b, ToolCall)]
            if not text and not calls:
                continue
            item = {"role": "assistant", "content": text or None}
            if calls:
                item["tool_calls"] = calls
            out.append(item)
        elif isinstance(msg, ToolResultMessage):
            out.append({"role": "tool", "tool_call_id": msg.tool_call_id, "name": msg.tool_name,
                        "content": "".join(b.text for b in msg.content if isinstance(b, TextContent))})
        elif isinstance(msg, UserMessage):
            out.append({"role": "user", "content": content_text(msg.content) if not isinstance(msg.content, list)
                        else "".join(getattr(b, "text", "") for b in msg.content)})
    return out


class PiTurnRunner:
    """pi 后端的一轮：每轮新建一个 pi Agent，跑完即弃。"""

    def __init__(self, backend: PiSdkBackend):
        self.backend = backend

    def run_turn(self, system: str, history: list[dict], prompt: str, tools: list[dict],
                 policy: ContextPolicy, session_id: str = "") -> TurnResult:
        gate = ToolGate(tools, policy)
        fut = asyncio.run_coroutine_threadsafe(self._turn(system, history, prompt, tools, policy, gate, session_id),
                                               _get_loop())
        try:
            return fut.result(policy.turn_timeout + 10)
        except concurrent.futures.TimeoutError:
            gate.aborted = True
            fut.cancel()
            return TurnResult(False, error="Agent 执行器没有响应", tools=gate.used, broken=True)
        except Exception as e:
            return TurnResult(False, error=f"{type(e).__name__}: {e}", tools=gate.used)

    async def _turn(self, system, history, prompt, tools, policy, gate, session_id) -> TurnResult:
        from pi_agent.agent_core import Agent, AgentTool, AgentToolResult, AssistantMessage, TextContent, UserMessage
        from pi_agent.pi_ai import create_agent_stream_fn

        backend = self.backend
        base_stream = create_agent_stream_fn(backend.registry)
        max_llm_calls = policy.max_tool_calls + 3
        llm_calls = 0

        def make_execute(name):
            async def _execute(tool_call_id, params, abort_event=None, on_update=None):
                loop = asyncio.get_running_loop()
                text, is_error = await loop.run_in_executor(_TOOL_POOL, gate.run, name, dict(params or {}))
                if is_error:
                    raise RuntimeError(text)  # pi 记为 is_error 的工具结果，原文回给模型
                return AgentToolResult(content=[TextContent(text=text)], details={})
            return _execute

        # 参数校验由 ToolGate 用 tool_schema 统一做（宽松规范化），所以给 pi 的 parameters 置空、
        # 只在发给模型时换回 JSON Schema
        agent_tools = [AgentTool(name=t["name"], label=t.get("label") or t["name"],
                                 description=t.get("description") or t["name"], parameters=None,
                                 execute=make_execute(t["name"])) for t in tools]
        llm_tools = [dataclasses.replace(at, parameters=tool_parameters(t)) for at, t in zip(agent_tools, tools)]

        async def stream_fn(model, context, config, abort_event):
            nonlocal llm_calls
            llm_calls += 1
            if llm_calls > max_llm_calls and abort_event is not None:
                abort_event.set()  # provider 见到 abort 即以 aborted 结束，本轮不提交
            offer = llm_tools if llm_tools and not gate.exhausted and llm_calls < max_llm_calls else None
            return await base_stream(model, dataclasses.replace(context, tools=offer), config, abort_event)

        agent = Agent(stream_fn=stream_fn, session_id=session_id or None,
                      get_api_key=(lambda _provider: backend.api_key) if backend.api_key else None)
        agent.set_model(backend.model)
        agent.set_system_prompt(system)
        agent.set_tools(agent_tools)
        agent.replace_messages(_to_pi(history, backend.model))
        base_len = len(agent.state.messages)

        # 纯字符串 content：部分 OpenAI 兼容服务不认 [{"type":"text"}] 数组
        task = asyncio.ensure_future(agent.prompt(UserMessage(content=prompt)))
        done, _ = await asyncio.wait({task}, timeout=policy.turn_timeout)
        if not done:
            gate.aborted = True
            agent.abort()  # 先置 abort，agent_loop 里的孤儿任务下次请求模型时就会结束
            done, _ = await asyncio.wait({task}, timeout=3)
            if not done:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            return TurnResult(False, error=f"超过 {policy.turn_timeout} 秒没有完成，已中止", tools=gate.used)
        if task.exception() is not None:
            return TurnResult(False, error=f"{type(task.exception()).__name__}: {task.exception()}", tools=gate.used)

        produced = agent.state.messages[base_len + 1:]  # 跳过本轮 user 消息
        usage: dict = {}
        for msg in produced:
            if isinstance(msg, AssistantMessage):
                u = msg.usage
                _add_usage(usage, {"input": u.input, "output": u.output, "cacheRead": u.cache_read,
                                   "total": u.total_tokens, "calls": 1})
        last = next((m for m in reversed(produced) if isinstance(m, AssistantMessage)), None)
        if last is None:
            return TurnResult(False, error="模型没有响应", usage=usage, tools=gate.used)
        if last.stop_reason in ("error", "aborted"):
            return TurnResult(False, error=last.error_message or last.stop_reason, usage=usage, tools=gate.used)
        new = _from_pi(produced)
        answer = "".join(b.text for b in last.content if isinstance(b, TextContent)).strip()
        if not answer:
            if not gate.exhausted:
                return TurnResult(False, error="模型返回为空", usage=usage, tools=gate.used)
            answer = LIMIT_ANSWER
            new.append({"role": "assistant", "content": answer})
        return TurnResult(True, answer, new, usage, gate.used)


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------

class Conversation:
    """一次多轮问答会话：持有规范化历史，按 ContextPolicy 渲染、提交、压缩。同一会话串行。"""

    def __init__(self, owner: "PiBridge", session_id: str, system: str, tools: list[dict],
                 policy: ContextPolicy | None = None):
        self.owner = owner
        self.session_id = session_id
        self.system = system
        self.tools = list(tools or [])
        self.policy = policy or owner.policy
        self.transcript = Transcript()
        self.last_turn: dict | None = None
        self.broken = False
        self.on_commit = None  # 每次提交/压缩后回调（SessionManager 用来落盘）
        self._lock = threading.Lock()
        self._compacting = False
        backend = owner.backend
        self._runner = backend.runner() if hasattr(backend, "runner") else HttpTurnRunner(backend)

    @property
    def history(self) -> list[dict]:
        return self.transcript.messages

    def ask(self, text: str, context: str | None = None) -> str | None:
        text = str(text or "").strip()
        with self._lock:
            result = self._runner.run_turn(
                system_with_summary(self.system, self.transcript.summary),
                render(self.transcript.messages, self.policy), with_context(text, context),
                self.tools, self.policy, self.session_id)
            self.transcript.add_usage(result.usage)
            self.last_turn = {"ok": result.ok, "tools": list(result.tools), "usage": dict(result.usage),
                              "error": result.error, "at": int(time.time() * 1000)}
            if result.broken:
                self.broken = True
            if not result.ok:
                self.owner.log(f"[pi_bridge] session {self.session_id} turn failed: {result.error}")
                return None
            self.transcript.messages.extend(
                [{"role": "user", "content": text}]
                + shrink_tool_results(result.messages, self.policy.old_tool_result_chars))
            self.transcript.turns += 1
            self.transcript.last_used = time.time()
            need_compaction = pick_compaction(self.transcript.messages, self.policy) > 0
        self._committed()
        if need_compaction:
            self._compact_later()
        return result.answer

    # -- 压缩 ---------------------------------------------------

    def _compact_later(self):
        with self._lock:
            if self._compacting:
                return
            self._compacting = True

        def job():
            try:
                self.compact_now()
            except Exception as e:  # pragma: no cover
                self.owner.log(f"[pi_bridge] session {self.session_id} compaction failed: {e}")
            finally:
                self._compacting = False
        threading.Thread(target=job, daemon=True, name=f"compact-{self.session_id}"[:60]).start()

    def compact_now(self) -> bool:
        """把超预算的较早轮次压成摘要（模型失败时直接丢弃）。持会话锁，期间同会话的 ask 会等待。"""
        with self._lock:
            messages = self.transcript.messages
            n = pick_compaction(messages, self.policy)
            if n <= 0:
                return False
            system, prompt = summary_request(self.transcript.summary, messages[:n], self.policy.summary_chars)
            summary = self.owner.complete_messages(system, [{"role": "user", "content": prompt}])
            if summary and summary.strip():
                self.transcript.summary = clip(summary.strip(), self.policy.summary_chars * 2)
            else:
                self.owner.log(f"[pi_bridge] session {self.session_id} 摘要失败，直接丢弃最早 {n} 条消息")
            del messages[:n]
            self.transcript.compactions += 1
        self._committed()
        return True

    # -- 落盘与观测 -----------------------------------------------

    def _committed(self):
        hook = self.on_commit
        if hook is not None:
            try:
                hook(self)
            except Exception as e:  # pragma: no cover
                self.owner.log(f"[pi_bridge] session {self.session_id} on_commit failed: {e}")

    def export(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self.transcript.to_json(), ensure_ascii=False))

    def restore(self, data: dict):
        with self._lock:
            self.transcript = Transcript.from_json(data)

    def stats(self) -> dict:
        with self._lock:
            t = self.transcript
            return {"sessionId": self.session_id, "turns": t.turns, "messages": len(t.messages),
                    "chars": transcript_chars(t.messages), "summaryChars": len(t.summary),
                    "compactions": t.compactions, "usage": dict(t.usage), "tools": [x["name"] for x in self.tools],
                    "createdAt": int(t.created * 1000), "lastUsedAt": int(t.last_used * 1000),
                    "lastTurn": self.last_turn, "broken": self.broken}


SessionBridge = Conversation  # 兼容旧名


# ---------------------------------------------------------------------------
# 桥：统一外观
# ---------------------------------------------------------------------------

class PiBridge:
    def __init__(self, backend, backend_kind: str, warn: str = "", policy: ContextPolicy | None = None,
                 session_cfg: dict | None = None):
        self.backend = backend
        self.backend_kind = backend_kind
        self.warn = warn
        self.policy = policy or ContextPolicy()
        self.session_cfg = dict(session_cfg or {})  # agent.config.json 的 session 段（会话过期/落盘）
        self.log = print

    # -- 工厂 -- 从 agent.config.json 的 dict 构建 -------------------------

    @staticmethod
    def from_config(cfg: dict | None) -> "PiBridge | None":
        """cfg 形如 {"provider":"auto","base_url":...,"api_key":...,"model":...,"context":{...}}。
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
        temperature = cfg.get("temperature", 0)
        policy = ContextPolicy.from_config(cfg.get("context"), timeout)
        extra = {"policy": policy, "session_cfg": cfg.get("session") if isinstance(cfg.get("session"), dict) else {}}

        if provider in ("http", "pi", "auto"):
            if not base_url or not api_key:
                print("[pi_bridge] 配置缺少 base_url/api_key，跳过 Agent", file=sys.stderr)
                return None
        if provider in ("pi", "auto"):
            try:
                backend = PiSdkBackend(base_url, api_key, model, timeout=timeout, temperature=temperature)
                return PiBridge(backend, "pi", **extra)
            except BridgeError as e:
                if provider == "pi":
                    print(f"[pi_bridge] {e}", file=sys.stderr)
                    return None
                print(f"[pi_bridge] {e}，降级 http 后端", file=sys.stderr)
                return PiBridge(HttpBackend(base_url, api_key, model, timeout, temperature), "http", **extra)
        if provider == "http":
            return PiBridge(HttpBackend(base_url, api_key, model, timeout, temperature), "http", **extra)
        if provider == "mock":
            try:
                backend = PiSdkBackend("", "", model or "mock-model", mock=True, timeout=timeout)
                return PiBridge(backend, "mock", **extra)
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
                      tools: list[dict] | None = None) -> Conversation:
        if any(tool["name"] == "compare_quotes" for tool in (tools or [])):
            system += "\n\n" + quote_guidance() + "\n询价比较调用compare_quotes，使用quote_evidence核查依据。latest_quotes/search_quotes是历史记录，不代表当前有效报价。不得把待确认方案当最低可采购价格，不得编造基准行情。"
        return Conversation(self, session_id, system, tools or [])

    # -- 报价格式化（LLM，失败自动降级模板） -------------------------

    def format_quotes(self, room: str, rows: list[dict]) -> str:
        payload = [{
            "供应商": r.get("供应商", ""), "项目": r.get("项目", ""), "价格": r.get("价格", ""),
            "单位": r.get("单位", ""), "数量": r.get("数量", ""),
            "备注": r.get("备注", ""), "发送人": r.get("sender", ""),
            "时间": r.get("timeLabel", ""),
        } for r in rows]
        try:
            out = self.complete_chat(
                FORMAT_SYSTEM,
                f"房间：{room_label(room, rows)}\n" + json.dumps(payload, ensure_ascii=False))
            if out and out.strip():
                return out.strip()
        except Exception as e:
            self.log(f"[pi_bridge] format failed, fallback template: {e}")
        return fallback_format(room, rows)
