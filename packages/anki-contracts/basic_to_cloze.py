"""Canonical structural checks for Basic -> Cloze conversions.

The language model remains responsible for the semantic rewrite.  These checks
reject mechanical conversions that are known to be unsafe before an operation
can reach the Anki collection.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

_CLOZE_RE = re.compile(
    r"\{\{c(?P<number>[1-9]\d*)::(?P<body>.*?)(?:::(?P<hint>.*?))?\}\}", re.DOTALL
)
_LOW_INFORMATION_ANSWERS = {
    "a",
    "as",
    "de",
    "do",
    "e",
    "eh",
    "em",
    "is",
    "nao",
    "não",
    "no",
    "o",
    "os",
    "pode",
    "porque",
    "sim",
}
_STRUCTURAL_PATTERNS = (
    re.compile(r"<img\b[^>]*>", re.IGNORECASE),
    re.compile(r"<audio\b[^>]*>.*?</audio\s*>", re.IGNORECASE | re.DOTALL),
    re.compile(r"<source\b[^>]*>", re.IGNORECASE),
    re.compile(r"\[sound:[^\]]+\]", re.IGNORECASE),
    re.compile(r"\\\(.*?\\\)", re.DOTALL),
    re.compile(r"\\\[.*?\\\]", re.DOTALL),
    re.compile(r"\$\$.*?\$\$", re.DOTALL),
)


class _PlainTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def plain_text(value: str) -> str:
    parser = _PlainTextParser()
    parser.feed(value or "")
    parser.close()
    return html.unescape(" ".join("".join(parser.parts).split()))


def comparable_text(value: str) -> str:
    value = plain_text(value).casefold()
    value = value.replace("é", "e")
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def cloze_deletions(value: str) -> list[dict]:
    return [
        {
            "number": int(match.group("number")),
            "body": match.group("body"),
            "hint": match.group("hint") or "",
        }
        for match in _CLOZE_RE.finditer(value or "")
    ]


def render_cloze_answers(value: str) -> str:
    return _CLOZE_RE.sub(lambda match: match.group("body"), value or "")


def structural_fragments(value: str) -> list[str]:
    fragments: list[str] = []
    for pattern in _STRUCTURAL_PATTERNS:
        fragments.extend(match.group(0) for match in pattern.finditer(value or ""))
    return fragments


def validate_basic_to_cloze_fields(
    *,
    front: str,
    back: str,
    text: str,
    back_extra: str,
) -> dict:
    """Validate a model-authored conversion and return audit-friendly stats.

    This intentionally does not try to generate prose or prove factual
    equivalence.  When a faithful declarative rewrite cannot be authored, the
    caller must leave the source note unchanged and request manual review.
    """

    values = {
        "front": front,
        "back": back,
        "text": text,
        "back_extra": back_extra,
    }
    for name, value in values.items():
        if not isinstance(value, str):
            raise ValueError(f"basic_to_cloze_invalid_{name}")
    if cloze_deletions(front) or cloze_deletions(back):
        raise ValueError("basic_to_cloze_source_already_cloze")
    if not comparable_text(front) or not comparable_text(back):
        raise ValueError("basic_to_cloze_source_incomplete")

    deletions = cloze_deletions(text)
    if not deletions:
        raise ValueError("basic_to_cloze_missing_cloze")
    if cloze_deletions(back_extra):
        raise ValueError("basic_to_cloze_extra_contains_cloze")
    if "?" in plain_text(render_cloze_answers(text)):
        raise ValueError("basic_to_cloze_question_not_declarative")

    for deletion in deletions:
        answer = comparable_text(deletion["body"])
        if answer in _LOW_INFORMATION_ANSWERS:
            raise ValueError("basic_to_cloze_low_information_deletion")
        body_plain = plain_text(deletion["body"])
        body_words = comparable_text(deletion["body"]).split()
        oversized_conjunction = len(body_words) > 12 and " e " in f" {body_plain.casefold()} "
        if (
            "<br" in deletion["body"].casefold()
            or len(re.findall(r"[.!?]+", body_plain)) > 1
            or oversized_conjunction
        ):
            raise ValueError("basic_to_cloze_oversized_deletion")

    normalized_front = comparable_text(front.rstrip("? "))
    normalized_text = comparable_text(render_cloze_answers(text))
    normalized_back = comparable_text(back)
    whole_back_hidden = any(
        comparable_text(deletion["body"]) == normalized_back for deletion in deletions
    )
    back_has_multiple_propositions = len(re.findall(r"[.!?]+", plain_text(back))) > 1
    if whole_back_hidden and back_has_multiple_propositions:
        raise ValueError("basic_to_cloze_entire_multisentence_back_hidden")
    if whole_back_hidden and normalized_front and normalized_front in normalized_text:
        raise ValueError("basic_to_cloze_mechanical_front_back_join")

    rendered_main = comparable_text(render_cloze_answers(text))
    if comparable_text(back_extra) and comparable_text(back_extra) == rendered_main:
        raise ValueError("basic_to_cloze_extra_duplicates_text")

    target = f"{text}\n{back_extra}"
    missing_fragments = [
        fragment for fragment in structural_fragments(f"{front}\n{back}") if fragment not in target
    ]
    if missing_fragments:
        raise ValueError("basic_to_cloze_structural_content_missing")

    numbers = sorted({item["number"] for item in deletions})
    if numbers[0] != 1 or numbers != list(range(1, numbers[-1] + 1)):
        raise ValueError("basic_to_cloze_noncontiguous_cloze_numbers")

    return {
        "cloze_numbers": numbers,
        "cloze_deletion_count": len(deletions),
        "structural_fragment_count": len(structural_fragments(f"{front}\n{back}")),
        "whole_back_hidden": whole_back_hidden,
    }
