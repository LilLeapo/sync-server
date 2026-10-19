#!/usr/bin/env python3
"""本地 Skill（.agents/skills/<name>/SKILL.md）的索引与按需读取。

沿用 pi 的 Skill 做法：问答 Agent 的 system prompt 里只列 Skill 的名称和一句说明，
正文和参考资料由 Agent 在需要时调用 read_skill 读取，不再整段拼进 system prompt
（否则几千字的领域知识会压过回答本身，问什么都往报价上扯）。

报价提取（quote_agent）仍用 quote_skill.quote_guidance() 读取全文 —— 提取每次都需要。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent.parent / ".agents" / "skills"


def _split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    head, _, body = text[3:].partition("\n---")
    meta = {}
    for line in head.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip():
            meta[key.strip()] = value.strip()
    return meta, body.lstrip("-").strip()


@lru_cache(maxsize=1)
def list_skills() -> tuple[dict, ...]:
    """[{name, description, files}]，files 为可读取的参考资料（相对 Skill 目录）。"""
    out = []
    if not SKILLS_DIR.is_dir():
        return ()
    for skill_md in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        meta, _ = _split_frontmatter(skill_md.read_text(encoding="utf-8"))
        root = skill_md.parent
        files = sorted(str(p.relative_to(root)) for p in root.rglob("*.md") if p != skill_md)
        out.append({"name": meta.get("name") or root.name, "description": meta.get("description") or "",
                    "dir": str(root), "files": files})
    return tuple(out)


def read_skill(name: str, file: str | None = None) -> str:
    skill = next((s for s in list_skills() if s["name"] == name), None)
    if skill is None:
        names = "、".join(s["name"] for s in list_skills()) or "（无）"
        return f"没有名为 {name} 的资料，可读：{names}"
    if file:
        file = file.strip().lstrip("./")
        if file not in skill["files"]:
            return f"{name} 里没有 {file}，可读文件：{'、'.join(skill['files']) or '（无）'}"
        return (Path(skill["dir"]) / file).read_text(encoding="utf-8").strip()
    _, body = _split_frontmatter((Path(skill["dir"]) / "SKILL.md").read_text(encoding="utf-8"))
    if skill["files"]:
        body += "\n\n（参考资料：" + "、".join(skill["files"]) + "；需要时用 read_skill 的 file 参数读取）"
    return body
