import os
import json
from datetime import datetime, date
from pathlib import Path

import pandas as pd
from bson import ObjectId
from dotenv import load_dotenv
from pymongo import MongoClient


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "mf_analyst")

OUTPUT_FILE = ROOT_DIR / "mf_export.xlsx"


def clean_value(value):
    """
    Convert MongoDB/Python values into Excel-safe values.
    """
    if isinstance(value, ObjectId):
        return str(value)

    if isinstance(value, (datetime, date)):
        return value.isoformat()

    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, default=str)

    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False, default=str)

    return value


def flatten_dict(d, parent_key="", sep="."):
    """
    Flatten nested dict:
    {"a": {"b": 1}} -> {"a.b": 1}
    """
    rows = {}

    for key, value in d.items():
        new_key = f"{parent_key}{sep}{key}" if parent_key else key

        if isinstance(value, dict):
            rows.update(flatten_dict(value, new_key, sep=sep))
        else:
            rows[new_key] = clean_value(value)

    return rows


def collection_to_dataframe(db, collection_name, query=None, projection=None, limit=None):
    query = query or {}

    cursor = db[collection_name].find(query, projection)

    if limit:
        cursor = cursor.limit(limit)

    rows = []
    for doc in cursor:
        rows.append(flatten_dict(doc))

    return pd.DataFrame(rows)


def export_fund_analysis(db):
    """
    Split fund_analysis into:
    1. summary sheet: one row per fund
    2. horizons sheet: one row per fund per horizon
    """
    summary_rows = []
    horizon_rows = []

    cursor = db.fund_analysis.find({})

    for doc in cursor:
        doc = dict(doc)

        horizons = doc.pop("horizons", []) or []

        summary_rows.append(flatten_dict(doc))

        for h in horizons:
            row = {
                "scheme_code": doc.get("scheme_code"),
                "scheme_name": doc.get("scheme_name"),
            }

            if isinstance(h, dict):
                row.update(flatten_dict(h))
            else:
                row["horizon_raw"] = clean_value(h)

            horizon_rows.append(row)

    summary_df = pd.DataFrame(summary_rows)
    horizons_df = pd.DataFrame(horizon_rows)

    return summary_df, horizons_df


def autosize_columns(writer, sheet_name, dataframe):
    """
    Basic Excel formatting: readable column widths.
    """
    worksheet = writer.sheets[sheet_name]

    for idx, col in enumerate(dataframe.columns):
        series = dataframe[col].astype(str)
        max_len = max(
            [len(str(col))] + [len(x) for x in series.head(500)]
        )
        width = min(max(max_len + 2, 12), 45)
        worksheet.column_dimensions[chr(65 + idx) if idx < 26 else None]


def write_sheet(writer, df, sheet_name):
    if df.empty:
        df = pd.DataFrame([{"message": "No data found"}])

    # Excel sheet names max length is 31 chars
    safe_sheet_name = sheet_name[:31]

    df.to_excel(writer, sheet_name=safe_sheet_name, index=False)

    ws = writer.sheets[safe_sheet_name]
    ws.freeze_panes = "A2"

    for col_idx, column_name in enumerate(df.columns, start=1):
        max_len = len(str(column_name))

        for value in df[column_name].head(500):
            max_len = max(max_len, len(str(value)))

        width = min(max(max_len + 2, 12), 45)
        ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = width


def main():
    client = MongoClient(MONGO_URL)
    db = client[DB_NAME]

    print(f"Connected to DB: {DB_NAME}")
    print(f"Exporting to: {OUTPUT_FILE}")

    fund_summary_df, fund_horizons_df = export_fund_analysis(db)

    collections = {
        "fund_nav_meta": collection_to_dataframe(db, "fund_nav_meta"),
        "fund_recent": collection_to_dataframe(db, "fund_recent"),
        "refresh_log": collection_to_dataframe(db, "refresh_log"),
        "fqs_refresh_log": collection_to_dataframe(db, "fqs_refresh_log"),
        "nav_meta_refresh_log": collection_to_dataframe(db, "nav_meta_refresh_log"),
    }

    with pd.ExcelWriter(OUTPUT_FILE, engine="openpyxl") as writer:
        write_sheet(writer, fund_summary_df, "fund_analysis_summary")
        write_sheet(writer, fund_horizons_df, "fund_analysis_horizons")

        for sheet_name, df in collections.items():
            write_sheet(writer, df, sheet_name)

    client.close()

    print("Done.")
    print(f"Excel file created: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()