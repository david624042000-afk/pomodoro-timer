"""
Matching engine: rule-based, no LLM required.

Match pipeline per input term:
  1. Exact         → 完全對應  (one unique Chinese name)
  2. Exact-multi   → 多項候選  (same English, multiple Chinese names)
  3. Token+fuzzy   → 接近對應 / 多項候選
     combined_score = 0.4 * char_ratio + 0.6 * token_jaccard
     - combined >= NEAR_THRESHOLD and only one Chinese name → 接近對應
     - combined >= MULTI_THRESHOLD → 多項候選
  4. No match      → 待手動

Why token_jaccard dominates (60%):
  Prevents long shared suffixes (e.g. "for cosmetic purposes") from
  inflating similarity between semantically different terms.
"""

import re
import difflib

STATUS_EXACT  = "✓ 完全對應"
STATUS_NEAR   = "~ 接近對應"
STATUS_MULTI  = "? 多項候選"
STATUS_MANUAL = "○ 待手動"

NEAR_THRESHOLD  = 0.65
MULTI_THRESHOLD = 0.50
MAX_CANDIDATES  = 8   # cap per input term to keep Excel readable

_STOPWORDS = {
    "for", "the", "of", "and", "in", "to", "by", "a", "an", "with",
    "from", "as", "at", "on", "use", "used", "non", "other", "than",
    "preparation", "preparations", "purpose", "purposes",
}


def _stem(word: str) -> str:
    """Minimal English stemming: strip trailing inflections."""
    if len(word) > 5 and word.endswith("ing"):
        return word[:-3]
    if len(word) > 4 and word.endswith("ed"):
        return word[:-2]
    if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _significant_tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z]+", text.lower())
    return {_stem(w) for w in words if len(w) >= 3 and w not in _STOPWORDS}


def _token_jaccard(a: str, b: str) -> float:
    ta = _significant_tokens(a)
    tb = _significant_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _combined_score(norm_input: str, norm_key: str) -> float:
    char_r = difflib.SequenceMatcher(None, norm_input, norm_key, autojunk=False).ratio()
    tok_j  = _token_jaccard(norm_input, norm_key)
    return 0.4 * char_r + 0.6 * tok_j


def normalize(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"\s+", " ", text)
    return text.rstrip(".")


def parse_input(raw: str) -> list[str]:
    """Split on semicolons (preferred) or commas. Strip blanks."""
    sep = ";" if ";" in raw else ","
    return [t.strip() for t in raw.split(sep) if t.strip()]


def _deduplicate(records: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    out: list[dict] = []
    for r in records:
        key = (r["code"], r["chinese"])
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _unique_chinese(records: list[dict]) -> list[str]:
    return list(dict.fromkeys(r["chinese"] for r in records))


def match_term(
    term: str,
    index: dict[str, list[dict]],
    all_keys: list[str],
    token_idx: dict[str, set[str]] | None = None,
) -> tuple[str, list[dict]]:
    norm = normalize(term)

    # ── 1 & 2: Exact match ─────────────────────────────────────────────────
    if norm in index:
        records = _deduplicate(index[norm])
        status = STATUS_EXACT if len(_unique_chinese(records)) == 1 else STATUS_MULTI
        return status, records

    # ── 3: Token + fuzzy combined scoring ──────────────────────────────────
    # Build candidate set from token inverted index (fast O(|tokens|) lookup)
    # Fall back to O(n) difflib scan only when no token index is provided
    input_tokens = _significant_tokens(norm)
    if token_idx is not None and input_tokens:
        difflib_candidates: set[str] = set()
        for tok in input_tokens:
            difflib_candidates |= token_idx.get(tok, set())
        # For very short inputs (≤2 tokens) also run a limited difflib pass
        # to catch character-level similarities (e.g. acronyms, brand names)
        if len(input_tokens) <= 2:
            difflib_candidates |= set(
                difflib.get_close_matches(norm, all_keys, n=10, cutoff=0.70)
            )
    else:
        # Slow fallback: scan all keys
        difflib_candidates = set(
            difflib.get_close_matches(norm, all_keys, n=30, cutoff=0.45)
        )
        for key in all_keys:
            if _significant_tokens(key) & input_tokens:
                difflib_candidates.add(key)

    # Score and filter
    scored: list[tuple[float, float, str]] = []
    for key in difflib_candidates:
        if not key:
            continue
        score = _combined_score(norm, key)
        if score >= MULTI_THRESHOLD:
            char_r = difflib.SequenceMatcher(None, norm, key, autojunk=False).ratio()
            scored.append((score, char_r, key))

    if not scored:
        return STATUS_MANUAL, []

    scored.sort(reverse=True)
    top_score = scored[0][0]

    # Accept candidates within a window of the top score
    window = max(MULTI_THRESHOLD, top_score - 0.12)
    accepted_keys = [key for s, _, key in scored if s >= window][:MAX_CANDIDATES]

    records = _deduplicate(
        [r for key in accepted_keys for r in index.get(key, [])]
    )

    if not records:
        return STATUS_MANUAL, []

    if top_score >= NEAR_THRESHOLD and len(_unique_chinese(records)) == 1:
        return STATUS_NEAR, records
    return STATUS_MULTI, records


def match_all(
    terms: list[str],
    index: dict[str, list[dict]],
    token_idx: dict[str, set[str]] | None = None,
) -> list[dict]:
    """Return result rows, one per (term × candidate)."""
    all_keys = list(index.keys())
    rows: list[dict] = []

    for term in terms:
        status, candidates = match_term(term, index, all_keys, token_idx)
        if not candidates:
            rows.append({
                "input_en": term,
                "status": status,
                "code": "",
                "chinese": "",
                "db_english": "",
                "similarity_note": "",
            })
        else:
            for rec in candidates:
                char_r = difflib.SequenceMatcher(
                    None, normalize(term), normalize(rec["english"]), autojunk=False
                ).ratio()
                tok_j = _token_jaccard(normalize(term), normalize(rec["english"]))
                note = f"詞{tok_j:.0%}/字{char_r:.0%}"
                rows.append({
                    "input_en": term,
                    "status": status,
                    "code": rec["code"],
                    "chinese": rec["chinese"],
                    "db_english": rec["english"],
                    "similarity_note": note if status != STATUS_EXACT else "100%",
                })
    return rows
