"""
Tool logic for Data Tools (no web framework in here).
Each run_* function takes (args, out_dir): args holds uploaded file paths / text options.
"""
import io
import os
import re
import sys
import threading
from pathlib import Path

import pandas as pd


# =====================================================================
# Shared helpers
# =====================================================================
def read_file(file_path, **kwargs):
    """Read CSV or Excel files with better error handling for corrupted Excel files."""
    extension = os.path.splitext(str(file_path))[1].lower()
    if extension in [".xlsx", ".xls", ".xlsm"]:
        # For .xls files, try xlrd first (better for old format)
        # For .xlsx files, try openpyxl first (better for new format)
        if extension == ".xls":
            engines = ["xlrd", "openpyxl"]
        else:
            engines = ["openpyxl", "xlrd"]

        last_error = None
        for engine in engines:
            try:
                return pd.read_excel(file_path, engine=engine, **kwargs)
            except ImportError:
                # Engine not installed, try next
                continue
            except Exception as e:
                last_error = e
                # Try next engine
                continue
        # If all engines failed, raise the last error with helpful message
        raise ValueError(
            f"Could not read Excel file: {file_path}\n"
            f"Error: {type(last_error).__name__}: {last_error}\n"
            f"TIP: The file may be corrupted. Try opening it in Excel and saving it as .xlsx format."
        )
    for encoding in ["utf-8", "utf-8-sig", "cp1252", "ISO-8859-1"]:
        try:
            return pd.read_csv(file_path, encoding=encoding, low_memory=False, **kwargs)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Could not read file: {file_path}")


def read_file_flexible(file_path, **kwargs):
    """Read a file by trying multiple formats regardless of extension.
    Tries: CSV (multiple encodings), Excel (openpyxl, xlrd), Excel with different extensions.
    Useful for files with wrong or missing extensions."""
    path_str = str(file_path)
    print(f"  -> Attempting to read file flexibly: {path_str}")

    errors = []

    # Try CSV with multiple encodings first
    for encoding in ["utf-8", "utf-8-sig", "cp1252", "ISO-8859-1", "latin1"]:
        try:
            df = pd.read_csv(file_path, encoding=encoding, low_memory=False, **kwargs)
            print(f"  -> Successfully read as CSV with encoding: {encoding}")
            return df
        except Exception as e:
            errors.append(f"CSV ({encoding}): {type(e).__name__}: {e}")
            continue

    # Try Excel with openpyxl
    try:
        df = pd.read_excel(file_path, engine="openpyxl", **kwargs)
        print(f"  -> Successfully read as Excel with openpyxl")
        return df
    except Exception as e:
        errors.append(f"Excel (openpyxl): {type(e).__name__}: {e}")

    # Try Excel with xlrd
    try:
        df = pd.read_excel(file_path, engine="xlrd", **kwargs)
        print(f"  -> Successfully read as Excel with xlrd")
        return df
    except Exception as e:
        errors.append(f"Excel (xlrd): {type(e).__name__}: {e}")

    # Try Excel without specifying engine (let pandas decide)
    try:
        df = pd.read_excel(file_path, **kwargs)
        print(f"  -> Successfully read as Excel (auto engine)")
        return df
    except Exception as e:
        errors.append(f"Excel (auto): {type(e).__name__}: {e}")

    # If all failed, raise with detailed error info
    error_msg = f"Could not read file: {file_path}\n\nAttempted formats:\n"
    for err in errors:
        error_msg += f"  - {err}\n"
    error_msg += "\nTIP: Try opening the file in Excel and saving it as .xlsx format."
    raise ValueError(error_msg)


def load_table(file_path):
    """Read .csv or .xlsx (used by the split tools) with better error handling."""
    p = str(file_path).lower()
    if p.endswith(".xlsx") or p.endswith(".xls"):
        # For .xls files, try xlrd first (better for old format)
        # For .xlsx files, try openpyxl first (better for new format)
        if p.endswith(".xls"):
            engines = ["xlrd", "openpyxl"]
        else:
            engines = ["openpyxl", "xlrd"]

        last_error = None
        for engine in engines:
            try:
                return pd.read_excel(file_path, engine=engine), "xlsx"
            except ImportError:
                continue
            except Exception as e:
                last_error = e
                continue
        raise ValueError(
            f"Could not read Excel file: {file_path}\n"
            f"Error: {type(last_error).__name__}: {last_error}\n"
            f"TIP: The file may be corrupted. Try opening it in Excel and saving it as .xlsx format."
        )
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
    for name in ["PHONE", "PHONE_NUMBER", "MOBILE PHONE", "MOBILE_PHONE", "MOBILE", "TEL", "TELEPHONE", "NUMBER", "PHONE NUMBER"]:
        if name in normalized:
            return normalized[name]
    print(f"Warning: No recognized phone column found. Using first column: {df.columns[0]}")
    return df.columns[0]


CHUNK_ROWS = 100_000


def update_dnc_file(dnc_path, new_phones, out_path):
    """Append new phones to the original DNC file and save a copy in the results."""
    if not new_phones:
        print("No new DNC phones to add to master DNC file.")
        return

    try:
        existing = read_file(dnc_path, dtype=str)
        # Normalize existing phones using clean_phone_re for consistency
        existing_phones = set(clean_phone_re(existing.iloc[:, 0]))

        truly_new = new_phones - existing_phones
        if not truly_new:
            print("All DNC phones already exist in master DNC file.")
            return
        print(f"Adding {len(truly_new)} new DNC phones to master DNC file...")
        add = pd.DataFrame({existing.columns[0]: sorted(truly_new)})
        updated = pd.concat([existing, add], ignore_index=True)

        # Update the original DNC file in place
        try:
            if is_excel(dnc_path):
                extension = os.path.splitext(str(dnc_path))[1].lower()
                # For .xls files, try xlrd first (better for old format)
                # For .xlsx files, try openpyxl first (better for new format)
                if extension == ".xls":
                    engines = ["xlrd", "openpyxl"]
                else:
                    engines = ["openpyxl", "xlrd"]

                last_error = None
                for engine in engines:
                    try:
                        updated.to_excel(dnc_path, index=False, engine=engine)
                        break
                    except ImportError:
                        continue
                    except Exception as e:
                        last_error = e
                        continue
                else:
                    raise last_error if last_error else Exception("No Excel engine available")
            else:
                updated.to_csv(dnc_path, index=False)
            print(f"Original DNC file updated: {os.path.basename(str(dnc_path))} ({len(updated)} total records)")
        except Exception as e:
            print(f"Could not update original DNC file: {e}")

        updated.to_excel(out_path, index=False)
        print(f"Updated DNC file saved as DNC_updated.xlsx: {len(updated)} total records")
    except Exception as e:
        print(f"Error updating DNC file: {type(e).__name__}: {e}")
        print(f"  -> File path: {dnc_path}")
        print(f"  -> TIP: The DNC file may be corrupted. Try opening it in Excel and saving it again.")


