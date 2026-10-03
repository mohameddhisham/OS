"""
Data Tools - local web UI for five data-cleaning / splitting scripts.

Cloud version: password-protected, background jobs, auto-delete of files.
Local test:  python app.py   (open http://127.0.0.1:5000)
Cloud:       gunicorn app:app   (set APP_PASSWORD and SECRET_KEY first)
"""
import hmac
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
import webbrowser
from datetime import timedelta
from pathlib import Path

import pandas as pd
from flask import Flask, abort, jsonify, redirect, render_template_string, request, send_from_directory, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

BASE = Path(__file__).resolve().parent
JOBS = Path(os.environ.get("JOBS_DIR", BASE / "jobs"))
JOBS.mkdir(exist_ok=True)

app = Flask(__name__)


# =====================================================================
# Shared helpers
# =====================================================================
def read_file(file_path, **kwargs):
    """Read CSV or Excel files."""
    extension = os.path.splitext(str(file_path))[1].lower()
    if extension in [".xlsx", ".xls", ".xlsm"]:
        return pd.read_excel(file_path, **kwargs)
    for encoding in ["utf-8", "utf-8-sig", "cp1252", "ISO-8859-1"]:
        try:
            return pd.read_csv(file_path, encoding=encoding, low_memory=False, **kwargs)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Could not read file: {file_path}")


def load_table(file_path):
    """Read .csv or .xlsx (used by the split tools)."""
    p = str(file_path).lower()
    if p.endswith(".xlsx"):
        return pd.read_excel(file_path), "xlsx"
    if p.endswith(".csv"):
        return pd.read_csv(file_path), "csv"
    raise ValueError("Unsupported file type. Only .csv and .xlsx are supported.")


def need_placeholder(name_format):
    name_format = (name_format or "").strip() or "{}.csv"
    if "{}" not in name_format:
        raise ValueError("File name format must contain {} (it is replaced by the state).")
    return name_format


# =====================================================================
# Tool 1 - Remove Pipeline
# =====================================================================
def clean_phone_last10(series):
    """Normalize phone numbers to their last 10 digits (keeps only 10-digit values)."""
    phones = series.fillna("").astype(str).str.strip()
    phones = phones.str.replace(r"\D", "", regex=True)
    phones = phones.str[-10:]
    return phones[phones.str.len() == 10]


def find_phone_column(df):
    normalized = {str(c).strip().upper(): c for c in df.columns}
    for name in ["PHONE", "PHONE_NUMBER", "MOBILE PHONE", "MOBILE_PHONE", "MOBILE", "TEL", "TELEPHONE", "NUMBER"]:
        if name in normalized:
            return normalized[name]
    print(f"Warning: No recognized phone column found. Using first column: {df.columns[0]}")
    return df.columns[0]


def run_remove(a, out):
    name_format = need_placeholder(a["name_format"])
    split_dir = out / "SPLITS"
    split_dir.mkdir(parents=True, exist_ok=True)

    print("--- Starting Multi-Stage Filtering ---")
    print(" -> Loading input file...")
    fresh = read_file(a["order"])
    fresh["clean_phone"] = clean_phone_last10(fresh[find_phone_column(fresh)])

    exclude = set()

    print(" -> Loading CRM...")
    crm = read_file(a["crm"])
    exclude.update(clean_phone_last10(crm[find_phone_column(crm)]).tolist())

    print(" -> Loading Answer Machine TXT...")
    am = pd.read_csv(a["am_txt"], header=None, names=["AM_PHONE"], dtype=str,
                     encoding="ISO-8859-1", low_memory=False)
    exclude.update(clean_phone_last10(am["AM_PHONE"]).tolist())

    print(" -> Loading Answer Machine Excel...")
    am_x = read_file(a["am_excel"])
    am_x.columns = am_x.columns.astype(str).str.strip()
    exclude.update(clean_phone_last10(am_x[find_phone_column(am_x)]).tolist())

    print(" -> Loading DNC list...")
    dnc = read_file(a["dnc"])
    dnc.columns = dnc.columns.astype(str).str.strip()
    dnc_phones = set(clean_phone_last10(dnc[find_phone_column(dnc)]).tolist())
    exclude.update(dnc_phones)
    print(f" -> DNC numbers loaded: {len(dnc_phones):,}")
    print(f" -> Total excluded numbers: {len(exclude):,}")

    print(" -> Removing CRM, Answer Machine, and DNC matches...")
    is_match = fresh["clean_phone"].isin(exclude)
    removed = fresh[is_match].drop(columns=["clean_phone"])
    kept = fresh[~is_match].drop(columns=["clean_phone"])

    nodup_path = out / "combined_data_nodup.csv"
    kept.to_csv(nodup_path, index=False)
    removed.to_csv(out / "combined_data_removed_matches.csv", index=False)
    print(f"Records kept: {len(kept):,}")
    print(f"Records removed: {len(removed):,}")

    print("\n--- Starting Data Splitting by STATE ---")
    df = pd.read_csv(nodup_path, low_memory=False, dtype=str)
    df.columns = df.columns.astype(str).str.strip().str.upper()
    if "STATE" not in df.columns:
        print("STATE column not found. Skipping split.")
        return
    df["STATE"] = df["STATE"].fillna("UNKNOWN").astype(str).str.strip().str.upper()
    for state in df["STATE"].unique():
        part = df[df["STATE"] == state]
        clean_state = "".join(ch for ch in str(state) if ch.isalnum()) or "UNKNOWN"
        file_name = name_format.format(clean_state)
        part.to_csv(split_dir / file_name, index=False)
        print(f"Saved {file_name} ({len(part):,} records)")


