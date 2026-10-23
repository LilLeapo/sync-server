#!/usr/bin/env python3
"""问答 Agent 的提示词 —— 分层组装，各层只写这一层该管的事。

    system prompt = 角色（渠道默认或用户/设备自定义）
                  + 回答规则（所有问答共用：答所问、不主动汇报无关情况、不重复）
                  + 工具使用要点（按会话实际拥有的工具生成，几行）
                  + 可按需读取的资料索引（有 read_skill 工具时；正文不进 system prompt）

领域知识（报价黑话、比较规则等 Skill）只在 Agent 调 read_skill 时进入上下文，
避免它压过角色和问题本身。报价提取的提示词在 quote_agent.py，不走这里。
"""
from __future__ import annotations

from .skills import list_skills

WECHAT_REPLY_SYSTEM = """你是微信群里的助手，只有在被 @ 时才会看到消息。
回复会直接发到群里：用简体中文，控制在一两段，不用 Markdown 标题、代码块或表格；不要 @ 任何人，也不要复述这些说明。"""

QNA_SYSTEM = """你是群聊助手，可以查询已接入微信群的聊天记录和报价数据。
用简体中文回答；多条数据逐行列出，不用 Markdown 标题和表格。"""

ANSWER_RULES = """回答规则：
- 只回答这次问的内容，篇幅跟问题相称：打招呼、简单问题一两句话就够。
- 需要数据时才调用工具。问题没涉及的情况不要主动汇报（比如没问报价，就不要说有没有报价），也不要每次都附带建议、清单或追问。
- 已经说过的内容不要重复；对方追问时只补充新的信息。
- 只依据工具结果和对话内容回答；查不到就简短说明，不要编造。"""


def tool_hints(names: set[str]) -> str:
    lines = []
    if names & {"current_quotes", "compare_quotes"}:
        lines.append("问「现在」的报价或谁家便宜时用 current_quotes / compare_quotes；"
                     "search_quotes、latest_quotes 查的是历史记录，不代表现在有效。")
    if "compare_quotes" in names:
        lines.append("给采购结论前用 quote_evidence 核对原文；待确认的方案不能当成最低可采购价；基准行情不能编造"
                     + ("，需要时用 futures_quote 查。" if "futures_quote" in names else "。"))
    if names & {"send_message", "send_image", "send_card"}:
        lines.append("send_message / send_image / send_card 会真实发到微信群，只在用户明确要求发送或转发时使用。")
    if "send_card" in names:
        lines.append("要把报价、比较结果或行情做成图时用 send_card；发出后回复一句即可，不要再把表格贴成文字。")
    return ("工具使用要点：\n" + "\n".join("- " + line for line in lines)) if lines else ""


def skills_index() -> str:
    skills = list_skills()
    if not skills:
        return ""
    return ("可按需读取的资料（调用 read_skill；只在问题需要时读，平时不用读）：\n"
            + "\n".join(f"- {s['name']}：{s['description']}" for s in skills))


def compose_system(role: str, tools: list[dict] | None = None) -> str:
    names = {t.get("name") for t in tools or [] if isinstance(t, dict)}
    parts = [str(role or "").strip(), ANSWER_RULES, tool_hints(names)]
    if "read_skill" in names:
        parts.append(skills_index())
    return "\n\n".join(p for p in parts if p)


def wechat_base_prompt() -> str:
    """网页设备设置里展示的「固定的基础提示词」（设备职责说明接在它后面）。"""
    return WECHAT_REPLY_SYSTEM + "\n\n" + ANSWER_RULES