def detect_encoding(path):
    """Pick the first encoding that can decode the whole file (streamed, low memory)."""
    for enc in ("utf-8-sig", "cp1252", "ISO-8859-1"):
        try:
            with open(path, "r", encoding=enc, newline="") as f:
                while f.read(1 << 20):
                    pass
            return enc
        except UnicodeDecodeError:
            continue
    return "ISO-8859-1"


def is_excel(path):
    return os.path.splitext(str(path))[1].lower() in (".xlsx", ".xls", ".xlsm")


def table_header(path):
    """Only the column names of a CSV/Excel file with better error handling."""
    if is_excel(path):
        extension = os.path.splitext(str(path))[1].lower()
        # For .xls files, try xlrd first (better for old format)
        # For .xlsx files, try openpyxl first (better for new format)
        if extension == ".xls":
            engines = ["xlrd", "openpyxl"]
        else:
            engines = ["openpyxl", "xlrd"]

        last_error = None
        for engine in engines:
            try:
                return pd.read_excel(path, nrows=0, dtype=str, engine=engine)
            except ImportError:
                continue
            except Exception as e:
                last_error = e
                continue
        raise ValueError(
            f"Could not read Excel file header: {path}\n"
            f"Error: {type(last_error).__name__}: {last_error}\n"
            f"TIP: The file may be corrupted. Try opening it in Excel and saving it as .xlsx format."
        )
    return pd.read_csv(path, nrows=0, dtype=str, encoding=detect_encoding(path))


def iter_table(path, chunk_rows=CHUNK_ROWS):
    """Yield the file in pieces of text-only rows (CSV streams; Excel must be loaded once)."""
    if is_excel(path):
        extension = os.path.splitext(str(path))[1].lower()
        # For .xls files, try xlrd first (better for old format)
        # For .xlsx files, try openpyxl first (better for new format)
        if extension == ".xls":
            engines = ["xlrd", "openpyxl"]
        else:
            engines = ["openpyxl", "xlrd"]

        last_error = None
        df = None
        for engine in engines:
            try:
                df = pd.read_excel(path, dtype=str, engine=engine)
                break
            except ImportError:
                continue
            except Exception as e:
                last_error = e
                continue
        if df is None:
            raise ValueError(
                f"Could not read Excel file: {path}\n"
                f"Error: {type(last_error).__name__}: {last_error}\n"
                f"TIP: The file may be corrupted. Try opening it in Excel and saving it as .xlsx format."
            )
        for i in range(0, len(df), chunk_rows):
            yield df.iloc[i:i + chunk_rows]
    else:
        yield from pd.read_csv(path, dtype=str, encoding=detect_encoding(path), chunksize=chunk_rows)


def read_phone_set(path):
    """Cleaned phone numbers of a CSV/Excel file, reading ONLY the phone column with better error handling."""
    col = find_phone_column(table_header(path))
    if is_excel(path):
        extension = os.path.splitext(str(path))[1].lower()
        # For .xls files, try xlrd first (better for old format)
        # For .xlsx files, try openpyxl first (better for new format)
        if extension == ".xls":
            engines = ["xlrd", "openpyxl"]
        else:
            engines = ["openpyxl", "xlrd"]

        last_error = None
        df = None
        for engine in engines:
            try:
                df = pd.read_excel(path, usecols=[col], dtype=str, engine=engine)
                break
            except ImportError:
                continue
            except Exception as e:
                last_error = e
                continue
        if df is None:
            raise ValueError(
                f"Could not read Excel file: {path}\n"
                f"Error: {type(last_error).__name__}: {last_error}\n"
                f"TIP: The file may be corrupted. Try opening it in Excel and saving it as .xlsx format."
            )
    else:
        df = pd.read_csv(path, usecols=[col], dtype=str, encoding=detect_encoding(path))
    return set(clean_phone_last10(df[col]).tolist())