# =====================================================================
# Tool 2 - RE Pipeline
# =====================================================================
def clean_phone_re(series):
    s = series.astype(str).str.strip().str.replace(r"\D", "", regex=True)
    s = s.apply(lambda x: x[:-1] if (isinstance(x, str) and len(x) > 10 and x.endswith("0")) else x)
    s = s.apply(lambda x: x[-10:] if (isinstance(x, str) and len(x) >= 10) else x)
    return s


def extract_list_id(file_name):
    name = os.path.splitext(file_name)[0]
    parts = name.split("_")
    return parts[1] if len(parts) >= 2 else name


def split_by_state(df, split_dir, list_name, fmt):
    print("Splitting by state...")
    for state in df["state"].unique():
        part = df[df["state"] == state]
        clean_state = "".join(c for c in state if c.isalnum() or c in ["-", "_", " "]).strip().replace(" ", "_")
        name = f"{list_name}_{fmt.format(clean_state)}"
        part.to_csv(split_dir / name, index=False, encoding="utf-8")
        print(f"  -> {state}: {len(part)} records")


def process_list_file(input_file, out_base, exclude, fmt, valid_zips):
    file_name = os.path.basename(input_file)
    list_name = extract_list_id(file_name)
    print(f"\n{'=' * 60}\nProcessing: {file_name} -> Output as: {list_name}\n{'=' * 60}")

    list_dir = out_base / list_name
    split_dir = list_dir / "Split"
    split_dir.mkdir(parents=True, exist_ok=True)
    new_dnc = set()

    try:
        df = pd.read_csv(input_file, sep="\t", dtype={"postal_code": str, "phone_number": str},
                         low_memory=False, on_bad_lines="skip", encoding="ISO-8859-1")
        statuses = ["AA", "A", "AB", "AL", "B", "AM", "CBHOLD", "CALLBK", "DAIR", "DEC", "DROP", "NEW",
                    "N", "NP", "PDROP", "DC", "PU", "OA", "ERI", "UA", "DNC"]
        df = df[df["status"].isin(statuses)]
        sel = df[["phone_number", "first_name", "last_name", "address1", "city", "state",
                  "postal_code", "status"]].copy()
        sel["state"] = sel["state"].astype(str).fillna("Unknown").replace("nan", "Unknown").str.strip().str.upper()

        special = ["AA", "AM", "NEW", "CALLBK", "DNC"]
        normal = sel[~sel["status"].isin(special)].copy()
        aa = sel[sel["status"] == "AA"].copy()
        am = sel[sel["status"] == "AM"].copy()
        new = sel[sel["status"] == "NEW"].copy()
        callbk = sel[sel["status"] == "CALLBK"].copy()
        dnc = sel[sel["status"] == "DNC"].copy()
        counts = {"AA": len(aa), "AM": len(am), "NEW": len(new), "CALLBK": len(callbk), "DNC": len(dnc)}
        for k, v in counts.items():
            print(f"{k} Records Found: {v}")

        frames = {}
        if counts["AM"] > 0:
            frames["AM"] = am.drop(columns=["status"])
            frames["AM"].to_csv(list_dir / f"{list_name}_AM.csv", index=False, encoding="utf-8")
            print(f"AM file saved: {list_name}_AM.csv")
        if counts["DNC"] > 0:
            frames["DNC"] = dnc.drop(columns=["status"])
            frames["DNC"].to_csv(list_dir / f"{list_name}_DNC.csv", index=False, encoding="utf-8")
            print(f"DNC file saved: {list_name}_DNC.csv")
            found = set(clean_phone_re(dnc["phone_number"]))
            new_dnc.update(found)
            print(f"Found {len(found)} DNC phones in this file")

        combined = pd.concat([normal.drop(columns=["status"]), aa.drop(columns=["status"]),
                              new.drop(columns=["status"])], ignore_index=True)
        tx = combined[combined["state"] == "TX"].copy()
        no_tx = combined[combined["state"] != "TX"].copy()
        print(f"Total records before TX removal: {len(combined)}")
        print(f"TX records removed: {len(tx)}")
        print(f"Records after TX removal: {len(no_tx)}")
        if not tx.empty:
            tx.to_csv(list_dir / f"{list_name}_TX.csv", index=False, encoding="utf-8")
            print(f"Saved TX records to: {list_name}_TX.csv")

        no_tx.to_csv(list_dir / f"{list_name}.csv", index=False, encoding="utf-8")
        print(f"Saved original data (normal + AA + NEW, no TX): {len(no_tx)} records")

        no_tx["clean_phone"] = clean_phone_re(no_tx["phone_number"])
        is_dup = no_tx["clean_phone"].isin(exclude)
        is_short = no_tx["clean_phone"].str.len() < 10
        dup = no_tx[is_dup | is_short]
        filtered = no_tx[~(is_dup | is_short)].drop(columns=["clean_phone"]).copy()
        print(f"Filtered data (before ZIP validation): {len(filtered)} records")
        print(f"Removed as duplicate/fake/DNC: {len(dup)} records (NOT SAVED)")

        if valid_zips and not filtered.empty:
            print("Validating ZIP codes...")
            zip_clean = filtered["postal_code"].astype(str).str.strip()
            ok = zip_clean.isin(valid_zips)
            invalid = filtered[~ok]
            invalid.to_csv(list_dir / f"{list_name}_invalidzip.csv", index=False, encoding="utf-8")
            print(f"Saved invalid ZIP records: {len(invalid)} records to {list_name}_invalidzip.csv")
            filtered = filtered[ok].copy()
            print(f"Valid ZIP records for processing: {len(filtered)} records")

        filtered.to_csv(list_dir / f"{list_name}_nodup.csv", index=False, encoding="utf-8")
        print(f"Saved nodup data (AA + NEW + normal, valid ZIP, no TX, no DNC): {len(filtered)} records")

        if not filtered.empty:
            split_by_state(filtered, split_dir, list_name, fmt)
        else:
            print("No data to split by state.")
    except Exception as e:  # keep going with the other list files
        print(f"ERROR processing {file_name}: {type(e).__name__}: {e}")
        return False, {}, {}, list_name, new_dnc
    return True, counts, frames, list_name, new_dnc


