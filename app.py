"""Data Tools - Streamlit UI. Run locally with:  streamlit run app.py"""
import gc
import hmac
import io
import tempfile
import time
import zipfile
from pathlib import Path

import streamlit as st

from tools import TOOLS, run_tool, safe_name

st.set_page_config(page_title="Data Tools", layout="centered")


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


# ---------------------------------------------------------------------
# Password gate (the password lives in the app's Secrets, not in the code)
# ---------------------------------------------------------------------
def require_login():
    try:
        expected = str(st.secrets["APP_PASSWORD"])
    except Exception:
        expected = ""
    if not expected:
        st.error("No password is set. Add APP_PASSWORD in the app's Secrets settings, then reload.")
        st.stop()
    if st.session_state.get("ok"):
        return

    st.title("Data Tools")
    with st.form("login"):
        pw = st.text_input("Password", type="password")
        go = st.form_submit_button("Log in")
    if go:
        if hmac.compare_digest(pw.encode(), expected.encode()):
            st.session_state["ok"] = True
            st.rerun()
        else:
            time.sleep(1)  # slows down guessing
            st.error("Wrong password.")
    st.stop()


# ---------------------------------------------------------------------
# Running a tool
# ---------------------------------------------------------------------
def process(key, spec, values):
    """Save uploads to a temp folder, run the tool, return outputs as bytes."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        out = tmp / "out"
        out.mkdir(parents=True)
        args = {}
        for f in spec["fields"]:
            v = values[f["name"]]
            if f["kind"] == "text":
                args[f["name"]] = (v or "").strip()
                continue
            folder = tmp / "in" / f["name"]
            folder.mkdir(parents=True, exist_ok=True)
            saved = []
            for i, u in enumerate([v] if f["kind"] == "file" else v):
                path = folder / safe_name(u.name)
                if path.exists():  # two uploads with the same name
                    path = folder / f"{i}_{path.name}"
                path.write_bytes(u.getbuffer())  # no extra copy in memory
                saved.append(path)
            args[f["name"]] = saved[0] if f["kind"] == "file" else saved
        ok, log = run_tool(key, args, out)
        files = {p.relative_to(out).as_posix(): p.read_bytes() for p in sorted(out.rglob("*")) if p.is_file()}
    gc.collect()
    return {"ok": ok, "log": log, "files": files}


def make_zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def show_results(key):
    res = st.session_state.get("results", {}).get(key)
    if not res:
        return
    st.divider()
    if res["ok"]:
        st.success(f"Finished. {len(res['files'])} output file(s).")
    else:
        st.error("Stopped with an error. Open the log below to see what went wrong.")

    files = res["files"]
    total = sum(len(d) for d in files.values())
    if len(files) > 1 and total <= 60 * 1024 * 1024:  # a ZIP of big results would double the memory use
        st.download_button("Download everything (ZIP)", make_zip(files), file_name=f"{key}_results.zip",
                           mime="application/zip", key=f"zip_{key}")
    for i, (name, data) in enumerate(files.items()):
        left, right = st.columns([4, 1])
        left.write(f"{name}  ·  {human(len(data))}")
        right.download_button("Download", data, file_name=Path(name).name, key=f"dl_{key}_{i}")

    with st.expander("Log", expanded=not res["ok"]):
        st.code(res["log"] or "(empty)", language=None)
    if st.button("Clear results", key=f"clear_{key}"):
        del st.session_state["results"][key]
        st.rerun()


# ---------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------
require_login()

st.title("Data Tools")
st.caption("Upload your files, set the options and press Run. Results stay in this browser session only "
           "and disappear when you close the page.")
with st.sidebar:
    if st.button("Log out"):
        st.session_state.clear()
        st.rerun()

keys = list(TOOLS)
tabs = st.tabs([TOOLS[k]["title"] for k in keys])

for key, tab in zip(keys, tabs):
    spec = TOOLS[key]
    with tab:
        st.write(spec["desc"])
        with st.form(f"form_{key}"):
            values = {}
            for f in spec["fields"]:
                wid = f"{key}_{f['name']}"
                if f["kind"] == "text":
                    values[f["name"]] = st.text_input(f["label"], value=f["default"], help=f.get("hint"), key=wid)
                else:
                    types = [e.strip().lstrip(".") for e in f["accept"].split(",") if e.strip()]
                    values[f["name"]] = st.file_uploader(
                        f["label"], type=types, accept_multiple_files=(f["kind"] == "files"), key=wid)
            submitted = st.form_submit_button(f"Run {spec['title']}")

        if submitted:
            missing = [f["label"] for f in spec["fields"] if f["kind"] != "text" and not values[f["name"]]]
            if missing:
                st.error("Missing file: " + ", ".join(missing))
            else:
                with st.spinner("Processing your files..."):
                    st.session_state.setdefault("results", {})[key] = process(key, spec, values)
        show_results(key)