def run_remove(a, out):
    name_format = need_placeholder(a["name_format"])
    split_dir = out / "SPLITS"
    split_dir.mkdir(parents=True, exist_ok=True)

    print("--- Starting Multi-Stage Filtering ---")
    exclude = set()
    new_dnc_phones = set()  # Track new DNC phones found in input file

    print(" -> Loading CRM...")
    try:
        crm = read_file_flexible(a["crm"], dtype=str)
        crm_phone_col = find_phone_column(crm)
        exclude.update(clean_phone_last10(crm[crm_phone_col]))
        del crm
    except Exception as e:
        print(f"  -> ERROR loading CRM: {type(e).__name__}: {e}")
        print(f"  -> WARNING: Continuing without CRM filtering. This may result in more duplicates in output.")
        print(f"  -> To fix: Open the file in Excel and save it as .xlsx format, then try again.")
        # Don't raise - continue without CRM filtering

    print(" -> Loading Answer Machine TXT...")
    try:
        am = pd.read_csv(a["am_txt"], header=None, names=["AM_PHONE"], dtype=str,
                         encoding="ISO-8859-1", low_memory=False)
        exclude.update(clean_phone_last10(am["AM_PHONE"]).tolist())
        del am
    except Exception as e:
        print(f"  -> ERROR loading Answer Machine TXT: {type(e).__name__}: {e}")
        raise

    print(" -> Loading Answer Machine Excel...")
    try:
        exclude.update(read_phone_set(a["am_excel"]))
    except Exception as e:
        print(f"  -> ERROR loading Answer Machine Excel: {type(e).__name__}: {e}")
        raise

    print(" -> Loading DNC list...")
    try:
        dnc_phones = read_phone_set(a["dnc"])
        exclude.update(dnc_phones)
        print(f" -> DNC numbers loaded: {len(dnc_phones):,}")
        print(f" -> Total excluded numbers: {len(exclude):,}")
    except Exception as e:
        print(f"  -> ERROR loading DNC list: {type(e).__name__}: {e}")
        raise

    print(" -> Loading input file and removing matches (in chunks)...")
    header = table_header(a["order"])
    phone_col = find_phone_column(header)
    nodup_path = out / "combined_data_nodup.csv"
    removed_path = out / "combined_data_removed_matches.csv"
    kept_n = removed_n = 0
    wrote_kept = wrote_removed = False
    written = {}          # split file name -> rows written
    state_missing = False

    # Check if input file has a status column for DNC tracking
    col_map = {str(c).strip().lower(): c for c in header.columns}
    status_col = col_map.get("status", None)
    if status_col:
        print(f" -> Found status column: {status_col} - will track DNC records")

    for chunk in iter_table(a["order"]):
        clean = clean_phone_last10(chunk[phone_col])
        is_match = clean.isin(exclude).reindex(chunk.index, fill_value=False)
        removed, kept = chunk[is_match], chunk[~is_match]
        removed_n += len(removed)
        kept_n += len(kept)
        removed.to_csv(removed_path, mode="a", header=not wrote_removed, index=False)
        kept.to_csv(nodup_path, mode="a", header=not wrote_kept, index=False)
        wrote_removed = wrote_kept = True

        # Track DNC records from input file if status column exists
        if status_col:
            dnc_records = chunk[chunk[status_col].astype(str).str.upper() == "DNC"]
            if not dnc_records.empty:
                dnc_clean = clean_phone_last10(dnc_records[phone_col])
                new_dnc_phones.update(dnc_clean.tolist())

        part = kept.copy()
        part.columns = part.columns.astype(str).str.strip().str.upper()
        if "STATE" not in part.columns:
            state_missing = True
            continue
        part["STATE"] = part["STATE"].fillna("UNKNOWN").astype(str).str.strip().str.upper()
        for state, grp in part.groupby("STATE", sort=False):
            clean_state = "".join(ch for ch in str(state) if ch.isalnum()) or "UNKNOWN"
            file_name = name_format.format(clean_state)
            grp.to_csv(split_dir / file_name, mode="a", header=file_name not in written, index=False)
            written[file_name] = written.get(file_name, 0) + len(grp)

    if not wrote_kept:  # empty input
        header.to_csv(nodup_path, index=False)
        header.to_csv(removed_path, index=False)

    print(f"Records kept: {kept_n:,}")
    print(f"Records removed: {removed_n:,}")
    print("\n--- Data Splitting by STATE ---")
    if state_missing:
        print("STATE column not found. Skipping split.")
    for file_name, n in written.items():
        print(f"Saved {file_name} ({n:,} records)")

    # Update DNC file with new DNC phones found
    if new_dnc_phones:
        print(f"\n--- Updating DNC File ---")
        print(f"Found {len(new_dnc_phones)} DNC records in input file")
        update_dnc_file(a["dnc"], new_dnc_phones, out / "DNC_updated.xlsx")
    else:
        print(f"\n--- DNC Update ---")
        print("No new DNC records found in input file")


# =====================================================================
# Tool 2 - RE Pipeline
# =====================================================================
def clean_phone_re(series):
    s = series.fillna("").astype(str).str.strip().str.replace(r"\D", "", regex=True)
    s = s.mask((s.str.len() > 10) & s.str.endswith("0"), s.str[:-1])
    s = s.mask(s.str.len() >= 10, s.str[-10:])
    return s


def extract_list_id(file_name):
    """Extract list ID from filename - just use the base name without extension."""
    name = os.path.splitext(file_name)[0]
    return name


def split_by_state(df, split_dir, list_name, fmt):
    print("Splitting by state...")
    for state in df["state"].unique():
        part = df[df["state"] == state]
        clean_state = "".join(c for c in state if c.isalnum() or c in ["-", "_", " "]).strip().replace(" ", "_")
        name = f"{list_name}_{fmt.format(clean_state)}"
        part.to_csv(split_dir / name, index=False, encoding="utf-8")
        print(f"  -> {state}: {len(part)} records")


def select_output_columns(df):
    """Select and normalize to the required output columns:
    phone_number, first_name, last_name, address1, city, state"""
    # Create column mapping (case-insensitive)
    col_map = {str(c).strip().lower(): c for c in df.columns}

    # Define required columns and their possible names
    column_mappings = {
        "phone_number": ["phone_number", "phone", "phone number", "telephone", "tel", "mobile", "mobile_phone"],
        "first_name": ["first_name", "first name", "fname", "first", "given_name"],
        "last_name": ["last_name", "last name", "lname", "last", "surname", "family_name"],
        "address1": ["address1", "address", "address_1", "street", "street_address", "addr1"],
        "city": ["city"],
        "state": ["state", "st"]
    }

    # Find the actual column names
    output_cols = {}
    missing_cols = []

    for target_name, possible_names in column_mappings.items():
        found = None
        for possible in possible_names:
            if possible in col_map:
                found = col_map[possible]
                break
        if found:
            output_cols[target_name] = found
        else:
            missing_cols.append(target_name)

    if missing_cols:
        print(f"  -> WARNING: Missing required columns: {', '.join(missing_cols)}")
        print(f"  -> Available columns: {', '.join(df.columns)}")

    # Select the columns with renamed headers
    if output_cols:
        result = df[list(output_cols.values())].copy()
        result.columns = list(output_cols.keys())
        return result
    else:
        print("  -> ERROR: Could not find any required columns. Returning original data.")
        return df