def update_dnc_file(dnc_path, new_phones, out_path):
    if not new_phones:
        print("No new DNC phones to add to master DNC file.")
        return
    existing = pd.read_excel(dnc_path)
    existing_phones = set(clean_phone_re(existing.iloc[:, 0]))
    truly_new = new_phones - existing_phones
    if not truly_new:
        print("All DNC phones already exist in master DNC file.")
        return
    print(f"Adding {len(truly_new)} new DNC phones to master DNC file...")
    add = pd.DataFrame({existing.columns[0]: list(truly_new)})
    updated = pd.concat([existing, add], ignore_index=True)
    updated.to_excel(out_path, index=False)
    print(f"Updated DNC file saved as DNC_updated.xlsx: {len(updated)} total records")


def run_re(a, out):
    fmt = need_placeholder(a["name_format"])
    print("=" * 60)
    print("RE PIPELINE - LIST ID BASED WITH ZIP VALIDATION AND DNC FILTERING")
    print("=" * 60)

    print("Loading reference data...")
    crm = pd.read_csv(a["crm"])
    print(f"  -> Loaded CRM: {len(crm)} records")
    am_txt = pd.read_csv(a["am_txt"], header=None, names=["AM_PHONE"], dtype=str, low_memory=False)
    print(f"  -> Loaded Answer Machine TXT: {len(am_txt)} records")
    am_xl = pd.read_excel(a["am_excel"])
    print(f"  -> Loaded Answer Machine Excel: {len(am_xl)} records")
    try:
        dnc = pd.read_excel(a["dnc"])
        print(f"  -> Loaded DNC file: {len(dnc)} records")
    except Exception as e:
        print(f"  -> Error loading DNC file: {e}")
        dnc = pd.DataFrame()

    print("Building exclusion phone set...")
    exclude = set(clean_phone_re(crm["Mobile Phone"]))
    exclude.update(set(clean_phone_re(am_txt["AM_PHONE"])))
    exclude.update(set(clean_phone_re(am_xl["Mobile Phone"])))
    if not dnc.empty:
        dnc_phones = clean_phone_re(dnc.iloc[:, 0])
        exclude.update(set(dnc_phones))
        print(f"  -> Added {len(dnc_phones)} DNC phones to exclusion set")
    print(f"  -> Total exclusion phones: {len(exclude)}")

    print("Loading valid ZIP codes...")
    try:
        zdf = pd.read_csv(a["zip_file"])
        valid_zips = set(zdf["ZIP Code"].astype(str).str.strip())
        print(f"  -> Loaded {len(valid_zips):,} valid ZIP codes")
    except Exception as e:
        print(f"  -> Error loading ZIP validation file: {e}")
        valid_zips = set()

    files = sorted(a["lists"], key=lambda p: p.name)
    print(f"\nFound {len(files)} files to process")

    ok_n = fail_n = 0
    totals = {"AA": 0, "AM": 0, "NEW": 0, "CALLBK": 0, "DNC": 0}
    all_special = {"AM": [], "DNC": []}
    all_new_dnc, ids = set(), []
    for f in files:
        ok, counts, frames, list_id, new_dnc = process_list_file(f, out, exclude, fmt, valid_zips)
        if ok:
            ok_n += 1
            ids.append(list_id)
            for k in totals:
                totals[k] += counts.get(k, 0)
                if k in frames and not frames[k].empty:
                    all_special[k].append(frames[k])
            all_new_dnc.update(new_dnc)
        else:
            fail_n += 1

    for k in ("AM", "DNC"):
        if all_special[k]:
            merged = pd.concat(all_special[k], ignore_index=True)
            merged.to_csv(out / f"ALL_{k}_RECORDS.csv", index=False, encoding="utf-8")
            print(f"\nConsolidated {k} file saved: ALL_{k}_RECORDS.csv ({len(merged)} records)")

    if all_new_dnc:
        print(f"\nFound {len(all_new_dnc)} new DNC phones across all files")
        update_dnc_file(a["dnc"], all_new_dnc, out / "DNC_updated.xlsx")

    print(f"\n{'=' * 60}\nPROCESSING COMPLETE")
    print(f"Successful: {ok_n}\nFailed: {fail_n}\nProcessed List IDs: {', '.join(ids)}")
    for k, v in totals.items():
        print(f"Total {k} Records Found: {v:,}")
    print(f"Total New DNC Phones Added: {len(all_new_dnc):,}\n{'=' * 60}")


