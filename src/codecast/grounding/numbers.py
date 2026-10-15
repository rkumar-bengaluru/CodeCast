"""Single source of truth for number/identifier tokenization and matching (spec §7.1, §7.2)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

NUMBER_RE = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?%?")
_TOKEN_RE = re.compile(r"(?:[A-Za-z0-9_@.\-/]|,(?=\d{3}(?!\d)))+%?")

_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
         "eighty": 80, "ninety": 90}
_SCALES = {"thousand": 1_000, "million": 1_000_000}
_NUMWORD = "|".join(sorted([*_UNITS, *_TENS, "hundred", *_SCALES], key=len, reverse=True))
_SEQ_RE = re.compile(
    rf"\b(?:{_NUMWORD})(?:(?:\s+and\s+|\s+|-)(?:{_NUMWORD}))*\b", re.IGNORECASE)


@dataclass(frozen=True)
class Claim:
    kind: Literal["number", "identifier"]
    token: str


def normalize_text(text: str) -> str:
    return text.replace("−", "-")


def _words_value(words: list[str]) -> int:
    total, current = 0, 0
    for w in words:
        if w in _UNITS:
            current += _UNITS[w]
        elif w in _TENS:
            current += _TENS[w]
        elif w == "hundred":
            current = (current or 1) * 100
        else:
            total += (current or 1) * _SCALES[w]
            current = 0
    return total + current


def words_to_digits(text: str) -> str:
    def repl(m: re.Match) -> str:
        words = [w for w in re.split(r"\s+|-", m.group(0).lower()) if w and w != "and"]
        return str(_words_value(words))
    return _SEQ_RE.sub(repl, text)


def split_tokens(text: str) -> list[str]:
    out = []
    for m in _TOKEN_RE.finditer(text):
        tok = m.group(0).rstrip(".-/")
        if tok:
            out.append(tok)
    return out


def classify(token: str) -> Literal["number", "identifier"] | None:
    if NUMBER_RE.fullmatch(token):
        return "number"
    if any(ch.isdigit() for ch in token):
        return "identifier"
    return None


def _prepared_tokens(text: str) -> list[str]:
    return split_tokens(words_to_digits(normalize_text(text)))


def extract_claims(text: str) -> list[Claim]:
    claims = []
    for tok in _prepared_tokens(text):
        kind = classify(tok)
        if kind:
            claims.append(Claim(kind, tok))
    return claims


def source_tokens(text: str) -> set[str]:
    return set(_prepared_tokens(text))


def source_number_literals(text: str) -> list[str]:
    return [t for t in _prepared_tokens(text) if classify(t) == "number"]