def select_output_columns(df):
    """Select and normalize to the required output columns:
    phone_number, first_name, last_name, address1, city, state"""
    # Create column mapping (case-insensitive)
    col_map = {str(c).strip().lower(): c for c in df.columns}

    # Define required columns and their possible names
    column_mappings = {
        "phone_number": ["phone_number", "phone", "phone number", "telephone", "tel", "mobile", "mobile_phone"],
        "first_name": ["first_name", "first name", "fname", "first", "given_name"],
        "last_name": ["last_name", "last name", "lname", "last", "surname", "family_name"],
        "address1": ["address1", "address", "address_1", "street", "street_address", "addr1"],
        "city": ["city"],
        "state": ["state", "st"]
    }

    # Find the actual column names
    output_cols = {}
    missing_cols = []

    for target_name, possible_names in column_mappings.items():
        found = None
        for possible in possible_names:
            if possible in col_map:
                found = col_map[possible]
                break
        if found:
            output_cols[target_name] = found
        else:
            missing_cols.append(target_name)

    if missing_cols:
        print(f"  -> WARNING: Missing required columns: {', '.join(missing_cols)}")
        print(f"  -> Available columns: {', '.join(df.columns)}")

    # Select the columns with renamed headers
    if output_cols:
        result = df[list(output_cols.values())].copy()
        result.columns = list(output_cols.keys())
        return result
    else:
        print("  -> ERROR: Could not find any required columns. Returning original data.")
        return df


def process_list_file(input_file, out_base, exclude, fmt, valid_zips, preloaded=None, list_name=None):
    file_name = os.path.basename(input_file) if input_file is not None else "combined_lists"
    list_name = list_name or extract_list_id(file_name)
    print(f"\n{'=' * 60}\nProcessing: {file_name} -> Output as: {list_name}\n{'=' * 60}")

    list_dir = out_base / list_name
    split_dir = list_dir / "Split"
    split_dir.mkdir(parents=True, exist_ok=True)
    new_dnc = set()

    try:
        if preloaded is not None:
            df = preloaded.copy()
        else:
            df = pd.read_csv(input_file, sep="\t", dtype=str, on_bad_lines="skip", encoding="ISO-8859-1")
        
        # Find columns by name (case-insensitive)
        col_map = {str(c).strip().lower(): c for c in df.columns}
        
        phone_col = col_map.get("phone_number", col_map.get("phone", df.columns[0]))
        status_col = col_map.get("status", df.columns[-1])
        state_col = col_map.get("state", col_map.get("st", None))
        zip_col = col_map.get("postal_code", col_map.get("zip", col_map.get("postalcode", None)))
        
        statuses = ["AA", "A", "AB", "AL", "B", "AM", "CBHOLD", "CALLBK", "DAIR", "DEC", "DROP", "NEW",
                    "N", "NP", "PDROP", "DC", "PU", "OA", "ERI", "UA", "DNC", "U"]
        df = df[df[status_col].isin(statuses)]
        
        # Keep all columns but rename key ones
        df = df.rename(columns={phone_col: "phone_number", status_col: "status"})
        if state_col:
            df = df.rename(columns={state_col: "state"})
        if zip_col:
            df = df.rename(columns={zip_col: "postal_code"})
        
        sel = df.copy()
        del df
        
        if "state" in sel.columns:
            sel["state"] = sel["state"].astype(str).fillna("Unknown").replace("nan", "Unknown").str.strip().str.upper()

        special = ["AA", "AM", "NEW", "CALLBK", "DNC", "U"]
        normal = sel[~sel["status"].isin(special)].copy()
        aa = sel[sel["status"] == "AA"].copy()
        am = sel[sel["status"] == "AM"].copy()
        new = sel[sel["status"] == "NEW"].copy()
        callbk = sel[sel["status"] == "CALLBK"].copy()
        dnc = sel[sel["status"] == "DNC"].copy()
        u = sel[sel["status"] == "U"].copy()
        counts = {"AA": len(aa), "AM": len(am), "NEW": len(new), "CALLBK": len(callbk), "DNC": len(dnc), "U": len(u)}
        for k, v in counts.items():
            print(f"{k} Records Found: {v}")

        frames = {}
        if counts["AM"] > 0:
            am_output = select_output_columns(am.drop(columns=["status"]))
            frames["AM"] = am_output
            am_output.to_csv(list_dir / f"{list_name}_AM.csv", index=False, encoding="utf-8")
            print(f"AM file saved: {list_name}_AM.csv")
        if counts["DNC"] > 0:
            dnc_output = select_output_columns(dnc.drop(columns=["status"]))
            frames["DNC"] = dnc_output
            dnc_output.to_csv(list_dir / f"{list_name}_DNC.csv", index=False, encoding="utf-8")
            print(f"DNC file saved: {list_name}_DNC.csv")
            found = set(clean_phone_re(dnc["phone_number"]))
            new_dnc.update(found)
            print(f"Found {len(found)} DNC phones in this file")
        if counts["U"] > 0:
            u_output = select_output_columns(u.drop(columns=["status"]))
            frames["U"] = u_output
            u_output.to_csv(list_dir / f"{list_name}_U.csv", index=False, encoding="utf-8")
            print(f"U (Underage) file saved: {list_name}_U.csv")
            found_u = set(clean_phone_re(u["phone_number"]))
            new_dnc.update(found_u)
            print(f"Found {len(found_u)} Underage phones in this file")

        combined = pd.concat([normal.drop(columns=["status"]), aa.drop(columns=["status"]),
                              new.drop(columns=["status"])], ignore_index=True)
        tx = combined[combined["state"] == "TX"].copy()
        no_tx = combined[combined["state"] != "TX"].copy()
        print(f"Total records before TX removal: {len(combined)}")
        print(f"TX records removed: {len(tx)}")
        print(f"Records after TX removal: {len(no_tx)}")

        # Save TX file with selected columns
        if not tx.empty:
            tx_output = select_output_columns(tx)
            tx_output.to_csv(list_dir / f"{list_name}_TX.csv", index=False, encoding="utf-8")
            print(f"Saved TX records to: {list_name}_TX.csv")

        # Deduplication (needs phone_number column - use full no_tx data)
        no_tx["clean_phone"] = clean_phone_re(no_tx["phone_number"])
        is_dup = no_tx["clean_phone"].isin(exclude)
        is_short = no_tx["clean_phone"].str.len() < 10
        dup = no_tx[is_dup | is_short]
        filtered = no_tx[~(is_dup | is_short)].drop(columns=["clean_phone"]).copy()
        print(f"Filtered data (before ZIP validation): {len(filtered)} records")
        print(f"Removed as duplicate/fake/DNC: {len(dup)} records (NOT SAVED)")

        # Save original data with selected columns (from no_tx - includes duplicates)
        no_tx_output = select_output_columns(no_tx)
        no_tx_output.to_csv(list_dir / f"{list_name}.csv", index=False, encoding="utf-8")
        print(f"Saved original data (normal + AA + NEW, no TX): {len(no_tx_output)} records")

        if valid_zips and not filtered.empty:
            print("Validating ZIP codes...")
            zip_clean = filtered["postal_code"].astype(str).str.strip()
            ok = zip_clean.isin(valid_zips)
            invalid = filtered[~ok]
            # Select required columns for invalid ZIP file
            invalid_output = select_output_columns(invalid)
            invalid_output.to_csv(list_dir / f"{list_name}_nodup.csv", index=False, encoding="utf-8")
            print(f"Saved invalid ZIP records: {len(invalid_output)} records to {list_name}_nodup.csv")
            filtered = filtered[ok].copy()
            print(f"Valid ZIP records for processing: {len(filtered)} records")

        # Select only required columns for final output
        print("Selecting required columns for final output...")
        filtered_output = select_output_columns(filtered)
        filtered_output.to_csv(list_dir / f"{list_name}_filtersZIP.csv", index=False, encoding="utf-8")
        print(f"Saved filtersZIP data (AA + NEW + normal, valid ZIP, no TX, no DNC): {len(filtered_output)} records")

        if not filtered_output.empty:
            split_by_state(filtered_output, split_dir, list_name, fmt)
        else:
            print("No data to split by state.")
    except Exception as e:  # keep going with the other list files
        print(f"ERROR processing {file_name}: {type(e).__name__}: {e}")
        return False, {}, {}, list_name, new_dnc
    return True, counts, frames, list_name, new_dnc


