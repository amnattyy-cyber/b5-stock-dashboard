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

DEFAULT_SOURCE = "https://docs.google.com/spreadsheets/d/1appOkfuCPuReEM63lS5RT3gWt2dD4HB-FGl2_h5Vhrw/export?format=xlsx"
SOURCE_URL = os.environ.get("STOCK_XLSX_URL", DEFAULT_SOURCE)
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


def download_workbook(destination):
    last_error = None
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        separator = "&" if "?" in SOURCE_URL else "?"
        cache_busted_url = (
            f"{SOURCE_URL}{separator}_refresh={int(time.time())}&attempt={attempt}"
        )
        request = urllib.request.Request(
            cache_busted_url,
            headers={
                "User-Agent": "b5-stock-dashboard/1.1",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
                destination.write_bytes(response.read())
            print(f"Downloaded Google Sheet on attempt {attempt}")
            return
        except Exception as exc:
            last_error = exc
            if attempt == DOWNLOAD_ATTEMPTS:
                break
            delay = min(5 * (2 ** (attempt - 1)), 30)
            print(
                f"Google Sheet download attempt {attempt} failed: {exc}; "
                f"retrying in {delay}s",
                file=sys.stderr,
            )
            time.sleep(delay)

    raise RuntimeError(
        f"Google Sheet download failed after {DOWNLOAD_ATTEMPTS} attempts: {last_error}"
    )


def main():
    with tempfile.TemporaryDirectory() as temp_dir:
        workbook = Path(temp_dir) / "stock.xlsx"
        download_workbook(workbook)
        df = pd.read_excel(workbook, sheet_name="Data Stock")

    mappings = load_mappings()
    fill_from_mapping(df, "SHOP_CODE", ["SHOP", "AREA"], mappings["shopByCode"])
    fill_from_mapping(
        df,
        "PRODUCT_CODE",
        [
            "CUSTOM_CATEGORY",
            "CUSTOM_GROUP_BRAND",
            "CUSTOM_MODEL",
            "CUSTOM_SUBMODEL",
            "PRODUCT_GROUP_GP",
            "BRAND",
        ],
        mappings["productByCode"],
    )

    shop_name = clean(df["SHOP_NAME"])
    shop = clean(df["SHOP"])
    shop = shop.where(
        shop.ne("Unmapped"),
        shop_name.str.extract(r"\(([^()]*)\)\s*$", expand=False).fillna(shop_name),
    )

    fields = [
        ("AREA", clean(df["AREA"])),
        ("SHOP", shop),
        ("SHOP_CODE", clean(df["SHOP_CODE"])),
        ("TYPE_SHOP", shop.map(classify_shop)),
        ("CUSTOM_CATEGORY", clean(df["CUSTOM_CATEGORY"])),
        ("CUSTOM_GROUP_BRAND", clean(df["CUSTOM_GROUP_BRAND"])),
        ("CUSTOM_MODEL", clean(df["CUSTOM_MODEL"])),
        ("CUSTOM_SUBMODEL", clean(df["CUSTOM_SUBMODEL"])),
        ("PRODUCT_GROUP_GP", clean(df["PRODUCT_GROUP_GP"])),
        ("BRAND", clean(df["BRAND"])),
        ("PRODUCT_CODE", clean(df["PRODUCT_CODE"])),
        ("PRODUCT_NAME", clean(df["PRODUCT_NAME"])),
        ("PRODUCT_STATUS", clean(df["PRODUCT_STATUS"], "Unknown")),
    ]

    dictionaries = {}
    encoded_columns = []
    for name, values in fields:
        codes, unique_values = pd.factorize(values, sort=True)
        dictionaries[name] = unique_values.tolist()
        encoded_columns.append(codes.tolist())

    rows = list(
        map(
            list,
            zip(
                *encoded_columns,
                df["BALANCE"].fillna(0).round(2).tolist(),
                df["AMOUNT"].fillna(0).round(2).tolist(),
            ),
        )
    )

    stock_data = {
        "fields": [name for name, _ in fields],
        "dicts": dictionaries,
        "rows": rows,
    }

    if OUTPUT_PATH.exists():
        try:
            existing = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
            existing_stock_data = {
                "fields": existing.get("fields"),
                "dicts": existing.get("dicts"),
                "rows": existing.get("rows"),
            }
            if existing_stock_data == stock_data:
                print(f"No stock data changes ({len(rows):,} rows)")
                return
        except (OSError, json.JSONDecodeError):
            pass

    payload = {
        "updatedAt": pd.Timestamp.now(tz=ZoneInfo("Asia/Bangkok")).isoformat(),
        **stock_data,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8") as output:
        json.dump(payload, output, ensure_ascii=False, separators=(",", ":"))

    print(f"Wrote {len(rows):,} stock rows to {OUTPUT_PATH}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Stock refresh failed: {exc}", file=sys.stderr)
        raise
