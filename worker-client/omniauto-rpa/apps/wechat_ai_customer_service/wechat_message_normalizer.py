"""Normalize OCR-derived WeChat message text before semantic processing."""

from __future__ import annotations

import re
from typing import Any


BUSINESS_FIRST_LINE_TERMS = (
    "预算",
    "价格",
    "车型",
    "车源",
    "电话",
    "地址",
    "姓名",
    "库存",
    "贷款",
    "置换",
    "公司",
    "商品",
    "型号",
    "sku",
    "数量",
    "单价",
    "总价",
    "报价",
    "发票",
    "开票",
    "收货",
    "联系人",
    "备注",
)
GREETING_LIKE_TERMS = ("你好", "您好", "在吗", "在么", "在不在", "有人吗", "哈喽", "嗨")
GENERIC_SENDER_VALUES = {"", "unknown", "customer", "contact", "other", "group_member", "用户", "客户"}
TITLE_SALUTATION_VALUES = {
    "老师",
    "老板",
    "老总",
    "客服",
    "顾问",
    "销售",
    "师傅",
    "哥",
    "姐",
    "大哥",
    "姐姐",
    "美女",
    "帅哥",
    "朋友",
    "兄弟",
}


def normalize_text_for_speaker_check(value: Any) -> str:
    return re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff]+", "", str(value or "")).lower()


def split_wechat_ocr_speaker_prefix(
    text: Any,
    *,
    conversation_type: str = "",
    target_name: str = "",
    known_speakers: list[str] | tuple[str, ...] | set[str] | None = None,
    allow_unlisted_name_like_prefix: bool = False,
) -> dict[str, Any]:
    """Split an OCR message shaped like ``speaker\\nbody``.

    wxauto4 returns structured sender/body fields. RPA OCR can accidentally
    merge the group member name above a bubble into the bubble body.  Keep this
    conservative: only short name-like first lines are stripped, while business
    labels such as ``预算\\n15万`` remain intact.
    """

    raw = str(text or "").strip()
    if not raw:
        return {"changed": False, "content": "", "speaker_name": ""}
    split = split_one_line_colon_prefix(
        raw,
        conversation_type=conversation_type,
        target_name=target_name,
        known_speakers=known_speakers,
        allow_unlisted_name_like_prefix=allow_unlisted_name_like_prefix,
    )
    if split.get("changed"):
        return split
    if "\n" not in raw:
        return {"changed": False, "content": raw, "speaker_name": ""}
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) < 2:
        return {"changed": False, "content": raw, "speaker_name": ""}
    first = lines[0].strip(" :：，,。.;；")
    rest = "\n".join(lines[1:]).strip()
    if not is_speaker_prefix_candidate(
        first,
        rest,
        conversation_type=conversation_type,
        target_name=target_name,
        known_speakers=known_speakers,
        allow_unlisted_name_like_prefix=allow_unlisted_name_like_prefix,
    ):
        return {"changed": False, "content": raw, "speaker_name": ""}
    return {
        "changed": True,
        "content": rest,
        "speaker_name": first,
        "original_content": raw,
        "reason": "ocr_group_speaker_prefix",
    }


def split_one_line_colon_prefix(
    raw: str,
    *,
    conversation_type: str = "",
    target_name: str = "",
    known_speakers: list[str] | tuple[str, ...] | set[str] | None = None,
    allow_unlisted_name_like_prefix: bool = False,
) -> dict[str, Any]:
    match = re.match(r"^\s*([A-Za-z_\u4e00-\u9fff·.\-]{1,16})\s*[：:]\s*(\S.+)$", str(raw or ""), flags=re.DOTALL)
    if not match:
        return {"changed": False, "content": raw, "speaker_name": ""}
    first = match.group(1).strip()
    rest = match.group(2).strip()
    if not is_speaker_prefix_candidate(
        first,
        rest,
        conversation_type=conversation_type,
        target_name=target_name,
        known_speakers=known_speakers,
        allow_unlisted_name_like_prefix=allow_unlisted_name_like_prefix,
    ):
        return {"changed": False, "content": raw, "speaker_name": ""}
    return {
        "changed": True,
        "content": rest,
        "speaker_name": first,
        "original_content": raw,
        "reason": "ocr_colon_speaker_prefix",
    }