def run_re(a, out):
    fmt = need_placeholder(a["name_format"])
    print("=" * 60)
    print("RE PIPELINE - LIST ID BASED WITH ZIP VALIDATION AND DNC FILTERING")
    print("=" * 60)

    print("Loading reference data...")
    exclude = set()
    try:
        crm = read_file_flexible(a["crm"], dtype=str)
        crm_phone_col = find_phone_column(crm)
        print(f"  -> Loaded CRM: {len(crm)} records (phone column: {crm_phone_col})")
        exclude.update(clean_phone_re(crm[crm_phone_col]))
        del crm
    except Exception as e:
        print(f"  -> ERROR loading CRM file: {type(e).__name__}: {e}")
        print(f"  -> File path: {a['crm']}")
        print(f"  -> WARNING: Continuing without CRM filtering. This may result in more duplicates in output.")
        print(f"  -> To fix: Open the file in Excel and save it as .xlsx format, then try again.")
        # Don't raise - continue without CRM filtering

    try:
        am_txt = pd.read_csv(a["am_txt"], header=None, names=["AM_PHONE"], dtype=str, low_memory=False)
        print(f"  -> Loaded Answer Machine TXT: {len(am_txt)} records")
        exclude.update(clean_phone_re(am_txt["AM_PHONE"]))
        del am_txt
    except Exception as e:
        print(f"  -> ERROR loading Answer Machine TXT: {type(e).__name__}: {e}")
        print(f"  -> File path: {a['am_txt']}")
        raise

    try:
        am_xl = read_file(a["am_excel"], dtype=str)
        am_xl_phone_col = find_phone_column(am_xl)
        print(f"  -> Loaded Answer Machine Excel: {len(am_xl)} records (phone column: {am_xl_phone_col})")
        exclude.update(clean_phone_re(am_xl[am_xl_phone_col]))
        del am_xl
    except Exception as e:
        print(f"  -> ERROR loading Answer Machine Excel: {type(e).__name__}: {e}")
        print(f"  -> File path: {a['am_excel']}")
        print(f"  -> TIP: The Excel file may be corrupted. Try opening it in Excel and saving it again.")
        raise

    try:
        dnc = read_file(a["dnc"], dtype=str)
        print(f"  -> Loaded DNC file: {len(dnc)} records")
        dnc_phones = clean_phone_re(dnc.iloc[:, 0])
        exclude.update(set(dnc_phones))
        print(f"  -> Added {len(dnc_phones)} DNC phones to exclusion set")
        del dnc
    except Exception as e:
        print(f"  -> ERROR loading DNC file: {type(e).__name__}: {e}")
        print(f"  -> File path: {a['dnc']}")
        print(f"  -> TIP: The Excel file may be corrupted. Try opening it in Excel and saving it again.")
        raise
    print(f"Building exclusion phone set... total exclusion phones: {len(exclude)}")

    print("Loading valid ZIP codes...")
    try:
        zdf = pd.read_csv(a["zip_file"], dtype=str)
        # Find ZIP column
        zip_col = None
        for col in zdf.columns:
            if "zip" in str(col).lower():
                zip_col = col
                break
        if zip_col is None:
            zip_col = zdf.columns[0]
            print(f"  -> Warning: No ZIP column found, using first column: {zip_col}")
        valid_zips = set(zdf[zip_col].astype(str).str.strip())
        print(f"  -> Loaded {len(valid_zips):,} valid ZIP codes (column: {zip_col})")
    except Exception as e:
        print(f"  -> Error loading ZIP validation file: {e}")
        valid_zips = set()

    files = sorted(a["lists"], key=lambda p: p.name)
    combine = bool(a.get("combine"))
    print(f"\nFound {len(files)} files to process")
    if combine:
        print(" -> Combine mode ON: merging all list files into one list")

        parts = []
        for f in files:
            try:
                parts.append(pd.read_csv(f, sep="\t", dtype=str, on_bad_lines="skip", encoding="ISO-8859-1"))
            except Exception as e:
                print(f" ERROR reading {f.name}: {type(e).__name__}: {e}")
        if not parts:
            raise ValueError("No readable list files to combine.")
        combined_df = pd.concat(parts, ignore_index=True)
        print(f" -> Combined {len(parts)} file(s): {len(combined_df):,} records")
        jobs = [(None, combined_df, "COMBINED")]
    else:
        jobs = [(f, None, None) for f in files]

    ok_n = fail_n = 0
    totals = {"AA": 0, "AM": 0, "NEW": 0, "CALLBK": 0, "DNC": 0, "U": 0}
    all_special = {"AM": [], "DNC": [], "U": []}
    all_new_dnc, ids = set(), []
    for f, preloaded, lname in jobs:
        ok, counts, frames, list_id, new_dnc = process_list_file(f, out, exclude, fmt, valid_zips,
                                                                 preloaded=preloaded, list_name=lname)
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

    for k in ("AM", "DNC", "U"):
        if all_special[k]:
            merged = pd.concat(all_special[k], ignore_index=True)
            merged.to_csv(out / f"ALL_{k}_RECORDS.csv", index=False, encoding="utf-8")
            print(f"\nConsolidated {k} file saved: ALL_{k}_RECORDS.csv ({len(merged)} records)")

    print(f"\n{'=' * 60}\nPROCESSING COMPLETE")
    print(f"Successful: {ok_n}\nFailed: {fail_n}\nProcessed List IDs: {', '.join(ids)}")
    for k, v in totals.items():
        print(f"Total {k} Records Found: {v:,}")
    print(f"Total New DNC Phones Found: {len(all_new_dnc):,}")
    
    # Update DNC file with new DNC phones found across all files
    if all_new_dnc:
        print(f"\n--- Updating DNC File ---")
        update_dnc_file(a["dnc"], all_new_dnc, out / "DNC_updated.xlsx")
    else:
        print(f"\n--- DNC Update ---")
        print("No new DNC phones found across all files")
    
    print(f"{'=' * 60}")


