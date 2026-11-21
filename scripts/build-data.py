import base64
import gzip
import json
import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

DEFAULT_STOCK_SOURCE = "https://docs.google.com/spreadsheets/d/1appOkfuCPuReEM63lS5RT3gWt2dD4HB-FGl2_h5Vhrw/export?format=xlsx"
DEFAULT_SALES_SOURCE = "https://docs.google.com/spreadsheets/d/1qsVJk2DbXW8EInVK7gFIOtCB9-5GdVp5vJhU4hPK29k/export?format=xlsx"
STOCK_URL = os.environ.get("STOCK_XLSX_URL", DEFAULT_STOCK_SOURCE)
SALES_URL = os.environ.get("SALES_XLSX_URL", DEFAULT_SALES_SOURCE)
OUTPUT_PATH = Path(os.environ.get("STOCK_OUTPUT", "data/stock.json"))
DOWNLOAD_ATTEMPTS = int(os.environ.get("STOCK_DOWNLOAD_ATTEMPTS", "4"))
DOWNLOAD_TIMEOUT = int(os.environ.get("STOCK_DOWNLOAD_TIMEOUT", "120"))
MAPPING_PARTS = sorted(Path("config").glob("mappings.part*.b64"))


def clean(series, fallback="Unmapped"):
    result = (
        series.fillna(fallback)
        .astype(str)
        .str.strip()
        .str.replace(r"\.0$", "", regex=True)
    )
    return result.mask(result.eq(""), fallback)


def normalized_key(series):
    return clean(series, "").str.upper().str.replace(r"\s+", " ", regex=True)


def numeric(series):
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_numeric(series, errors="coerce").fillna(0)
    return pd.to_numeric(
        series.astype(str).str.replace(r"[^0-9.\-]", "", regex=True),
        errors="coerce",
    ).fillna(0)


def load_mappings():
    encoded = "".join(path.read_text(encoding="utf-8").strip() for path in MAPPING_PARTS)
    if not encoded:
        raise RuntimeError("Stock mapping files are missing")
    return json.loads(gzip.decompress(base64.b64decode(encoded)).decode("utf-8"))


def fill_from_mapping(df, key_column, target_columns, mapping):
    keys = clean(df[key_column], "")
    for target in target_columns:
        current = df[target]
        invalid = current.isna() | current.astype(str).str.startswith("#")
        resolved = keys.map(lambda item: mapping.get(item, {}).get(target))
        df[target] = current.where(~invalid, resolved)


def classify_shop(value):
    value = value.lower()
    if value.startswith("true shop"):
        return "True Shop"
    if value.startswith("kiosk"):
        return "Kiosk"
    if value.startswith("true move"):
        return "True Move"
    if value.startswith("ww "):
        return "WW"
    if "miniupc" in value:
        return "MiniUPC"
    if "minibkk" in value:
        return "MiniBKK"
    if "7-11" in value:
        return "7-Eleven"
    return "Unmapped"


