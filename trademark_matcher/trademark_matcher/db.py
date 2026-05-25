"""
Load and index the trademark ODS databases.
Caches to CSV for fast subsequent loads.
"""

import re
import pandas as pd
from pathlib import Path

GOODS_PATH = Path(
    "/Users/Corn/Library/CloudStorage/Dropbox/DownloadFile"
    "/第1至34類商品名稱中英對照表(含商品碼)202601.ods"
)
SERVICES_PATH = Path(
    "/Users/Corn/Library/CloudStorage/Dropbox/DownloadFile"
    "/第35至45類服務名稱中英對照表(含商品碼)202601.ods"
)
CACHE_PATH = Path(__file__).parent / "_db_cache.csv"


def _normalize(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"\s+", " ", text)
    text = text.rstrip(".")
    return text


def load_db(force_reload: bool = False) -> pd.DataFrame:
    """Return combined DataFrame with an extra _norm_en column."""
    if not force_reload and CACHE_PATH.exists():
        df = pd.read_csv(CACHE_PATH, dtype={"商品代碼": str})
        return df

    df1 = pd.read_excel(GOODS_PATH, engine="odf", dtype={"商品代碼": str})
    df1["類別"] = "商品"
    df2 = pd.read_excel(SERVICES_PATH, engine="odf", dtype={"商品代碼": str})
    df2["類別"] = "服務"

    df = pd.concat([df1, df2], ignore_index=True)
    df["_norm_en"] = df["商品英文名稱"].fillna("").apply(_normalize)
    df.to_csv(CACHE_PATH, index=False)
    return df


def build_token_inverted_index(df: pd.DataFrame) -> dict[str, set[str]]:
    """
    Pre-built token inverted index for fast candidate lookup.
    Returns: { stemmed_token -> set of normalized_english_keys }
    """
    import re

    stopwords = {
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

    token_idx: dict[str, set[str]] = {}
    for _, row in df.iterrows():
        key = row["_norm_en"]
        words = re.findall(r"[a-z]+", key)
        for w in words:
            if len(w) >= 3 and w not in stopwords:
                stem = _stem(w)
                token_idx.setdefault(stem, set()).add(key)
    return token_idx


def get_class(code: str) -> int:
    """Extract class number (1-45) from a product/service code string."""
    return int(str(code).zfill(4)[:2])


def filter_by_class(df: pd.DataFrame, classes: list[int]) -> pd.DataFrame:
    """Return only rows belonging to the given class numbers."""
    class_nums = df["商品代碼"].apply(get_class)
    return df[class_nums.isin(classes)].reset_index(drop=True)


def build_index(df: pd.DataFrame) -> dict[str, list[dict]]:
    """
    Returns: { normalized_english_key -> [{"code", "chinese", "english", "type"}, ...] }
    Rows with the same normalized key but different Chinese names are kept separately.
    """
    index: dict[str, list[dict]] = {}
    for _, row in df.iterrows():
        key = row["_norm_en"]
        record = {
            "code": row["商品代碼"],
            "chinese": row["商品名稱"],
            "english": row["商品英文名稱"],
            "type": row["類別"],
        }
        index.setdefault(key, []).append(record)
    return index