# =====================================================================
# Tool 3 - Add State Column from Phone Area Code
# =====================================================================
AREA_CODE_TO_STATE = {
    # Alabama
    "205": "AL", "251": "AL", "256": "AL", "334": "AL", "659": "AL", "938": "AL",
    # Alaska
    "907": "AK",
    # Arizona
    "480": "AZ", "520": "AZ", "602": "AZ", "623": "AZ", "928": "AZ",
    # Arkansas
    "327": "AR", "479": "AR", "501": "AR", "870": "AR",
    # California
    "209": "CA", "213": "CA", "279": "CA", "310": "CA", "323": "CA", "341": "CA", "350": "CA", "369": "CA",
    "408": "CA", "415": "CA", "424": "CA", "442": "CA", "458": "CA", "510": "CA", "530": "CA", "559": "CA",
    "562": "CA", "619": "CA", "626": "CA", "628": "CA", "650": "CA", "657": "CA", "661": "CA", "669": "CA",
    "707": "CA", "714": "CA", "747": "CA", "760": "CA", "805": "CA", "818": "CA", "820": "CA", "831": "CA",
    "837": "CA", "840": "CA", "858": "CA", "909": "CA", "916": "CA", "925": "CA", "949": "CA", "951": "CA",
    # Colorado
    "303": "CO", "719": "CO", "720": "CO", "970": "CO", "983": "CO",
    # Connecticut
    "203": "CT", "475": "CT", "860": "CT", "959": "CT",
    # Delaware
    "302": "DE",
    # Florida
    "239": "FL", "305": "FL", "321": "FL", "352": "FL", "386": "FL", "407": "FL", "448": "FL", "561": "FL",
    "645": "FL", "656": "FL", "689": "FL", "727": "FL", "728": "FL", "754": "FL", "772": "FL", "786": "FL",
    "813": "FL", "850": "FL", "863": "FL", "904": "FL", "941": "FL", "954": "FL",
    # Georgia
    "229": "GA", "404": "GA", "470": "GA", "478": "GA", "678": "GA", "706": "GA", "762": "GA", "770": "GA",
    "912": "GA", "943": "GA",
    # Hawaii
    "808": "HI",
    # Idaho
    "208": "ID", "986": "ID",
    # Illinois
    "217": "IL", "224": "IL", "309": "IL", "312": "IL", "331": "IL", "447": "IL", "464": "IL", "618": "IL",
    "630": "IL", "708": "IL", "773": "IL", "779": "IL", "815": "IL", "847": "IL", "872": "IL",
    # Indiana
    "219": "IN", "260": "IN", "317": "IN", "463": "IN", "574": "IN", "765": "IN", "812": "IN", "930": "IN",
    # Iowa
    "319": "IA", "515": "IA", "563": "IA", "641": "IA", "712": "IA",
    # Kansas
    "316": "KS", "620": "KS", "785": "KS", "913": "KS",
    # Kentucky
    "270": "KY", "364": "KY", "502": "KY", "606": "KY", "859": "KY",
    # Louisiana
    "225": "LA", "318": "LA", "337": "LA", "457": "LA", "504": "LA", "985": "LA",
    # Maine
    "207": "ME",
    # Maryland
    "227": "MD", "240": "MD", "301": "MD", "410": "MD", "443": "MD", "667": "MD",
    # Massachusetts
    "339": "MA", "351": "MA", "413": "MA", "508": "MA", "617": "MA", "774": "MA", "781": "MA", "857": "MA",
    "978": "MA",
    # Michigan
    "231": "MI", "248": "MI", "269": "MI", "313": "MI", "517": "MI", "586": "MI", "616": "MI", "679": "MI",
    "734": "MI", "810": "MI", "906": "MI", "947": "MI", "989": "MI",
    # Minnesota
    "218": "MN", "320": "MN", "507": "MN", "612": "MN", "651": "MN", "763": "MN", "952": "MN",
    # Mississippi
    "228": "MS", "601": "MS", "662": "MS", "769": "MS",
    # Missouri
    "235": "MO", "314": "MO", "417": "MO", "557": "MO", "573": "MO", "636": "MO", "660": "MO", "816": "MO",
    "975": "MO",
    # Montana
    "406": "MT",
    # Nebraska
    "308": "NE", "402": "NE", "531": "NE",
    # Nevada
    "702": "NV", "725": "NV", "775": "NV",
    # New Hampshire
    "603": "NH",
    # New Jersey
    "201": "NJ", "551": "NJ", "609": "NJ", "640": "NJ", "732": "NJ", "848": "NJ", "856": "NJ", "862": "NJ",
    "908": "NJ", "973": "NJ",
    # New Mexico
    "505": "NM", "575": "NM",
    # New York
    "212": "NY", "315": "NY", "332": "NY", "347": "NY", "363": "NY", "516": "NY", "518": "NY", "585": "NY",
    "607": "NY", "631": "NY", "646": "NY", "680": "NY", "716": "NY", "718": "NY", "838": "NY", "845": "NY",
    "914": "NY", "917": "NY", "929": "NY", "934": "NY",
    # North Carolina
    "252": "NC", "336": "NC", "472": "NC", "704": "NC", "743": "NC", "828": "NC", "910": "NC", "919": "NC",
    "980": "NC", "984": "NC",
    # North Dakota
    "701": "ND",
    # Ohio
    "216": "OH", "220": "OH", "234": "OH", "283": "OH", "326": "OH", "330": "OH", "380": "OH", "419": "OH",
    "436": "OH", "440": "OH", "513": "OH", "567": "OH", "614": "OH", "740": "OH", "937": "OH",
    # Oklahoma
    "405": "OK", "539": "OK", "572": "OK", "580": "OK", "918": "OK",
    # Oregon
    "458": "OR", "503": "OR", "541": "OR", "971": "OR",
    # Pennsylvania
    "215": "PA", "223": "PA", "267": "PA", "272": "PA", "412": "PA", "445": "PA", "484": "PA", "570": "PA",
    "582": "PA", "610": "PA", "717": "PA", "724": "PA", "814": "PA", "835": "PA", "878": "PA",
    # Rhode Island
    "401": "RI",
    # South Carolina
    "803": "SC", "839": "SC", "843": "SC", "854": "SC", "864": "SC",
    # South Dakota
    "605": "SD",
    # Tennessee
    "423": "TN", "615": "TN", "629": "TN", "731": "TN", "865": "TN", "901": "TN", "931": "TN",
    # Texas
    "210": "TX", "214": "TX", "254": "TX", "281": "TX", "325": "TX", "346": "TX", "361": "TX", "409": "TX",
    "430": "TX", "432": "TX", "469": "TX", "512": "TX", "621": "TX", "682": "TX", "713": "TX", "726": "TX",
    "737": "TX", "806": "TX", "817": "TX", "830": "TX", "832": "TX", "903": "TX", "915": "TX", "936": "TX",
    "940": "TX", "945": "TX", "956": "TX", "972": "TX", "979": "TX",
    # Utah
    "385": "UT", "435": "UT", "801": "UT",
    # Vermont
    "802": "VT",
    # Virginia
    "276": "VA", "434": "VA", "540": "VA", "571": "VA", "703": "VA", "757": "VA", "804": "VA", "826": "VA",
    "948": "VA",
    # Washington
    "206": "WA", "253": "WA", "360": "WA", "425": "WA", "509": "WA", "564": "WA",
    # West Virginia
    "304": "WV", "681": "WV",
    # Wisconsin
    "262": "WI", "274": "WI", "414": "WI", "534": "WI", "608": "WI", "715": "WI", "920": "WI",
    # Wyoming
    "307": "WY",
    # District of Columbia
    "202": "DC",
    # Puerto Rico
    "787": "PR", "939": "PR",
    # U.S. Virgin Islands
    "340": "VI",
    # Guam
    "671": "GU",
    # Northern Mariana Islands
    "670": "MP",
}