# =====================================================================
# Tool 3 - Split by percentage
# =====================================================================
def parse_ratios(text):
    vals = [float(x) for x in re.split(r"[,\s]+", (text or "").strip()) if x]
    if not vals:
        raise ValueError("Enter the percentages, for example: 60, 40")
    vals = [int(v) if v == int(v) else v for v in vals]
    if abs(sum(vals) - 100) > 1e-9:
        raise ValueError(f"Percentages must sum up to 100. Current sum: {sum(vals)}%")
    return vals


def run_ratio(a, out):
    pcts = parse_ratios(a["ratios"])
    df, ext = load_table(a["file"])
    total = len(df)
    base = a["file"].stem
    idx, cur = [], 0
    for p in pcts[:-1]:
        cur += int(total * (p / 100.0))
        idx.append(cur)
    slices, prev = [], 0
    for i in idx:
        slices.append(df.iloc[prev:i])
        prev = i
    slices.append(df.iloc[prev:])

    print(f"Starting split for {total} total rows...")
    for i, (chunk, p) in enumerate(zip(slices, pcts), start=1):
        path = out / f"{base}_part{i}_{p}pct.{ext}"
        chunk.to_excel(path, index=False) if ext == "xlsx" else chunk.to_csv(path, index=False)
        print(f"  Saved: {path.name} -> {len(chunk)} rows ({p}%)")


# =====================================================================
# Tool 4 - Split into N equal parts (many files)
# =====================================================================
def run_parts(a, out):
    n = int(a["num_parts"])
    if n < 2:
        raise ValueError("Number of parts must be 2 or more.")
    for f in a["files"]:
        print(f"Processing file: {f.name}")
        try:
            df, ext = load_table(f)
            total = len(df)
            per, rem = divmod(total, n)
            start = 0
            for part in range(1, n + 1):
                end = start + per + (1 if part <= rem else 0)
                path = out / f"{f.stem}_part{part}.{ext}"
                chunk = df.iloc[start:end]
                chunk.to_excel(path, index=False) if ext == "xlsx" else chunk.to_csv(path, index=False)
                print(f"  {path.name} -> {end - start} rows")
                start = end
            print(f"File split complete: {total} rows into {n} parts.")
        except Exception as e:
            print(f"Error processing {f.name}: {e}")


# =====================================================================
# Tool 5 - Split by time zone
# =====================================================================
def _tz_map():
    zones = {
        "Eastern": ("Connecticut,Delaware,Florida,Georgia,Indiana,Kentucky,Maine,Maryland,Massachusetts,Michigan,"
                    "New Hampshire,New Jersey,New York,North Carolina,Ohio,Pennsylvania,Rhode Island,South Carolina,"
                    "Vermont,Virginia,West Virginia,Washington DC,District of Columbia,"
                    "CT,DE,FL,GA,IN,KY,ME,MD,MA,MI,NH,NJ,NY,NC,OH,PA,RI,SC,VT,VA,WV,DC"),
        "Central": ("Alabama,Arkansas,Illinois,Iowa,Kansas,Louisiana,Minnesota,Mississippi,Missouri,Nebraska,"
                    "North Dakota,Oklahoma,South Dakota,Tennessee,Texas,Wisconsin,"
                    "AL,AR,IL,IA,KS,LA,MN,MS,MO,NE,ND,OK,SD,TN,TX,WI"),
        "Mountain": ("Arizona,Colorado,Idaho,Montana,New Mexico,Utah,Wyoming,AZ,CO,ID,MT,NM,UT,WY"),
        "Pacific": ("California,Nevada,Oregon,Washington,CA,NV,OR,WA"),
    }
    return {s: z for z, names in zones.items() for s in names.split(",")}


TZ = _tz_map()


def run_timezone(a, out):
    col = (a["state_col"] or "STATE").strip()
    frames = []
    for f in a["files"]:
        print(f"Reading {f.name}")
        frames.append(load_table(f)[0])
    df = pd.concat(frames, ignore_index=True)
    if col not in df.columns:
        raise ValueError(f"Could not find a column named '{col}'. Columns found: {', '.join(map(str, df.columns))}")
    s = df[col].astype(str).str.strip()
    df["Time_Zone"] = s.map(TZ).fillna(s.str.upper().map(TZ)).fillna("Other")
    print("Successfully mapped states to Time Zones.\n")
    for tz, group in df.groupby("Time_Zone"):
        name = f"combined_data_{tz}.xlsx"
        group.to_excel(out / name, index=False)
        print(f"Saved file: '{name}' with {len(group)} rows.")
    print("\nProcess complete!")


