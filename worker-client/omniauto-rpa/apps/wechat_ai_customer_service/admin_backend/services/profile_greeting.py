"""Dynamic greeting generator based on customer profile.

Rules:
- High-confidence male + new customer → "许先生您好"
- High-confidence male + engaged → "许哥"
- High-confidence female + new customer → "许女士您好"
- High-confidence female + engaged → "许姐"
- Low-confidence / unknown → "您好" or use nickname directly
"""

from __future__ import annotations

from typing import Any


class GreetingGenerator:
    """Generate context-aware greeting for a customer."""

    def __init__(self, profile: dict[str, Any] | None = None) -> None:
        self.profile = profile or {}

    def generate(self, *, fallback_name: str = "") -> str:
        """Return greeting prefix like '许哥' or '您好' or ''."""
        basic = self.profile.get("basic_info") if isinstance(self.profile.get("basic_info"), dict) else {}
        display_name = str(self.profile.get("display_name") or fallback_name or "").strip()
        if not display_name:
            return ""

        gender = str(basic.get("gender") or "").strip().lower()
        confidence = float(basic.get("gender_confidence") or 0.0)
        total_messages = int(basic.get("total_messages", 0) or 0)

        # Determine relationship stage
        stage = "engaged" if total_messages >= 10 else "new"

        # High confidence
        if confidence >= 0.8:
            surname = _extract_surname(display_name)
            if gender == "male":
                if stage == "new":
                    return f"{surname}先生您好" if surname else "您好"
                return f"{surname}哥" if surname else "您好"
            if gender == "female":
                if stage == "new":
                    return f"{surname}女士您好" if surname else "您好"
                return f"{surname}姐" if surname else "您好"

        # Medium confidence — use neutral polite
        if confidence >= 0.5:
            return "您好"

        # Low confidence — just use the name or neutral
        return ""

    def inject_into_reply(self, reply_text: str, *, fallback_name: str = "") -> str:
        """Prepend greeting to reply if not already present."""
        greeting = self.generate(fallback_name=fallback_name)
        if not greeting:
            return reply_text
        text = str(reply_text or "").strip()
        if not text:
            return greeting
        # Avoid double greeting
        if text.startswith(greeting) or any(text.startswith(g) for g in ("您好", "你好", "亲", "Hi", "Hello")):
            return text
        return f"{greeting}，{text}"


def _extract_surname(full_name: str) -> str:
    """Extract surname from Chinese name."""
    name = str(full_name or "").strip()
    if not name:
        return ""
    # Common compound surnames
    compound = ("欧阳", "太史", "端木", "上官", "司马", "东方", "独孤", "南宫",
                "万俟", "闻人", "夏侯", "诸葛", "尉迟", "公羊", "赫连", "澹台",
                "皇甫", "宗政", "濮阳", "公冶", "太叔", "申屠", "公孙", "慕容",
                "仲孙", "钟离", "长孙", "宇文", "司徒", "鲜于", "司空", "闾丘",
                "子车", "亓官", "司寇", "巫马", "公西", "颛孙", "壤驷", "公良",
                "漆雕", "乐正", "宰父", "谷梁", "拓跋", "夹谷", "轩辕", "令狐",
                "段干", "百里", "呼延", "东郭", "南门", "羊舌", "微生", "梁丘",
                "左丘", "东门", "西门", "南宫")
    for c in compound:
        if name.startswith(c):
            return c
    return name[0] if name else ""


def infer_gender_from_text(text: str) -> tuple[str, float]:
    """Infer gender from message text using pronoun statistics.

    Returns (gender, confidence) where gender is 'male', 'female', or ''.
    """
    text = str(text or "").strip()
    if not text:
        return "", 0.0

    # Female indicators
    female_pronouns = ("我男朋友", "我老公", "我儿子", "我先生", "爸爸", "父亲",
                       "哥哥", "弟弟", "男的", "汉子", "爷们")
    # Male indicators
    male_pronouns = ("我女朋友", "我老婆", "我女儿", "我太太", "我夫人", "妈妈",
                     "母亲", "姐姐", "妹妹", "女的", "妹子", "姑娘")

    f_count = sum(1 for p in female_pronouns if p in text)
    m_count = sum(1 for p in male_pronouns if p in text)

    if f_count > m_count:
        return "female", min(0.9, 0.6 + 0.1 * f_count)
    if m_count > f_count:
        return "male", min(0.9, 0.6 + 0.1 * m_count)
    return "", 0.0