def clean_phone_state(phone):
    """Convert common US phone formats to a 10-digit phone number."""
    if pd.isna(phone):
        return None
    digits = re.sub(r"\D", "", str(phone).strip())
    # Remove US country code when present
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else None


def get_state_from_phone(phone):
    """Infer a state/territory from the phone number's area code."""
    cleaned = clean_phone_state(phone)
    if not cleaned:
        return "INVALID"
    return AREA_CODE_TO_STATE.get(cleaned[:3], "UNKNOWN")


def run_add_state(a, out):
    """Add state column based on phone area code."""
    print("=" * 60)
    print("ADD STATE COLUMN FROM PHONE AREA CODE")
    print("=" * 60)

    print("Reading input file...")
    input_file = a["input_file"]

    # Read the file with flexible handling
    df = read_file_flexible(input_file, dtype=str)

    if df.empty:
        raise ValueError("The input file appears to be empty.")

    print(f"Total rows: {len(df):,}")

    # Determine phone column
    phone_column = a.get("phone_column")
    if phone_column and phone_column.strip():
        phone_column = phone_column.strip()
        if phone_column in df.columns:
            actual_phone_column = phone_column
        else:
            # Try case-insensitive search
            col_map = {str(c).strip().lower(): c for c in df.columns}
            phone_lower = phone_column.lower()
            if phone_lower in col_map:
                actual_phone_column = col_map[phone_lower]
            else:
                raise KeyError(
                    f"Phone column {phone_column!r} was not found. "
                    f"Available columns: {list(df.columns)}"
                )
    else:
        # Use first column
        actual_phone_column = df.columns[0]

    print(f"Phone column detected: {actual_phone_column}")

    # Clean phone numbers and detect states
    print("Processing phone numbers...")
    df["Clean_Phone"] = df[actual_phone_column].apply(clean_phone_state)
    df["State"] = df[actual_phone_column].apply(get_state_from_phone)
    df["Area_Code"] = df["Clean_Phone"].str[:3]

    # Save output
    output_ext = ".xlsx" if is_excel(input_file) else ".csv"
    output_filename = f"with_state{output_ext}"
    output_path = out / output_filename

    print(f"Saving output file: {output_filename}")
    if is_excel(input_file):
        df.to_excel(output_path, index=False, engine="openpyxl")
    else:
        df.to_csv(output_path, index=False, encoding="utf-8")

    print("\n" + "=" * 60)
    print("PROCESSING COMPLETE")
    print("=" * 60)
    print(f"Input file: {os.path.basename(str(input_file))}")
    print(f"Output file: {output_filename}")
    print(f"Rows processed: {len(df):,}")

    print("\nState Summary:")
    print("-" * 60)
    print(df["State"].value_counts().to_string())
    print("-" * 60)
    print(f"Invalid numbers: {int((df['State'] == 'INVALID').sum()):,}")
    print(f"Unknown area codes: {int((df['State'] == 'UNKNOWN').sum()):,}")


