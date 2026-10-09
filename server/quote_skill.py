"""Load the repository's supplier vocabulary for extraction and QA."""
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def quote_guidance() -> str:
    root = Path(__file__).resolve().parent.parent / ".agents/skills/supplier-quote-analysis"
    skill = (root / "SKILL.md").read_text(encoding="utf-8")
    body = skill.split("---", 2)[-1].strip()
    reference = (root / "references/phrases-and-cases.md").read_text(encoding="utf-8")
    return body + "\n\n" + reference
