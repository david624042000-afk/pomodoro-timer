"""
LLM fallback for unmatched terms using Claude Haiku.

Only called for terms with STATUS_MANUAL.  For each such term we collect
the best DB candidates (by token Jaccard, no combined-score gate) and ask
Haiku to pick the right one(s) or declare no match.

All manual terms are batched into ONE API call to minimise latency and cost.
"""

import difflib
import re
from typing import Optional

STATUS_LLM    = "◆ LLM建議"
STATUS_MANUAL = "○ 待手動"    # kept for terms where LLM also says "none"

_STOPWORDS = {
    "for", "the", "of", "and", "in", "to", "by", "a", "an", "with",
    "from", "as", "at", "on", "use", "used", "non", "other", "than",
    "preparation", "preparations", "purpose", "purposes",
}


def _stem(word: str) -> str:
    if len(word) > 5 and word.endswith("ing"):
        return word[:-3]
    if len(word) > 4 and word.endswith("ed"):
        return word[:-2]
    if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z]+", text.lower())
    return {_stem(w) for w in words if len(w) >= 3 and w not in _STOPWORDS}


def _token_jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _get_llm_candidates(
    term: str,
    index: dict[str, list[dict]],
    token_idx: dict[str, set[str]],
    top_n: int = 20,
) -> list[dict]:
    """
    Return the top_n DB records most relevant to term, using a loose
    combined score (token Jaccard 60% + char ratio 40%).
    No minimum threshold — we want to give the LLM something to work with
    even when nothing looks similar.
    """
    norm = term.lower().strip()
    # Gather candidates via token index (fast)
    candidate_keys: set[str] = set()
    for tok in _tokens(norm):
        candidate_keys |= token_idx.get(tok, set())

    # Also add difflib top-10 at low cutoff for character-similar terms
    all_keys = list(index.keys())
    candidate_keys |= set(difflib.get_close_matches(norm, all_keys, n=10, cutoff=0.40))

    # Score and rank
    scored: list[tuple[float, str]] = []
    for key in candidate_keys:
        char_r = difflib.SequenceMatcher(None, norm, key, autojunk=False).ratio()
        tok_j  = _token_jaccard(norm, key)
        score  = 0.4 * char_r + 0.6 * tok_j
        scored.append((score, key))

    scored.sort(reverse=True)

    # Collect unique (code, chinese) records from top keys
    seen: set[tuple] = set()
    results: list[dict] = []
    for _, key in scored:
        for rec in index.get(key, []):
            uid = (rec["code"], rec["chinese"])
            if uid not in seen:
                seen.add(uid)
                results.append(rec)
            if len(results) >= top_n:
                break
        if len(results) >= top_n:
            break
    return results


_SYSTEM_PROMPT = """\
You are a Taiwan trademark examiner.
Your job: match English trademark terms to official Chinese entries in the TIPO database.
Be conservative — only confirm a match when the semantic meaning is clearly the same.\
"""

_USER_TEMPLATE = """\
For each numbered term below, choose the best candidate(s) from the list, or reply "none".

Rules:
- Reply ONLY with the term number, a colon, then candidate number(s) separated by commas.
- If multiple candidates are equally valid, list all (e.g.  1: 2,5).
- If no candidate is a reasonable semantic match, reply  1: none.
- Do not add any explanation.

Example output format:
1: 3
2: 1,4
3: none

---
{blocks}
"""


def _build_prompt(manual_results: list[dict], candidates_map: dict[str, list[dict]]) -> str:
    blocks = []
    for i, res in enumerate(manual_results, start=1):
        term = res["input_en"]
        cands = candidates_map.get(term, [])
        if not cands:
            cand_lines = "  (no candidates found in database)"
        else:
            cand_lines = "\n".join(
                f"  {j}. [{rec['code']}] {rec['chinese']} | {rec['english']}"
                for j, rec in enumerate(cands, start=1)
            )
        blocks.append(f"[TERM {i}] \"{term}\"\n{cand_lines}")
    return _USER_TEMPLATE.format(blocks="\n\n".join(blocks))