# =====================================================================
# Tool 5 - Split by percentage
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
# Tool 6 - Split into N equal parts (many files)
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
# Tool 7 - Split by time zone
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
            dict(name="am_excel", label="Answer Machine Calls (Excel)", kind="file", accept=CSV_XLSX,
                 default=str(Path(__file__).parent / "filles" / "Answer Machine Calls.xlsx")),
            dict(name="dnc", label="DNC list", kind="file", accept=CSV_XLSX,
                 default=str(Path(__file__).parent / "filles" / "DNC.xlsx")),
            dict(name="name_format", label="Split file name", kind="text", default="CYB-30-9-{}.csv",
                 hint="{} is replaced by the state, for example CYB-30-9-NY.csv"),
        ]),
    "re": dict(
        title="RE Pipeline",
        desc="Processes list .txt files: filters statuses, separates AM, DNC, U (Underage) and TX, removes duplicates, "
             "validates ZIP codes and splits by state. New DNC and Underage numbers are saved in an updated DNC file.",
        fn=run_re,
        fields=[
            dict(name="lists", label="List files (.txt, select several)", kind="files", accept=".txt"),
            dict(name="combine", label="Combine all lists into one combined list output", kind="check",
                 default=False,
                 hint="Checked: every selected list file is merged and processed as a single list. "
                      "Unchecked: each list file is processed separately."),
            dict(name="crm", label="CRM file (.csv / .xlsx / .xls / .xlsm)", kind="file", accept=CSV_XLSX),
            dict(name="am_txt", label="Answer Machine Calls (.txt)", kind="file", accept=".txt,.csv"),
            dict(name="am_excel", label="Answer Machine Calls (Excel)", kind="file", accept=".xlsx,.xls",
                 default=str(Path(__file__).parent / "filles" / "Answer Machine Calls.xlsx")),
            dict(name="zip_file", label="ZIP targeting file (.csv)", kind="file", accept=".csv",
                 default=str(Path(__file__).parent / "filles" / "MLA ZIP_Targeting.csv")),
            dict(name="dnc", label="DNC list (Excel)", kind="file", accept=".xlsx,.xls",
                 default=str(Path(__file__).parent / "filles" / "DNC.xlsx")),
            dict(name="name_format", label="Split file name", kind="text", default="{}.csv",
                 hint="The list ID is added in front automatically."),
        ]),
    "add_state": dict(
        title="Add State Column",
        desc="Adds a state column to your file based on phone area codes. Processes phone numbers to detect US states "
             "and territories from area codes. Outputs a file with Clean_Phone, State, and Area_Code columns added.",
        fn=run_add_state,
        fields=[
            dict(name="input_file", label="Input file (.csv / .xlsx / .xls)", kind="file", accept=CSV_XLSX),
            dict(name="phone_column", label="Phone column name (optional)", kind="text", default="",
                 hint="Leave empty to use the first column. Specify column name if phone data is in a specific column."),
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
# Runner helpers used by the Streamlit UI
# =====================================================================
def safe_name(name):
    name = os.path.basename(str(name))
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "file"


class _ThreadStdout:
    """Sends print() output to a per-thread buffer, so parallel sessions don't mix logs."""

    def __init__(self, real):
        self.real = real
        self.local = threading.local()

    def write(self, text):
        buf = getattr(self.local, "buf", None)
        return buf.write(text) if buf is not None else self.real.write(text)

    def flush(self):
        buf = getattr(self.local, "buf", None)
        (buf or self.real).flush()

    def __getattr__(self, name):
        return getattr(self.real, name)


if not isinstance(sys.stdout, _ThreadStdout):
    sys.stdout = _ThreadStdout(sys.stdout)
_stdout = sys.stdout


def run_tool(key, args, out_dir):
    """Run one tool. Returns (ok, log_text)."""
    buf = io.StringIO()
    _stdout.local.buf = buf
    ok = True
    try:
        TOOLS[key]["fn"](args, Path(out_dir))
    except Exception as e:
        ok = False
        print(f"\nERROR: {type(e).__name__}: {e}")
    finally:
        _stdout.local.buf = None
    return ok, buf.getvalue()