# =====================================================================
# Tool registry (drives both the forms and the runner)
# =====================================================================
CSV_XLSX = ".csv,.xlsx,.xls,.xlsm"
TOOLS = {
    "remove": dict(
        title="Remove Pipeline",
        desc="Takes the new order file, removes every number found in the CRM, Answer Machine and DNC lists, "
             "then splits what is left by state.",
        fn=run_remove,
        fields=[
            dict(name="order", label="Order file", kind="file", accept=CSV_XLSX),
            dict(name="crm", label="CRM file", kind="file", accept=CSV_XLSX),
            dict(name="am_txt", label="Answer Machine Calls (.txt)", kind="file", accept=".txt,.csv"),
            dict(name="am_excel", label="Answer Machine Calls (Excel)", kind="file", accept=CSV_XLSX),
            dict(name="dnc", label="DNC list", kind="file", accept=CSV_XLSX),
            dict(name="name_format", label="Split file name", kind="text", default="CYB-30-9-{}.csv",
                 hint="{} is replaced by the state, for example CYB-30-9-NY.csv"),
        ]),
    "re": dict(
        title="RE Pipeline",
        desc="Processes list .txt files: filters statuses, separates AM, DNC and TX, removes duplicates, "
             "validates ZIP codes and splits by state. New DNC numbers are saved in an updated DNC file.",
        fn=run_re,
        fields=[
            dict(name="lists", label="List files (.txt, select several)", kind="files", accept=".txt"),
            dict(name="crm", label="CRM file (.csv)", kind="file", accept=".csv"),
            dict(name="am_txt", label="Answer Machine Calls (.txt)", kind="file", accept=".txt,.csv"),
            dict(name="am_excel", label="Answer Machine Calls (Excel)", kind="file", accept=".xlsx,.xls"),
            dict(name="zip_file", label="ZIP targeting file (.csv)", kind="file", accept=".csv"),
            dict(name="dnc", label="DNC list (Excel)", kind="file", accept=".xlsx,.xls"),
            dict(name="name_format", label="Split file name", kind="text", default="{}.csv",
                 hint="The list ID is added in front automatically."),
        ]),
    "ratio": dict(
        title="Split by percentage",
        desc="Splits one file into parts using the percentages you enter.",
        fn=run_ratio,
        fields=[
            dict(name="file", label="File to split", kind="file", accept=".csv,.xlsx"),
            dict(name="ratios", label="Percentages", kind="text", default="60, 40",
                 hint="Must add up to 100. Example: 50, 35, 15"),
        ]),
    "parts": dict(
        title="Split into equal parts",
        desc="Splits every selected file into the same number of equal parts.",
        fn=run_parts,
        fields=[
            dict(name="files", label="Files to split (select several)", kind="files", accept=".csv,.xlsx"),
            dict(name="num_parts", label="Number of parts", kind="text", default="2"),
        ]),
    "tz": dict(
        title="Split by time zone",
        desc="Combines the selected files, maps each state to its time zone and saves one Excel file per zone.",
        fn=run_timezone,
        fields=[
            dict(name="files", label="Files to combine (select several)", kind="files", accept=".csv,.xlsx"),
            dict(name="state_col", label="State column name", kind="text", default="STATE"),
        ]),
}


# =====================================================================
# Web UI
# =====================================================================
STYLE = """
:root{--bg:#f2f5f7;--panel:#fff;--ink:#16212b;--mute:#5b6b79;--line:#d5dde4;--accent:#0b6b63;--accent-ink:#fff;
--mark:#ffd166;--bad:#a8281c;--good:#0b6b63;--code:#0f1a22;--code-ink:#cfe3ea}
@media (prefers-color-scheme:dark){:root{--bg:#10171d;--panel:#18222b;--ink:#e6edf2;--mute:#9db0be;--line:#2b3a46;
--accent:#3cc2b3;--accent-ink:#06211e;--bad:#ff8a7d;--good:#3cc2b3;--code:#0a1116;--code-ink:#cfe3ea}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 "Segoe UI",system-ui,-apple-system,sans-serif}
main{max-width:860px;margin:0 auto;padding:32px 20px 80px}
h1{font-size:1.9rem;margin:0 0 4px;letter-spacing:-.01em}
h2{font-size:1.25rem;margin:0 0 4px}
.sub{color:var(--mute);margin:0 0 24px}
.tabs{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:20px}
.tab{font:inherit;padding:10px 16px;border:1px solid var(--line);background:var(--panel);color:var(--ink);
border-radius:6px;cursor:pointer}
.tab[aria-selected=true]{background:var(--ink);color:var(--bg);border-color:var(--ink);box-shadow:inset 0 -3px 0 var(--mark)}
.tab:focus-visible,button.run:focus-visible,a:focus-visible,input:focus-visible{outline:3px solid var(--mark);outline-offset:2px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:24px}
.desc{color:var(--mute);margin:0 0 20px;max-width:64ch}
.field{display:block;margin-bottom:16px}
.lbl{display:block;font-weight:600;margin-bottom:6px}
.hint{display:block;color:var(--mute);font-size:.88rem;margin-top:4px}
input[type=text],input[type=file]{width:100%;font:inherit;color:var(--ink);background:var(--bg);
border:1px solid var(--line);border-radius:6px;padding:9px 10px}
button.run{font:inherit;font-weight:600;padding:11px 28px;border:0;border-radius:6px;background:var(--accent);
color:var(--accent-ink);cursor:pointer;margin-top:6px}
#busy{position:fixed;inset:0;background:rgba(10,17,22,.72);display:grid;place-items:center;color:#fff;
text-align:center;padding:20px}
#busy[hidden]{display:none}
#busy p{max-width:36ch;margin:6px auto 0;color:#cfe3ea}
.spin{width:34px;height:34px;border:4px solid #ffffff44;border-top-color:var(--mark);border-radius:50%;
margin:0 auto 14px;animation:s 1s linear infinite}
@keyframes s{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.spin{animation:none}}
.badge{display:inline-block;padding:3px 10px;border-radius:99px;font-size:.85rem;font-weight:600;border:1px solid}
.badge.ok{color:var(--good);border-color:var(--good)}.badge.bad{color:var(--bad);border-color:var(--bad)}
table{width:100%;border-collapse:collapse;margin:14px 0 22px}
th,td{text-align:left;padding:9px 8px;border-bottom:1px solid var(--line);vertical-align:top}
td.num{white-space:nowrap;color:var(--mute)}
a{color:var(--accent)}
pre{background:var(--code);color:var(--code-ink);padding:16px;border-radius:8px;overflow:auto;max-height:420px;
font:13px/1.5 Consolas,monospace;white-space:pre-wrap}
.back{display:inline-block;margin-bottom:16px}
"""