def _parse_llm_response(
    response_text: str,
    manual_results: list[dict],
    candidates_map: dict[str, list[dict]],
) -> list[dict]:
    """Convert LLM output lines into updated result rows."""
    updated: list[dict] = []
    lines = {
        line.split(":")[0].strip(): line.split(":", 1)[1].strip()
        for line in response_text.strip().splitlines()
        if ":" in line
    }

    for i, res in enumerate(manual_results, start=1):
        term = res["input_en"]
        answer = lines.get(str(i), "none").strip().lower()
        cands = candidates_map.get(term, [])

        if answer == "none" or not cands:
            updated.append({**res, "status": STATUS_MANUAL})
            continue

        # Parse selected indices (1-based)
        try:
            selected_idxs = [int(x.strip()) - 1 for x in answer.split(",") if x.strip().isdigit()]
        except ValueError:
            updated.append({**res, "status": STATUS_MANUAL})
            continue

        chosen = [cands[idx] for idx in selected_idxs if 0 <= idx < len(cands)]
        if not chosen:
            updated.append({**res, "status": STATUS_MANUAL})
            continue

        for rec in chosen:
            char_r = difflib.SequenceMatcher(
                None, term.lower(), rec["english"].lower(), autojunk=False
            ).ratio()
            tok_j = _token_jaccard(term, rec["english"])
            updated.append({
                "input_en":        term,
                "status":          STATUS_LLM,
                "code":            rec["code"],
                "chinese":         rec["chinese"],
                "db_english":      rec["english"],
                "similarity_note": f"詞{tok_j:.0%}/字{char_r:.0%}",
            })

    return updated


def enrich_manual_results(
    results: list[dict],
    index: dict[str, list[dict]],
    token_idx: dict[str, set[str]],
    model: str = "claude-haiku-4-5-20251001",
    api_key: Optional[str] = None,
    verbose: bool = True,
) -> list[dict]:
    """
    Replace STATUS_MANUAL rows with LLM-suggested matches where possible.
    Non-manual rows are returned unchanged.

    Requires ANTHROPIC_API_KEY env variable, or pass api_key= explicitly.
    """
    try:
        import anthropic
    except ImportError:
        print("  [警告] anthropic 套件未安裝，跳過 LLM 步驟。請執行: pip install anthropic")
        return results

    manual_idx = [i for i, r in enumerate(results) if r["status"] == STATUS_MANUAL]
    if not manual_idx:
        return results

    # Collect unique manual terms (preserve order)
    seen_terms: dict[str, list[int]] = {}
    for i in manual_idx:
        term = results[i]["input_en"]
        seen_terms.setdefault(term, []).append(i)

    unique_manual = [{"input_en": t} for t in seen_terms]

    # Build candidate lists
    candidates_map: dict[str, list[dict]] = {
        res["input_en"]: _get_llm_candidates(res["input_en"], index, token_idx)
        for res in unique_manual
    }

    prompt = _build_prompt(unique_manual, candidates_map)

    if verbose:
        print(f"  → 送出 {len(unique_manual)} 項待手動至 Haiku...", end=" ", flush=True)

    try:
        client = anthropic.Anthropic(api_key=api_key)  # None → reads ANTHROPIC_API_KEY
    except Exception as e:
        print(f"\n  [錯誤] 無法初始化 Anthropic 客戶端: {e}")
        print("  請設定環境變數 ANTHROPIC_API_KEY 或使用 --api-key 參數。")
        return results

    try:
        message = client.messages.create(
            model=model,
            max_tokens=256,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = message.content[0].text
    except Exception as e:
        print(f"\n  [錯誤] API 呼叫失敗: {e}")
        return results

    if verbose:
        tokens_in  = message.usage.input_tokens
        tokens_out = message.usage.output_tokens
        print(f"完成 (in={tokens_in} / out={tokens_out} tokens)")

    llm_rows = _parse_llm_response(response_text, unique_manual, candidates_map)

    # Build replacement map: term -> list[dict]
    replacement: dict[str, list[dict]] = {}
    for row in llm_rows:
        replacement.setdefault(row["input_en"], []).append(row)

    # Rebuild full result list, swapping in LLM rows
    new_results: list[dict] = []
    replaced_terms: set[str] = set()
    for res in results:
        term = res["input_en"]
        if res["status"] == STATUS_MANUAL and term not in replaced_terms:
            replaced_terms.add(term)
            new_results.extend(replacement.get(term, [res]))
        elif res["status"] != STATUS_MANUAL:
            new_results.append(res)
        # Skip duplicate MANUAL rows for same term (already added above)

    return new_results
