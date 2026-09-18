"""Bounded language-polarity check; dimensions and reply strategy remain external.

Only an explicit local negation/question suppresses a cargo-fit predicate. This
does not establish dimensions, authorize a fact, or author a customer reply.
"""

from __future__ import annotations

import re
import unicodedata


_CLAIM = re.compile(r"大概率能[塞装]|基本能[塞装]|应该能[塞装]|能[塞装]下|[塞装]得下|够装|问题不大")
_SCOPE = re.compile(r"[。.!！?？;；\r\n]|但是|不过|然而|但")
_UNCERTAIN = re.compile(r"不能保证|无法保证|不保证|不能确定|无法确定|不确定|尚不能确认|未确认|不一定")
_DOUBLE_NEGATIVE = re.compile(r"不是不能[塞装]|不能说[^，,。！？!?；;\n]*[塞装]不下")
_ASSERTIVE = re.compile(r"肯定|一定|必定|保证|大概率|基本|应该")
_QUOTES = re.compile(r'“([^“”]*)”|「([^「」]*)」|"([^"\n]*)"')
_CUSTOMER_QUOTE = re.compile(r"(?:您|你|客户)(?:刚才|之前)?(?:问|说)(?:的)?\s*[：:]?\s*$")
_QUESTION = re.compile(r"能不能|能否|是否|[吗么?？]")


def has_unsupported_cargo_assertion(reply: str, *, current_message: str = "") -> bool:
    text = unicodedata.normalize("NFKC", str(reply or ""))
    current = unicodedata.normalize("NFKC", str(current_message or ""))
    # Quoted predicates cannot acquire a negation/question exemption unless the
    # complete customer question is actually present in this turn.
    pieces: list[str] = []
    cursor = 0
    for quote in _QUOTES.finditer(text):
        quoted = next(value for value in quote.groups() if value is not None)
        pieces.append(text[cursor:quote.start()])
        if _CLAIM.search(quoted) or _DOUBLE_NEGATIVE.search(quoted):
            if not (quoted and quoted in current and _QUESTION.search(quoted)
                    and _CUSTOMER_QUOTE.search(text[cursor:quote.start()])):
                return True
            pieces.append(" ")
        else:
            pieces.append(quote.group())
        cursor = quote.end()
    pieces.append(text[cursor:])
    for clause in _SCOPE.split("".join(pieces)):
        if _DOUBLE_NEGATIVE.search(clause):
            return True
        previous_end = 0
        for claim in _CLAIM.finditer(clause):
            prefix = clause[previous_end:claim.start()]
            previous_end = claim.end()
            # The second 能 in 能不能装下 is the detected predicate. Do not
            # globally remove words: a subsequent assertion is a new predicate.
            if prefix.endswith("能不") or re.search(r"(?:能否|是否)\s*$", prefix):
                continue
            if re.search(r"(?:不|无法)$", prefix) and claim.group().startswith("能"):
                continue
            uncertain = list(_UNCERTAIN.finditer(prefix))
            if uncertain:
                last = uncertain[-1]
                before = prefix[:last.start()]
                governed = prefix[last.end():]
                # One immediate comma may join a negator to its object. A
                # completed earlier object or another comma cannot transfer
                # that exemption. Certainty inside the same object is itself
                # negated (不能保证一定能装下); after a comma it starts a new
                # assertion (不能保证，肯定能装下).
                comma_parts = re.split(r"[,，]", governed)
                local_object = len(comma_parts) == 1 or (
                    len(comma_parts) == 2 and not comma_parts[0].strip()
                )
                if (not re.search(r"不是\s*$", before) and local_object
                        and (len(comma_parts) == 1 or not _ASSERTIVE.search(governed))):
                    continue
            return True
    return False