STYLE += """
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}
.linkbtn{font:inherit;background:none;border:1px solid var(--line);color:var(--mute);border-radius:6px;
padding:7px 12px;cursor:pointer}
.badge.wait{color:var(--mute);border-color:var(--mute)}
.note{color:var(--mute);font-size:.9rem;margin:0 0 18px}
.recent-h{margin:34px 0 0}
.login{max-width:380px;margin:12vh auto 0}
.err{color:var(--bad);margin:0 0 12px}
form.inline{display:inline;margin:0}
button.danger{font:inherit;background:none;border:1px solid var(--bad);color:var(--bad);border-radius:6px;
padding:8px 14px;cursor:pointer}
"""

# =====================================================================
# Cloud settings
# =====================================================================
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
JOB_TTL_MIN = int(os.environ.get("JOB_TTL_MINUTES", "120"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "300"))
RUN_SLOTS = threading.BoundedSemaphore(int(os.environ.get("MAX_CONCURRENT_JOBS", "1")))

if not APP_PASSWORD and __name__ != "__main__":
    raise RuntimeError("APP_PASSWORD is not set. Refusing to start a public server without a password.")

app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    MAX_CONTENT_LENGTH=MAX_UPLOAD_MB * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)
if os.environ.get("TRUST_PROXY", "1") == "1":
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


# ---- per-thread stdout: every job writes its prints to its own log file ----
class _ThreadStdout:
    def __init__(self, real):
        self.real = real
        self.local = threading.local()

    def write(self, text):
        f = getattr(self.local, "f", None)
        if f:
            f.write(text)
            f.flush()
            return len(text)
        return self.real.write(text)

    def flush(self):
        f = getattr(self.local, "f", None)
        (f or self.real).flush()

    def __getattr__(self, name):
        return getattr(self.real, name)


_stdout = _ThreadStdout(sys.stdout)
sys.stdout = _stdout


# =====================================================================
# Job bookkeeping
# =====================================================================
def read_meta(d):
    try:
        return json.loads((d / "meta.json").read_text(encoding="utf-8"))
    except Exception:
        return None


def write_meta(d, **kw):
    m = read_meta(d) or {}
    m.update(kw)
    tmp = d / "meta.tmp"
    tmp.write_text(json.dumps(m), encoding="utf-8")
    os.replace(tmp, d / "meta.json")


def job_dir(job_id):
    if not re.fullmatch(r"[0-9a-f]{16}", job_id):
        abort(404)
    d = JOBS / job_id
    if not d.is_dir() or not read_meta(d):
        abort(404)
    return d


def ago(ts):
    m = int((time.time() - ts) / 60)
    return "just now" if m < 1 else f"{m} min ago" if m < 60 else f"{m // 60} h ago"


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def run_job(job_id, tool, args):
    d = JOBS / job_id
    try:
        with RUN_SLOTS:  # waits here if another job is running
            write_meta(d, status="running")
            logf = open(d / "log.txt", "a", encoding="utf-8")
            _stdout.local.f = logf
            ok = True
            try:
                TOOLS[tool]["fn"](args, d / "out")
            except Exception as e:
                ok = False
                print(f"\nERROR: {type(e).__name__}: {e}")
            finally:
                _stdout.local.f = None
                logf.close()
            write_meta(d, status="done" if ok else "failed")
    except Exception as e:
        write_meta(d, status="failed")
        with open(d / "log.txt", "a", encoding="utf-8") as f:
            f.write(f"\nERROR: {type(e).__name__}: {e}\n")
    finally:
        shutil.rmtree(d / "in", ignore_errors=True)  # uploads are not kept


def janitor():
    while True:
        try:
            cutoff = time.time() - JOB_TTL_MIN * 60
            for d in JOBS.iterdir():
                if d.is_dir() and (read_meta(d) or {}).get("created", d.stat().st_mtime) < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass
        time.sleep(300)


def start_background():
    # jobs that were running when the server stopped can never finish
    for d in JOBS.iterdir():
        m = read_meta(d) if d.is_dir() else None
        if m and m.get("status") in ("queued", "running"):
            write_meta(d, status="failed")
            with open(d / "log.txt", "a", encoding="utf-8") as f:
                f.write("\nERROR: The server restarted while this job was running. Please run it again.\n")
    threading.Thread(target=janitor, daemon=True).start()


