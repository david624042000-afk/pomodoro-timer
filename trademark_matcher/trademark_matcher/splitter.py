"""
Compound term detection and splitting.

Handles two patterns:
  • Period-separated  "Soaps.Dental preparations.Perfumery"
  • Comma-separated   "laundry bleach, fabric softeners, stain removers, ..."
    (only when ≥ 3 segments each ≥ 2 words, with smart clause-continuation)
"""

import re

_REJOIN_FIRST = {
    "other", "except", "not", "but", "than", "including", "such",
}
_REJOIN_BIGRAM = {
    "not for", "and for", "or for", "other than", "for use", "except for",
}


def _smart_comma_split(text: str) -> list[str]:
    parts = [p.strip() for p in text.split(",")]
    items = [parts[0]] if parts else []
    for part in parts[1:]:
        words = part.strip().split()
        if not words:
            continue
        first = words[0].lower()
        bigram = " ".join(words[:2]).lower() if len(words) >= 2 else ""
        if first in _REJOIN_FIRST or bigram in _REJOIN_BIGRAM:
            items[-1] = items[-1] + "," + part   # continuation clause
        else:
            items.append(part.strip())
    return [i.strip() for i in items if i.strip()]


def split_compound(text: str) -> list[str]:
    """
    Return a list of sub-terms.
    Returns [text] unchanged if no compound pattern detected.
    """
    text = text.strip()

    # 1. Period-separated items (e.g. "Soaps.Dental preparations")
    period_parts = [
        p.strip().rstrip(".")
        for p in re.split(r"\.(?=[A-Z\s])", text)
        if p.strip()
    ]
    if len(period_parts) > 1:
        return period_parts

    # 2. Comma-separated noun phrases (≥3 segments, each ≥2 words)
    comma_parts = _smart_comma_split(text)
    if len(comma_parts) >= 3 and all(len(p.split()) >= 2 for p in comma_parts):
        return comma_parts

    return [text]


def is_compound(text: str) -> bool:
    return len(split_compound(text)) > 1