def download_workbook(source_url, destination, label):
    last_error = None
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        separator = "&" if "?" in source_url else "?"
        cache_busted_url = f"{source_url}{separator}_refresh={int(time.time())}&attempt={attempt}"
        request = urllib.request.Request(
            cache_busted_url,
            headers={
                "User-Agent": "b5-stock-dashboard/1.2",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
                destination.write_bytes(response.read())
            print(f"Downloaded {label} Google Sheet on attempt {attempt}")
            return
        except Exception as exc:
            last_error = exc
            if attempt == DOWNLOAD_ATTEMPTS:
                break
            delay = min(5 * (2 ** (attempt - 1)), 30)
            print(f"{label} download attempt {attempt} failed: {exc}; retrying in {delay}s", file=sys.stderr)
            time.sleep(delay)
    raise RuntimeError(f"{label} download failed after {DOWNLOAD_ATTEMPTS} attempts: {last_error}")


def build_sales_metrics(sales):
    required = {"Sales Date", "Shop Code", "Qty", "Sum of QTY"}
    missing = required.difference(sales.columns)
    if missing:
        raise RuntimeError(f"Sales sheet is missing columns: {', '.join(sorted(missing))}")

    sales = sales.copy()
    sales["SALES_DATE"] = pd.to_datetime(sales["Sales Date"], errors="coerce").dt.normalize()
    latest_date = sales["SALES_DATE"].max()
    if pd.isna(latest_date):
        raise RuntimeError("Sales sheet has no valid Sales Date")
    window_start = latest_date - pd.Timedelta(days=27)
    sales = sales[sales["SALES_DATE"].between(window_start, latest_date)].copy()
    sales["SHOP_KEY"] = normalized_key(sales["Shop Code"])
    sales["MODEL_KEY"] = normalized_key(sales["Qty"])
    sales["SALES_QTY"] = numeric(sales["Sum of QTY"])
    sales = sales[(sales["SHOP_KEY"] != "") & (sales["MODEL_KEY"] != "")]

    l4w = sales.groupby(["SHOP_KEY", "MODEL_KEY"], as_index=False)["SALES_QTY"].sum()
    return l4w, latest_date, window_start


def main():
    with tempfile.TemporaryDirectory() as temp_dir:
        stock_workbook = Path(temp_dir) / "stock.xlsx"
        sales_workbook = Path(temp_dir) / "sales.xlsx"
        download_workbook(STOCK_URL, stock_workbook, "Stock")
        download_workbook(SALES_URL, sales_workbook, "Sales")
        df = pd.read_excel(stock_workbook, sheet_name="Data Stock")
        sales = pd.read_excel(sales_workbook, sheet_name="Daily_Sales_Model")

    mappings = load_mappings()
    fill_from_mapping(df, "SHOP_CODE", ["SHOP", "AREA"], mappings["shopByCode"])
    fill_from_mapping(
        df,
        "PRODUCT_CODE",
        ["CUSTOM_CATEGORY", "CUSTOM_GROUP_BRAND", "CUSTOM_MODEL", "CUSTOM_SUBMODEL", "PRODUCT_GROUP_GP", "BRAND"],
        mappings["productByCode"],
    )

    shop_name = clean(df["SHOP_NAME"])
    shop = clean(df["SHOP"])
    shop = shop.where(shop.ne("Unmapped"), shop_name.str.extract(r"\(([^()]*)\)\s*$", expand=False).fillna(shop_name))

    l4w, sales_latest, sales_window_start = build_sales_metrics(sales)
    df["SHOP_KEY"] = normalized_key(df["SHOP_CODE"])
    df["MODEL_KEY"] = normalized_key(df["CUSTOM_SUBMODEL"])
    df = df.merge(l4w, how="left", on=["SHOP_KEY", "MODEL_KEY"])
    df["L4W"] = df["SALES_QTY"].fillna(0).clip(lower=0)
    df["AVG_WEEKLY_SALES"] = df["L4W"] / 4
    balance = numeric(df["BALANCE"])
    df["STOCK_COVER_WEEK"] = -1.0
    has_sales = df["AVG_WEEKLY_SALES"] > 0
    df.loc[has_sales, "STOCK_COVER_WEEK"] = balance[has_sales] / df.loc[has_sales, "AVG_WEEKLY_SALES"]
    df.loc[balance <= 0, "STOCK_COVER_WEEK"] = 0

    fields = [
        ("AREA", clean(df["AREA"])), ("SHOP", shop), ("SHOP_CODE", clean(df["SHOP_CODE"])),
        ("TYPE_SHOP", shop.map(classify_shop)), ("CUSTOM_CATEGORY", clean(df["CUSTOM_CATEGORY"])),
        ("CUSTOM_GROUP_BRAND", clean(df["CUSTOM_GROUP_BRAND"])), ("CUSTOM_MODEL", clean(df["CUSTOM_MODEL"])),
        ("CUSTOM_SUBMODEL", clean(df["CUSTOM_SUBMODEL"])), ("PRODUCT_GROUP_GP", clean(df["PRODUCT_GROUP_GP"])),
        ("BRAND", clean(df["BRAND"])), ("PRODUCT_CODE", clean(df["PRODUCT_CODE"])),
        ("PRODUCT_NAME", clean(df["PRODUCT_NAME"])), ("PRODUCT_STATUS", clean(df["PRODUCT_STATUS"], "Unknown")),
    ]

    dictionaries = {}
    encoded_columns = []
    for name, values in fields:
        codes, unique_values = pd.factorize(values, sort=True)
        dictionaries[name] = unique_values.tolist()
        encoded_columns.append(codes.tolist())

    rows = list(map(list, zip(
        *encoded_columns,
        balance.round(2).tolist(),
        numeric(df["AMOUNT"]).round(2).tolist(),
        df["AVG_WEEKLY_SALES"].round(2).tolist(),
        df["L4W"].round(2).tolist(),
        df["STOCK_COVER_WEEK"].round(2).tolist(),
    )))

    stock_data = {
        "salesUpdatedAt": sales_latest.date().isoformat(),
        "salesWindowStart": sales_window_start.date().isoformat(),
        "metrics": ["AVG_WEEKLY_SALES", "L4W", "STOCK_COVER_WEEK"],
        "fields": [name for name, _ in fields],
        "dicts": dictionaries,
        "rows": rows,
    }

    if OUTPUT_PATH.exists():
        try:
            existing = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
            existing_stock_data = {key: existing.get(key) for key in stock_data}
            if existing_stock_data == stock_data:
                print(f"No stock or sales data changes ({len(rows):,} rows)")
                return
        except (OSError, json.JSONDecodeError):
            pass

    payload = {"updatedAt": pd.Timestamp.now(tz=ZoneInfo("Asia/Bangkok")).isoformat(), **stock_data}
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8") as output:
        json.dump(payload, output, ensure_ascii=False, separators=(",", ":"))

    matched = int((df["L4W"] > 0).sum())
    print(f"Wrote {len(rows):,} stock rows; matched L4W sales for {matched:,} rows")
    print(f"Sales window: {sales_window_start.date()} to {sales_latest.date()}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Stock refresh failed: {exc}", file=sys.stderr)
        raise
