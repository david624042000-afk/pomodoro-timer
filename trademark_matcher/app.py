"""
Streamlit web UI for the trademark matcher.
Run: streamlit run app.py
"""

import io
import time
from pathlib import Path

import streamlit as st

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="商標名稱比對工具",
    page_icon="🔍",
    layout="wide",
)

st.title("🔍 商標英文名稱 → 中文對照")
st.caption("輸入英文商品/服務名稱，自動比對台灣商標知識庫，輸出建議 Excel")

# ── Load DB (cached so it only runs once per session) ─────────────────────────
@st.cache_resource(show_spinner="載入知識庫中…")
def get_db():
    from trademark_matcher.db import (
        load_db, build_index, build_token_inverted_index
    )
    df = load_db()
    index = build_index(df)
    token_idx = build_token_inverted_index(df)
    return df, index, token_idx

df_full, index_full, token_idx_full = get_db()

# ── Sidebar: options ───────────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ 設定")

    # Class filter
    all_classes = sorted(
        df_full["商品代碼"].apply(
            lambda c: int(str(c).zfill(4)[:2])
        ).unique()
    )
    class_options = ["全部（不篩選）"] + [f"第 {c} 類" for c in all_classes]
    selected_labels = st.multiselect(
        "限定類別（可多選）",
        options=class_options,
        default=[],
        help="不選即搜尋全部 20,629 筆；選定類別可加快速度並減少雜訊",
    )
    selected_classes = [
        int(lbl.replace("第 ", "").replace(" 類", ""))
        for lbl in selected_labels
        if lbl != "全部（不篩選）"
    ]

    st.divider()

    # LLM
    use_llm = st.toggle("啟用 AI 補強（Haiku）", value=False)
    api_key = ""
    if use_llm:
        api_key = st.text_input(
            "Anthropic API Key",
            type="password",
            placeholder="sk-ant-...",
            help="處理「待手動」的詞條，需要 Anthropic API Key",
        )

    st.divider()
    st.markdown("**知識庫資訊**")
    st.metric("總筆數", f"{len(df_full):,}")
    st.metric("商品（1–34 類）", f"{(df_full['類別']=='商品').sum():,}")
    st.metric("服務（35–45 類）", f"{(df_full['類別']=='服務').sum():,}")

# ── Main: input ────────────────────────────────────────────────────────────────
st.subheader("輸入英文名稱")
raw_input = st.text_area(
    label="以分號或逗號分隔，每行也可以",
    placeholder=(
        "Cosmetics; mask packs for cosmetic purposes; "
        "skin whitening preparations; toiletry preparations"
    ),
    height=140,
)

run_btn = st.button("🚀 開始比對", type="primary", use_container_width=True)

# ── Run matching ───────────────────────────────────────────────────────────────
if run_btn and raw_input.strip():
    from trademark_matcher.db import filter_by_class
    from trademark_matcher.match import parse_input, match_all
    from trademark_matcher.export import export_excel
    from trademark_matcher.match import STATUS_EXACT, STATUS_NEAR, STATUS_MULTI, STATUS_MANUAL
    from trademark_matcher.llm import STATUS_LLM, enrich_manual_results

    # Filter DB if classes selected
    if selected_classes:
        df_use = filter_by_class(df_full, selected_classes)
        from trademark_matcher.db import build_index, build_token_inverted_index
        index_use = build_index(df_use)
        token_idx_use = build_token_inverted_index(df_use)
        st.info(f"篩選類別 {selected_classes}：使用 {len(df_use):,} 筆")
    else:
        index_use, token_idx_use = index_full, token_idx_full

    terms = parse_input(raw_input.replace("\n", ";"))

    with st.spinner(f"比對 {len(terms)} 項…"):
        t0 = time.time()
        results = match_all(terms, index_use, token_idx_use)
        elapsed = time.time() - t0

    # LLM pass
    if use_llm and api_key:
        manual_n = sum(1 for r in results if r["status"] == STATUS_MANUAL)
        if manual_n:
            with st.spinner(f"AI 補強 {manual_n} 項待手動…"):
                results = enrich_manual_results(
                    results, index_use, token_idx_use, api_key=api_key
                )
    elif use_llm and not api_key:
        st.warning("已勾選 AI 補強但未填入 API Key，跳過。")

    # ── Summary metrics ────────────────────────────────────────────────────────
    st.divider()
    counts = {}
    seen = set()
    for r in results:
        if r["input_en"] not in seen:
            seen.add(r["input_en"])
            counts[r["status"]] = counts.get(r["status"], 0) + 1

    cols = st.columns(5)
    labels = [
        (STATUS_EXACT,  "✓ 完全對應", "normal"),
        (STATUS_NEAR,   "~ 接近對應", "normal"),
        (STATUS_MULTI,  "? 多項候選", "normal"),
        (STATUS_LLM,    "◆ AI 建議",  "normal"),
        (STATUS_MANUAL, "○ 待手動",   "inverse"),
    ]
    for col, (status, label, delta_color) in zip(cols, labels):
        col.metric(label, counts.get(status, 0))

    st.caption(f"比對耗時 {elapsed:.2f}s｜共 {len(terms)} 項輸入")

    # ── Preview table ──────────────────────────────────────────────────────────
    st.subheader("比對結果預覽")

    STATUS_EMOJI = {
        STATUS_EXACT:  "✅",
        STATUS_NEAR:   "🟡",
        STATUS_MULTI:  "🟠",
        STATUS_LLM:    "🔵",
        STATUS_MANUAL: "🔴",
    }

    import pandas as pd
    preview_rows = []
    seq, prev_term = 0, None
    for r in results:
        if r["input_en"] != prev_term:
            seq += 1
            prev_term = r["input_en"]
        preview_rows.append({
            "#":        seq,
            "輸入英文":  r["input_en"],
            "狀態":     STATUS_EMOJI.get(r["status"], "") + " " + r["status"],
            "商品代碼":  r["code"] or "",
            "建議中文":  r["chinese"] or "",
            "資料庫英文": r["db_english"] or "",
            "相似度":   r["similarity_note"] or "",
        })
    preview_df = pd.DataFrame(preview_rows)
    st.dataframe(preview_df, use_container_width=True, hide_index=True)

    # ── Download Excel ─────────────────────────────────────────────────────────
    buf = io.BytesIO()
    export_excel(results, output_path=None)   # write to temp, then re-read
    # Write to buffer directly
    from datetime import datetime
    import openpyxl
    tmp_path = Path("/tmp/tm_export.xlsx")
    export_excel(results, tmp_path)
    buf.write(tmp_path.read_bytes())
    buf.seek(0)

    fname = f"trademark_matches_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    st.download_button(
        label="📥 下載 Excel",
        data=buf,
        file_name=fname,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
        type="primary",
    )

elif run_btn:
    st.warning("請先輸入英文名稱。")
