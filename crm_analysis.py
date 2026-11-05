"""CRM Analysis page: date validation, cleaning, month and date filters, KPIs and charts.

Rules:
  Sale     = Closing Status is Approved, Postdated, Pending Bank Approval or Pending Client Reply
  Transfer = Opening Status is Transferred or Red Flag
  Transfers are counted by Creation Date; sales by Date of Sale.
  Every number is calculated from the latest saved CRM file.
"""
import io
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

CRM_DATA_DIR = Path(__file__).parent / "saved_crm_data"
CRM_DATA_DIR.mkdir(exist_ok=True)

REQUIRED_COLS = ["Closing Status", "Opening Status", "State", "Creation Date", "Date of Sale"]
SALE_LABELS = {
    "approved": "Approved",
    "postdated": "Postdated",
    "pending bank approval": "Pending Bank Approval",
    "pending client reply": "Pending Client Reply",
}
TRANSFER_LABELS = {"transferred": "Transferred", "red flag": "Red Flag"}
SALE_ORDER = list(SALE_LABELS.values())
TRANSFER_ORDER = list(TRANSFER_LABELS.values())
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DAY = pd.Timedelta(days=1)
MIN_VALID_DATE = pd.Timestamp("2020-01-01")
EMPTY_TOKENS = {"", "nan", "nat", "none", "null"}
ALL_MONTHS = "All months"

US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY", "district of columbia": "DC", "washington dc": "DC",
    "washington, d.c.": "DC", "d.c.": "DC",
}
STATE_CODES = set(US_STATES.values())


