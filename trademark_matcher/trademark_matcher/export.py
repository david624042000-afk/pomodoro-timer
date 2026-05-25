"""
Export match results to a colour-coded Excel file.

Row colours:
  ✓ 完全對應  → light green
  ~ 接近對應  → light yellow
  ? 多項候選  → light orange
  ○ 待手動   → light pink / no fill
"""

from datetime import datetime
from pathlib import Path

import openpyxl
from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from .match import STATUS_EXACT, STATUS_NEAR, STATUS_MULTI, STATUS_MANUAL
from .llm import STATUS_LLM

# ── Colour palette ────────────────────────────────────────────────────────
FILL = {
    STATUS_EXACT:  PatternFill("solid", fgColor="C6EFCE"),   # green
    STATUS_NEAR:   PatternFill("solid", fgColor="FFEB9C"),   # yellow
    STATUS_MULTI:  PatternFill("solid", fgColor="FFCC99"),   # orange
    STATUS_LLM:    PatternFill("solid", fgColor="D9E1F2"),   # light blue
    STATUS_MANUAL: PatternFill("solid", fgColor="FCE4D6"),   # pink
}
HEADER_FILL = PatternFill("solid", fgColor="4472C4")
HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)

COLUMNS = [
    ("No.",        6),
    ("輸入英文名稱",  40),
    ("匹配狀態",     12),
    ("商品代碼",     10),
    ("建議中文名稱",  22),
    ("資料庫英文",   40),
    ("相似度",       8),
    ("確認 ✓",       8),   # user fills this
    ("備註",        20),   # user fills this
]

THIN = Side(style="thin", color="BBBBBB")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def _write_header(ws):
    for col_idx, (header, width) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = BORDER
        ws.column_dimensions[get_column_letter(col_idx)].width = width
    ws.row_dimensions[1].height = 24


def _write_row(ws, row_idx: int, seq: int, result: dict):
    status = result["status"]
    fill = FILL.get(status, FILL[STATUS_MANUAL])

    values = [
        seq,
        result["input_en"],
        result["status"],
        result["code"],
        result["chinese"],
        result["db_english"],
        result["similarity_note"],
        "",   # 確認 (user fills)
        "",   # 備註 (user fills)
    ]

    for col_idx, val in enumerate(values, start=1):
        cell = ws.cell(row=row_idx, column=col_idx, value=val)
        cell.fill = fill
        cell.alignment = Alignment(
            horizontal="left" if col_idx > 1 else "center",
            vertical="center",
            wrap_text=True,
        )
        cell.border = BORDER

    ws.row_dimensions[row_idx].height = 18


def export_excel(results: list[dict], output_path: Path | None = None) -> Path:
    if output_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        output_path = Path.cwd() / f"trademark_matches_{ts}.xlsx"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "商標比對結果"
    ws.freeze_panes = "A2"

    _write_header(ws)

    # Group consecutive rows by input term, assign sequence number
    seq = 0
    prev_term = None
    for row_idx, result in enumerate(results, start=2):
        if result["input_en"] != prev_term:
            seq += 1
            prev_term = result["input_en"]
        _write_row(ws, row_idx, seq, result)

    # ── Legend sheet ──────────────────────────────────────────────────────
    leg = wb.create_sheet("說明")
    legend_rows = [
        ("顏色", "狀態", "說明"),
        ("綠色", STATUS_EXACT,  "完全對應，可直接確認"),
        ("黃色", STATUS_NEAR,   "接近對應，請確認是否合適"),
        ("橘色", STATUS_MULTI,  "多項候選，請選擇最合適的一項"),
        ("淡藍", STATUS_LLM,    "AI 建議，請確認語意是否正確"),
        ("粉紅", STATUS_MANUAL, "無法自動比對，請手動填寫"),
    ]
    fills = [HEADER_FILL, FILL[STATUS_EXACT], FILL[STATUS_NEAR], FILL[STATUS_MULTI], FILL[STATUS_LLM], FILL[STATUS_MANUAL]]
    for r, (row_data, fill) in enumerate(zip(legend_rows, fills), start=1):
        for c, val in enumerate(row_data, start=1):
            cell = leg.cell(row=r, column=c, value=val)
            cell.fill = fill
            if r == 1:
                cell.font = HEADER_FONT
            cell.border = BORDER
            leg.column_dimensions[get_column_letter(c)].width = 20

    wb.save(output_path)
    return output_path
