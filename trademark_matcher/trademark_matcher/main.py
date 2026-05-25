"""
CLI entry point.

Usage:
    python main.py "Cosmetics; mask packs for cosmetic purposes; ..."
    python main.py --file input.txt --class 3 --llm
    python main.py --reload   # force reload ODS files (clear cache)

--class  Comma-separated class numbers to restrict the search scope.
         If omitted you are prompted interactively (press Enter to skip).
--llm    After rule-based matching, send ○ 待手動 terms to Claude Haiku
         for a second-pass suggestion.
"""

import argparse
import sys
import time
from pathlib import Path

from .db import load_db, build_index, build_token_inverted_index, filter_by_class
from .match import parse_input, match_all
from .export import export_excel
from .match import STATUS_EXACT, STATUS_NEAR, STATUS_MULTI, STATUS_MANUAL
from .llm import STATUS_LLM, enrich_manual_results
from .splitter import split_compound


def _parse_classes(raw: str) -> list[int]:
    """Parse '3,30' → [3, 30]."""
    parts = [p.strip() for p in raw.replace("，", ",").split(",") if p.strip()]
    return [int(p) for p in parts if p.isdigit()]


def _ask_classes() -> list[int] | None:
    """Interactive prompt. Returns list of ints, or None if user skips."""
    print("\n已知商標類別嗎？（例如：3 或 3,30）")
    print("直接按 Enter 搜尋全部類別：", end=" ", flush=True)
    raw = input().strip()
    if not raw:
        return None
    classes = _parse_classes(raw)
    if not classes:
        print("  格式不符，將搜尋全部類別。")
        return None
    return classes


def print_summary(results: list[dict], raw_term_count: int | None = None):
    counts: dict[str, int] = {}
    seen: set[str] = set()
    for r in results:
        if r["input_en"] not in seen:
            seen.add(r["input_en"])
            counts[r["status"]] = counts.get(r["status"], 0) + 1

    total = len(seen)
    header = f"共 {raw_term_count} 項輸入（拆分後 {total} 個子項）" \
        if raw_term_count and raw_term_count != total \
        else f"共 {total} 項輸入"
    print(f"\n{'─'*52}")
    print(f"  {header}")
    for status in [STATUS_EXACT, STATUS_NEAR, STATUS_MULTI, STATUS_LLM, STATUS_MANUAL]:
        n = counts.get(status, 0)
        if n:
            suffix = " (需確認)" if status == STATUS_LLM else (" (需手動)" if status == STATUS_MANUAL else "")
            print(f"  {status} : {n} 項{suffix}")
    print(f"{'─'*52}\n")


def run(
    raw_input: str,
    output: Path | None = None,
    force_reload: bool = False,
    classes: list[int] | None = None,
    use_llm: bool = False,
    api_key: str | None = None,
    interactive: bool = False,
):
    print("載入知識庫...", end=" ", flush=True)
    t0 = time.time()
    df = load_db(force_reload=force_reload)

    # ── Class filter ────────────────────────────────────────────────────────
    if classes is None and interactive:
        classes = _ask_classes()

    if classes:
        df = filter_by_class(df, classes)
        print(f"篩選類別 {classes}：剩 {len(df):,} 筆 ", end="")

    index     = build_index(df)
    token_idx = build_token_inverted_index(df)
    print(f"完成 ({time.time()-t0:.1f}s，共 {len(df):,} 筆)")

    # ── Parse & expand compound terms ───────────────────────────────────────
    raw_terms = parse_input(raw_input)
    expanded: list[dict] = []   # {term, parent_en}
    for t in raw_terms:
        parts = split_compound(t)
        if len(parts) > 1:
            for p in parts:
                expanded.append({"term": p, "parent_en": t})
        else:
            expanded.append({"term": t, "parent_en": None})

    compound_count = sum(1 for e in expanded if e["parent_en"])
    print(f"輸入項目: {len(raw_terms)} 項（含拆分後共 {len(expanded)} 個子項）"
          if compound_count else f"輸入項目: {len(raw_terms)} 項")

    # ── Rule-based matching ─────────────────────────────────────────────────
    terms_only = [e["term"] for e in expanded]
    print("比對中...", end=" ", flush=True)
    t1 = time.time()
    flat = match_all(terms_only, index, token_idx)
    print(f"完成 ({time.time()-t1:.1f}s)")

    # Attach parent_en / is_sub metadata to flat results
    parent_map = {e["term"]: e["parent_en"] for e in expanded}
    results = []
    for r in flat:
        parent = parent_map.get(r["input_en"])
        results.append({**r, "is_sub": parent is not None, "parent_en": parent})

    # ── LLM second pass ────────────────────────────────────────────────────
    if use_llm:
        manual_count = sum(1 for r in results if r["status"] == STATUS_MANUAL)
        if manual_count:
            print(f"LLM 補強 ({manual_count} 項待手動)...")
            results = enrich_manual_results(results, index, token_idx, api_key=api_key)
        else:
            print("  無待手動項目，跳過 LLM 步驟。")

    print_summary(results, raw_term_count=len(raw_terms))

    out_path = export_excel(results, output)
    print(f"輸出檔案: {out_path}")
    return out_path


def main():
    parser = argparse.ArgumentParser(
        description="商標英文名稱 → 中文對照建議 Excel",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "terms",
        nargs="?",
        help="以分號或逗號分隔的英文商品/服務名稱",
    )
    parser.add_argument(
        "--file", "-f",
        type=Path,
        help="從文字檔讀取輸入（每行或分號/逗號分隔均可）",
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=None,
        help="輸出 Excel 路徑（預設：當前目錄 trademark_matches_YYYYMMDD_HHMM.xlsx）",
    )
    parser.add_argument(
        "--class", "-c",
        dest="classes",
        default=None,
        help="逗號分隔的類別編號，例如 3 或 3,30（不指定則搜尋全部）",
    )
    parser.add_argument(
        "--llm",
        action="store_true",
        help="啟用 Claude Haiku 二次比對，處理無法自動比對的項目",
    )
    parser.add_argument(
        "--api-key",
        dest="api_key",
        default=None,
        help="Anthropic API key（預設讀取環境變數 ANTHROPIC_API_KEY）",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="強制重新載入 ODS 檔案，清除快取",
    )
    args = parser.parse_args()

    if args.file:
        raw = args.file.read_text(encoding="utf-8")
        raw = raw.replace("\n", ";").replace(";;", ";")
        interactive = False
    elif args.terms:
        raw = args.terms
        interactive = False
    else:
        print("請輸入商品名稱（分號或逗號分隔）：", end="")
        raw = input()
        interactive = True

    classes = _parse_classes(args.classes) if args.classes else None

    run(
        raw,
        output=args.output,
        force_reload=args.reload,
        classes=classes,
        use_llm=args.llm,
        api_key=args.api_key,
        interactive=interactive,
    )


if __name__ == "__main__":
    main()