def recent_jobs(n=8):
    rows = []
    for d in JOBS.iterdir():
        m = read_meta(d) if d.is_dir() else None
        if m:
            rows.append((m.get("created", 0), d.name, m))
    rows.sort(reverse=True)
    return [dict(id=i, title=m["title"], status=m.get("status", "queued"), ago=ago(c)) for c, i, m in rows[:n]]


# =====================================================================
# Templates
# =====================================================================
HEAD = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>{{ title }}</title><style>{{ style|safe }}</style></head><body><main>"""

LOGIN = HEAD + """
<div class="login"><h1>Data Tools</h1><p class="sub">Enter the password to continue.</p>
<div class="panel"><form method="post">
{% if err %}<p class="err" role="alert">{{ err }}</p>{% endif %}
<label class="field"><span class="lbl">Password</span>
<input type="password" name="password" autofocus required autocomplete="current-password"
style="width:100%;font:inherit;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:9px 10px"></label>
<button class="run" type="submit">Log in</button></form></div></div></main></body></html>"""

HOME = HEAD + """
<div class="top"><div><h1>Data Tools</h1>
<p class="sub">Pick a tool, upload your files, set the options and press Run.</p></div>
{% if auth %}<form method="post" action="/logout"><button class="linkbtn" type="submit">Log out</button></form>{% endif %}</div>
<p class="note">Uploaded files are removed as soon as processing ends. Results are deleted automatically
{{ ttl }} minutes after each run.</p>
<div class="tabs" role="tablist">
{% for key, t in tools.items() %}<button type="button" class="tab" role="tab" data-tab="{{ key }}">{{ t["title"] }}</button>{% endfor %}
</div>
{% for key, t in tools.items() %}
<section class="panel" id="p-{{ key }}" hidden>
<h2>{{ t["title"] }}</h2><p class="desc">{{ t["desc"] }}</p>
<form method="post" action="/run/{{ key }}" enctype="multipart/form-data" class="run-form">
{% for f in t["fields"] %}
<label class="field"><span class="lbl">{{ f["label"] }}</span>
{% if f["kind"] == "text" %}<input type="text" name="{{ f['name'] }}" value="{{ f['default'] }}">
{% else %}<input type="file" name="{{ f['name'] }}" accept="{{ f['accept'] }}" {% if f['kind'] == 'files' %}multiple{% endif %} required>{% endif %}
{% if f.get("hint") %}<span class="hint">{{ f["hint"] }}</span>{% endif %}</label>
{% endfor %}
<button class="run" type="submit">Run {{ t["title"] }}</button>
</form></section>
{% endfor %}
{% if recent %}<h2 class="recent-h">Recent runs</h2>
<table><tbody>{% for r in recent %}<tr><td>{{ r["title"] }}</td><td class="num">{{ r["ago"] }}</td>
<td>{% if r["status"] == "done" %}<span class="badge ok">Finished</span>
{% elif r["status"] == "failed" %}<span class="badge bad">Error</span>
{% else %}<span class="badge wait">Running</span>{% endif %}</td>
<td><a href="/job/{{ r['id'] }}">Open</a></td></tr>{% endfor %}</tbody></table>{% endif %}
<div id="busy" hidden><div><div class="spin"></div><strong>Uploading your files</strong>
<p>Keep this tab open until the progress page appears.</p></div></div>
<script>
const tabs=[...document.querySelectorAll('.tab')];
function show(k){tabs.forEach(t=>t.setAttribute('aria-selected',t.dataset.tab===k));
document.querySelectorAll('.panel').forEach(p=>p.hidden=p.id!=='p-'+k);history.replaceState(null,'','#'+k)}
tabs.forEach(t=>t.addEventListener('click',()=>show(t.dataset.tab)));
const h=location.hash.slice(1);show(tabs.some(t=>t.dataset.tab===h)?h:tabs[0].dataset.tab);
document.querySelectorAll('.run-form').forEach(f=>f.addEventListener('submit',()=>{document.getElementById('busy').hidden=false}));
window.addEventListener('pageshow',()=>{document.getElementById('busy').hidden=true});
</script></main></body></html>"""

JOB = HEAD + """
<a class="back" href="/#{{ meta['tool'] }}">Back to tools</a>
<h1>{{ meta["title"] }}</h1>
{% if running %}
<p class="sub"><span class="badge wait" id="state">{{ label }}</span></p>
<p class="note">You can close this page and come back later from "Recent runs" on the home page.</p>
<h2>Progress</h2><pre id="log">{{ log }}</pre>
<script>
const id="{{ job_id }}",logEl=document.getElementById('log'),st=document.getElementById('state');
async function tick(){
  try{
    const r=await fetch('/status/'+id,{cache:'no-store'});
    if(r.status===401){location='/login';return}
    const j=await r.json();
    logEl.textContent=j.log;logEl.scrollTop=logEl.scrollHeight;
    st.textContent=j.status==='queued'?'Waiting for another job to finish':'Processing';
    if(j.status==='done'||j.status==='failed'){location.reload();return}
  }catch(e){}
  setTimeout(tick,2000);
}
tick();
</script>
{% else %}
<p class="sub">{% if meta["status"] == "done" %}<span class="badge ok">Finished</span>{% else %}<span class="badge bad">Stopped with an error</span>{% endif %}
&nbsp;{{ files|length }} output file{{ '' if files|length == 1 else 's' }}</p>
<p class="note">These files are deleted automatically {{ left }} minutes from now.</p>
{% if files %}
<table><thead><tr><th>File</th><th>Size</th><th></th></tr></thead><tbody>
{% for f in files %}<tr><td>{{ f["name"] }}</td><td class="num">{{ f["size"] }}</td>
<td><a href="{{ url_for('download', job_id=job_id, filename=f['name']) }}">Download</a></td></tr>{% endfor %}
</tbody></table>
{% else %}<p class="desc">No output files were created. Check the log below to see what went wrong.</p>{% endif %}
<form class="inline" method="post" action="/delete/{{ job_id }}"><button class="danger" type="submit">Delete these files now</button></form>
<h2 style="margin-top:28px">Log</h2><pre>{{ log }}</pre>
{% endif %}
</main></body></html>"""


# =====================================================================
# Routes
# =====================================================================
FAILS = {}


@app.before_request
def gate():
    if not APP_PASSWORD or request.endpoint in ("login", "healthz", "static"):
        return None
    if not session.get("ok"):
        if request.path.startswith("/status/"):
            return jsonify(error="login"), 401
        return redirect(url_for("login"))
    return None


@app.after_request
def secure_headers(r):
    r.headers["Cache-Control"] = "no-store"
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["X-Frame-Options"] = "DENY"
    r.headers["Referrer-Policy"] = "no-referrer"
    return r


@app.errorhandler(413)
def too_big(_):
    return f"Upload too large. The limit is {MAX_UPLOAD_MB} MB in total per run.", 413


@app.get("/healthz")
def healthz():
    return "ok"


@app.route("/login", methods=["GET", "POST"])
def login():
    if not APP_PASSWORD:
        return redirect("/")
    err, code = "", 200
    if request.method == "POST":
        ip = request.remote_addr or "?"
        now = time.time()
        recent = [t for t in FAILS.get(ip, []) if now - t < 600]
        FAILS[ip] = recent
        if len(recent) >= 5:
            err, code = "Too many attempts. Try again in 10 minutes.", 429
        elif hmac.compare_digest(request.form.get("password", "").encode(), APP_PASSWORD.encode()):
            session.clear()
            session["ok"] = True
            session.permanent = True
            return redirect("/")
        else:
            recent.append(now)
            err, code = "Wrong password.", 401
    return render_template_string(LOGIN, title="Log in", style=STYLE, err=err), code


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login") if APP_PASSWORD else "/")


@app.get("/")
def home():
    return render_template_string(HOME, title="Data Tools", tools=TOOLS, style=STYLE,
                                  auth=bool(APP_PASSWORD), ttl=JOB_TTL_MIN, recent=recent_jobs())


@app.post("/run/<tool>")
def run(tool):
    spec = TOOLS.get(tool)
    if not spec:
        abort(404)

    job_id = secrets.token_hex(8)
    d = JOBS / job_id
    inp, out = d / "in", d / "out"
    out.mkdir(parents=True)

    args = {}
    for f in spec["fields"]:
        name = f["name"]
        if f["kind"] == "text":
            args[name] = request.form.get(name, "").strip()
            continue
        uploads = [u for u in request.files.getlist(name) if u and u.filename]
        if not uploads:
            shutil.rmtree(d, ignore_errors=True)
            return f"Missing file: {f['label']}. Go back and select it.", 400
        folder = inp / name
        folder.mkdir(parents=True, exist_ok=True)
        saved = []
        for i, u in enumerate(uploads):
            path = folder / (secure_filename(u.filename) or f"file{i}")
            u.save(path)
            saved.append(path)
        args[name] = saved if f["kind"] == "files" else saved[0]

    write_meta(d, title=spec["title"], tool=tool, status="queued", created=time.time())
    threading.Thread(target=run_job, args=(job_id, tool, args), daemon=True).start()
    return redirect(url_for("job_page", job_id=job_id))


@app.get("/job/<job_id>")
def job_page(job_id):
    d = job_dir(job_id)
    meta = read_meta(d)
    p = d / "log.txt"
    log = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
    running = meta.get("status") in ("queued", "running")
    files = [] if running else [
        {"name": f.relative_to(d / "out").as_posix(), "size": human(f.stat().st_size)}
        for f in sorted((d / "out").rglob("*")) if f.is_file()]
    left = max(0, int(JOB_TTL_MIN - (time.time() - meta.get("created", time.time())) / 60))
    label = "Waiting for another job to finish" if meta.get("status") == "queued" else "Processing"
    return render_template_string(JOB, title=f"{meta['title']} - results", style=STYLE, meta=meta, log=log,
                                  files=files, job_id=job_id, running=running, left=left, label=label)


@app.get("/status/<job_id>")
def status(job_id):
    d = job_dir(job_id)
    p = d / "log.txt"
    log = p.read_text(encoding="utf-8", errors="replace")[-20000:] if p.exists() else ""
    return jsonify(status=(read_meta(d) or {}).get("status", "queued"), log=log)


@app.get("/download/<job_id>/<path:filename>")
def download(job_id, filename):
    return send_from_directory(job_dir(job_id) / "out", filename, as_attachment=True)


@app.post("/delete/<job_id>")
def delete(job_id):
    shutil.rmtree(job_dir(job_id), ignore_errors=True)
    return redirect("/")


start_background()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    app.run(host="127.0.0.1", port=port, threaded=True)
