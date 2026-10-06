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


CHUNK_ROWS = 100_000


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
    """Only the column names of a CSV/Excel file."""
    if is_excel(path):
        return pd.read_excel(path, nrows=0, dtype=str)
    return pd.read_csv(path, nrows=0, dtype=str, encoding=detect_encoding(path))


def iter_table(path, chunk_rows=CHUNK_ROWS):
    """Yield the file in pieces of text-only rows (CSV streams; Excel must be loaded once)."""
    if is_excel(path):
        df = pd.read_excel(path, dtype=str)
        for i in range(0, len(df), chunk_rows):
            yield df.iloc[i:i + chunk_rows]
    else:
        yield from pd.read_csv(path, dtype=str, encoding=detect_encoding(path), chunksize=chunk_rows)


def read_phone_set(path):
    """Cleaned phone numbers of a CSV/Excel file, reading ONLY the phone column."""
    col = find_phone_column(table_header(path))
    if is_excel(path):
        df = pd.read_excel(path, usecols=[col], dtype=str)
    else:
        df = pd.read_csv(path, usecols=[col], dtype=str, encoding=detect_encoding(path))
    return set(clean_phone_last10(df[col]).tolist())


def run_remove(a, out):
    name_format = need_placeholder(a["name_format"])
    split_dir = out / "SPLITS"
    split_dir.mkdir(parents=True, exist_ok=True)

    print("--- Starting Multi-Stage Filtering ---")
    exclude = set()

    print(" -> Loading CRM...")
    exclude.update(read_phone_set(a["crm"]))

    print(" -> Loading Answer Machine TXT...")
    am = pd.read_csv(a["am_txt"], header=None, names=["AM_PHONE"], dtype=str,
                     encoding="ISO-8859-1", low_memory=False)
    exclude.update(clean_phone_last10(am["AM_PHONE"]).tolist())
    del am

    print(" -> Loading Answer Machine Excel...")
    exclude.update(read_phone_set(a["am_excel"]))

    print(" -> Loading DNC list...")
    dnc_phones = read_phone_set(a["dnc"])
    exclude.update(dnc_phones)
    print(f" -> DNC numbers loaded: {len(dnc_phones):,}")
    print(f" -> Total excluded numbers: {len(exclude):,}")

    print(" -> Loading input file and removing matches (in chunks)...")
    header = table_header(a["order"])
    phone_col = find_phone_column(header)
    nodup_path = out / "combined_data_nodup.csv"
    removed_path = out / "combined_data_removed_matches.csv"
    kept_n = removed_n = 0
    wrote_kept = wrote_removed = False
    written = {}          # split file name -> rows written
    state_missing = False

    for chunk in iter_table(a["order"]):
        clean = clean_phone_last10(chunk[phone_col])
        is_match = clean.isin(exclude).reindex(chunk.index, fill_value=False)
        removed, kept = chunk[is_match], chunk[~is_match]
        removed_n += len(removed)
        kept_n += len(kept)
        removed.to_csv(removed_path, mode="a", header=not wrote_removed, index=False)
        kept.to_csv(nodup_path, mode="a", header=not wrote_kept, index=False)
        wrote_removed = wrote_kept = True

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


# =====================================================================
# Tool 2 - RE Pipeline
# =====================================================================
def clean_phone_re(series):
    s = series.fillna("").astype(str).str.strip().str.replace(r"\D", "", regex=True)
    s = s.mask((s.str.len() > 10) & s.str.endswith("0"), s.str[:-1])
    s = s.mask(s.str.len() >= 10, s.str[-10:])
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
        df = pd.read_csv(input_file, sep="\t", dtype=str, on_bad_lines="skip", encoding="ISO-8859-1",
                         usecols=["phone_number", "first_name", "last_name", "address1", "city",
                                  "state", "postal_code", "status"])
        statuses = ["AA", "A", "AB", "AL", "B", "AM", "CBHOLD", "CALLBK", "DAIR", "DEC", "DROP", "NEW",
                    "N", "NP", "PDROP", "DC", "PU", "OA", "ERI", "UA", "DNC"]
        df = df[df["status"].isin(statuses)]
        sel = df[["phone_number", "first_name", "last_name", "address1", "city", "state",
                  "postal_code", "status"]].copy()
        del df
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
    add = pd.DataFrame({existing.columns[0]: sorted(truly_new)})
    updated = pd.concat([existing, add], ignore_index=True)
    updated.to_excel(out_path, index=False)
    print(f"Updated DNC file saved as DNC_updated.xlsx: {len(updated)} total records")


def run_re(a, out):
    fmt = need_placeholder(a["name_format"])
    print("=" * 60)
    print("RE PIPELINE - LIST ID BASED WITH ZIP VALIDATION AND DNC FILTERING")
    print("=" * 60)

    print("Loading reference data...")
    exclude = set()
    crm = pd.read_csv(a["crm"], usecols=["Mobile Phone"], dtype=str)
    print(f"  -> Loaded CRM: {len(crm)} records")
    exclude.update(clean_phone_re(crm["Mobile Phone"]))
    del crm
    am_txt = pd.read_csv(a["am_txt"], header=None, names=["AM_PHONE"], dtype=str, low_memory=False)
    print(f"  -> Loaded Answer Machine TXT: {len(am_txt)} records")
    exclude.update(clean_phone_re(am_txt["AM_PHONE"]))
    del am_txt
    am_xl = pd.read_excel(a["am_excel"], usecols=["Mobile Phone"], dtype=str)
    print(f"  -> Loaded Answer Machine Excel: {len(am_xl)} records")
    exclude.update(clean_phone_re(am_xl["Mobile Phone"]))
    del am_xl
    try:
        dnc = pd.read_excel(a["dnc"], usecols=[0], dtype=str)
        print(f"  -> Loaded DNC file: {len(dnc)} records")
        dnc_phones = clean_phone_re(dnc.iloc[:, 0])
        exclude.update(set(dnc_phones))
        print(f"  -> Added {len(dnc_phones)} DNC phones to exclusion set")
        del dnc
    except Exception as e:
        print(f"  -> Error loading DNC file: {e}")
    print(f"Building exclusion phone set... total exclusion phones: {len(exclude)}")

    print("Loading valid ZIP codes...")
    try:
        zdf = pd.read_csv(a["zip_file"], usecols=["ZIP Code"], dtype=str)
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