# ---------------- storage ----------------
def save_crm_data(df, filename):
    """Save CRM data to disk. Keeps only the most recent file."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = CRM_DATA_DIR / f"crm_{timestamp}_{filename}"
    df.to_csv(filepath, index=False)
    for old_file in sorted(CRM_DATA_DIR.glob("crm_*.csv"))[:-1]:
        old_file.unlink()
    return filepath


@st.cache_data(show_spinner=False)
def _read_crm_csv(path_str, mtime):
    """Re-reads only when the file changes (the mtime is part of the cache key)."""
    return pd.read_csv(path_str, low_memory=False)


def load_latest_crm_data():
    """Return (raw dataframe, file timestamp) for the newest saved CRM file, or (None, None)."""
    files = sorted(CRM_DATA_DIR.glob("crm_*.csv"))
    if not files:
        return None, None
    latest = files[-1]
    mtime = latest.stat().st_mtime
    return _read_crm_csv(str(latest), mtime), mtime


# ---------------- cleaning ----------------
def clean_state(value):
    """Map a state name, code, or 'City, ST' value to a two-letter code, else 'Unknown'."""
    if pd.isna(value):
        return "Unknown"
    text = re.sub(r"\s+", " ", str(value).strip())
    lower = text.lower()
    if lower in US_STATES:
        return US_STATES[lower]
    if text.upper() in STATE_CODES:
        return text.upper()
    m = re.search(r",\s*([A-Za-z]{2})$", text)
    if m and m.group(1).upper() in STATE_CODES:
        return m.group(1).upper()
    return "Unknown"


def parse_price(value):
    """'$44.95/Month' -> 44.95. NaN when no number is found."""
    if pd.isna(value):
        return float("nan")
    m = re.search(r"\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(m.group(0)) if m else float("nan")


def clean_dates(series, upper):
    """Clean one raw date column.

    Returns (valid_dates, reason). Reason is one of:
    Valid, Empty, Zero date, Unreadable, Before 2020, In the future.
    Invalid values become NaT, so they are excluded whenever a date filter is applied.
    """
    raw = series.fillna("").astype(str).str.strip()
    empty = raw.str.lower().isin(EMPTY_TOKENS)
    zero = raw.str.startswith("0000")
    candidate = raw.where(~empty & ~zero)
    parsed = pd.to_datetime(candidate, errors="coerce", format="mixed")
    too_old = parsed < MIN_VALID_DATE
    too_new = parsed >= upper
    valid = parsed.where(~too_old & ~too_new)

    reason = pd.Series("Valid", index=series.index)
    reason.loc[empty] = "Empty"
    reason.loc[zero] = "Zero date"
    reason.loc[parsed.isna() & ~empty & ~zero] = "Unreadable"
    reason.loc[too_old] = "Before 2020"
    reason.loc[too_new] = "In the future"
    return valid, reason


def _text(df, col, fill="Unknown"):
    if col not in df.columns:
        return pd.Series(fill, index=df.index)
    s = df[col].fillna("").astype(str).str.strip()
    return s.where(s != "", fill)


def clean_crm(raw):
    """Add the derived columns used by the analysis. Raises ValueError if required columns are missing."""
    missing = [c for c in REQUIRED_COLS if c not in raw.columns]
    if missing:
        raise ValueError("CRM file is missing required columns: " + ", ".join(missing))

    df = raw.copy()
    upper = pd.Timestamp.now().normalize() + DAY
    df["created"], df["created_check"] = clean_dates(df["Creation Date"], upper)
    df["sold"], df["sold_check"] = clean_dates(df["Date of Sale"], upper)

    closing = _text(df, "Closing Status", "").str.lower()
    opening = _text(df, "Opening Status", "").str.lower()
    df["Sale Label"] = closing.map(SALE_LABELS)
    df["Transfer Label"] = opening.map(TRANSFER_LABELS)
    df["is_sale"] = df["Sale Label"].notna()
    df["is_transfer"] = df["Transfer Label"].notna()
    df["is_red_flag"] = df["Transfer Label"].eq("Red Flag")

    df["State_Clean"] = df["State"].map(clean_state)
    df["Monthly Price"] = df["Pricing List"].map(parse_price) if "Pricing List" in df.columns else float("nan")
    df["Product_Clean"] = _text(df, "Product")
    df["Closer"] = _text(df, "Closer Name")
    df["Opener"] = _text(df, "Opener Name")
    return df


# ---------------- filtering ----------------
def in_range(series, rng):
    """Inclusive date-range mask. Invalid dates (NaT) are excluded only when a range is set."""
    if rng is None:
        return pd.Series(True, index=series.index)
    start, end = rng
    return (series >= start) & (series < end + DAY)


def apply_filters(df, sale_rng, transfer_rng, states, products):
    base = df
    if states:
        base = base[base["State_Clean"].isin(states)]
    if products:
        base = base[base["Product_Clean"].isin(products)]
    transfers = base[base["is_transfer"] & in_range(base["created"], transfer_rng)]
    sales = base[base["is_sale"] & in_range(base["sold"], sale_rng)]
    return base, transfers, sales


def available_months(df):
    months = pd.concat([df["created"], df["sold"]]).dropna().dt.strftime("%Y-%m")
    return sorted(months.unique())


def month_range(choice):
    p = pd.Period(choice, freq="M")
    return p.start_time.normalize(), p.end_time.normalize()


# ---------------- analysis tables ----------------
def headline(t, s):
    transfers, sales = len(t), len(s)
    converted = int(t["is_sale"].sum())
    red = int(t["is_red_flag"].sum())
    days = days_to_sale(s)
    return {
        "transfers": transfers,
        "sales": sales,
        "transferred": int((t["Transfer Label"] == "Transferred").sum()),
        "red_flags": red,
        "converted": converted,
        "conversion": converted / transfers * 100 if transfers else 0.0,
        "red_flag_share": red / transfers * 100 if transfers else 0.0,
        "sales_value": float(s["Monthly Price"].sum()),
        "sales_no_price": int(s["Monthly Price"].isna().sum()),
        "median_days": float(days.median()) if len(days) else float("nan"),
    }


def days_to_sale(s):
    x = s.dropna(subset=["created", "sold"])
    d = (x["sold"] - x["created"]).dt.days
    return d[(d >= 0) & (d <= 365)]


def monthly_table(t, s):
    tm = t.groupby(t["created"].dt.to_period("M")).agg(Transfers=("is_transfer", "size"),
                                                       RedFlags=("is_red_flag", "sum"))
    sm = s.groupby(s["sold"].dt.to_period("M")).agg(Sales=("is_sale", "size"))
    out = tm.join(sm, how="outer").fillna(0).astype(int).sort_index()
    out["Conversion %"] = (out["Sales"] / out["Transfers"].where(out["Transfers"] > 0) * 100).round(1)
    out["Red Flag %"] = (out["RedFlags"] / out["Transfers"].where(out["Transfers"] > 0) * 100).round(1)
    out = out.rename(columns={"RedFlags": "Red Flags"})
    out.index = out.index.astype(str)
    out.index.name = "Month"
    return out.reset_index()


def monthly_status(s):
    x = s.dropna(subset=["sold"])
    if x.empty:
        return pd.DataFrame()
    month = x["sold"].dt.strftime("%Y-%m").rename("Month")
    m = x.groupby([month, "Sale Label"]).size().unstack(fill_value=0)
    return m.reindex(columns=SALE_ORDER, fill_value=0)


def daily_table(t, s):
    td = t.groupby(t["created"].dt.normalize()).size().rename("Transfers")
    sd = s.groupby(s["sold"].dt.normalize()).size().rename("Sales")
    out = pd.concat([td, sd], axis=1)
    cols = ["Date", "Transfers", "Sales", "Transfers 7d avg", "Sales 7d avg", "Cum Transfers", "Cum Sales"]
    if out.empty:
        return pd.DataFrame(columns=cols)
    out = out.reindex(pd.date_range(out.index.min(), out.index.max(), freq="D")).fillna(0).astype(int)
    out["Transfers 7d avg"] = out["Transfers"].rolling(7, min_periods=1).mean().round(1)
    out["Sales 7d avg"] = out["Sales"].rolling(7, min_periods=1).mean().round(1)
    out["Cum Transfers"] = out["Transfers"].cumsum()
    out["Cum Sales"] = out["Sales"].cumsum()
    out.index.name = "Date"
    return out.reset_index()


def weekday_table(t, s):
    tw = t["created"].dt.day_name().value_counts().reindex(WEEKDAYS, fill_value=0)
    sw = s["sold"].dt.day_name().value_counts().reindex(WEEKDAYS, fill_value=0)
    return pd.DataFrame({"Day": WEEKDAYS, "Transfers": tw.values, "Sales": sw.values})


def weekday_month_matrix(s):
    x = s.dropna(subset=["sold"])
    if x.empty:
        return pd.DataFrame()
    day = x["sold"].dt.day_name().rename("Day")
    month = x["sold"].dt.strftime("%Y-%m").rename("Month")
    m = x.groupby([day, month]).size().unstack(fill_value=0)
    return m.reindex(WEEKDAYS).fillna(0).astype(int)


def state_table(t, s):
    tr = t.groupby("State_Clean").size().rename("Transfers")
    sa = s.groupby("State_Clean").size().rename("Sales")
    out = pd.concat([tr, sa], axis=1).fillna(0).astype(int)
    out["Conversion %"] = (out["Sales"] / out["Transfers"].where(out["Transfers"] > 0) * 100).round(1)
    out.index.name = "State"
    return out.sort_values("Sales", ascending=False).reset_index()


def state_status(s, top=10):
    x = s[s["State_Clean"] != "Unknown"]
    if x.empty:
        return pd.DataFrame()
    top_states = list(x["State_Clean"].value_counts().head(top).index)
    m = x[x["State_Clean"].isin(top_states)].groupby(["State_Clean", "Sale Label"]).size().unstack(fill_value=0)
    m = m.reindex(index=top_states, columns=SALE_ORDER, fill_value=0)
    return m


def status_counts(t, s):
    sales = s["Sale Label"].value_counts().reindex(SALE_ORDER, fill_value=0)
    trans = t["Transfer Label"].value_counts().reindex(TRANSFER_ORDER, fill_value=0)
    return sales, trans


def price_table(s):
    p = s.dropna(subset=["Monthly Price"]).groupby("Monthly Price").size().rename("Sales").reset_index()
    p["Value / month"] = (p["Monthly Price"] * p["Sales"]).round(2)
    p["Tier"] = p["Monthly Price"].map(lambda v: f"${v:,.2f}/mo")
    return p.sort_values("Monthly Price")


def closer_table(s):
    c = s[s["Closer"] != "Unknown"].groupby("Closer").size().rename("Sales")
    return c.sort_values(ascending=False).head(10).reset_index()


def opener_table(t, min_transfers):
    g = t.groupby("Opener").agg(
        Transfers=("is_transfer", "size"),
        RedFlags=("is_red_flag", "sum"),
        Sales=("is_sale", "sum"),
    ).reset_index()
    g = g[(g["Opener"] != "Unknown") & (g["Transfers"] >= min_transfers)].copy()
    g["Red Flags"] = g["RedFlags"].astype(int)
    g["Sales"] = g["Sales"].astype(int)
    g["Conversion %"] = (g["Sales"] / g["Transfers"] * 100).round(1)
    g["Red Flag %"] = (g["Red Flags"] / g["Transfers"] * 100).round(1)
    return g[["Opener", "Transfers", "Red Flags", "Red Flag %", "Sales", "Conversion %"]].sort_values(
        "Conversion %", ascending=False
    )


def date_reason_table(df):
    c = df["created_check"].value_counts().rename("Creation Date")
    s = df["sold_check"].value_counts().rename("Date of Sale")
    out = pd.concat([c, s], axis=1).fillna(0).astype(int)
    out.index.name = "Result"
    return out.reset_index()


def quality_report(df, base, t, s):
    unknown_share = (base["State_Clean"] == "Unknown").mean() * 100 if len(base) else 0.0
    return pd.DataFrame({
        "Check": [
            "Rows in file",
            "Rows after filters",
            "Transfers with no valid Creation Date",
            "Sales with no valid Date of Sale",
            "Unknown state (after filters)",
            "Sales with no Pricing List value",
            "Sales with no Closer name",
        ],
        "Value": [
            f"{len(df):,}",
            f"{len(base):,}",
            f"{int(t['created'].isna().sum()):,}",
            f"{int(s['sold'].isna().sum()):,}",
            f"{unknown_share:.1f}%",
            f"{int(s['Monthly Price'].isna().sum()):,}",
            f"{int((s['Closer'] == 'Unknown').sum()):,}",
        ],
    })


def to_excel_bytes(sheets):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=name[:31], index=False)
    return buf.getvalue()


# ---------------- page ----------------
def date_range_input(label, series, key):
    valid = series.dropna()
    if valid.empty:
        st.caption(f"{label}: no valid dates in file")
        return None
    lo, hi = valid.min().date(), valid.max().date()
    # The bounds are part of the key, so a new daily upload resets the range to the new data.
    picked = st.date_input(label, value=(lo, hi), min_value=lo, max_value=hi, key=f"{key}_{lo}_{hi}")
    if isinstance(picked, (tuple, list)) and len(picked) == 2:
        return pd.Timestamp(picked[0]), pd.Timestamp(picked[1])
    return None


def show_crm_analysis_page():
    st.title("CRM Analysis")
    st.caption(
        "Transfers are counted by Creation Date and sales by Date of Sale. "
        "Sale = Approved, Postdated, Pending Bank Approval or Pending Client Reply. "
        "Transfer = Transferred or Red Flag."
    )

    raw, file_mtime = load_latest_crm_data()
    if raw is None:
        st.info("No CRM data available. Upload a CRM file in the Data Tools page first.")
        return
    try:
        df = clean_crm(raw)
    except ValueError as e:
        st.error(str(e))
        return

    def _fmt(ts):
        return ts.strftime("%Y-%m-%d") if pd.notna(ts) else "n/a"

    st.success(
        f"Data file saved {datetime.fromtimestamp(file_mtime):%Y-%m-%d %H:%M} · "
        f"{len(df):,} leads · latest Creation Date {_fmt(df['created'].max())} · "
        f"latest Date of Sale {_fmt(df['sold'].max())}"
    )

    with st.sidebar:
        st.header("Filters")
        if st.button("Refresh from latest file"):
            st.cache_data.clear()
            st.rerun()
        month_choice = st.selectbox("Month", [ALL_MONTHS] + available_months(df), key="month_choice")
        if month_choice != ALL_MONTHS:
            transfer_rng = sale_rng = month_range(month_choice)
            st.caption("The month applies to both transfer and sale dates. Date ranges are ignored.")
        else:
            transfer_rng = date_range_input("Transfer dates (Creation Date)", df["created"], "tr_dates")
            sale_rng = date_range_input("Sale dates (Date of Sale)", df["sold"], "sale_dates")
        states = st.multiselect("State", sorted(df["State_Clean"].unique()))
        products = st.multiselect("Product", sorted(df["Product_Clean"].unique()))
        min_transfers = st.slider("Min transfers to rank an opener", 1, 200, 20)
        st.divider()
        if st.button("Clear saved CRM data"):
            for f in CRM_DATA_DIR.glob("crm_*.csv"):
                f.unlink()
            st.rerun()

    base, t, s = apply_filters(df, sale_rng, transfer_rng, states, products)
    if len(base) == 0:
        st.warning("No records match the current filters.")
        return
    k = headline(t, s)
    sales_status, trans_status = status_counts(t, s)

    # KPI cards
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Transfers", f"{k['transfers']:,}")
    c2.metric("Sales", f"{k['sales']:,}")
    c3.metric("Transfer → Sale", f"{k['conversion']:.1f}%",
              help="Transfers in the window that are also sales, divided by transfers.")
    c4.metric("Monthly sales value", f"${k['sales_value']:,.0f}",
              help="Sum of Pricing List per month for sales in the window.")
    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Transferred", f"{k['transferred']:,}")
    c6.metric("Red flags", f"{k['red_flags']:,}", f"{k['red_flag_share']:.1f}% of transfers", delta_color="off")
    c7.metric("Approved + Postdated", f"{int(sales_status['Approved'] + sales_status['Postdated']):,}")
    c8.metric("Median days to sale",
              "n/a" if pd.isna(k["median_days"]) else f"{k['median_days']:.0f}",
              help="Days from Creation Date to Date of Sale, for sales with both dates.")

    tab_over, tab_trend, tab_geo, tab_people, tab_quality = st.tabs(
        ["Overview", "Trends", "Geography", "People & pricing", "Data quality"]
    )

    # ---- Overview ----
    with tab_over:
        col_a, col_b = st.columns(2)
        with col_a:
            fig = px.pie(names=["Transfers", "Sales"], values=[k["transfers"], k["sales"]],
                         hole=0.5, title="Sales vs transfers")
            st.plotly_chart(fig, width="stretch", key="ov_donut")
        with col_b:
            fig = px.funnel(
                pd.DataFrame({"Stage": ["Transfers", "Converted to sale", "Sales"],
                              "Count": [k["transfers"], k["converted"], k["sales"]]}),
                x="Count", y="Stage", title="Transfer to sale funnel")
            st.plotly_chart(fig, width="stretch", key="ov_funnel")

        col_c, col_d = st.columns(2)
        with col_c:
            fig = px.bar(x=sales_status.index, y=sales_status.values,
                         labels={"x": "Sale status", "y": "Count"}, title="Sales by status")
            st.plotly_chart(fig, width="stretch", key="ov_sales_status")
        with col_d:
            fig = px.bar(x=trans_status.index, y=trans_status.values,
                         labels={"x": "Transfer status", "y": "Count"}, title="Transfers by status")
            st.plotly_chart(fig, width="stretch", key="ov_trans_status")

        prices = price_table(s)
        if len(prices):
            col_e, col_f = st.columns(2)
            with col_e:
                fig = px.pie(prices, names="Tier", values="Sales", title="Sales by price tier")
                st.plotly_chart(fig, width="stretch", key="ov_price_pie")
            with col_f:
                fig = px.bar(prices, x="Tier", y="Value / month", title="Monthly value by price tier")
                st.plotly_chart(fig, width="stretch", key="ov_price_bar")

        monthly_df = monthly_table(t, s)
        st.download_button(
            "Download summary (Excel, counts only)",
            data=to_excel_bytes({
                "Headline": pd.DataFrame(list(k.items()), columns=["Metric", "Value"]),
                "Monthly": monthly_df,
                "Daily": daily_table(t, s),
                "By state": state_table(t, s),
                "Openers": opener_table(t, min_transfers),
            }),
            file_name="crm_analysis_summary.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    # ---- Trends ----
    with tab_trend:
        monthly_df = monthly_table(t, s)
        if len(monthly_df):
            fig = go.Figure()
            fig.add_bar(x=monthly_df["Month"], y=monthly_df["Transfers"], name="Transfers")
            fig.add_bar(x=monthly_df["Month"], y=monthly_df["Sales"], name="Sales")
            fig.update_layout(barmode="group", title="Monthly transfers and sales",
                              xaxis_title="Month", yaxis_title="Count")
            st.plotly_chart(fig, width="stretch", key="tr_monthly")
            st.caption("The latest month may be partial.")

            fig = go.Figure()
            fig.add_scatter(x=monthly_df["Month"], y=monthly_df["Conversion %"], mode="lines+markers",
                            name="Transfer → Sale %")
            fig.add_scatter(x=monthly_df["Month"], y=monthly_df["Red Flag %"], mode="lines+markers",
                            name="Red flag % of transfers")
            fig.update_layout(title="Monthly conversion and red-flag rate", yaxis_title="%")
            st.plotly_chart(fig, width="stretch", key="tr_rates")

        msd = monthly_status(s)
        if len(msd):
            fig = go.Figure()
            for col in msd.columns:
                fig.add_bar(x=msd.index, y=msd[col], name=col)
            fig.update_layout(barmode="stack", title="Sales by status per month")
            st.plotly_chart(fig, width="stretch", key="tr_status_month")

        daily = daily_table(t, s)
        if len(daily):
            fig = go.Figure()
            fig.add_scatter(x=daily["Date"], y=daily["Transfers"], mode="markers",
                            name="Transfers (daily)", opacity=0.35)
            fig.add_scatter(x=daily["Date"], y=daily["Transfers 7d avg"], mode="lines",
                            name="Transfers (7-day avg)")
            fig.add_scatter(x=daily["Date"], y=daily["Sales"], mode="markers",
                            name="Sales (daily)", opacity=0.35)
            fig.add_scatter(x=daily["Date"], y=daily["Sales 7d avg"], mode="lines",
                            name="Sales (7-day avg)")
            fig.update_layout(title="Daily trend with 7-day average", xaxis_title="Date", yaxis_title="Count")
            st.plotly_chart(fig, width="stretch", key="tr_daily")

            fig = go.Figure()
            fig.add_scatter(x=daily["Date"], y=daily["Cum Transfers"], mode="lines", name="Cumulative transfers")
            fig.add_scatter(x=daily["Date"], y=daily["Cum Sales"], mode="lines", name="Cumulative sales")
            fig.update_layout(title="Cumulative transfers and sales", yaxis_title="Count")
            st.plotly_chart(fig, width="stretch", key="tr_cumulative")

        wd = weekday_table(t, s)
        fig = go.Figure()
        fig.add_bar(x=wd["Day"], y=wd["Transfers"], name="Transfers")
        fig.add_bar(x=wd["Day"], y=wd["Sales"], name="Sales")
        fig.update_layout(barmode="group", title="Day of week (Monday to Sunday)")
        st.plotly_chart(fig, width="stretch", key="tr_weekday")

        wm = weekday_month_matrix(s)
        if wm.size:
            fig = px.imshow(wm, aspect="auto", color_continuous_scale="Blues",
                            labels={"x": "Month", "y": "Day", "color": "Sales"},
                            title="Sales heatmap: day of week × month")
            st.plotly_chart(fig, width="stretch", key="tr_heatmap")

        days = days_to_sale(s)
        if len(days):
            fig = px.histogram(days, nbins=30, title="Days from creation to sale",
                               labels={"value": "Days", "count": "Sales"})
            fig.update_layout(showlegend=False)
            st.plotly_chart(fig, width="stretch", key="tr_days_hist")
            st.caption(f"Median {days.median():.0f} days, 75th percentile {days.quantile(0.75):.0f} days.")

    # ---- Geography ----
    with tab_geo:
        st_df = state_table(t, s)
        known = st_df[st_df["State"].isin(STATE_CODES)]
        if len(known):
            fig = px.choropleth(known, locations="State", locationmode="USA-states", scope="usa",
                                color="Sales", hover_data={"Transfers": True, "Conversion %": True},
                                color_continuous_scale="Blues", title="Sales by state")
            st.plotly_chart(fig, width="stretch", key="geo_map")

        top = st_df.head(15)
        fig = go.Figure()
        fig.add_bar(x=top["State"], y=top["Transfers"], name="Transfers")
        fig.add_bar(x=top["State"], y=top["Sales"], name="Sales")
        fig.update_layout(barmode="group", title="Top 15 states")
        st.plotly_chart(fig, width="stretch", key="geo_bar")

        ss = state_status(s)
        if len(ss):
            fig = go.Figure()
            for col in ss.columns:
                fig.add_bar(x=ss.index, y=ss[col], name=col)
            fig.update_layout(barmode="stack", title="Sales by status, top 10 states")
            st.plotly_chart(fig, width="stretch", key="geo_status")

        conv_top = st_df[st_df["Transfers"] >= min_transfers].sort_values("Conversion %", ascending=False).head(15)
        if len(conv_top):
            fig = px.bar(conv_top, x="State", y="Conversion %",
                         title=f"Best conversion states (at least {min_transfers} transfers)")
            st.plotly_chart(fig, width="stretch", key="geo_conversion")

        st.dataframe(st_df, width="stretch", hide_index=True)

    # ---- People & pricing ----
    with tab_people:
        closers = closer_table(s)
        if len(closers):
            fig = px.bar(closers, x="Closer", y="Sales", title="Top 10 closers by sales")
            st.plotly_chart(fig, width="stretch", key="pp_closers")

        st.subheader("Openers ranked by conversion")
        openers = opener_table(t, min_transfers)
        if len(openers):
            top_openers = openers.head(15)
            fig = px.bar(top_openers, x="Opener", y="Conversion %",
                         title=f"Top 15 openers by conversion (at least {min_transfers} transfers)")
            st.plotly_chart(fig, width="stretch", key="pp_openers")
            st.dataframe(openers, width="stretch", hide_index=True)
        else:
            st.info(f"No opener has at least {min_transfers} transfers in the filtered data.")

        prices = price_table(s)
        if len(prices):
            st.subheader("Price tiers")
            st.dataframe(prices[["Tier", "Sales", "Value / month"]], width="stretch", hide_index=True)

    # ---- Data quality ----
    with tab_quality:
        st.subheader("Date validation")
        st.caption("Dates that can't be used are counted here and excluded from date filters. "
                   "Values before 2020 or after today are treated as invalid.")
        st.dataframe(date_reason_table(df), width="stretch", hide_index=True)

        st.subheader("Other checks")
        st.dataframe(quality_report(df, base, t, s), width="stretch", hide_index=True)

        with st.expander("View raw data (first 100 rows; contains personal data)"):
            st.dataframe(raw.head(100), width="stretch")




            