def is_speaker_prefix_candidate(
    first_line: str,
    rest: str,
    *,
    conversation_type: str = "",
    target_name: str = "",
    known_speakers: list[str] | tuple[str, ...] | set[str] | None = None,
    allow_unlisted_name_like_prefix: bool = False,
) -> bool:
    first = str(first_line or "").strip(" :：，,。.;；")
    body = str(rest or "").strip()
    if not first or not body:
        return False
    first_norm = normalize_text_for_speaker_check(first)
    body_norm = normalize_text_for_speaker_check(body)
    if not first_norm or not body_norm:
        return False
    if len(first_norm) > 16:
        return False
    if re.search(r"[?？!！。；;，,、]", first):
        return False
    if re.search(r"\d", first_norm):
        return False
    if not re.fullmatch(r"[\u4e00-\u9fffA-Za-z_·.\-]{1,16}", first):
        return False
    if contains_any(first_norm, BUSINESS_FIRST_LINE_TERMS):
        return False
    if contains_any(first_norm, GREETING_LIKE_TERMS):
        return False
    if first_norm in {normalize_text_for_speaker_check(item) for item in TITLE_SALUTATION_VALUES}:
        return False

    known = {normalize_text_for_speaker_check(item) for item in (known_speakers or []) if str(item).strip()}
    target_norm = normalize_text_for_speaker_check(target_name)
    conversation = str(conversation_type or "").strip().lower()
    if first_norm in known:
        return True
    if target_norm and first_norm == target_norm:
        return True
    if conversation == "group":
        return True
    if allow_unlisted_name_like_prefix and looks_like_ocr_name_label(first, body_norm):
        return True
    # Private chats are stricter: strip only known peer names. Otherwise a real
    # two-line message such as ``老师\n这个多少钱`` could lose its salutation.
    return False


def looks_like_ocr_name_label(first_line: str, body_norm: str) -> bool:
    first = normalize_text_for_speaker_check(first_line)
    if not first or not body_norm:
        return False
    if len(first) < 2 or len(first) > 8:
        return False
    if first in {normalize_text_for_speaker_check(item) for item in TITLE_SALUTATION_VALUES}:
        return False
    body = body_norm.lower()
    strong_body_terms = (
        "在吗",
        "在不在",
        "你好",
        "您好",
        "多少钱",
        "价格",
        "报价",
        "车况",
        "配置",
        "贷款",
        "置换",
        "预算",
        "推荐",
        "有吗",
        "还有吗",
        "能不能",
        "可以吗",
        "怎么",
        "哪里",
        "什么",
        "哪台",
        "哪辆",
        "这辆",
        "这台",
        "刚才",
    )
    return any(term in body for term in strong_body_terms)


def normalize_wechat_message_record(
    record: dict[str, Any],
    *,
    conversation_type: str = "",
    target_name: str = "",
    known_speakers: list[str] | tuple[str, ...] | set[str] | None = None,
    allow_unlisted_name_like_prefix: bool = False,
) -> dict[str, Any]:
    """Return a copy whose ``content`` is semantic body-only text."""

    if not isinstance(record, dict):
        return {}
    next_record = dict(record)
    content = str(next_record.get("content") or next_record.get("text") or "")
    speaker_candidates = [
        str(next_record.get("sender") or ""),
        str(next_record.get("group_member_name") or ""),
        str(next_record.get("speaker_name") or ""),
    ]
    speaker_candidates.extend([str(item) for item in (known_speakers or []) if str(item).strip()])
    split = split_wechat_ocr_speaker_prefix(
        content,
        conversation_type=conversation_type,
        target_name=target_name,
        known_speakers=speaker_candidates,
        allow_unlisted_name_like_prefix=allow_unlisted_name_like_prefix,
    )
    if not split.get("changed"):
        return next_record
    speaker_name = str(split.get("speaker_name") or "").strip()
    cleaned = str(split.get("content") or "").strip()
    next_record["content"] = cleaned
    if "text" in next_record:
        next_record["text"] = cleaned
    next_record.setdefault("original_content", content)
    next_record["speaker_name"] = speaker_name
    next_record["ocr_speaker_prefix"] = {
        "speaker_name": speaker_name,
        "original_content": content,
        "reason": str(split.get("reason") or "ocr_speaker_prefix"),
    }
    if speaker_name and str(conversation_type or "").strip().lower() == "group":
        next_record.setdefault("group_member_name", speaker_name)
        sender_value = str(next_record.get("sender") or "").strip()
        if normalize_text_for_speaker_check(sender_value) in GENERIC_SENDER_VALUES:
            next_record["sender"] = speaker_name
    return next_record


def contains_any(text: str, terms: tuple[str, ...] | list[str] | set[str]) -> bool:
    normalized = str(text or "").lower()
    return any(str(term).lower() in normalized for term in terms if str(term))
